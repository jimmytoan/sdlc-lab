"""Meeting room booking API: a small JSON over HTTP service with in-memory state.

Standard library only. Run with: python server.py --port <N>  (listens on 127.0.0.1).
"""

# ---------------------------------------------------------------------------
# Constants and error codes
# ---------------------------------------------------------------------------

import argparse
import base64
import binascii
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ERROR_STATUS = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
}

MAX_BODY_BYTES = 64 * 1024  # 65536

UTC = timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING_DURATION = timedelta(hours=8)
ROOM_NAME_MAX = 50
CAPACITY_MIN = 1
CAPACITY_MAX = 500
TITLE_MIN = 1
TITLE_MAX = 200
DEFAULT_LIMIT = 20
MIN_LIMIT = 1
MAX_LIMIT = 100
QUARTER_MINUTES = (0, 15, 30, 45)


class ApiError(Exception):
    """An API error that maps to a documented error code and HTTP status."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message or code.replace("_", " ")


def error_body(code, message):
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Timestamp and date parsing and formatting
# ---------------------------------------------------------------------------

_INSTANT_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z"
)
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DIGITS_RE = re.compile(r"[0-9]+")


def parse_instant(text):
    """Parse exactly YYYY-MM-DDTHH:MM:SSZ into an aware UTC datetime."""
    if not isinstance(text, str):
        raise ApiError("validation_error", "timestamp must be a string like 2026-03-02T09:00:00Z")
    m = _INSTANT_RE.fullmatch(text)
    if not m:
        raise ApiError("validation_error", f"invalid timestamp {text!r}; expected YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"invalid timestamp {text!r}")


def format_instant(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_quarter_aligned(dt):
    return dt.minute in QUARTER_MINUTES and dt.second == 0


def parse_date(text):
    """Parse a real calendar date in YYYY-MM-DD form."""
    if not isinstance(text, str):
        raise ApiError("validation_error", "date must be YYYY-MM-DD")
    m = _DATE_RE.fullmatch(text)
    if not m:
        raise ApiError("validation_error", f"invalid date {text!r}; expected YYYY-MM-DD")
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError:
        raise ApiError("validation_error", f"invalid date {text!r}")


# ---------------------------------------------------------------------------
# Request validation helpers
# ---------------------------------------------------------------------------

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_room(body):
    """Return (name, capacity) for a room creation body or raise validation_error."""
    if not isinstance(body, dict):
        raise ApiError("validation_error", "body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not (1 <= len(name) <= ROOM_NAME_MAX):
        raise ApiError("validation_error", f"name must be a string of 1 to {ROOM_NAME_MAX} characters")
    if not _is_int(capacity) or not (CAPACITY_MIN <= capacity <= CAPACITY_MAX):
        raise ApiError("validation_error", f"capacity must be an integer from {CAPACITY_MIN} to {CAPACITY_MAX}")
    return name, capacity


def _normalise_booking_fields(body):
    """Parse the booking fields without applying the business rules.

    Used both for validation and for building the idempotency fingerprint.
    Raises validation_error if a field cannot be parsed.
    """
    if not isinstance(body, dict):
        raise ApiError("validation_error", "body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise ApiError("validation_error", "room_id must be a string")
    start = parse_instant(body.get("start"))
    end = parse_instant(body.get("end"))
    title = body.get("title")
    if not isinstance(title, str) or not (TITLE_MIN <= len(title) <= TITLE_MAX):
        raise ApiError("validation_error", f"title must be a string of {TITLE_MIN} to {TITLE_MAX} characters")
    attendees = body.get("attendees")
    if not _is_int(attendees) or attendees < 1:
        raise ApiError("validation_error", "attendees must be an integer of at least 1")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def validate_booking(body):
    """Full booking validation, in the order documented in research.md section 7."""
    fields = _normalise_booking_fields(body)
    start, end = fields["start"], fields["end"]
    if not is_quarter_aligned(start) or not is_quarter_aligned(end):
        raise ApiError("validation_error", "start and end must fall on a quarter hour (:00, :15, :30, :45)")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_BOOKING_DURATION:
        raise ApiError("validation_error", "a booking may last at most 8 hours")
    return fields


def booking_fingerprint(body):
    """Fingerprint for idempotency, or None when the body cannot be parsed into fields."""
    try:
        f = _normalise_booking_fields(body)
    except ApiError:
        return None
    return (f["room_id"], f["start"], f["end"], f["title"], f["attendees"])


# ---------------------------------------------------------------------------
# Paging cursor (opaque, not stored)
# ---------------------------------------------------------------------------

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")


def encode_cursor(sort_key):
    start, num = sort_key
    raw = f"{format_instant(start)}|{num}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(text):
    """Return the sort key (start, numeric id) or raise validation_error."""
    bad = ApiError("validation_error", "malformed cursor")
    if not isinstance(text, str) or not _B64URL_RE.fullmatch(text):
        raise bad
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise bad
    parts = raw.split("|")
    if len(parts) != 2 or not _DIGITS_RE.fullmatch(parts[1]):
        raise bad
    try:
        start = parse_instant(parts[0])
    except ApiError:
        raise bad
    return start, int(parts[1])


# ---------------------------------------------------------------------------
# Pure scheduling functions
# ---------------------------------------------------------------------------

def intervals_overlap(s1, e1, s2, e2):
    """Half-open intervals [s1, e1) and [s2, e2); touching endpoints do not overlap."""
    return s1 < e2 and s2 < e1


def has_conflict(room_bookings, start, end):
    return any(intervals_overlap(b["start"], b["end"], start, end) for b in room_bookings)


def free_intervals(intervals, day):
    """Gaps inside the working day [08:00Z, 18:00Z) of `day`, not covered by `intervals`.

    `intervals` is an iterable of (start, end) aware datetimes.
    """
    window_start = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
    window_end = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
    clipped = []
    for s, e in intervals:
        cs, ce = max(s, window_start), min(e, window_end)
        if cs < ce:
            clipped.append((cs, ce))
    clipped.sort()
    merged = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    free = []
    cursor = window_start
    for s, e in merged:
        if cursor < s:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < window_end:
        free.append((cursor, window_end))
    return free


def _sort_key(booking):
    return (booking["start"], int(booking["id"][1:]))


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

def room_view(room):
    return {"id": room["id"], "name": room["name"], "capacity": room["capacity"]}


def booking_view(booking):
    return {
        "id": booking["id"],
        "room_id": booking["room_id"],
        "start": format_instant(booking["start"]),
        "end": format_instant(booking["end"]),
        "title": booking["title"],
        "attendees": booking["attendees"],
    }


class Store:
    """All state lives here. A single lock guards every check-and-write."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> room dict, insertion order == id order
        self._room_names = set()
        self.bookings = {}       # id -> booking dict (internal datetimes)
        self.idempotency = {}    # key -> {"fingerprint", "status", "response"}
        self.next_room_seq = 1
        self.next_booking_seq = 1

    # rooms -------------------------------------------------------------
    def create_room(self, body):
        name, capacity = validate_room(body)
        with self.lock:
            if name in self._room_names:
                raise ApiError("room_exists", f"a room named {name!r} already exists")
            room = {"id": f"r{self.next_room_seq}", "name": name, "capacity": capacity}
            self.next_room_seq += 1
            self.rooms[room["id"]] = room
            self._room_names.add(name)
            return room_view(room)

    def list_rooms(self):
        with self.lock:
            return [room_view(r) for r in self.rooms.values()]

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", f"room {room_id!r} not found")
            return room_view(room)

    def availability(self, room_id, day):
        with self.lock:
            if room_id not in self.rooms:
                raise ApiError("not_found", f"room {room_id!r} not found")
            intervals = [(b["start"], b["end"]) for b in self.bookings.values() if b["room_id"] == room_id]
            free = free_intervals(intervals, day)
        return {
            "date": day.isoformat(),
            "free": [{"start": format_instant(s), "end": format_instant(e)} for s, e in free],
        }

    # bookings ----------------------------------------------------------
    def create_booking(self, body, key=None):
        """Create a booking. Returns (status, response body)."""
        with self.lock:
            fingerprint = None
            if key is not None:
                fingerprint = booking_fingerprint(body)
                record = self.idempotency.get(key)
                if record is not None:
                    if record["fingerprint"] == fingerprint and fingerprint is not None:
                        # Replay: original response, no new booking. A body that cannot be
                        # parsed into fields has no fingerprint and is treated as a mismatch.
                        return record["status"], dict(record["response"])
                    raise ApiError("idempotency_mismatch",
                                   "this Idempotency-Key was already used for a different request")
            fields = validate_booking(body)
            room = self.rooms.get(fields["room_id"])
            if room is None:
                raise ApiError("validation_error", f"room {fields['room_id']!r} does not exist")
            if fields["attendees"] > room["capacity"]:
                raise ApiError("over_capacity", f"{fields['attendees']} attendees exceed room capacity {room['capacity']}")
            room_bookings = [b for b in self.bookings.values() if b["room_id"] == fields["room_id"]]
            if has_conflict(room_bookings, fields["start"], fields["end"]):
                raise ApiError("conflict", "the room is already booked for an overlapping time")
            booking = {"id": f"b{self.next_booking_seq}", **fields}
            self.next_booking_seq += 1
            self.bookings[booking["id"]] = booking
            view = booking_view(booking)
            if key is not None:
                self.idempotency[key] = {"fingerprint": fingerprint, "status": 201, "response": view}
            return 201, dict(view)

    def get_booking(self, booking_id):
        with self.lock:
            booking = self.bookings.get(booking_id)
            if booking is None:
                raise ApiError("not_found", f"booking {booking_id!r} not found")
            return booking_view(booking)

    def delete_booking(self, booking_id):
        with self.lock:
            if self.bookings.pop(booking_id, None) is None:
                raise ApiError("not_found", f"booking {booking_id!r} not found")

    def list_bookings(self, room_id, frm, to, cursor, limit):
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm is not None and not b["end"] > frm:
                    continue
                if to is not None and not b["start"] < to:
                    continue
                items.append(b)
            items.sort(key=_sort_key)
            if cursor is not None:
                items = [b for b in items if _sort_key(b) > cursor]
            page = items[:limit]
            next_cursor = None
            if len(items) > limit and page:
                next_cursor = encode_cursor(_sort_key(page[-1]))
            return [booking_view(b) for b in page], next_cursor


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_LENGTH_RE = re.compile(r"[0-9]+")


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (the default): each connection serves one response, then closes.
    protocol_version = "HTTP/1.0"
    server_version = "BookingsAPI/1.0"

    def log_message(self, format, *args):  # keep the process quiet
        pass

    @property
    def store(self):
        return self.server.store

    # --- responses ----------------------------------------------------
    def _send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_no_content(self):
        self.send_response(204)
        self.end_headers()

    # --- request body -------------------------------------------------
    def _read_body(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        text = raw_length.strip()
        if not _LENGTH_RE.fullmatch(text):
            raise ApiError("bad_json", "invalid Content-Length header")
        length = int(text)
        if length > MAX_BODY_BYTES:
            # Rejected without reading the body.
            raise ApiError("validation_error", f"request body exceeds {MAX_BODY_BYTES} bytes")
        return self.rfile.read(length) if length else b""

    def _read_json_object(self):
        raw = self._read_body()
        if not raw.strip():
            raise ApiError("bad_json", "request body must be a JSON object")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError("bad_json", "request body is not valid JSON")
        if not isinstance(value, dict):
            raise ApiError("validation_error", "body must be a JSON object")
        return value

    # --- routing ------------------------------------------------------
    def _resolve(self, path):
        """Return (method -> handler map, positional args) or raise not_found."""
        segs = path.split("/")
        n = len(segs)
        if segs == ["", "rooms"]:
            return {"GET": self._list_rooms, "POST": self._create_room}, ()
        if n == 3 and segs[1] == "rooms" and segs[2]:
            return {"GET": self._get_room}, (segs[2],)
        if n == 4 and segs[1] == "rooms" and segs[2] and segs[3] == "availability":
            return {"GET": self._availability}, (segs[2],)
        if segs == ["", "bookings"]:
            return {"GET": self._list_bookings, "POST": self._create_booking}, ()
        if n == 3 and segs[1] == "bookings" and segs[2]:
            return {"GET": self._get_booking, "DELETE": self._delete_booking}, (segs[2],)
        raise ApiError("not_found", f"no route for {path}")

    def _dispatch(self, method):
        try:
            parts = urlsplit(self.path)
            handlers, args = self._resolve(parts.path)
            handler = handlers.get(method)
            if handler is None:
                raise ApiError("method_not_allowed", f"{method} is not allowed on {parts.path}")
            query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            handler(query, *args)
        except ApiError as err:
            self._send_json(err.status, error_body(err.code, err.message))
        except Exception:  # pragma: no cover - defensive
            self._send_json(500, error_body("internal_error", "unexpected server error"))

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

    # --- handlers: rooms ----------------------------------------------
    def _list_rooms(self, query):
        self._send_json(200, {"rooms": self.store.list_rooms()})

    def _create_room(self, query):
        body = self._read_json_object()
        self._send_json(201, self.store.create_room(body))

    def _get_room(self, query, room_id):
        self._send_json(200, self.store.get_room(room_id))

    def _availability(self, query, room_id):
        if "date" not in query:
            raise ApiError("validation_error", "query parameter date is required")
        day = parse_date(query["date"])
        self._send_json(200, self.store.availability(room_id, day))

    # --- handlers: bookings -------------------------------------------
    def _list_bookings(self, query):
        limit = DEFAULT_LIMIT
        if "limit" in query:
            text = query["limit"]
            if not _DIGITS_RE.fullmatch(text) or not (MIN_LIMIT <= int(text) <= MAX_LIMIT):
                raise ApiError("validation_error", f"limit must be an integer from {MIN_LIMIT} to {MAX_LIMIT}")
            limit = int(text)
        frm = parse_instant(query["from"]) if "from" in query else None
        to = parse_instant(query["to"]) if "to" in query else None
        if frm is not None and to is not None and not frm < to:
            raise ApiError("validation_error", "from must be before to")
        cursor = decode_cursor(query["cursor"]) if "cursor" in query else None
        room_id = query.get("room_id")
        bookings, next_cursor = self.store.list_bookings(room_id, frm, to, cursor, limit)
        self._send_json(200, {"bookings": bookings, "next_cursor": next_cursor})

    def _create_booking(self, query):
        body = self._read_json_object()
        key = self.headers.get("Idempotency-Key") or None
        status, payload = self.store.create_booking(body, key)
        self._send_json(status, payload)

    def _get_booking(self, query, booking_id):
        self._send_json(200, self.store.get_booking(booking_id))

    def _delete_booking(self, query, booking_id):
        self.store.delete_booking(booking_id)
        self._send_no_content()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def make_server(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store = Store()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API (127.0.0.1 only)")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on")
    args = parser.parse_args(argv)
    server = make_server(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
