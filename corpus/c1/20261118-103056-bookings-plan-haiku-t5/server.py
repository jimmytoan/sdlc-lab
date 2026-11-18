"""Meeting room booking API (in-memory, stdlib only).

Run: python server.py --port N   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

UTC = dt.timezone.utc
MAX_BODY = 1024 * 1024
WORK_START_H = 8
WORK_END_H = 18
MAX_DURATION = dt.timedelta(hours=8)

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
INT_RE = re.compile(r"-?[0-9]+")

LOCK = threading.Lock()
STATE = {
    "rooms": {},          # id -> room dict
    "room_names": {},     # name -> id
    "room_seq": 0,
    "bookings": {},       # id -> booking record (internal, datetimes)
    "booking_seq": 0,
    "idem": {},           # key -> (fingerprint, response body)
}


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def validation(msg):
    return ApiError(422, "validation_error", msg)


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str):
        raise validation(f"{field} must be a timestamp string like 2026-03-02T09:00:00Z")
    m = TS_RE.fullmatch(value)
    if not m:
        raise validation(f"{field} must be a timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return dt.datetime(*[int(x) for x in m.groups()], tzinfo=UTC)
    except ValueError:
        raise validation(f"{field} is not a valid date or time")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_room_view(room):
    return {"id": room["id"], "name": room["name"], "capacity": room["capacity"]}


def booking_view(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def read_json_object(handler):
    raw_len = handler.headers.get("Content-Length")
    try:
        length = int(raw_len) if raw_len not in (None, "") else 0
    except ValueError:
        raise ApiError(400, "bad_json", "invalid Content-Length")
    if length < 0:
        raise ApiError(400, "bad_json", "invalid Content-Length")
    if length > MAX_BODY:
        # Drain (bounded) so the client sees the response instead of a reset.
        remaining = min(length, 8 * MAX_BODY)
        while remaining > 0:
            chunk = handler.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
        raise ApiError(400, "bad_json", "request body too large")
    raw = handler.rfile.read(length) if length else b""
    if not raw.strip():
        raise ApiError(400, "bad_json", "request body is empty")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(body, dict):
        raise validation("request body must be a JSON object")
    return body


# ---------------------------------------------------------------- rooms

def create_room(handler, groups, query, body_required=True):
    body = read_json_object(handler)
    name = body.get("name")
    if not isinstance(name, str):
        raise validation("name must be a string")
    name = name.strip()
    if not name:
        raise validation("name must not be empty")
    if len(name) > 50:
        raise validation("name must be at most 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity):
        raise validation("capacity must be an integer")
    if not 1 <= capacity <= 500:
        raise validation("capacity must be between 1 and 500")
    with LOCK:
        if name in STATE["room_names"]:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        STATE["room_seq"] += 1
        rid = f"r{STATE['room_seq']}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STATE["rooms"][rid] = room
        STATE["room_names"][name] = rid
    return 201, parse_room_view(room), {}


def list_rooms(handler, groups, query):
    with LOCK:
        rooms = sorted(STATE["rooms"].values(), key=lambda r: int(r["id"][1:]))
        return 200, {"rooms": [parse_room_view(r) for r in rooms]}, {}


def get_room(handler, groups, query):
    with LOCK:
        room = STATE["rooms"].get(groups[0])
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, parse_room_view(room), {}


def availability(handler, groups, query):
    room_id = groups[0]
    with LOCK:
        if room_id not in STATE["rooms"]:
            raise ApiError(404, "not_found", "room not found")
        date_str = query.get("date")
        if date_str is None:
            raise validation("date is required (YYYY-MM-DD)")
        m = DATE_RE.fullmatch(date_str)
        if not m:
            raise validation("date must be YYYY-MM-DD")
        try:
            day = dt.date(*[int(x) for x in m.groups()])
        except ValueError:
            raise validation("date is not a valid calendar date")
        win_start = dt.datetime(day.year, day.month, day.day, WORK_START_H, tzinfo=UTC)
        win_end = dt.datetime(day.year, day.month, day.day, WORK_END_H, tzinfo=UTC)
        spans = []
        for b in STATE["bookings"].values():
            if b["room_id"] != room_id:
                continue
            if b["start"] < win_end and b["end"] > win_start:
                spans.append((max(b["start"], win_start), min(b["end"], win_end)))
        spans.sort()
        free = []
        cursor = win_start
        for s, e in spans:
            if s > cursor:
                free.append({"start": fmt_ts(cursor), "end": fmt_ts(s)})
            if e > cursor:
                cursor = e
        if cursor < win_end:
            free.append({"start": fmt_ts(cursor), "end": fmt_ts(win_end)})
        return 200, {"date": date_str, "free": free}, {}


# ---------------------------------------------------------------- bookings

def _check_quarter(d, field):
    if d.minute not in (0, 15, 30, 45) or d.second != 0:
        raise validation(f"{field} must fall on a quarter hour")


def create_booking(handler, groups, query):
    body = read_json_object(handler)

    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise validation("room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    _check_quarter(start, "start")
    _check_quarter(end, "end")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking must be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise validation("title must be a string")
    if not title.strip():
        raise validation("title must not be empty")
    if len(title.strip()) > 200:
        raise validation("title must be at most 200 characters")
    attendees = body.get("attendees")
    if not is_int(attendees):
        raise validation("attendees must be an integer")
    if attendees < 1:
        raise validation("attendees must be at least 1")

    key = handler.headers.get("Idempotency-Key")
    if key is not None:
        if not key.strip():
            raise validation("Idempotency-Key must not be empty")
        if len(key) > 255:
            raise validation("Idempotency-Key must be at most 255 characters")

    fingerprint = json.dumps(
        {"room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
         "title": title, "attendees": attendees},
        sort_keys=True,
    )

    with LOCK:
        if key is not None and key in STATE["idem"]:
            stored_fp, stored_body = STATE["idem"][key]
            if stored_fp != fingerprint:
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return 201, stored_body, {}

        room = STATE["rooms"].get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        for other in STATE["bookings"].values():
            if other["room_id"] == room_id and start < other["end"] and end > other["start"]:
                raise ApiError(409, "conflict", "room is already booked for this time")

        STATE["booking_seq"] += 1
        bid = f"b{STATE['booking_seq']}"
        rec = {"id": bid, "n": STATE["booking_seq"], "room_id": room_id, "start": start,
               "end": end, "title": title, "attendees": attendees}
        STATE["bookings"][bid] = rec
        view = booking_view(rec)
        if key is not None:
            STATE["idem"][key] = (fingerprint, view)
        return 201, view, {}


def get_booking(handler, groups, query):
    with LOCK:
        rec = STATE["bookings"].get(groups[0])
        if rec is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, booking_view(rec), {}


def delete_booking(handler, groups, query):
    with LOCK:
        if groups[0] not in STATE["bookings"]:
            raise ApiError(404, "not_found", "booking not found")
        del STATE["bookings"][groups[0]]
    return 204, None, {}


def _encode_cursor(rec):
    payload = json.dumps([fmt_ts(rec["start"]), rec["n"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(text):
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
        if not (isinstance(payload, list) and len(payload) == 2):
            raise ValueError
        start_s, n = payload
        if not is_int(n):
            raise ValueError
        start = parse_ts(start_s, "cursor")
        return start, n
    except (ValueError, TypeError, binascii.Error, UnicodeError, ApiError):
        raise validation("cursor is malformed")


def _parse_limit(text):
    if text is None:
        return 20
    if not INT_RE.fullmatch(text):
        raise validation("limit must be an integer between 1 and 100")
    v = int(text)
    if not 1 <= v <= 100:
        raise validation("limit must be between 1 and 100")
    return v


def list_bookings(handler, groups, query):
    limit = _parse_limit(query.get("limit"))
    cursor = None
    if query.get("cursor") is not None:
        cursor = _decode_cursor(query["cursor"])

    room_filter = query.get("room_id")
    from_dt = parse_ts(query["from"], "from") if query.get("from") is not None else None
    to_dt = parse_ts(query["to"], "to") if query.get("to") is not None else None
    if from_dt is not None and to_dt is not None and from_dt >= to_dt:
        raise validation("from must be before to")

    with LOCK:
        items = []
        for rec in STATE["bookings"].values():
            if room_filter is not None and rec["room_id"] != room_filter:
                continue
            if from_dt is not None and not rec["end"] > from_dt:
                continue
            if to_dt is not None and not rec["start"] < to_dt:
                continue
            items.append(rec)
        items.sort(key=lambda r: (r["start"], r["n"]))
        if cursor is not None:
            items = [r for r in items if (r["start"], r["n"]) > cursor]
        page = items[: limit + 1]
        next_cursor = None
        if len(page) > limit:
            page = page[:limit]
            next_cursor = _encode_cursor(page[-1])
        return 200, {"bookings": [booking_view(r) for r in page], "next_cursor": next_cursor}, {}


# ---------------------------------------------------------------- routing

ROUTES = [
    (re.compile(r"/rooms"), {"GET": list_rooms, "POST": create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": availability}),
    (re.compile(r"/bookings"), {"GET": list_bookings, "POST": create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": get_booking, "DELETE": delete_booking}),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep the server quiet
        pass

    def _send_json(self, status, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_error(self, status, code, message, headers=None):
        self._send_json(status, {"error": {"code": code, "message": message}}, headers)

    def _dispatch(self):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = {}
            for k, v in parse_qsl(parts.query, keep_blank_values=True):
                query.setdefault(k, v)  # first value wins

            matched = None
            for pattern, methods in ROUTES:
                m = pattern.fullmatch(path)
                if m:
                    matched = (m.groups(), methods)
                    break
            if matched is None:
                raise ApiError(404, "not_found", "no such path")
            groups, methods = matched
            handler_fn = methods.get(self.command)
            if handler_fn is None:
                allow = ", ".join(methods.keys())
                raise ApiError(405, "method_not_allowed", "method not allowed for this path",
                               {"Allow": allow})
            status, payload, headers = handler_fn(self, groups, query)
            if status == 204:
                self.send_response(204)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
            else:
                self._send_json(status, payload, headers)
        except ApiError as e:
            self._send_error(e.status, e.code, e.message, e.headers)
        except Exception:  # noqa: BLE001
            self._send_error(500, "internal_error", "unexpected server error")

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch
    do_PATCH = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
