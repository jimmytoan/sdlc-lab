"""Meeting room booking API (bookings). Standard library only.

Run: python server.py --port N   (listens on 127.0.0.1)
"""
import argparse
import base64
import datetime as dt
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

MAX_BODY = 1024 * 1024
MAX_IDEM_KEY = 255
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_DURATION_MIN = 8 * 60

LOCK = threading.Lock()
ROOMS = {}          # id -> {"id", "name", "capacity", "n"}; insertion order == creation order
ROOM_NAMES = set()
BOOKINGS = {}       # id -> internal booking dict
ROOM_SEQ = 0
BOOKING_SEQ = 0
IDEM = {}           # key -> (fingerprint, status, body)


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def bad(msg="invalid request"):
    return ApiError(422, "validation_error", msg)


# ---------- shared validation helpers ----------

_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")


def parse_ts(value):
    """Strict timestamp parse. Returns naive UTC datetime or None."""
    if not isinstance(value, str):
        return None
    m = _TS_RE.fullmatch(value)
    if not m:
        return None
    try:
        return dt.datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def fmt_ts(d):
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"


def is_int(v):
    return type(v) is int


def require_object_body(raw):
    if not raw or not raw.strip():
        raise ApiError(400, "bad_json", "request body is empty")
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise bad("request body must be a JSON object")
    return obj


def _reject_constant(name):
    raise ValueError("non-standard constant " + name)


# ---------- rooms ----------

def list_rooms(args, query, headers, body):
    with LOCK:
        return 200, {"rooms": [_room_out(r) for r in ROOMS.values()]}, {}


def _room_out(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def create_room(args, query, headers, body):
    global ROOM_SEQ
    data = require_object_body(body)
    name = data.get("name")
    capacity = data.get("capacity")
    if not isinstance(name, str):
        raise bad("name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise bad("name must be 1 to 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise bad("capacity must be an integer from 1 to 500")
    with LOCK:
        if name in ROOM_NAMES:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        ROOM_SEQ += 1
        room = {"id": f"r{ROOM_SEQ}", "name": name, "capacity": capacity, "n": ROOM_SEQ}
        ROOMS[room["id"]] = room
        ROOM_NAMES.add(name)
        return 201, _room_out(room), {}


def _find_room(room_id):
    room = ROOMS.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return room


def get_room(args, query, headers, body):
    with LOCK:
        return 200, _room_out(_find_room(args[0])), {}


# ---------- bookings ----------

def _booking_out(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def _fingerprint(path, data):
    return json.dumps({"path": path, "body": data}, sort_keys=True, separators=(",", ":"))


def create_booking(args, query, headers, body):
    global BOOKING_SEQ
    data = require_object_body(body)

    key = headers.get("Idempotency-Key")
    fp = None
    if key is not None:
        if not key.strip():
            raise bad("Idempotency-Key must not be empty")
        if len(key) > MAX_IDEM_KEY:
            raise bad("Idempotency-Key must be at most 255 characters")
        fp = _fingerprint("/bookings", data)

    with LOCK:
        if key is not None and key in IDEM:
            stored_fp, status, payload = IDEM[key]
            if stored_fp != fp:
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return status, payload, {}

        room_id = data.get("room_id")
        if not isinstance(room_id, str):
            raise bad("room_id must be a string")
        start = parse_ts(data.get("start"))
        end = parse_ts(data.get("end"))
        if start is None or end is None:
            raise bad("start and end must be UTC timestamps like 2026-03-02T09:30:00Z")
        for t in (start, end):
            if t.minute not in (0, 15, 30, 45) or t.second != 0:
                raise bad("start and end must fall on quarter hours")
        if end <= start:
            raise bad("end must be after start")
        duration = (end - start).total_seconds() / 60
        if duration > MAX_DURATION_MIN:
            raise bad("booking may be at most 8 hours long")
        title = data.get("title")
        if not isinstance(title, str):
            raise bad("title must be a string")
        title = title.strip()
        if not (1 <= len(title) <= 200):
            raise bad("title must be 1 to 200 characters")
        attendees = data.get("attendees")
        if not is_int(attendees) or attendees < 1:
            raise bad("attendees must be an integer of at least 1")

        room = ROOMS.get(room_id)
        if room is None:
            raise bad("room_id does not refer to an existing room")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        for other in BOOKINGS.values():
            if other["room_id"] == room_id and start < other["end"] and other["start"] < end:
                raise ApiError(409, "conflict", "the room is already booked for this time")

        BOOKING_SEQ += 1
        booking = {"id": f"b{BOOKING_SEQ}", "room_id": room_id, "start": start, "end": end,
                   "title": title, "attendees": attendees, "n": BOOKING_SEQ}
        BOOKINGS[booking["id"]] = booking
        payload = _booking_out(booking)
        if key is not None:
            IDEM[key] = (fp, 201, payload)
        return 201, payload, {}


def get_booking(args, query, headers, body):
    with LOCK:
        b = BOOKINGS.get(args[0])
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, _booking_out(b), {}


def delete_booking(args, query, headers, body):
    with LOCK:
        if BOOKINGS.pop(args[0], None) is None:
            raise ApiError(404, "not_found", "booking not found")
        return 204, None, {}


def _encode_cursor(b):
    raw = json.dumps([fmt_ts(b["start"]), b["n"]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(text):
    try:
        padded = text + "=" * (-len(text) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise bad("cursor is malformed")
    if not (isinstance(obj, list) and len(obj) == 2 and isinstance(obj[0], str)
            and is_int(obj[1]) and obj[1] >= 1):
        raise bad("cursor is malformed")
    start = parse_ts(obj[0])
    if start is None:
        raise bad("cursor is malformed")
    return (start, obj[1])


def _param(query, name):
    for k, v in query:
        if k == name:
            return v
    return None


def list_bookings(args, query, headers, body):
    pairs = parse_qsl(query, keep_blank_values=True)

    limit = 20
    raw_limit = _param(pairs, "limit")
    if raw_limit is not None:
        if not re.fullmatch(r"[0-9]+", raw_limit):
            raise bad("limit must be an integer from 1 to 100")
        limit = int(raw_limit)
        if not (1 <= limit <= 100):
            raise bad("limit must be an integer from 1 to 100")

    cursor_key = None
    raw_cursor = _param(pairs, "cursor")
    if raw_cursor is not None:
        cursor_key = _decode_cursor(raw_cursor)

    room_id = _param(pairs, "room_id")
    raw_from = _param(pairs, "from")
    raw_to = _param(pairs, "to")
    t_from = t_to = None
    if raw_from is not None:
        t_from = parse_ts(raw_from)
        if t_from is None:
            raise bad("from must be a UTC timestamp")
    if raw_to is not None:
        t_to = parse_ts(raw_to)
        if t_to is None:
            raise bad("to must be a UTC timestamp")
    if t_from is not None and t_to is not None and t_from >= t_to:
        raise bad("from must be before to")

    with LOCK:
        items = []
        for b in BOOKINGS.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if t_from is not None and not (b["end"] > t_from):
                continue
            if t_to is not None and not (b["start"] < t_to):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start"], b["n"]))
        if cursor_key is not None:
            items = [b for b in items if (b["start"], b["n"]) > cursor_key]
        page = items[:limit]
        next_cursor = _encode_cursor(page[-1]) if (len(items) > limit and page) else None
        return 200, {"bookings": [_booking_out(b) for b in page], "next_cursor": next_cursor}, {}


# ---------- availability ----------

def availability(args, query, headers, body):
    room_id = args[0]
    with LOCK:
        _find_room(room_id)
        pairs = parse_qsl(query, keep_blank_values=True)
        raw_date = _param(pairs, "date")
        m = _DATE_RE.fullmatch(raw_date) if raw_date is not None else None
        day = None
        if m:
            try:
                day = dt.date(*(int(g) for g in m.groups()))
            except ValueError:
                day = None
        if day is None:
            raise bad("date must be a real calendar date in YYYY-MM-DD form")

        ws = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR)
        we = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR)
        busy = []
        for b in BOOKINGS.values():
            if b["room_id"] == room_id and b["start"] < we and b["end"] > ws:
                busy.append((max(b["start"], ws), min(b["end"], we)))
        busy.sort()
        merged = []
        for s, e in busy:
            if merged and s <= merged[-1][1]:
                if e > merged[-1][1]:
                    merged[-1][1] = e
            else:
                merged.append([s, e])
        free = []
        cur = ws
        for s, e in merged:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            cur = max(cur, e)
        if cur < we:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
        return 200, {"date": raw_date, "free": free}, {}


# ---------- routing ----------

ROUTES = [
    (re.compile(r"/rooms"), {"GET": list_rooms, "POST": create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": availability}),
    (re.compile(r"/bookings"), {"GET": list_bookings, "POST": create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": get_booking, "DELETE": delete_booking}),
]

BODY_METHODS = {"POST"}


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):  # keep output quiet
        pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        if n > MAX_BODY:
            # drain what the client sent so the response can be read cleanly
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise ApiError(400, "bad_json", "request body is too large")
        return self.rfile.read(n)

    def _dispatch(self, method):
        method_for_send = method
        try:
            path, _, query = self.path.partition("?")
            handler = None
            for pattern, methods in ROUTES:
                m = pattern.fullmatch(path)
                if m is None:
                    continue
                if method not in methods:
                    raise ApiError(405, "method_not_allowed", "method not allowed",
                                   {"Allow": ", ".join(methods)})
                handler = (methods[method], list(m.groups()))
                break
            if handler is None:
                raise ApiError(404, "not_found", "no such endpoint")
            fn, args = handler
            body = self._read_body() if method in BODY_METHODS else b""
            status, payload, extra = fn(args, query, self.headers, body)
        except ApiError as e:
            status = e.status
            payload = {"error": {"code": e.code, "message": e.message}}
            extra = e.headers
        except Exception:  # pragma: no cover - safety net
            status = 500
            payload = {"error": {"code": "internal_error", "message": "internal server error"}}
            extra = {}
        self._send(method_for_send, status, payload, extra)

    def _send(self, method, status, payload, extra):
        self.send_response(status)
        if status == 204:
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    httpd = Server(("127.0.0.1", args.port), Handler)
    print(f"listening on 127.0.0.1:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
