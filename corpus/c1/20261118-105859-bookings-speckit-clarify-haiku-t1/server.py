"""Meeting room booking API: a small in-memory JSON-over-HTTP service.

Run as:  python server.py --port <N>
Listens on 127.0.0.1 only. Standard library only (Python 3.13).
"""

import argparse
import base64
import datetime as dt
import http.server
import json
import re
import threading
import urllib.parse

HOST = "127.0.0.1"
UTC = dt.timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING_LENGTH = dt.timedelta(hours=8)
QUARTER_MINUTES = (0, 15, 30, 45)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
BOOKING_ID_RE = re.compile(r"b([0-9]+)")
INT_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    """An error that is returned to the client in the standard error envelope."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation_error(message):
    return ApiError(422, "validation_error", message)


def not_found_error(message="resource not found"):
    return ApiError(404, "not_found", message)


# ---------------------------------------------------------------------------
# Timestamp and value helpers
# ---------------------------------------------------------------------------

def parse_utc(value, field):
    """Parse a strict `YYYY-MM-DDTHH:MM:SSZ` timestamp into an aware UTC datetime."""
    if not isinstance(value, str):
        raise validation_error(f"{field} must be a string in the form YYYY-MM-DDTHH:MM:SSZ")
    match = TS_RE.fullmatch(value)
    if match is None:
        raise validation_error(f"{field} must be a UTC timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return dt.datetime(*(int(g) for g in match.groups()), tzinfo=UTC)
    except ValueError:
        raise validation_error(f"{field} is not a valid date and time")


def format_utc(value):
    return (f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
            f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}Z")


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Request body and validation
# ---------------------------------------------------------------------------

def json_body(raw):
    """Parse a request body that must be a JSON object."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(value, dict):
        raise validation_error("request body must be a JSON object")
    return value


def validate_room(body):
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 50:
        raise validation_error("name must be a string of 1 to 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not 1 <= capacity <= 500:
        raise validation_error("capacity must be an integer from 1 to 500")
    return name, capacity


def validate_quarter_hour(value, field):
    if value.minute not in QUARTER_MINUTES or value.second != 0:
        raise validation_error(f"{field} must fall on a quarter hour (minutes 00, 15, 30 or 45, seconds 00)")


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise validation_error("room_id must be a non-empty string")
    title = body.get("title")
    if not isinstance(title, str) or title == "":
        raise validation_error("title must be a non-empty string")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise validation_error("attendees must be an integer of at least 1")
    start = parse_utc(body.get("start"), "start")
    end = parse_utc(body.get("end"), "end")
    validate_quarter_hour(start, "start")
    validate_quarter_hour(end, "end")
    if end <= start:
        raise validation_error("end must be strictly after start")
    if end - start > MAX_BOOKING_LENGTH:
        raise validation_error("a booking may be at most 8 hours long")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def fingerprint(fields):
    """Canonical form of a validated booking request, used to compare idempotent retries."""
    canonical = {
        "room_id": fields["room_id"],
        "start": format_utc(fields["start"]),
        "end": format_utc(fields["end"]),
        "title": fields["title"],
        "attendees": fields["attendees"],
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Pagination cursor
# ---------------------------------------------------------------------------

def sort_key(booking):
    return (booking["start"], int(booking["id"][1:]))


def encode_cursor(booking):
    payload = json.dumps([format_utc(booking["start"]), booking["id"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(text):
    """Return the (start, numeric id) sort key that a cursor points at."""
    invalid = validation_error("cursor is not valid")
    try:
        padded = text + "=" * (-len(text) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise invalid
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, str) for v in value)):
        raise invalid
    start = parse_utc(value[0], "cursor")
    match = BOOKING_ID_RE.fullmatch(value[1])
    if match is None:
        raise invalid
    return (start, int(match.group(1)))


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def room_view(room):
    return {"id": room["id"], "name": room["name"], "capacity": room["capacity"]}


def booking_view(booking):
    return {
        "id": booking["id"],
        "room_id": booking["room_id"],
        "start": format_utc(booking["start"]),
        "end": format_utc(booking["end"]),
        "title": booking["title"],
        "attendees": booking["attendees"],
    }


# ---------------------------------------------------------------------------
# Service: in-memory state and endpoint logic, all guarded by one lock
# ---------------------------------------------------------------------------

def resolve_route(path):
    """Map a path to (route name, path params, allowed methods), or None for unknown paths."""
    segments = path.split("/")[1:]
    if segments == ["rooms"]:
        return "rooms", {}, ("GET", "POST")
    if len(segments) == 2 and segments[0] == "rooms" and segments[1]:
        return "room", {"id": segments[1]}, ("GET",)
    if len(segments) == 3 and segments[0] == "rooms" and segments[1] and segments[2] == "availability":
        return "availability", {"id": segments[1]}, ("GET",)
    if segments == ["bookings"]:
        return "bookings", {}, ("GET", "POST")
    if len(segments) == 2 and segments[0] == "bookings" and segments[1]:
        return "booking", {"id": segments[1]}, ("GET", "DELETE")
    return None


def first_param(query, name):
    values = query.get(name)
    return None if values is None else values[0]


class Service:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> room dict, insertion order = creation order
        self.bookings = {}       # id -> booking dict (datetimes for start/end)
        self.idempotency = {}    # key -> (fingerprint, 201 response body)
        self._room_seq = 0
        self._booking_seq = 0

    def dispatch(self, method, path, query_string, headers, raw):
        """Return (status, body) for a request. Body None means no content."""
        route = resolve_route(path)
        if route is None:
            raise not_found_error("no such path")
        name, params, allowed = route
        if method not in allowed:
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed on this path")
        query = urllib.parse.parse_qs(query_string, keep_blank_values=True)
        with self.lock:
            if name == "rooms":
                if method == "GET":
                    return 200, {"rooms": [room_view(r) for r in self.rooms.values()]}
                return self.create_room(raw)
            if name == "room":
                return 200, room_view(self.find_room(params["id"]))
            if name == "availability":
                return 200, self.availability(params["id"], query)
            if name == "bookings":
                if method == "GET":
                    return 200, self.list_bookings(query)
                key = headers.get("Idempotency-Key") or None
                return self.create_booking(raw, key)
            if name == "booking":
                if method == "GET":
                    return 200, booking_view(self.find_booking(params["id"]))
                return self.delete_booking(params["id"])
        raise not_found_error("no such path")  # pragma: no cover

    # -- rooms ---------------------------------------------------------------

    def find_room(self, room_id):
        room = self.rooms.get(room_id)
        if room is None:
            raise not_found_error(f"room {room_id} does not exist")
        return room

    def create_room(self, raw):
        name, capacity = validate_room(json_body(raw))
        if any(r["name"] == name for r in self.rooms.values()):
            raise ApiError(409, "room_exists", "a room with this name already exists")
        self._room_seq += 1
        room = {"id": f"r{self._room_seq}", "name": name, "capacity": capacity}
        self.rooms[room["id"]] = room
        return 201, room_view(room)

    def availability(self, room_id, query):
        self.find_room(room_id)
        date_text = first_param(query, "date")
        match = None if date_text is None else DATE_RE.fullmatch(date_text)
        if match is None:
            raise validation_error("date is required and must be YYYY-MM-DD")
        try:
            day = dt.date(*(int(g) for g in match.groups()))
        except ValueError:
            raise validation_error("date is not a real calendar date")
        window_start = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
        window_end = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
        busy = sorted(
            (max(b["start"], window_start), min(b["end"], window_end))
            for b in self.bookings.values()
            if b["room_id"] == room_id and b["start"] < window_end and b["end"] > window_start
        )
        free = []
        cursor = window_start
        for start, end in busy:
            if start > cursor:
                free.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < window_end:
            free.append((cursor, window_end))
        return {
            "date": day.isoformat(),
            "free": [{"start": format_utc(a), "end": format_utc(b)} for a, b in free],
        }

    # -- bookings ------------------------------------------------------------

    def find_booking(self, booking_id):
        booking = self.bookings.get(booking_id)
        if booking is None:
            raise not_found_error(f"booking {booking_id} does not exist")
        return booking

    def create_booking(self, raw, key):
        fields = validate_booking(json_body(raw))
        fp = fingerprint(fields)
        if key is not None and key in self.idempotency:
            stored_fp, stored_response = self.idempotency[key]
            if stored_fp == fp:
                return 201, dict(stored_response)
            raise ApiError(422, "idempotency_mismatch",
                           "this Idempotency-Key was already used for a different request")
        room = self.rooms.get(fields["room_id"])
        if room is None:
            raise not_found_error(f"room {fields['room_id']} does not exist")
        if fields["attendees"] > room["capacity"]:
            raise ApiError(422, "over_capacity", "attendees exceed the room capacity")
        for other in self.bookings.values():
            if (other["room_id"] == room["id"]
                    and fields["start"] < other["end"] and other["start"] < fields["end"]):
                raise ApiError(409, "conflict", "the room is already booked for an overlapping time")
        self._booking_seq += 1
        booking = {"id": f"b{self._booking_seq}", **fields}
        self.bookings[booking["id"]] = booking
        response = booking_view(booking)
        if key is not None:
            self.idempotency[key] = (fp, response)
        return 201, dict(response)

    def delete_booking(self, booking_id):
        self.find_booking(booking_id)
        del self.bookings[booking_id]
        return 204, None

    def list_bookings(self, query):
        room_id = first_param(query, "room_id")

        limit_text = first_param(query, "limit")
        if limit_text is None:
            limit = DEFAULT_LIMIT
        else:
            if INT_RE.fullmatch(limit_text) is None or not 1 <= int(limit_text) <= MAX_LIMIT:
                raise validation_error("limit must be an integer from 1 to 100")
            limit = int(limit_text)

        from_text = first_param(query, "from")
        window_from = None if from_text is None else parse_utc(from_text, "from")
        to_text = first_param(query, "to")
        window_to = None if to_text is None else parse_utc(to_text, "to")
        if window_from is not None and window_to is not None and window_from > window_to:
            raise validation_error("from must not be later than to")

        cursor_text = first_param(query, "cursor")
        after = None if cursor_text is None else decode_cursor(cursor_text)

        matches = []
        for booking in self.bookings.values():
            if room_id is not None and booking["room_id"] != room_id:
                continue
            if window_from is not None and not booking["end"] > window_from:
                continue
            if window_to is not None and not booking["start"] < window_to:
                continue
            if after is not None and not sort_key(booking) > after:
                continue
            matches.append(booking)
        matches.sort(key=sort_key)

        page = matches[:limit]
        has_more = len(matches) > limit
        next_cursor = encode_cursor(page[-1]) if has_more else None
        return {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BookingsAPI/1.0"
    sys_version = ""

    def _handle(self):
        raw = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            raw = self.rfile.read(length)

        parts = urllib.parse.urlsplit(self.path)
        try:
            status, body = self.server.service.dispatch(
                self.command, parts.path, parts.query, self.headers, raw)
        except ApiError as err:
            status = err.status
            body = {"error": {"code": err.code, "message": err.message}}
        except Exception:  # defensive: never leak a stack trace or HTML page
            status = 500
            body = {"error": {"code": "internal_error", "message": "unexpected server error"}}

        self._send(status, body)

    def _send(self, status, body):
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

    def log_message(self, format, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API (127.0.0.1 only).")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    server = Server((HOST, args.port), Handler)
    server.service = Service()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
