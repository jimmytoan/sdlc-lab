"""Meeting room booking API.

Single-file, standard-library-only JSON over HTTP service.

    python server.py --port <N>

Listens on 127.0.0.1 only. All state is held in memory and is lost on restart.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import threading
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
UTC = timezone.utc

ROOM_NAME_MAX_LENGTH = 50
CAPACITY_MIN = 1
CAPACITY_MAX = 500
MAX_BOOKING_DURATION = timedelta(hours=8)
QUARTER_MINUTES = (0, 15, 30, 45)
WORKDAY_START_HOUR = 8
WORKDAY_END_HOUR = 18
DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 100
IDEMPOTENCY_HEADER = "Idempotency-Key"

# Status for each error code (FR-003).
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

_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UINT_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message or code.replace("_", " ")
        super().__init__(self.message)


def error_body(code, message):
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------- validators

def is_int(value):
    """JSON integer, excluding booleans (bool subclasses int in Python)."""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_timestamp(value, field):
    """Strict UTC timestamp YYYY-MM-DDTHH:MM:SSZ -> aware datetime."""
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise ApiError("validation_error",
                       f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")


def format_timestamp(dt):
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_json_object(raw):
    """Parse a request body that must be a JSON object."""
    if not raw.strip():
        raise ApiError("bad_json", "request body is empty")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    return obj


def validate_room(body):
    name = body.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= ROOM_NAME_MAX_LENGTH):
        raise ApiError("validation_error",
                       f"name must be a string of 1 to {ROOM_NAME_MAX_LENGTH} characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not (CAPACITY_MIN <= capacity <= CAPACITY_MAX):
        raise ApiError("validation_error",
                       f"capacity must be an integer from {CAPACITY_MIN} to {CAPACITY_MAX}")
    return name, capacity


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise ApiError("validation_error", "room_id must be a non-empty string")
    start = parse_timestamp(body.get("start"), "start")
    end = parse_timestamp(body.get("end"), "end")
    for field, dt in (("start", start), ("end", end)):
        if dt.second != 0 or dt.minute not in QUARTER_MINUTES:
            raise ApiError("validation_error",
                           f"{field} must be on a quarter hour (minutes 00, 15, 30 or 45)")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_BOOKING_DURATION:
        raise ApiError("validation_error", "booking must be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or title == "":
        raise ApiError("validation_error", "title must be a non-empty string")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise ApiError("validation_error", "attendees must be an integer of at least 1")
    return {"room_id": room_id, "start": start, "end": end,
            "title": title, "attendees": attendees}


def single_param(query, name):
    values = query.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise ApiError("validation_error", f"query parameter {name} may only be given once")
    return values[0]


# ---------------------------------------------------------------- store

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> {"id", "name", "capacity"}, insertion = creation order
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}       # id -> internal booking dict (datetimes)
        self.booking_seq = 0
        self.idempotency = {}    # key -> (fingerprint, response)


def render_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": format_timestamp(b["start"]),
        "end": format_timestamp(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def numeric_id(identifier):
    return int(identifier[1:])


def booking_sort_key(b):
    return (b["start"], numeric_id(b["id"]))


# ---------------------------------------------------------------- handlers
# Each handler: (store, path_groups, query, headers, raw_body) -> (status, payload or None)

def create_room(store, groups, query, headers, raw):
    body = parse_json_object(raw)
    name, capacity = validate_room(body)
    with store.lock:
        if name in store.room_names:
            raise ApiError("room_exists", f"a room named {name!r} already exists")
        store.room_seq += 1
        room = {"id": f"r{store.room_seq}", "name": name, "capacity": capacity}
        store.rooms[room["id"]] = room
        store.room_names.add(name)
        return 201, dict(room)


def list_rooms(store, groups, query, headers, raw):
    with store.lock:
        rooms = [dict(r) for r in store.rooms.values()]
    return 200, {"rooms": rooms}


def get_room(store, groups, query, headers, raw):
    room_id = groups[0]
    with store.lock:
        room = store.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", f"room {room_id} does not exist")
        return 200, dict(room)


def _idempotency_key(headers):
    key = headers.get(IDEMPOTENCY_HEADER)
    if key is None:
        return None
    if key.strip() == "":
        raise ApiError("validation_error", "Idempotency-Key must not be empty")
    return key


def create_booking(store, groups, query, headers, raw):
    body = parse_json_object(raw)
    fields = validate_booking(body)
    key = _idempotency_key(headers)
    fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with store.lock:
        if key is not None and key in store.idempotency:
            recorded_fp, recorded_response = store.idempotency[key]
            if recorded_fp != fingerprint:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used with a different request")
            return 201, dict(recorded_response)

        room = store.rooms.get(fields["room_id"])
        if room is None:
            raise ApiError("not_found", f"room {fields['room_id']} does not exist")
        if fields["attendees"] > room["capacity"]:
            raise ApiError("over_capacity",
                           f"{fields['attendees']} attendees exceed room capacity {room['capacity']}")
        for other in store.bookings.values():
            if (other["room_id"] == room["id"]
                    and fields["start"] < other["end"]
                    and fields["end"] > other["start"]):
                raise ApiError("conflict", f"room {room['id']} is already booked in that time")

        store.booking_seq += 1
        booking = {"id": f"b{store.booking_seq}", **fields}
        store.bookings[booking["id"]] = booking
        response = render_booking(booking)
        if key is not None:
            store.idempotency[key] = (fingerprint, response)
        return 201, dict(response)


def get_booking(store, groups, query, headers, raw):
    booking_id = groups[0]
    with store.lock:
        booking = store.bookings.get(booking_id)
        if booking is None:
            raise ApiError("not_found", f"booking {booking_id} does not exist")
        return 200, render_booking(booking)


def delete_booking(store, groups, query, headers, raw):
    booking_id = groups[0]
    with store.lock:
        if store.bookings.pop(booking_id, None) is None:
            raise ApiError("not_found", f"booking {booking_id} does not exist")
    return 204, None


def _filter_digest(room_id, frm, to):
    canonical = json.dumps([room_id, frm, to], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor(token, digest):
    """Returns (start datetime, numeric id) of the last item of the previous page."""
    try:
        padded = token + "=" * (-len(token) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        raise ApiError("validation_error", "cursor is malformed")
    if not isinstance(obj, dict) or set(obj) != {"s", "i", "f"}:
        raise ApiError("validation_error", "cursor is malformed")
    start = parse_timestamp(obj["s"], "cursor")
    index = obj["i"]
    if not is_int(index) or index < 1:
        raise ApiError("validation_error", "cursor is malformed")
    if obj["f"] != digest:
        raise ApiError("validation_error", "cursor does not match the requested filters")
    return start, index


def list_bookings(store, groups, query, headers, raw):
    room_id = single_param(query, "room_id")
    if room_id == "":
        raise ApiError("validation_error", "room_id must not be empty")

    from_raw = single_param(query, "from")
    to_raw = single_param(query, "to")
    frm = parse_timestamp(from_raw, "from") if from_raw is not None else None
    to = parse_timestamp(to_raw, "to") if to_raw is not None else None
    if frm is not None and to is not None and frm >= to:
        raise ApiError("validation_error", "from must be before to")

    limit_raw = single_param(query, "limit")
    if limit_raw is None:
        limit = DEFAULT_LIMIT
    else:
        if not _UINT_RE.fullmatch(limit_raw):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        limit = int(limit_raw)
        if not (MIN_LIMIT <= limit <= MAX_LIMIT):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")

    digest = _filter_digest(room_id, from_raw, to_raw)
    cursor_raw = single_param(query, "cursor")
    after = _decode_cursor(cursor_raw, digest) if cursor_raw is not None else None

    with store.lock:
        items = []
        for b in store.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not b["end"] > frm:
                continue
            if to is not None and not b["start"] < to:
                continue
            items.append(b)
        items.sort(key=booking_sort_key)
        if after is not None:
            items = [b for b in items
                     if (b["start"], numeric_id(b["id"])) > after]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last = page[-1]
            next_cursor = _encode_cursor({
                "s": format_timestamp(last["start"]),
                "i": numeric_id(last["id"]),
                "f": digest,
            })
        rendered = [render_booking(b) for b in page]
    return 200, {"bookings": rendered, "next_cursor": next_cursor}


def availability(store, groups, query, headers, raw):
    room_id = groups[0]
    date_raw = single_param(query, "date")
    if date_raw is None or not _DATE_RE.fullmatch(date_raw):
        raise ApiError("validation_error", "date must be a calendar date in YYYY-MM-DD form")
    try:
        day = date_type.fromisoformat(date_raw)
    except ValueError:
        raise ApiError("validation_error", "date must be a valid calendar date")

    window_start = datetime(day.year, day.month, day.day, WORKDAY_START_HOUR, tzinfo=UTC)
    window_end = datetime(day.year, day.month, day.day, WORKDAY_END_HOUR, tzinfo=UTC)

    with store.lock:
        if room_id not in store.rooms:
            raise ApiError("not_found", f"room {room_id} does not exist")
        clips = sorted(
            (max(b["start"], window_start), min(b["end"], window_end))
            for b in store.bookings.values()
            if b["room_id"] == room_id
            and b["start"] < window_end and b["end"] > window_start
        )

    free = []
    cursor = window_start
    for busy_start, busy_end in clips:
        if busy_start > cursor:
            free.append((cursor, busy_start))
        cursor = max(cursor, busy_end)
    if cursor < window_end:
        free.append((cursor, window_end))

    return 200, {
        "date": date_raw,
        "free": [{"start": format_timestamp(s), "end": format_timestamp(e)} for s, e in free],
    }


# ---------------------------------------------------------------- routing

ROUTES = [
    (re.compile(r"/rooms"), {"GET": list_rooms, "POST": create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": availability}),
    (re.compile(r"/bookings"), {"GET": list_bookings, "POST": create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": get_booking, "DELETE": delete_booking}),
]


def resolve(method, path):
    for pattern, handlers in ROUTES:
        match = pattern.fullmatch(path)
        if match is not None:
            handler = handlers.get(method)
            if handler is None:
                raise ApiError("method_not_allowed", f"{method} is not allowed on {path}")
            return handler, match.groups()
    raise ApiError("not_found", f"no resource at {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "BookingsAPI/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

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

    def _dispatch(self, method):
        try:
            url = urlsplit(self.path)
            handler, groups = resolve(method, url.path)
            raw = self._read_body() if method == "POST" else b""
            query = parse_qs(url.query, keep_blank_values=True)
            status, payload = handler(self.server.store, groups, query, self.headers, raw)
            self._send(status, payload)
        except ApiError as err:
            self._send(err.status, error_body(err.code, err.message))
        except Exception:
            self._send(500, error_body("internal_error", "unexpected server error"))

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send(self, status, payload):
        if payload is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API (127.0.0.1 only).")
    parser.add_argument("--port", type=int, required=True, help="TCP port, 1-65535")
    args = parser.parse_args(argv)
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")

    server = ThreadingHTTPServer((HOST, args.port), Handler)
    server.daemon_threads = True
    server.store = Store()
    print(f"listening on http://{HOST}:{args.port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
