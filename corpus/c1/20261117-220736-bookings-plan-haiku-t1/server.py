#!/usr/bin/env python3
"""bookings: small in-memory meeting room booking JSON API (stdlib only).

Run:  python server.py --port 8000   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 1024 * 1024
UTC = timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)

_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_INT_RE = re.compile(r"[0-9]+")
_B64_RE = re.compile(r"[A-Za-z0-9_-]+")

LOCK = threading.Lock()
ROOMS = {}          # "r1" -> {"id", "name", "capacity"}
ROOM_NAMES = set()
ROOM_SEQ = [0]
BOOKINGS = {}       # "b1" -> Booking
BOOKING_SEQ = [0]
IDEM = {}           # key -> (fingerprint, stored 201 response)


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def vfail(message):
    return ApiError(422, "validation_error", message)


def bad_json(message="invalid JSON body"):
    return ApiError(400, "bad_json", message)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_ts(value):
    """Strict UTC timestamp YYYY-MM-DDTHH:MM:SSZ -> aware datetime, or None."""
    if not isinstance(value, str):
        return None
    m = _TS_RE.fullmatch(value)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        return None


def fmt_ts(dt):
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def _is_quarter(dt):
    return dt.minute in (0, 15, 30, 45) and dt.second == 0


class Booking:
    __slots__ = ("n", "id", "room_id", "start", "end", "title", "attendees")

    def __init__(self, n, room_id, start, end, title, attendees):
        self.n = n
        self.id = f"b{n}"
        self.room_id = room_id
        self.start = start
        self.end = end
        self.title = title
        self.attendees = attendees

    def public(self):
        return {"id": self.id, "room_id": self.room_id, "start": fmt_ts(self.start),
                "end": fmt_ts(self.end), "title": self.title, "attendees": self.attendees}


# ---------------------------------------------------------------- rooms

def create_room(body):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not (1 <= len(name) <= 50) or not name.strip():
        raise vfail("name must be a non-blank string of 1 to 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise vfail("capacity must be an integer from 1 to 500")
    with LOCK:
        if name in ROOM_NAMES:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        ROOM_SEQ[0] += 1
        room = {"id": f"r{ROOM_SEQ[0]}", "name": name, "capacity": capacity}
        ROOMS[room["id"]] = room
        ROOM_NAMES.add(name)
        return 201, dict(room)


def list_rooms():
    with LOCK:
        return 200, {"rooms": [dict(r) for r in ROOMS.values()]}


def get_room(room_id):
    with LOCK:
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, dict(room)


def availability(room_id, date_str):
    m = _DATE_RE.fullmatch(date_str or "")
    if not m:
        raise vfail("date must be YYYY-MM-DD")
    try:
        day = date(*(int(g) for g in m.groups()))
    except ValueError:
        raise vfail("date must be a valid calendar date")
    ws = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
    we = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
    with LOCK:
        if room_id not in ROOMS:
            raise ApiError(404, "not_found", "room not found")
        busy = []
        for b in BOOKINGS.values():
            if b.room_id == room_id and b.start < we and b.end > ws:
                busy.append((max(b.start, ws), min(b.end, we)))
    busy.sort()
    free = []
    cur = ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < we:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": date_str, "free": free}


# ---------------------------------------------------------------- bookings

def create_booking(body, idem_key):
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise vfail("room_id must be a string")
    start = parse_ts(body.get("start"))
    if start is None:
        raise vfail("start must be a UTC timestamp like 2026-03-02T09:30:00Z")
    end = parse_ts(body.get("end"))
    if end is None:
        raise vfail("end must be a UTC timestamp like 2026-03-02T10:30:00Z")
    if not _is_quarter(start) or not _is_quarter(end):
        raise vfail("start and end must be on quarter hours")
    duration = end - start
    if duration <= timedelta(0) or duration > MAX_BOOKING:
        raise vfail("booking must be longer than 0 and at most 8 hours")
    title = body.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 200) or not title.strip():
        raise vfail("title must be a non-blank string of 1 to 200 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise vfail("attendees must be an integer of at least 1")

    fingerprint = (room_id, fmt_ts(start), fmt_ts(end), title, attendees)
    with LOCK:
        if idem_key is not None and idem_key in IDEM:
            stored_fp, stored_resp = IDEM[idem_key]
            if stored_fp == fingerprint:
                return 201, dict(stored_resp)
            raise ApiError(422, "idempotency_mismatch",
                           "Idempotency-Key was already used for a different request")
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        for b in BOOKINGS.values():
            if b.room_id == room_id and start < b.end and b.start < end:
                raise ApiError(409, "conflict", "the room is already booked for this time")
        BOOKING_SEQ[0] += 1
        booking = Booking(BOOKING_SEQ[0], room_id, start, end, title, attendees)
        BOOKINGS[booking.id] = booking
        resp = booking.public()
        if idem_key is not None:
            IDEM[idem_key] = (fingerprint, dict(resp))
        return 201, resp


def get_booking(booking_id):
    with LOCK:
        b = BOOKINGS.get(booking_id)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, b.public()


def delete_booking(booking_id):
    with LOCK:
        if BOOKINGS.pop(booking_id, None) is None:
            raise ApiError(404, "not_found", "booking not found")
        return 204, None


def encode_cursor(booking):
    raw = json.dumps([fmt_ts(booking.start), booking.n], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(raw):
    if not _B64_RE.fullmatch(raw) or len(raw) % 4 == 1:
        raise vfail("invalid cursor")
    try:
        obj = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise vfail("invalid cursor")
    if not (isinstance(obj, list) and len(obj) == 2 and isinstance(obj[0], str)
            and is_int(obj[1]) and obj[1] >= 1):
        raise vfail("invalid cursor")
    start = parse_ts(obj[0])
    if start is None:
        raise vfail("invalid cursor")
    return start, obj[1]


def list_bookings(q):
    room_filter = q.get("room_id")
    from_raw = q.get("from")
    to_raw = q.get("to")
    f_dt = t_dt = None
    if from_raw is not None:
        f_dt = parse_ts(from_raw)
        if f_dt is None:
            raise vfail("from must be a UTC timestamp like 2026-03-02T09:00:00Z")
    if to_raw is not None:
        t_dt = parse_ts(to_raw)
        if t_dt is None:
            raise vfail("to must be a UTC timestamp like 2026-03-02T09:00:00Z")
    if f_dt is not None and t_dt is not None and f_dt >= t_dt:
        raise vfail("from must be before to")

    limit_raw = q.get("limit")
    if limit_raw is None:
        limit = 20
    else:
        if not _INT_RE.fullmatch(limit_raw):
            raise vfail("limit must be an integer from 1 to 100")
        limit = int(limit_raw)
        if not (1 <= limit <= 100):
            raise vfail("limit must be an integer from 1 to 100")

    cursor = None
    cursor_raw = q.get("cursor")
    if cursor_raw is not None:
        cursor = decode_cursor(cursor_raw)

    with LOCK:
        items = []
        for b in BOOKINGS.values():
            if room_filter is not None and b.room_id != room_filter:
                continue
            if f_dt is not None and not (b.end > f_dt):
                continue
            if t_dt is not None and not (b.start < t_dt):
                continue
            items.append(b)
    items.sort(key=lambda b: (b.start, b.n))
    if cursor is not None:
        cs, cn = cursor
        items = [b for b in items if (b.start, b.n) > (cs, cn)]
    page = items[:limit]
    next_cursor = encode_cursor(page[-1]) if (len(items) > limit and page) else None
    return 200, {"bookings": [b.public() for b in page], "next_cursor": next_cursor}


# ---------------------------------------------------------------- routing

ROUTES_ROOMS_COLL = ("GET", "POST")
ROUTES_BOOKINGS_COLL = ("GET", "POST")


def match_route(segs):
    """Returns (route name, allowed methods) or (None, None)."""
    n = len(segs)
    if n == 1 and segs[0] == "rooms":
        return "rooms", ROUTES_ROOMS_COLL
    if n == 2 and segs[0] == "rooms":
        return "room", ("GET",)
    if n == 3 and segs[0] == "rooms" and segs[2] == "availability":
        return "availability", ("GET",)
    if n == 1 and segs[0] == "bookings":
        return "bookings", ROUTES_BOOKINGS_COLL
    if n == 2 and segs[0] == "bookings":
        return "booking", ("GET", "DELETE")
    return None, None


def _reject_constant(name):
    raise ValueError(f"invalid constant {name}")


def parse_body_object(raw):
    if not raw:
        raise bad_json("empty body")
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise bad_json()
    if not isinstance(obj, dict):
        raise bad_json("body must be a JSON object")
    return obj


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):  # keep stderr quiet
        pass

    def _send(self, status, body, headers, head_only):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if status != 204:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only and status != 204:
            self.wfile.write(data)

    def _read_body(self):
        cl = self.headers.get("Content-Length")
        if cl is None or not cl.strip():
            return b""
        try:
            n = int(cl)
        except ValueError:
            raise bad_json("invalid Content-Length")
        if n < 0:
            raise bad_json("invalid Content-Length")
        if n > MAX_BODY:
            # drain a bounded amount so the client can read the error response
            remaining = min(n, 8 * MAX_BODY)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise bad_json("request body too large")
        return self.rfile.read(n) if n else b""

    def _query(self):
        q = {}
        if "?" in self.path:
            query = self.path.split("?", 1)[1].split("#", 1)[0]
            for k, v in _parse_pairs(query):
                q.setdefault(k, v)  # first value wins
        return q

    def _dispatch(self, method):
        head_only = method == "HEAD"
        try:
            result = self._route(method)
            status, body = result
            headers = None
        except ApiError as e:
            status = e.status
            body = {"error": {"code": e.code, "message": e.message}}
            headers = e.headers
        except Exception:  # pragma: no cover - defensive
            status = 500
            body = {"error": {"code": "internal_error", "message": "internal server error"}}
            headers = None
        self._send(status, body, headers, head_only)

    def _route(self, method):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        if not path.startswith("/") or path == "/":
            raise ApiError(404, "not_found", "no such path")
        segs = path[1:].split("/")
        name, allowed = match_route(segs)
        if name is None:
            raise ApiError(404, "not_found", "no such path")
        check = "GET" if method == "HEAD" else method
        if check not in allowed:
            raise ApiError(405, "method_not_allowed", f"method {method} not allowed",
                           headers={"Allow": ", ".join(allowed)})

        if name == "rooms":
            if method == "POST":
                body = parse_body_object(self._read_body())
                return create_room(body)
            return list_rooms()
        if name == "room":
            return get_room(segs[1])
        if name == "availability":
            return availability(segs[1], self._query().get("date"))
        if name == "bookings":
            if method == "POST":
                key = self.headers.get("Idempotency-Key")
                if key is not None and (not key.strip() or len(key) > 255):
                    raise vfail("Idempotency-Key must be non-blank and at most 255 characters")
                body = parse_body_object(self._read_body())
                return create_booking(body, key)
            return list_bookings(self._query())
        # booking
        if method == "DELETE":
            return delete_booking(segs[1])
        return get_booking(segs[1])

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")


def _parse_pairs(query):
    from urllib.parse import parse_qsl
    return parse_qsl(query, keep_blank_values=True)


def _port(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer")
    if not (0 <= value <= 65535):
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room bookings API")
    parser.add_argument("--port", type=_port, default=8000, help="port to listen on (default 8000)")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
