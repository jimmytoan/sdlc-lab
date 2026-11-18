"""Meeting room booking API: a small JSON-over-HTTP service with in-memory state.

Run with:  python server.py --port N     (listens on 127.0.0.1 only)
Standard library only (Python 3.13).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import http.server
import json
import re
import threading
import urllib.parse

# ---------------------------------------------------------------------------
# Constants and error codes
# ---------------------------------------------------------------------------

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

UTC = dt.timezone.utc
MAX_ROOM_NAME = 50
ROOM_CAPACITY_MIN = 1
ROOM_CAPACITY_MAX = 500
MAX_TITLE = 100
MAX_DURATION = dt.timedelta(hours=8)
DAY_START_HOUR = 8
DAY_END_HOUR = 18
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
IDEMPOTENCY_HEADER = "Idempotency-Key"


class ApiError(Exception):
    """An error that is returned to the client as the standard error envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = ERROR_STATUS[code]


def validation_error(message: str) -> ApiError:
    return ApiError("validation_error", message)


# ---------------------------------------------------------------------------
# Validation and time helpers
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INT_RE = re.compile(r"[0-9]+")


def parse_ts(value) -> dt.datetime:
    """Parse the exact wire form YYYY-MM-DDTHH:MM:SSZ into an aware UTC datetime."""
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        raise validation_error("timestamps must have the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise validation_error("timestamp is not a valid UTC date and time")


def format_ts(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value) -> dt.datetime:
    """Parse YYYY-MM-DD into a UTC midnight datetime."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise validation_error("date must have the form YYYY-MM-DD")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise validation_error("date is not a real calendar date")


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_room(body: dict) -> tuple[str, int]:
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise validation_error("name must be a string")
    name = name.strip()
    if not name:
        raise validation_error("name must not be empty")
    if len(name) > MAX_ROOM_NAME:
        raise validation_error(f"name must be at most {MAX_ROOM_NAME} characters")
    if not is_int(capacity) or not ROOM_CAPACITY_MIN <= capacity <= ROOM_CAPACITY_MAX:
        raise validation_error(
            f"capacity must be a whole number from {ROOM_CAPACITY_MIN} to {ROOM_CAPACITY_MAX}"
        )
    return name, capacity


def validate_booking(body: dict) -> dict:
    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id.strip():
        raise validation_error("room_id must be a non-empty string")
    if not isinstance(title, str):
        raise validation_error("title must be a string")
    title = title.strip()
    if not 1 <= len(title) <= MAX_TITLE:
        raise validation_error(f"title must be 1 to {MAX_TITLE} characters after trimming")
    if not is_int(attendees) or attendees < 1:
        raise validation_error("attendees must be an integer of at least 1")
    start = parse_ts(body.get("start"))
    end = parse_ts(body.get("end"))
    for label, moment in (("start", start), ("end", end)):
        if moment.minute % 15 != 0 or moment.second != 0:
            raise validation_error(f"{label} must fall on a quarter hour")
    if end <= start:
        raise validation_error("end must be after start")
    if end - start > MAX_DURATION:
        raise validation_error("a booking may last at most 8 hours")
    return {
        "room_id": room_id,
        "start": start,
        "end": end,
        "title": title,
        "attendees": attendees,
    }


def booking_fingerprint(norm: dict) -> str:
    return json.dumps(
        {
            "room_id": norm["room_id"],
            "start": format_ts(norm["start"]),
            "end": format_ts(norm["end"]),
            "title": norm["title"],
            "attendees": norm["attendees"],
        },
        sort_keys=True,
    )


def encode_cursor(start: dt.datetime, number: int) -> str:
    raw = json.dumps({"s": format_ts(start), "n": number}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> tuple[dt.datetime, int]:
    """Strictly decode a cursor; anything malformed is a validation_error."""
    bad = validation_error("cursor is malformed")
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError):
        raise bad
    if not isinstance(data, dict) or set(data) != {"s", "n"}:
        raise bad
    number = data["n"]
    if not is_int(number) or number < 1:
        raise bad
    try:
        start = parse_ts(data["s"])
    except ApiError:
        raise bad
    return start, number


# ---------------------------------------------------------------------------
# In-memory store (all state guarded by one lock)
# ---------------------------------------------------------------------------


class Room:
    __slots__ = ("number", "name", "capacity")

    def __init__(self, number: int, name: str, capacity: int):
        self.number = number
        self.name = name
        self.capacity = capacity

    @property
    def id(self) -> str:
        return f"r{self.number}"

    def wire(self) -> dict:
        return {"id": self.id, "name": self.name, "capacity": self.capacity}


class Booking:
    __slots__ = ("number", "room_id", "start", "end", "title", "attendees")

    def __init__(self, number, room_id, start, end, title, attendees):
        self.number = number
        self.room_id = room_id
        self.start = start
        self.end = end
        self.title = title
        self.attendees = attendees

    @property
    def id(self) -> str:
        return f"b{self.number}"

    def wire(self) -> dict:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "start": format_ts(self.start),
            "end": format_ts(self.end),
            "title": self.title,
            "attendees": self.attendees,
        }


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms: dict[str, Room] = {}  # insertion-ordered by creation
        self.room_names: set[str] = set()
        self.room_seq = 0
        self.bookings: dict[str, Booking] = {}
        self.booking_seq = 0
        # key -> (fingerprint, booking wire snapshot); only accepted bookings are stored.
        self.idempotency: dict[str, tuple[str, dict]] = {}

    # -- rooms -------------------------------------------------------------

    def create_room(self, body: dict) -> dict:
        name, capacity = validate_room(body)
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", f"a room named '{name}' already exists")
            self.room_seq += 1
            room = Room(self.room_seq, name, capacity)
            self.rooms[room.id] = room
            self.room_names.add(name)
            return room.wire()

    def list_rooms(self) -> dict:
        with self.lock:
            return {"rooms": [r.wire() for r in self.rooms.values()]}

    def get_room(self, room_id: str) -> dict:
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", f"room '{room_id}' not found")
            return room.wire()

    # -- bookings ----------------------------------------------------------

    def create_booking(self, body: dict, key: str | None) -> dict:
        with self.lock:
            if key is not None and key in self.idempotency:
                stored_fp, stored_wire = self.idempotency[key]
                try:
                    norm = validate_booking(body)
                except ApiError:
                    norm = None  # invalid content can never match an accepted request
                if norm is not None and booking_fingerprint(norm) == stored_fp:
                    return dict(stored_wire)
                raise ApiError(
                    "idempotency_mismatch",
                    "Idempotency-Key was already used for a different booking request",
                )

            norm = validate_booking(body)
            room = self.rooms.get(norm["room_id"])
            if room is None:
                raise ApiError("not_found", f"room '{norm['room_id']}' not found")
            if norm["attendees"] > room.capacity:
                raise ApiError("over_capacity", "attendees exceed the room capacity")
            for other in self.bookings.values():
                if (
                    other.room_id == room.id
                    and norm["start"] < other.end
                    and other.start < norm["end"]
                ):
                    raise ApiError("conflict", "the room is already booked for that time")

            self.booking_seq += 1
            booking = Booking(
                self.booking_seq,
                room.id,
                norm["start"],
                norm["end"],
                norm["title"],
                norm["attendees"],
            )
            self.bookings[booking.id] = booking
            wire = booking.wire()
            if key is not None:
                self.idempotency[key] = (booking_fingerprint(norm), dict(wire))
            return wire

    def get_booking(self, booking_id: str) -> dict:
        with self.lock:
            booking = self.bookings.get(booking_id)
            if booking is None:
                raise ApiError("not_found", f"booking '{booking_id}' not found")
            return booking.wire()

    def delete_booking(self, booking_id: str) -> None:
        with self.lock:
            if self.bookings.pop(booking_id, None) is None:
                raise ApiError("not_found", f"booking '{booking_id}' not found")

    def list_bookings(self, params: dict) -> dict:
        room_id = params.get("room_id")
        window_from = parse_ts(params["from"]) if "from" in params else None
        window_to = parse_ts(params["to"]) if "to" in params else None
        if window_from is not None and window_to is not None and window_from >= window_to:
            raise validation_error("from must be earlier than to")
        limit = parse_limit(params.get("limit"))
        cursor = decode_cursor(params["cursor"]) if "cursor" in params else None

        with self.lock:
            items = []
            for booking in self.bookings.values():
                if room_id is not None and booking.room_id != room_id:
                    continue
                if window_from is not None and not booking.end > window_from:
                    continue
                if window_to is not None and not booking.start < window_to:
                    continue
                items.append(booking)
            items.sort(key=lambda b: (b.start, b.number))
            if cursor is not None:
                items = [b for b in items if (b.start, b.number) > cursor]
            page = items[:limit]
            more = len(items) > limit
            next_cursor = None
            if more and page:
                last = page[-1]
                next_cursor = encode_cursor(last.start, last.number)
            return {"bookings": [b.wire() for b in page], "next_cursor": next_cursor}

    # -- availability ------------------------------------------------------

    def availability(self, room_id: str, date_text) -> dict:
        with self.lock:
            if room_id not in self.rooms:
                raise ApiError("not_found", f"room '{room_id}' not found")
            day = parse_date(date_text)
            window_start = day.replace(hour=DAY_START_HOUR)
            window_end = day.replace(hour=DAY_END_HOUR)
            busy = []
            for booking in self.bookings.values():
                if booking.room_id != room_id:
                    continue
                if booking.start < window_end and window_start < booking.end:
                    busy.append((max(booking.start, window_start), min(booking.end, window_end)))
            busy.sort()
            free = []
            cursor = window_start
            for start, end in busy:
                if start > cursor:
                    free.append((cursor, start))
                cursor = max(cursor, end)
            if cursor < window_end:
                free.append((cursor, window_end))
            return {
                "date": date_text,
                "free": [{"start": format_ts(s), "end": format_ts(e)} for s, e in free],
            }


def parse_limit(raw) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not _INT_RE.fullmatch(raw):
        raise validation_error(f"limit must be an integer from 1 to {MAX_LIMIT}")
    value = int(raw)
    if not 1 <= value <= MAX_LIMIT:
        raise validation_error(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return value


# ---------------------------------------------------------------------------
# Request handler and routing
# ---------------------------------------------------------------------------

_ID = object()  # placeholder in route patterns: matches any non-empty path segment

# (path pattern, {method: handler name})
ROUTES = [
    (("rooms",), {"GET": "list_rooms", "POST": "create_room"}),
    (("rooms", _ID), {"GET": "get_room"}),
    (("rooms", _ID, "availability"), {"GET": "availability"}),
    (("bookings",), {"GET": "list_bookings", "POST": "create_booking"}),
    (("bookings", _ID), {"GET": "get_booking", "DELETE": "delete_booking"}),
]


def match_route(segments: list[str]):
    """Return (route methods, captured ids) for an exact path match, or None."""
    for pattern, methods in ROUTES:
        if len(pattern) != len(segments):
            continue
        captured = []
        ok = True
        for want, got in zip(pattern, segments):
            if want is _ID:
                if not got:
                    ok = False
                    break
                captured.append(got)
            elif want != got:
                ok = False
                break
        if ok:
            return methods, captured
    return None


def parse_json_object(raw: bytes) -> dict:
    if not raw or not raw.strip():
        raise ApiError("bad_json", "request body is required and must be JSON")

    def reject_constant(name):
        raise ValueError(name)

    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise validation_error("request body must be a JSON object")
    return data


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "bookings"
    sys_version = ""

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    def _handle(self):
        try:
            raw = self._read_body()
            status, payload = self._route(raw)
        except ApiError as err:
            status = err.status
            payload = {"error": {"code": err.code, "message": err.message}}
        except Exception:  # never leak a ninth error code; plain text only
            self._send_plain(500, "internal server error")
            return
        self._send_json(status, payload)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _route(self, raw: bytes):
        parsed = urllib.parse.urlsplit(self.path)
        segments = parsed.path.split("/")[1:] if parsed.path.startswith("/") else None
        matched = match_route(segments) if segments is not None else None
        if matched is None:
            raise ApiError("not_found", "no such path")
        methods, captured = matched
        name = methods.get(self.command)
        if name is None:
            raise ApiError("method_not_allowed", f"{self.command} is not allowed on this path")

        store: Store = self.server.store
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}

        if name == "list_rooms":
            return 200, store.list_rooms()
        if name == "create_room":
            return 201, store.create_room(parse_json_object(raw))
        if name == "get_room":
            return 200, store.get_room(captured[0])
        if name == "availability":
            return 200, store.availability(captured[0], params.get("date"))
        if name == "list_bookings":
            return 200, store.list_bookings(params)
        if name == "create_booking":
            body = parse_json_object(raw)
            return 201, store.create_booking(body, self._idempotency_key())
        if name == "get_booking":
            return 200, store.get_booking(captured[0])
        if name == "delete_booking":
            store.delete_booking(captured[0])
            return 204, None
        raise RuntimeError(f"unrouted handler {name}")  # pragma: no cover

    def _idempotency_key(self) -> str | None:
        value = self.headers.get(IDEMPOTENCY_HEADER)
        if value is None or not value.strip():
            return None
        return value.strip()

    def _send_json(self, status: int, payload) -> None:
        if status == 204:
            self.send_response(204)
            self.send_header("Connection", "close")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_plain(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def make_server(port: int) -> http.server.ThreadingHTTPServer:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.store = Store()
    return server


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Meeting room booking API (loopback only).")
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
