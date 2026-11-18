"""Meeting room booking API (single file, Python 3.13 standard library only).

Start with: python server.py --port <N>
Listens on 127.0.0.1 only. All state is kept in memory for the life of the process.

Design references: SPEC.md, specs/001-room-booking-api/{spec.md,plan.md,data-model.md,
research.md,contracts/http-api.md}.
"""
import argparse
import base64
import hashlib
import json
import re
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"  # FR-020: loopback only, not configurable.
WORK_START_HOUR = 8  # Availability window 08:00-18:00 UTC.
WORK_END_HOUR = 18
MAX_BOOKING_DURATION = timedelta(hours=8)
QUARTER_MINUTES = (0, 15, 30, 45)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_ROOM_NAME = 50
MAX_CAPACITY = 500
MAX_TITLE = 200

# Error code -> HTTP status (SPEC.md, FR-018).
ERROR_STATUS = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
    "internal_error": 500,
}

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z", re.ASCII)
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})", re.ASCII)
INT_RE = re.compile(r"[0-9]+", re.ASCII)


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code, message):
    raise ApiError(code, message)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def is_int(value):
    """JSON integers only; booleans are rejected (research.md section 12)."""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_timestamp(value, field):
    """Canonical UTC timestamp: YYYY-MM-DDTHH:MM:SSZ with seconds 00 (contract, Timestamps)."""
    if not isinstance(value, str):
        fail("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:00:00Z")
    m = TS_RE.fullmatch(value)
    if m is None:
        fail("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:00:00Z")
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    try:
        dt = datetime(year, month, day, hour, minute, second)
    except ValueError:
        fail("validation_error", f"{field} is not a valid date and time")
    if second != 0:
        fail("validation_error", f"{field} must have seconds set to 00")
    return dt


def format_ts(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def parse_date(value):
    """Strict YYYY-MM-DD and a real calendar date."""
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        fail("validation_error", "date must be in YYYY-MM-DD format")
    year, month, day = (int(g) for g in DATE_RE.fullmatch(value).groups())
    try:
        return date(year, month, day)
    except ValueError:
        fail("validation_error", "date is not a valid calendar date")


# ---------------------------------------------------------------------------
# Store (all state; every check-and-write runs under self.lock)
# ---------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}        # id -> room dict, insertion ordered (creation order)
        self.bookings = {}     # id -> booking dict with datetime start/end
        self.idempotency = {}  # key -> (fingerprint, original 201 response body)
        self.next_room_number = 1
        self.next_booking_number = 1


def room_view(room):
    return {"id": room["id"], "name": room["name"], "capacity": room["capacity"]}


def booking_view(booking):
    return {
        "id": booking["id"],
        "room_id": booking["room_id"],
        "start": format_ts(booking["start"]),
        "end": format_ts(booking["end"]),
        "title": booking["title"],
        "attendees": booking["attendees"],
    }


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

class Request:
    def __init__(self, method, query, headers, raw, args):
        self.method = method
        self.query = query      # parse_qs result: name -> list of values
        self.headers = headers
        self.raw = raw          # request body bytes
        self.args = args        # path captures


def get_param(query, name):
    values = query.get(name)
    return values[0] if values else None


def parse_body(raw):
    """Body must be parseable JSON. Empty body is bad_json (contract: POST needs a body)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("bad_json", "request body must be UTF-8 encoded JSON")
    if not text.strip():
        fail("bad_json", "request body is required and must be JSON")
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        fail("bad_json", "request body is not valid JSON")


def parse_object_body(raw):
    body = parse_body(raw)
    if not isinstance(body, dict):
        fail("validation_error", "request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def validate_room(body):
    # data-model.md Room: name "Required. 1-50 characters. Unique across all rooms,
    # exact case-sensitive match." capacity "Required. 1-500 inclusive. Booleans are rejected."
    name = body.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= MAX_ROOM_NAME):
        fail("validation_error", "name must be a string of 1 to 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not (1 <= capacity <= MAX_CAPACITY):
        fail("validation_error", "capacity must be an integer from 1 to 500")
    return name, capacity


def create_room(store, req):
    body = parse_object_body(req.raw)
    name, capacity = validate_room(body)
    with store.lock:
        if any(room["name"] == name for room in store.rooms.values()):
            fail("room_exists", "a room with this name already exists")
        room_id = f"r{store.next_room_number}"
        store.next_room_number += 1
        room = {"id": room_id, "name": name, "capacity": capacity}
        store.rooms[room_id] = room
        return 201, room_view(room)


def list_rooms(store, req):
    with store.lock:
        rooms = [room_view(room) for room in store.rooms.values()]
    return 200, {"rooms": rooms}


def get_room(store, req):
    room_id = req.args[0]
    with store.lock:
        room = store.rooms.get(room_id)
        if room is None:
            fail("not_found", "room not found")
        return 200, room_view(room)


def availability(store, req):
    room_id = req.args[0]
    with store.lock:
        room = store.rooms.get(room_id)
        if room is None:
            fail("not_found", "room not found")
        date_raw = get_param(req.query, "date")
        if date_raw is None:
            fail("validation_error", "date query parameter is required (YYYY-MM-DD)")
        day = parse_date(date_raw)
        window_start = datetime(day.year, day.month, day.day, WORK_START_HOUR)
        window_end = datetime(day.year, day.month, day.day, WORK_END_HOUR)

        # Bookings overlapping the window, clipped to it.
        clipped = []
        for booking in store.bookings.values():
            if booking["room_id"] != room_id:
                continue
            if booking["start"] < window_end and window_start < booking["end"]:
                clipped.append((max(booking["start"], window_start), min(booking["end"], window_end)))
        clipped.sort()

        free = []
        cursor = window_start
        for start, end in clipped:
            if start > cursor:
                free.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < window_end:
            free.append((cursor, window_end))

    return 200, {
        "date": f"{day.year:04d}-{day.month:02d}-{day.day:02d}",
        "free": [{"start": format_ts(s), "end": format_ts(e)} for s, e in free],
    }


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

BOOKING_FINGERPRINT_FIELDS = ("room_id", "start", "end", "title", "attendees")


def booking_fingerprint(body):
    """Request identity for Idempotency-Key: (room_id, start, end, title, attendees)."""
    return json.dumps([body.get(field) for field in BOOKING_FINGERPRINT_FIELDS], ensure_ascii=True)


def validate_booking(store, body):
    """Field validation (data-model.md Booking). Caller holds store.lock."""
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id not in store.rooms:
        fail("validation_error", "room_id must refer to an existing room")

    start = parse_timestamp(body.get("start"), "start")
    end = parse_timestamp(body.get("end"), "end")
    if start.minute not in QUARTER_MINUTES:
        fail("validation_error", "start must fall on a quarter hour (:00, :15, :30, :45)")
    if end.minute not in QUARTER_MINUTES:
        fail("validation_error", "end must fall on a quarter hour (:00, :15, :30, :45)")
    if end <= start:
        fail("validation_error", "end must be after start")
    if end - start > MAX_BOOKING_DURATION:
        fail("validation_error", "booking must be at most 8 hours long")

    title = body.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= MAX_TITLE):
        fail("validation_error", "title must be a string of 1 to 200 characters")

    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        fail("validation_error", "attendees must be an integer of at least 1")

    return room_id, start, end, title, attendees


def create_booking(store, req):
    body = parse_object_body(req.raw)
    key = req.headers.get("Idempotency-Key") or None
    fingerprint = booking_fingerprint(body)

    with store.lock:
        # Idempotency lookup happens before field validation (contract: Evaluation order).
        if key is not None and key in store.idempotency:
            stored_fingerprint, response = store.idempotency[key]
            if stored_fingerprint == fingerprint:
                return 201, dict(response)
            fail("idempotency_mismatch", "Idempotency-Key was already used for a different booking request")

        room_id, start, end, title, attendees = validate_booking(store, body)
        room = store.rooms[room_id]
        if attendees > room["capacity"]:
            fail("over_capacity", "attendees exceed the room capacity")

        # Half-open overlap: a.start < b.end and b.start < a.end. Back-to-back is allowed.
        for other in store.bookings.values():
            if other["room_id"] == room_id and other["start"] < end and start < other["end"]:
                fail("conflict", "the room is already booked during this time")

        booking_id = f"b{store.next_booking_number}"
        store.next_booking_number += 1
        booking = {
            "id": booking_id,
            "room_id": room_id,
            "start": start,
            "end": end,
            "title": title,
            "attendees": attendees,
        }
        store.bookings[booking_id] = booking
        response = booking_view(booking)
        # Only successful creations are stored against the key; rejected requests leave it free.
        if key is not None:
            store.idempotency[key] = (fingerprint, response)
        return 201, dict(response)


def get_booking(store, req):
    booking_id = req.args[0]
    with store.lock:
        booking = store.bookings.get(booking_id)
        if booking is None:
            fail("not_found", "booking not found")
        return 200, booking_view(booking)


def delete_booking(store, req):
    booking_id = req.args[0]
    with store.lock:
        if store.bookings.pop(booking_id, None) is None:
            fail("not_found", "booking not found")
    return 204, None


def encode_cursor(offset, fingerprint):
    raw = json.dumps({"o": offset, "f": fingerprint}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor, fingerprint):
    """Returns the offset. Invalid, negative, or different-filter cursors are validation_error."""
    invalid = "cursor is invalid for these filters"
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, TypeError):
        fail("validation_error", invalid)
    if not isinstance(data, dict) or data.get("f") != fingerprint:
        fail("validation_error", invalid)
    offset = data.get("o")
    if not is_int(offset) or offset < 0:
        fail("validation_error", invalid)
    return offset


def list_bookings(store, req):
    query = req.query
    room_filter = get_param(query, "room_id")

    from_raw = get_param(query, "from")
    to_raw = get_param(query, "to")
    from_dt = parse_timestamp(from_raw, "from") if from_raw is not None else None
    to_dt = parse_timestamp(to_raw, "to") if to_raw is not None else None
    if from_dt is not None and to_dt is not None and from_dt >= to_dt:
        fail("validation_error", "from must be earlier than to")

    limit = DEFAULT_LIMIT
    limit_raw = get_param(query, "limit")
    if limit_raw is not None:
        if INT_RE.fullmatch(limit_raw) is None or not (1 <= int(limit_raw) <= MAX_LIMIT):
            fail("validation_error", "limit must be an integer from 1 to 100")
        limit = int(limit_raw)

    fingerprint = hashlib.sha256(json.dumps([
        room_filter,
        format_ts(from_dt) if from_dt is not None else None,
        format_ts(to_dt) if to_dt is not None else None,
    ]).encode("utf-8")).hexdigest()[:16]

    offset = 0
    cursor_raw = get_param(query, "cursor")
    if cursor_raw is not None:
        offset = decode_cursor(cursor_raw, fingerprint)

    with store.lock:
        matches = []
        for booking in store.bookings.values():
            if room_filter is not None and booking["room_id"] != room_filter:
                continue
            if from_dt is not None and not (from_dt < booking["end"]):
                continue
            if to_dt is not None and not (booking["start"] < to_dt):
                continue
            matches.append(booking)
        matches.sort(key=lambda b: (b["start"], int(b["id"][1:])))
        page = matches[offset:offset + limit]
        next_cursor = encode_cursor(offset + limit, fingerprint) if offset + limit < len(matches) else None
        return 200, {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

ROUTES = [
    (re.compile(r"/rooms"), {"GET": list_rooms, "POST": create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": availability}),
    (re.compile(r"/bookings"), {"GET": list_bookings, "POST": create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": get_booking, "DELETE": delete_booking}),
]


def route(store, method, path, query, headers, raw):
    for pattern, methods in ROUTES:
        match = pattern.fullmatch(path)
        if match is None:
            continue
        handler = methods.get(method)
        if handler is None:
            fail("method_not_allowed", f"{method} is not allowed on this path")
        return handler(store, Request(method, query, headers, raw, match.groups()))
    fail("not_found", "no such path")


class Handler(BaseHTTPRequestHandler):
    server_version = "BookingsAPI/1.0"
    sys_version = ""

    def _dispatch(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        parts = urlsplit(self.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            status, payload = route(self.server.store, self.command, parts.path, query, self.headers, raw)
        except ApiError as err:
            status = ERROR_STATUS[err.code]
            payload = {"error": {"code": err.code, "message": err.message}}
        except Exception:  # Never leak a traceback or non-JSON body to the client.
            status = 500
            payload = {"error": {"code": "internal_error", "message": "unexpected server error"}}
        self._send(status, payload)

    def _send(self, status, payload):
        self.send_response(status)
        if status == 204 or payload is None:
            self.end_headers()
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # Protocol-level errors from http.server (bad request line, unsupported method)
        # are also returned as JSON, per FR-019.
        if code == 501:
            error_code = "method_not_allowed"
        elif code == 404:
            error_code = "not_found"
        elif code == 400:
            error_code = "bad_json"
        else:
            error_code = "internal_error"
        self.send_response(code)
        body = json.dumps({"error": {"code": error_code, "message": message or "request failed"}}).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format, *args):  # Keep stdout/stderr quiet.
        pass

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_HEAD = _dispatch
    do_OPTIONS = _dispatch


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler_class):
        super().__init__(address, handler_class)
        self.store = Store()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API (in-memory).")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on (127.0.0.1 only)")
    args = parser.parse_args(argv)
    server = Server((HOST, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
