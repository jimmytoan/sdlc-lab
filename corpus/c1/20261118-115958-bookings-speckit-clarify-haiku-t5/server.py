"""Meeting room booking API.

Small HTTP JSON service (Python 3.13 standard library only, in-memory state).

Run with:  python server.py --port <N>
The server listens on 127.0.0.1 only.
"""
import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

# ---------------------------------------------------------------------------
# Configuration and constants
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
UTC = timezone.utc
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
ROOM_NAME_MAX = 50
ROOM_CAPACITY_MIN = 1
ROOM_CAPACITY_MAX = 500
TITLE_MAX = 200
MAX_BOOKING_DURATION = timedelta(hours=8)
GRID_MINUTES = (0, 15, 30, 45)
WORK_START_HOUR = 8
WORK_END_HOUR = 18

STATUS = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
    # Fallback for unexpected server faults only; not a client-facing rule.
    "internal_error": 500,
}

_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DIGITS_RE = re.compile(r"[0-9]+")
_BOOKING_ID_RE = re.compile(r"b([0-9]+)")


# ---------------------------------------------------------------------------
# Errors and small helpers
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """An error that maps to the documented error envelope."""

    def __init__(self, code, message=None):
        super().__init__(code)
        self.code = code
        self.status = STATUS[code]
        self.message = message or code.replace("_", " ")


def invalid(message):
    return ApiError("validation_error", message)


def error_envelope(code, message):
    return {"error": {"code": code, "message": message}}


def fmt_ts(dt):
    """Format a UTC datetime as YYYY-MM-DDTHH:MM:SSZ."""
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_ts(value, field):
    """Parse a strict UTC timestamp; anything else is a validation_error."""
    m = _TS_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise invalid(f"{field} must be a UTC timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    parts = [int(g) for g in m.groups()]
    try:
        return datetime(*parts, tzinfo=UTC)
    except ValueError:
        raise invalid(f"{field} is not a valid date and time") from None


def parse_grid_ts(value, field):
    """Parse a booking timestamp, which must fall on a quarter hour."""
    dt = parse_ts(value, field)
    if dt.minute not in GRID_MINUTES or dt.second != 0:
        raise invalid(f"{field} must fall on a quarter hour (minutes 00, 15, 30 or 45)")
    return dt


def parse_day(value):
    """Parse a YYYY-MM-DD calendar date."""
    m = _DATE_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise invalid("date must be in the form YYYY-MM-DD")
    try:
        return date(*[int(g) for g in m.groups()])
    except ValueError:
        raise invalid("date is not a valid calendar date") from None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def query_param(pairs, name):
    """Return the single value of a query parameter, or None if absent."""
    values = [v for k, v in pairs if k == name]
    if len(values) > 1:
        raise invalid(f"query parameter '{name}' may only be given once")
    return values[0] if values else None


def parse_json(raw):
    if not raw or not raw.strip():
        raise ApiError("bad_json", "request body is empty")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ApiError("bad_json", "request body is not valid JSON") from None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_room(body):
    name = body.get("name")
    if not isinstance(name, str) or name == "":
        raise invalid("name must be a non-empty string")
    if len(name) > ROOM_NAME_MAX:
        raise invalid(f"name must be at most {ROOM_NAME_MAX} characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not ROOM_CAPACITY_MIN <= capacity <= ROOM_CAPACITY_MAX:
        raise invalid(f"capacity must be an integer from {ROOM_CAPACITY_MIN} to {ROOM_CAPACITY_MAX}")
    return name, capacity


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise invalid("room_id must be a non-empty string")
    start = parse_grid_ts(body.get("start"), "start")
    end = parse_grid_ts(body.get("end"), "end")
    if end <= start:
        raise invalid("end must be after start")
    if end - start > MAX_BOOKING_DURATION:
        raise invalid("a booking may last at most 8 hours")
    title = body.get("title")
    if not isinstance(title, str) or title == "":
        raise invalid("title must be a non-empty string")
    if len(title) > TITLE_MAX:
        raise invalid(f"title must be at most {TITLE_MAX} characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise invalid("attendees must be an integer of at least 1")
    return {"room_id": room_id, "start": start, "end": end,
            "title": title, "attendees": attendees}


# ---------------------------------------------------------------------------
# Cursor codec (HMAC-signed, per process)
# ---------------------------------------------------------------------------

class CursorCodec:
    def __init__(self):
        self._key = secrets.token_bytes(32)

    def _sign(self, payload):
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def encode(self, booking):
        payload = f"{fmt_ts(booking['start'])}|{booking['id']}"
        raw = f"{payload}.{self._sign(payload)}".encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def decode(self, token):
        """Return the (start, numeric id) sort position, or raise validation_error."""
        def bad():
            return invalid("cursor is not valid")
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except ValueError:
            raise bad() from None
        payload, sep, sig = raw.rpartition(".")
        if not sep or not hmac.compare_digest(sig, self._sign(payload)):
            raise bad()
        ts_text, sep, booking_id = payload.partition("|")
        m = _BOOKING_ID_RE.fullmatch(booking_id)
        if not sep or m is None:
            raise bad()
        return parse_ts(ts_text, "cursor"), int(m.group(1))


# ---------------------------------------------------------------------------
# Store: all state, guarded by one lock
# ---------------------------------------------------------------------------

def booking_view(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}            # room id -> room dict (insertion = creation order)
        self.room_names = set()
        self.bookings = {}         # booking id -> internal booking dict
        self.idempotency = {}      # key -> (fingerprint, stored response view)
        self.next_room_seq = 1
        self.next_booking_seq = 1
        self.cursors = CursorCodec()

    # -- rooms ------------------------------------------------------------

    def create_room(self, name, capacity):
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", f"a room named '{name}' already exists")
            room_id = f"r{self.next_room_seq}"
            self.next_room_seq += 1
            room = {"id": room_id, "name": name, "capacity": capacity}
            self.rooms[room_id] = room
            self.room_names.add(name)
            return 201, dict(room)

    def list_rooms(self):
        with self.lock:
            return 200, {"rooms": [dict(r) for r in self.rooms.values()]}

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", f"room '{room_id}' does not exist")
            return 200, dict(room)

    # -- bookings ---------------------------------------------------------

    def create_booking(self, body, key):
        fingerprint = json.dumps(body, sort_keys=True)
        with self.lock:
            if key is not None and key in self.idempotency:
                stored_fp, stored_view = self.idempotency[key]
                if stored_fp != fingerprint:
                    raise ApiError("idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return 201, dict(stored_view)
            if not isinstance(body, dict):
                raise invalid("request body must be a JSON object")
            fields = validate_booking(body)
            room = self.rooms.get(fields["room_id"])
            if room is None:
                raise ApiError("not_found", f"room '{fields['room_id']}' does not exist")
            if fields["attendees"] > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            for other in self.bookings.values():
                if (other["room_id"] == fields["room_id"]
                        and other["start"] < fields["end"]
                        and fields["start"] < other["end"]):
                    raise ApiError("conflict", "the room is already booked for that time")
            num = self.next_booking_seq
            self.next_booking_seq += 1
            booking = {"id": f"b{num}", "num": num, **fields}
            self.bookings[booking["id"]] = booking
            view = booking_view(booking)
            if key is not None:
                self.idempotency[key] = (fingerprint, view)
            return 201, dict(view)

    def get_booking(self, booking_id):
        with self.lock:
            b = self.bookings.get(booking_id)
            if b is None:
                raise ApiError("not_found", f"booking '{booking_id}' does not exist")
            return 200, booking_view(b)

    def delete_booking(self, booking_id):
        with self.lock:
            if self.bookings.pop(booking_id, None) is None:
                raise ApiError("not_found", f"booking '{booking_id}' does not exist")
            return 204, None

    def list_bookings(self, room_id, window_from, window_to, limit, after):
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if window_from is not None and b["end"] <= window_from:
                    continue
                if window_to is not None and b["start"] >= window_to:
                    continue
                if after is not None and (b["start"], b["num"]) <= after:
                    continue
                items.append(b)
            items.sort(key=lambda b: (b["start"], b["num"]))
            page = items[:limit]
            next_cursor = self.cursors.encode(page[-1]) if len(items) > limit else None
            return 200, {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor}

    # -- availability -----------------------------------------------------

    def availability(self, room_id, day, day_text):
        ws = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
        we = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
        with self.lock:
            if room_id not in self.rooms:
                raise ApiError("not_found", f"room '{room_id}' does not exist")
            busy = sorted(
                (max(b["start"], ws), min(b["end"], we))
                for b in self.bookings.values()
                if b["room_id"] == room_id and b["start"] < we and b["end"] > ws
            )
        free = []
        cur = ws
        for start, end in busy:
            if start > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(start)})
            if end > cur:
                cur = end
        if cur < we:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
        return 200, {"date": day_text, "free": free}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

ALLOWED_METHODS = {
    "rooms": {"GET", "POST"},
    "room": {"GET"},
    "availability": {"GET"},
    "bookings": {"GET", "POST"},
    "booking": {"GET", "DELETE"},
}


def match_route(segments):
    """Map path segments (without the leading slash) to (route name, id argument)."""
    if segments == ["rooms"]:
        return "rooms", None
    if len(segments) == 2 and segments[0] == "rooms" and segments[1]:
        return "room", segments[1]
    if len(segments) == 3 and segments[0] == "rooms" and segments[1] and segments[2] == "availability":
        return "availability", segments[1]
    if segments == ["bookings"]:
        return "bookings", None
    if len(segments) == 2 and segments[0] == "bookings" and segments[1]:
        return "booking", segments[1]
    return None, None


def route(store, method, target, headers, read_body):
    """Return (status, payload). payload is None for 204 responses."""
    parts = urlsplit(target)
    path = parts.path
    if not path.startswith("/"):
        raise ApiError("not_found", "no such path")
    name, arg = match_route(path[1:].split("/"))
    if name is None:
        raise ApiError("not_found", "no such path")
    if method not in ALLOWED_METHODS[name]:
        raise ApiError("method_not_allowed", f"{method} is not allowed on this path")
    pairs = parse_qsl(parts.query, keep_blank_values=True)

    if name == "rooms":
        if method == "GET":
            return store.list_rooms()
        body = parse_json(read_body())
        if not isinstance(body, dict):
            raise invalid("request body must be a JSON object")
        room_name, capacity = validate_room(body)
        return store.create_room(room_name, capacity)

    if name == "room":
        return store.get_room(arg)

    if name == "availability":
        day_text = query_param(pairs, "date")
        if day_text is None:
            raise invalid("query parameter 'date' is required (YYYY-MM-DD)")
        day = parse_day(day_text)
        return store.availability(arg, day, day_text)

    if name == "bookings":
        if method == "GET":
            return list_bookings_route(store, pairs)
        body = parse_json(read_body())
        key = headers.get("Idempotency-Key")
        if key is not None and key.strip() == "":
            key = None
        return store.create_booking(body, key)

    if name == "booking":
        if method == "GET":
            return store.get_booking(arg)
        return store.delete_booking(arg)

    raise ApiError("internal_error", "unrouted request")  # pragma: no cover


def list_bookings_route(store, pairs):
    room_id = query_param(pairs, "room_id")

    from_text = query_param(pairs, "from")
    to_text = query_param(pairs, "to")
    window_from = parse_ts(from_text, "from") if from_text is not None else None
    window_to = parse_ts(to_text, "to") if to_text is not None else None
    if window_from is not None and window_to is not None and window_to <= window_from:
        raise invalid("'to' must be after 'from'")

    limit_text = query_param(pairs, "limit")
    if limit_text is None:
        limit = DEFAULT_LIMIT
    else:
        if not _DIGITS_RE.fullmatch(limit_text):
            raise invalid(f"limit must be an integer from 1 to {MAX_LIMIT}")
        limit = int(limit_text)
        if not 1 <= limit <= MAX_LIMIT:
            raise invalid(f"limit must be an integer from 1 to {MAX_LIMIT}")

    cursor_text = query_param(pairs, "cursor")
    after = store.cursors.decode(cursor_text) if cursor_text is not None else None

    return store.list_bookings(room_id, window_from, window_to, limit, after)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "BookingsAPI/1.0"
    sys_version = ""

    def log_message(self, format, *args):  # keep stderr quiet
        return

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        if not _DIGITS_RE.fullmatch(length.strip()):
            raise ApiError("bad_json", "invalid Content-Length header")
        n = int(length)
        return self.rfile.read(n) if n > 0 else b""

    def _handle(self, method):
        try:
            status, payload = route(self.server.store, method, self.path,
                                    self.headers, self._read_body)
        except ApiError as exc:
            status, payload = exc.status, error_envelope(exc.code, exc.message)
        except Exception:  # defensive: never drop the connection silently
            traceback.print_exc(file=sys.stderr)
            status, payload = 500, error_envelope("internal_error", "unexpected server error")
        self._send(status, payload)

    def _send(self, status, payload):
        self.send_response(status)
        if payload is None:
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


for _method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
    setattr(Handler, f"do_{_method}", (lambda m: lambda self: self._handle(m))(_method))


class BookingsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port):
        super().__init__((HOST, port), Handler)
        self.store = Store()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API (127.0.0.1 only).")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        server = BookingsServer(args.port)
    except OSError as exc:
        print(f"error: cannot listen on {HOST}:{args.port}: {exc}", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
