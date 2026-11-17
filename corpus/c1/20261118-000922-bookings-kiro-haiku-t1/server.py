#!/usr/bin/env python3
"""bookings: meeting room booking HTTP JSON API.

Runs as `python server.py --port <N>` on 127.0.0.1. Standard library only.
All state is kept in process memory.
"""
import argparse
import base64
import binascii
import json
import re
import socketserver
import sys
import threading
import traceback
import unicodedata
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

HOST = "127.0.0.1"
MAX_BODY = 64 * 1024
DEFAULT_LIMIT = 20
MAX_DURATION = 8 * 3600
DAY_START_HOUR = 8
DAY_END_HOUR = 18
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
ROOM_ID_RE = re.compile(r"r[1-9][0-9]*")
BOOKING_ID_RE = re.compile(r"b[1-9][0-9]*")
LIMIT_RE = re.compile(r"[0-9]+")
CURSOR_CHARS_RE = re.compile(r"[A-Za-z0-9_-]+")
CURSOR_PAYLOAD_RE = re.compile(r"(-?[0-9]+):([1-9][0-9]*)")

ROOM_LIST_PARAMS = ()
AVAILABILITY_PARAMS = ("date",)
BOOKING_LIST_PARAMS = ("room_id", "from", "to", "limit", "cursor")


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

def epoch_of(year, month, day, hour=0, minute=0, second=0):
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(seconds=1)


def format_ts(epoch):
    dt = EPOCH + timedelta(seconds=epoch)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def parse_timestamp(value, field):
    """Strict UTC form YYYY-MM-DDTHH:MM:SSZ naming a real date and time -> epoch seconds."""
    if not isinstance(value, str):
        raise validation_error(f"{field} must be a string")
    m = TS_RE.fullmatch(value)
    if m is None:
        raise validation_error(f"{field} must be a timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return epoch_of(*(int(g) for g in m.groups()))
    except ValueError:
        raise validation_error(f"{field} is not a real date and time")


def is_quarter_hour(epoch):
    return epoch % 900 == 0


def parse_date(value):
    """YYYY-MM-DD naming a real calendar date -> date object."""
    if not isinstance(value, str):
        raise validation_error("date must be a string")
    m = DATE_RE.fullmatch(value)
    if m is None:
        raise validation_error("date must be in the form YYYY-MM-DD")
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError:
        raise validation_error("date is not a real calendar date")


# ---------------------------------------------------------------- validation

def validate_name(name):
    if not isinstance(name, str):
        raise validation_error("name must be a string")
    if not 1 <= len(name) <= 50:
        raise validation_error("name must be 1 to 50 characters")
    if name.strip() == "":
        raise validation_error("name must contain a non-whitespace character")


def validate_capacity(capacity):
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise validation_error("capacity must be an integer")
    if not 1 <= capacity <= 500:
        raise validation_error("capacity must be between 1 and 500")


def validate_booking(obj):
    room_id = obj.get("room_id")
    if not isinstance(room_id, str):
        raise validation_error("room_id must be a string")
    start = parse_timestamp(obj.get("start"), "start")
    end = parse_timestamp(obj.get("end"), "end")
    if not is_quarter_hour(start):
        raise validation_error("start must be on a quarter-hour boundary")
    if not is_quarter_hour(end):
        raise validation_error("end must be on a quarter-hour boundary")
    if end == start:
        raise validation_error("end must be later than start")
    if end < start:
        raise validation_error("end must not be earlier than start")
    if end - start > MAX_DURATION:
        raise validation_error("booking must not be longer than 8 hours")
    title = obj.get("title")
    if not isinstance(title, str):
        raise validation_error("title must be a string")
    if not 1 <= len(title) <= 200:
        raise validation_error("title must be 1 to 200 characters")
    if title.strip() == "":
        raise validation_error("title must contain a non-whitespace character")
    attendees = obj.get("attendees")
    if isinstance(attendees, bool) or not isinstance(attendees, int):
        raise validation_error("attendees must be an integer")
    if attendees < 1:
        raise validation_error("attendees must be at least 1")
    return room_id, start, end, title, attendees


def validate_idempotency_key(value):
    if value.strip() == "":
        raise validation_error("Idempotency-Key must not be empty")
    if len(value) > 255:
        raise validation_error("Idempotency-Key must be at most 255 characters")
    for c in value:
        if c.isspace() or unicodedata.category(c) == "Cc":
            raise validation_error("Idempotency-Key must not contain whitespace or control characters")
    return value


def parse_limit(value):
    if LIMIT_RE.fullmatch(value) is None:
        raise validation_error("limit must be an integer")
    digits = value.lstrip("0") or "0"
    if len(digits) > 3:
        raise validation_error("limit must be between 1 and 100")
    n = int(digits)
    if not 1 <= n <= 100:
        raise validation_error("limit must be between 1 and 100")
    return n


def encode_cursor(start, num):
    raw = f"{start}:{num}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value):
    """Return (start, num) for a cursor previously produced by encode_cursor, else 422."""
    bad = validation_error("cursor is not a valid cursor value")
    if not value or CURSOR_CHARS_RE.fullmatch(value) is None or len(value) % 4 == 1:
        raise bad
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad
    if len(text) > 80:
        raise bad
    m = CURSOR_PAYLOAD_RE.fullmatch(text)
    if m is None:
        raise bad
    start, num = int(m.group(1)), int(m.group(2))
    if encode_cursor(start, num) != value:
        raise bad
    return start, num


def compute_free_slots(day_start, day_end, intervals):
    """Maximal free sub-intervals of [day_start, day_end) not covered by intervals."""
    clipped = []
    for s, e in intervals:
        s2, e2 = max(s, day_start), min(e, day_end)
        if s2 < e2:
            clipped.append((s2, e2))
    clipped.sort()
    free = []
    cursor = day_start
    for s, e in clipped:
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < day_end:
        free.append((cursor, day_end))
    return free


# ---------------------------------------------------------------- store

def booking_json(b):
    return {
        "id": f"b{b['num']}",
        "room_id": b["room_id"],
        "start": format_ts(b["start"]),
        "end": format_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


class Store:
    """All rooms, bookings and idempotency records. Every operation holds one lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.room_seq = 0
        self.booking_seq = 0
        self.rooms = {}      # "r<n>" -> {"id", "name", "capacity"}; insertion order = creation order
        self.bookings = {}   # numeric id -> booking dict; deleted bookings are removed
        self.idem = {}       # key -> (canonical body tuple, 201 response body); never removed

    def create_room(self, name, capacity):
        with self.lock:
            for room in self.rooms.values():
                if room["name"] == name:
                    raise ApiError(409, "room_exists", "a room with this name already exists")
            room_id = f"r{self.room_seq + 1}"
            room = {"id": room_id, "name": name, "capacity": capacity}
            self.rooms[room_id] = room
            self.room_seq += 1
            return dict(room)

    def list_rooms(self):
        with self.lock:
            return [dict(r) for r in self.rooms.values()]

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id)
            return dict(room) if room is not None else None

    def create_booking(self, room_id, start, end, title, attendees, key):
        canon = (room_id, start, end, title, attendees)
        with self.lock:
            if key is not None and key in self.idem:
                stored_canon, stored_response = self.idem[key]
                if stored_canon == canon:
                    return dict(stored_response)
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "more attendees than the room holds")
            for b in self.bookings.values():
                if b["room_id"] == room_id and b["start"] < end and start < b["end"]:
                    raise ApiError(409, "conflict", "the room is already booked in this time range")
            num = self.booking_seq + 1
            booking = {"num": num, "room_id": room_id, "start": start, "end": end,
                       "title": title, "attendees": attendees}
            self.bookings[num] = booking
            self.booking_seq = num
            response = booking_json(booking)
            if key is not None:
                self.idem[key] = (canon, dict(response))
            return response

    def get_booking(self, num):
        with self.lock:
            b = self.bookings.get(num)
            return booking_json(b) if b is not None else None

    def delete_booking(self, num):
        with self.lock:
            return self.bookings.pop(num, None) is not None

    def list_bookings(self, room_id, from_e, to_e, limit, after):
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if from_e is not None and not b["end"] > from_e:
                    continue
                if to_e is not None and not b["start"] < to_e:
                    continue
                key = (b["start"], b["num"])
                if after is not None and key <= after:
                    continue
                items.append(b)
            items.sort(key=lambda b: (b["start"], b["num"]))
            page = items[:limit]
            next_cursor = None
            if len(items) > limit:
                last = page[-1]
                next_cursor = encode_cursor(last["start"], last["num"])
            return [booking_json(b) for b in page], next_cursor

    def free_slots(self, room_id, day_start, day_end):
        with self.lock:
            intervals = [(b["start"], b["end"]) for b in self.bookings.values()
                         if b["room_id"] == room_id]
            return compute_free_slots(day_start, day_end, intervals)


# ---------------------------------------------------------------- routing

def match_route(path):
    """Return (name, allowed methods, identifier) for a path, or None if no route matches."""
    segs = path.split("/")
    if len(segs) < 2 or segs[0] != "":
        return None
    rest = segs[1:]
    if rest == ["rooms"]:
        return "rooms", ("GET", "HEAD", "POST"), None
    if rest == ["bookings"]:
        return "bookings", ("GET", "HEAD", "POST"), None
    if len(rest) == 2 and rest[0] == "rooms":
        return "room", ("GET", "HEAD"), rest[1]
    if len(rest) == 3 and rest[0] == "rooms" and rest[2] == "availability":
        return "availability", ("GET", "HEAD"), rest[1]
    if len(rest) == 2 and rest[0] == "bookings":
        return "booking", ("GET", "HEAD", "DELETE"), rest[1]
    return None


def parse_query(query, defined):
    """Values of the defined parameters; a defined parameter given twice is a 422."""
    params = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name not in defined:
            continue
        if name in params:
            raise validation_error(f"query parameter {name} was given more than once")
        params[name] = value
    return params


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def send_error(self, code, message=None, explain=None):
        # Unknown request methods reach here as 501; treat them as methods the route does not accept.
        if code == 501 and self.command:
            self._handle(self.command)
            return
        status_code = {404: "not_found", 405: "method_not_allowed"}.get(code, "bad_json")
        payload = json.dumps({"error": {"code": status_code, "message": message or "bad request"}}).encode("ascii")
        self.send_response(code, message)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _handle(self, method):
        try:
            self._raw, self._too_large = self._read_body()
            status, headers, obj = self._route(method)
        except ApiError as e:
            status, headers = e.status, e.headers
            obj = {"error": {"code": e.code, "message": e.message}}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            status, headers = 500, {}
            obj = {"error": {"code": "internal_error", "message": "internal server error"}}
        self._send(method, status, headers, obj)

    def _read_body(self):
        """Read the request body. Returns (bytes, too_large). Oversized bodies are drained and discarded."""
        cl = self.headers.get("Content-Length")
        if cl is None or re.fullmatch(r"[0-9]+", cl.strip()) is None:
            return b"", False
        n = int(cl.strip())
        if n == 0:
            return b"", False
        if n > MAX_BODY:
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return b"", True
        return self.rfile.read(n), False

    def _route(self, method):
        parts = urlsplit(self.path)
        route = match_route(parts.path)
        if route is None:
            raise ApiError(404, "not_found", "no route matches this path")
        name, allowed, ident = route
        allow = {"Allow": ", ".join(allowed + ("OPTIONS",))}
        if method == "OPTIONS":
            return 204, allow, None
        if method not in allowed:
            raise ApiError(405, "method_not_allowed", f"{method} is not allowed on this route", allow)
        verb = "GET" if method == "HEAD" else method
        store = self.server.store

        if name == "room" or name == "availability":
            if ROOM_ID_RE.fullmatch(ident) is None or store.get_room(ident) is None:
                raise ApiError(404, "not_found", "room not found")
        if name == "booking" and verb == "GET":
            if self._booking_num(ident) is None or store.get_booking(self._booking_num(ident)) is None:
                raise ApiError(404, "not_found", "booking not found")

        if name == "rooms" and verb == "GET":
            return 200, {}, {"rooms": store.list_rooms()}
        if name == "rooms":
            obj = self._body_object()
            name_v, cap_v = obj.get("name"), obj.get("capacity")
            validate_name(name_v)
            validate_capacity(cap_v)
            return 201, {}, store.create_room(name_v, cap_v)
        if name == "room":
            return 200, {}, store.get_room(ident)
        if name == "availability":
            params = parse_query(parts.query, AVAILABILITY_PARAMS)
            if "date" not in params:
                raise validation_error("date is required")
            d = parse_date(params["date"])
            day_start = epoch_of(d.year, d.month, d.day, DAY_START_HOUR)
            day_end = epoch_of(d.year, d.month, d.day, DAY_END_HOUR)
            slots = store.free_slots(ident, day_start, day_end)
            free = [{"start": format_ts(s), "end": format_ts(e)} for s, e in slots]
            return 200, {}, {"date": params["date"], "free": free}
        if name == "bookings" and verb == "GET":
            return self._list_bookings(parts.query)
        if name == "bookings":
            return self._create_booking()
        if name == "booking" and verb == "GET":
            return 200, {}, store.get_booking(self._booking_num(ident))
        if name == "booking":
            num = self._booking_num(ident)
            if num is None or not store.delete_booking(num):
                raise ApiError(404, "not_found", "booking not found")
            return 204, {}, None
        raise ApiError(404, "not_found", "no route matches this path")

    @staticmethod
    def _booking_num(ident):
        if BOOKING_ID_RE.fullmatch(ident) is None or len(ident) > 18:
            return None
        return int(ident[1:])

    def _body_object(self):
        if self._too_large:
            raise ApiError(400, "bad_json", "request body is larger than 64 KiB")
        raw = self._raw
        if not raw:
            raise ApiError(400, "bad_json", "request body is empty")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")
        if not isinstance(obj, dict):
            raise validation_error("request body must be a JSON object")
        return obj

    def _idempotency_key(self):
        value = self.headers.get("Idempotency-Key")
        if value is None:
            return None
        return validate_idempotency_key(value)

    def _create_booking(self):
        obj = self._body_object()
        key = self._idempotency_key()
        room_id, start, end, title, attendees = validate_booking(obj)
        booking = self.server.store.create_booking(room_id, start, end, title, attendees, key)
        return 201, {}, booking

    def _list_bookings(self, query):
        params = parse_query(query, BOOKING_LIST_PARAMS)
        room_id = params.get("room_id")
        if room_id == "":
            raise validation_error("room_id must not be empty")
        from_e = parse_timestamp(params["from"], "from") if "from" in params else None
        to_e = parse_timestamp(params["to"], "to") if "to" in params else None
        if from_e is not None and to_e is not None and from_e >= to_e:
            raise validation_error("from must be earlier than to")
        limit = parse_limit(params["limit"]) if "limit" in params else DEFAULT_LIMIT
        after = decode_cursor(params["cursor"]) if "cursor" in params else None
        items, next_cursor = self.server.store.list_bookings(room_id, from_e, to_e, limit, after)
        return 200, {}, {"bookings": items, "next_cursor": next_cursor}

    def _send(self, method, status, headers, obj):
        payload = b"" if obj is None else json.dumps(obj).encode("ascii")
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        if status != 204:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method != "HEAD" and status != 204 and payload:
            self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # Skip the reverse-DNS lookup of HTTPServer.server_bind; the address is fixed to 127.0.0.1.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def parse_port(value):
    if re.fullmatch(r"[0-9]+", value) is None or len(value.lstrip("0") or "0") > 5:
        raise argparse.ArgumentTypeError(f"invalid port {value!r}: expected an integer from 1 to 65535")
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {value!r}: expected an integer from 1 to 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(prog="server.py", description="bookings API, listens on 127.0.0.1")
    parser.add_argument("--port", type=parse_port, required=True,
                        help="TCP port to listen on, 1 to 65535 (required)")
    args = parser.parse_args(argv)
    try:
        server = Server((HOST, args.port), Handler)
    except OSError as e:
        print(f"server.py: cannot listen on {HOST}:{args.port}: {e}", file=sys.stderr)
        return 1
    server.store = Store()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
