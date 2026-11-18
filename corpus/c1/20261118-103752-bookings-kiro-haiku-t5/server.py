#!/usr/bin/env python3
"""bookings: meeting room booking HTTP JSON API.

Run as `python server.py --port <N>`. Listens on 127.0.0.1 only. Standard library
only. All state is held in memory and discarded when the process stops.
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import traceback
from bisect import insort
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_BODY_BYTES = 64 * 1024
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_DURATION_SECONDS = 8 * 3600
WORK_START_HOUR = 8
WORK_END_HOUR = 18
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_INT_RE = re.compile(r"[+-]?[0-9]+")
_ROOM_ID_RE = re.compile(r"r[0-9]+")
_BOOKING_ID_RE = re.compile(r"b[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def validation_error(message):
    return ApiError(422, "validation_error", message)


# ---------------------------------------------------------------- timestamps

def parse_timestamp(value, quarter_hours=True):
    """Parse YYYY-MM-DDTHH:MM:SSZ to epoch seconds (UTC).

    quarter_hours=True applies the booking rules (3.6 to 3.8). The query
    parameters from/to only require the form and a real date (8.9).
    """
    if not isinstance(value, str):
        raise validation_error("timestamp must be a string in the form YYYY-MM-DDTHH:MM:SSZ")
    m = _TS_RE.fullmatch(value)
    if not m:
        raise validation_error("timestamp must be in the form YYYY-MM-DDTHH:MM:SSZ")
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    try:
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        raise validation_error("timestamp is not a real date and time")
    if quarter_hours and (mi not in (0, 15, 30, 45) or s != 0):
        raise validation_error("timestamp must be on a quarter hour with zero seconds")
    return (dt - EPOCH) // timedelta(seconds=1)


def format_timestamp(epoch):
    dt = EPOCH + timedelta(seconds=epoch)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def parse_date(value):
    """Parse YYYY-MM-DD. Returns (year, month, day)."""
    if not isinstance(value, str):
        raise validation_error("date must be in the form YYYY-MM-DD")
    m = _DATE_RE.fullmatch(value)
    if not m:
        raise validation_error("date must be in the form YYYY-MM-DD")
    y, mo, d = (int(g) for g in m.groups())
    try:
        datetime(y, mo, d)
    except ValueError:
        raise validation_error("date is not a real calendar date")
    return y, mo, d


def day_epoch(ymd, hour):
    y, mo, d = ymd
    return (datetime(y, mo, d, hour, tzinfo=timezone.utc) - EPOCH) // timedelta(seconds=1)


# ---------------------------------------------------------------- validators

def validate_name(value):
    if not isinstance(value, str) or value.strip() == "":
        raise validation_error("name must be a non-blank string")
    if len(value) > 50:
        raise validation_error("name must be at most 50 characters")
    return value


def validate_capacity(value):
    if type(value) is not int or not (1 <= value <= 500):
        raise validation_error("capacity must be an integer from 1 to 500")
    return value


def validate_title(value):
    if not isinstance(value, str) or value.strip() == "":
        raise validation_error("title must be a non-blank string")
    if len(value) > 200:
        raise validation_error("title must be at most 200 characters")
    return value


def validate_attendees(value):
    if type(value) is not int or value < 1:
        raise validation_error("attendees must be an integer of at least 1")
    return value


def validate_room_id(value):
    if not isinstance(value, str):
        raise validation_error("room_id must be a string")
    return value


def validate_idempotency_key(value):
    if len(value) == 0 or len(value) > 255:
        raise validation_error("Idempotency-Key must be 1 to 255 characters")
    return value


def parse_limit(text):
    if text is None:
        return DEFAULT_LIMIT
    if not _INT_RE.fullmatch(text):
        raise validation_error("limit must be an integer from 1 to 100")
    value = int(text)
    if not (1 <= value <= MAX_LIMIT):
        raise validation_error("limit must be an integer from 1 to 100")
    return value


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- availability

def free_intervals(day_start, day_end, busy):
    """busy: iterable of (start, end) epoch pairs. Returns sorted non-empty free gaps."""
    clipped = []
    for s, e in busy:
        if s < day_end and day_start < e:
            clipped.append((max(s, day_start), min(e, day_end)))
    clipped.sort()
    free = []
    cur = day_start
    for s, e in clipped:
        if s > cur:
            free.append((cur, s))
        if e > cur:
            cur = e
    if cur < day_end:
        free.append((cur, day_end))
    return free


# ---------------------------------------------------------------- cursors

def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_cursor(key, start, num):
    payload = json.dumps([start, num], separators=(",", ":")).encode("utf-8")
    tag = hmac.new(key, payload, hashlib.sha256).digest()
    return _b64e(payload) + "." + _b64e(tag)


def decode_cursor(key, text):
    try:
        parts = text.split(".")
        if len(parts) != 2:
            raise ValueError("shape")
        payload = _b64d(parts[0])
        tag = _b64d(parts[1])
        expected = hmac.new(key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValueError("tag")
        start, num = json.loads(payload.decode("utf-8"))
        if type(start) is not int or type(num) is not int:
            raise ValueError("types")
        return start, num
    except Exception:
        raise validation_error("cursor is not valid")


# ---------------------------------------------------------------- store

class Room:
    __slots__ = ("id", "name", "capacity")

    def __init__(self, room_id, name, capacity):
        self.id = room_id
        self.name = name
        self.capacity = capacity

    def to_json(self):
        return {"id": self.id, "name": self.name, "capacity": self.capacity}


class Booking:
    """Identity-compared record; the numeric id is the tie-breaker in ordering."""

    def __init__(self, booking_id, num, room_id, start, end, title, attendees):
        self.id = booking_id
        self.num = num
        self.room_id = room_id
        self.start = start
        self.end = end
        self.title = title
        self.attendees = attendees

    def sort_key(self):
        return (self.start, self.num)

    def to_json(self):
        return {
            "id": self.id,
            "room_id": self.room_id,
            "start": format_timestamp(self.start),
            "end": format_timestamp(self.end),
            "title": self.title,
            "attendees": self.attendees,
        }


def _booking_key(b):
    return (b.start, b.num)


class Store:
    """All mutable state. Every state change happens while holding self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = []
        self.rooms_by_id = {}
        self.room_names = set()
        self.room_seq = 0
        self.bookings_by_id = {}
        self.room_bookings = {}
        self.all_bookings = []
        self.booking_seq = 0
        self.idem = {}
        self.cursor_key = os.urandom(32)


# ---------------------------------------------------------------- body parsing

def _reject_constant(name):
    raise ValueError("non-standard JSON constant: " + name)


def parse_json_object(raw):
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise validation_error("request body must be a JSON object")
    return obj


def parse_booking_input(body):
    room_id = validate_room_id(body.get("room_id"))
    start = parse_timestamp(body.get("start"))
    end = parse_timestamp(body.get("end"))
    if end <= start:
        raise validation_error("end must be later than start")
    if end - start > MAX_DURATION_SECONDS:
        raise validation_error("booking must be at most 8 hours long")
    title = validate_title(body.get("title"))
    attendees = validate_attendees(body.get("attendees"))
    return room_id, start, end, title, attendees


# ---------------------------------------------------------------- routes

ROUTES = [
    (re.compile(r"/rooms"), {"POST": "create_room", "GET": "list_rooms"}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": "get_room"}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": "availability"}),
    (re.compile(r"/bookings"), {"POST": "create_booking", "GET": "list_bookings"}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": "get_booking", "DELETE": "delete_booking"}),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""
    timeout = 30

    def log_message(self, format, *args):  # noqa: A002 - base class signature
        pass

    # --- HTTP method entry points -------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def send_error(self, code, message=None, explain=None):
        """Base-class protocol errors are answered with the JSON envelope."""
        self.close_connection = True
        if code == 501 and self.command:
            self._dispatch(self.command)
            return
        if code == 400:
            err_code = "bad_json"
        else:
            err_code = "internal_error"
        self._send(code, _error_payload(err_code, message or "request failed"))

    # --- core ----------------------------------------------------------
    def _dispatch(self, method):
        try:
            status, payload, headers = self._route(method)
        except ApiError as e:
            status, payload, headers = e.status, _error_payload(e.code, e.message), e.headers
        except Exception:
            traceback.print_exc(file=sys.stderr)
            status, payload, headers = 500, _error_payload("internal_error", "internal server error"), {}
        try:
            self._send(status, payload, headers)
        except Exception:
            traceback.print_exc(file=sys.stderr)

    def _route(self, method):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        query = {}
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            query.setdefault(k, v)

        matched = None
        for pattern, handlers in ROUTES:
            m = pattern.fullmatch(path)
            if m:
                matched = (handlers, m.groups())
                break
        if matched is None:
            raise ApiError(404, "not_found", "no route matches this path")
        handlers, groups = matched
        if method not in handlers:
            allow = ", ".join(sorted(handlers))
            raise ApiError(405, "method_not_allowed", f"method {method} is not allowed here",
                           {"Allow": allow})
        name = handlers[method]
        result = getattr(self, "h_" + name)(groups, query)
        status, payload = result[0], result[1]
        headers = result[2] if len(result) > 2 else {}
        return status, payload, headers

    def _read_json_body(self):
        """Read and parse a POST body. Only called by routes that accept a body."""
        raw_length = self.headers.get("Content-Length")
        length = None
        if raw_length is not None:
            try:
                length = int(raw_length.strip())
            except ValueError:
                length = None
        if length is None or length <= 0:
            raise ApiError(400, "bad_json", "a JSON request body is required")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ApiError(400, "bad_json", "request body is larger than 64 KiB",
                           {"Connection": "close"})
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "bad_json", "request body was incomplete")
        return parse_json_object(raw)

    def _send(self, status, payload=None, headers=None):
        extra = headers or {}
        if status == 204:
            self.send_response(204)
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    @property
    def store(self):
        return self.server.store

    # --- rooms ---------------------------------------------------------
    def h_create_room(self, groups, query):
        body = self._read_json_body()
        name = validate_name(body.get("name"))
        capacity = validate_capacity(body.get("capacity"))
        store = self.store
        with store.lock:
            if name in store.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            store.room_seq += 1
            room = Room(f"r{store.room_seq}", name, capacity)
            store.rooms.append(room)
            store.rooms_by_id[room.id] = room
            store.room_names.add(name)
            store.room_bookings[room.id] = []
        return 201, room.to_json()

    def h_list_rooms(self, groups, query):
        with self.store.lock:
            rooms = list(self.store.rooms)
        return 200, {"rooms": [r.to_json() for r in rooms]}

    def _find_room(self, room_id):
        if not _ROOM_ID_RE.fullmatch(room_id):
            raise ApiError(404, "not_found", "room not found")
        with self.store.lock:
            room = self.store.rooms_by_id.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return room

    def h_get_room(self, groups, query):
        return 200, self._find_room(groups[0]).to_json()

    def h_availability(self, groups, query):
        if "date" not in query:
            raise validation_error("date query parameter is required")
        ymd = parse_date(query["date"])
        room = self._find_room(groups[0])
        day_start = day_epoch(ymd, WORK_START_HOUR)
        day_end = day_epoch(ymd, WORK_END_HOUR)
        with self.store.lock:
            busy = [(b.start, b.end) for b in self.store.room_bookings.get(room.id, [])]
        gaps = free_intervals(day_start, day_end, busy)
        free = [{"start": format_timestamp(s), "end": format_timestamp(e)} for s, e in gaps]
        return 200, {"date": query["date"], "free": free}

    # --- bookings ------------------------------------------------------
    def h_create_booking(self, groups, query):
        body = self._read_json_body()
        key = None
        if "Idempotency-Key" in self.headers:
            key = validate_idempotency_key(self.headers["Idempotency-Key"])
        canon = canonical(body)
        store = self.store
        with store.lock:
            if key is not None and key in store.idem:
                recorded_canon, recorded_response = store.idem[key]
                if recorded_canon == canon:
                    return 201, dict(recorded_response)
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used with a different request body")

            room_id, start, end, title, attendees = parse_booking_input(body)
            room = store.rooms_by_id.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room.capacity:
                raise ApiError(422, "over_capacity", "more attendees than the room can hold")
            for other in store.room_bookings[room.id]:
                if other.start < end and start < other.end:
                    raise ApiError(409, "conflict", "the room is already booked for this time")

            # Commit: nothing above has changed state.
            store.booking_seq += 1
            booking = Booking(f"b{store.booking_seq}", store.booking_seq, room.id,
                              start, end, title, attendees)
            insort(store.room_bookings[room.id], booking, key=_booking_key)
            insort(store.all_bookings, booking, key=_booking_key)
            store.bookings_by_id[booking.id] = booking
            response = booking.to_json()
            if key is not None:
                store.idem[key] = (canon, dict(response))
        return 201, response

    def _find_booking(self, booking_id):
        if not _BOOKING_ID_RE.fullmatch(booking_id):
            raise ApiError(404, "not_found", "booking not found")
        with self.store.lock:
            booking = self.store.bookings_by_id.get(booking_id)
        if booking is None:
            raise ApiError(404, "not_found", "booking not found")
        return booking

    def h_get_booking(self, groups, query):
        return 200, self._find_booking(groups[0]).to_json()

    def h_delete_booking(self, groups, query):
        booking_id = groups[0]
        if not _BOOKING_ID_RE.fullmatch(booking_id):
            raise ApiError(404, "not_found", "booking not found")
        store = self.store
        with store.lock:
            booking = store.bookings_by_id.pop(booking_id, None)
            if booking is None:
                raise ApiError(404, "not_found", "booking not found")
            store.room_bookings[booking.room_id] = [
                b for b in store.room_bookings[booking.room_id] if b is not booking]
            store.all_bookings = [b for b in store.all_bookings if b is not booking]
        return 204, None

    def h_list_bookings(self, groups, query):
        room_filter = query.get("room_id")
        frm = parse_timestamp(query["from"], quarter_hours=False) if "from" in query else None
        to = parse_timestamp(query["to"], quarter_hours=False) if "to" in query else None
        if frm is not None and to is not None and frm >= to:
            raise validation_error("from must be earlier than to")
        limit = parse_limit(query.get("limit"))
        cursor = decode_cursor(self.store.cursor_key, query["cursor"]) if "cursor" in query else None

        store = self.store
        with store.lock:
            if room_filter is not None:
                items = list(store.room_bookings.get(room_filter, []))
            else:
                items = list(store.all_bookings)

        if frm is not None:
            items = [b for b in items if b.end > frm]
        if to is not None:
            items = [b for b in items if b.start < to]
        if cursor is not None:
            items = [b for b in items if (b.start, b.num) > cursor]

        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(store.cursor_key, last.start, last.num)
        return 200, {"bookings": [b.to_json() for b in page], "next_cursor": next_cursor}


def _error_payload(code, message):
    return {"error": {"code": code, "message": message}}


class BookingsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address, handler_class):
        self.store = Store()
        super().__init__(address, handler_class)


def _port_number(text):
    if not _INT_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("must be an integer")
    value = int(text)
    if not (1 <= value <= 65535):
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="bookings: meeting room booking API on 127.0.0.1")
    parser.add_argument("--port", required=True, type=_port_number,
                        help="TCP port to listen on (1 to 65535)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    server = BookingsServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
