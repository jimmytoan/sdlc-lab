#!/usr/bin/env python3
"""bookings: in-memory meeting room booking API on 127.0.0.1 (standard library only).

Start with:  python server.py --port N
"""

import argparse
import base64
import bisect
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

MAX_BODY_BYTES = 64 * 1024
MAX_NAME_LEN = 50
MAX_TITLE_LEN = 200
MAX_KEY_LEN = 255
MAX_DURATION = 8 * 3600
WORK_START = 8 * 3600
WORK_END = 18 * 3600
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
ROOM_ID_RE = re.compile(r"r[1-9][0-9]*")
BOOKING_ID_RE = re.compile(r"b[1-9][0-9]*")
TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CURSOR_RE = re.compile(r"[A-Za-z0-9_-]+")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TOO_LARGE = object()  # sentinel: POST body exceeded MAX_BODY_BYTES
CURSOR_KEY = secrets.token_bytes(32)  # per-process secret for opaque cursors


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def validation_error(message):
    return ApiError(422, "validation_error", message)


def bad_json(message):
    return ApiError(400, "bad_json", message)


def not_found(message="no such resource"):
    return ApiError(404, "not_found", message)


# ---------------------------------------------------------------- encoding

def json_bytes(obj):
    return json.dumps(obj).encode("utf-8")  # ASCII output, always valid UTF-8


def to_epoch(dt):
    return int((dt - EPOCH).total_seconds())


def format_ts(epoch):
    dt = EPOCH + timedelta(seconds=epoch)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def room_json(room):
    return {"id": room.id, "name": room.name, "capacity": room.capacity}


def booking_json(b):
    return {"id": b.id, "room_id": b.room_id, "start": format_ts(b.start), "end": format_ts(b.end),
            "title": b.title, "attendees": b.attendees}


# -------------------------------------------------------------- validators

def parse_timestamp(value, quarter_hour=True):
    """Exact form YYYY-MM-DDTHH:MM:SSZ. Returns epoch seconds."""
    if not isinstance(value, str) or TS_RE.fullmatch(value) is None:
        raise validation_error("timestamp must have the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        dt = datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]),
                      int(value[11:13]), int(value[14:16]), int(value[17:19]), tzinfo=timezone.utc)
    except ValueError:
        raise validation_error("timestamp is not a real date and time")
    if quarter_hour and (dt.minute not in (0, 15, 30, 45) or dt.second != 0):
        raise validation_error("timestamps must fall on quarter hours with zero seconds")
    return to_epoch(dt)


def parse_date(value):
    """YYYY-MM-DD, a real calendar date. Returns the epoch of 00:00Z on that day."""
    if not isinstance(value, str) or DATE_RE.fullmatch(value) is None:
        raise validation_error("date must have the form YYYY-MM-DD")
    try:
        dt = datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]), tzinfo=timezone.utc)
    except ValueError:
        raise validation_error("date is not a real calendar date")
    return to_epoch(dt)


def _is_int(v):
    return type(v) is int  # excludes bool and float


def validate_room(obj):
    if "name" not in obj:
        raise validation_error("name is required")
    name = obj["name"]
    if not isinstance(name, str):
        raise validation_error("name must be a string")
    if name.strip() == "":
        raise validation_error("name must not be empty or whitespace only")
    if len(name) > MAX_NAME_LEN:
        raise validation_error("name must be at most 50 characters")
    if "capacity" not in obj:
        raise validation_error("capacity is required")
    capacity = obj["capacity"]
    if not _is_int(capacity):
        raise validation_error("capacity must be an integer")
    if capacity < 1 or capacity > 500:
        raise validation_error("capacity must be between 1 and 500")
    return name, capacity


REQUIRED_BOOKING_FIELDS = ("room_id", "start", "end", "title", "attendees")


def validate_booking(obj):
    for f in REQUIRED_BOOKING_FIELDS:
        if f not in obj:
            raise validation_error(f"{f} is required")
    room_id, start_s, end_s, title, attendees = (obj[f] for f in REQUIRED_BOOKING_FIELDS)
    if not isinstance(room_id, str):
        raise validation_error("room_id must be a string")
    if not isinstance(start_s, str) or not isinstance(end_s, str):
        raise validation_error("start and end must be strings")
    if not isinstance(title, str):
        raise validation_error("title must be a string")
    if not _is_int(attendees):
        raise validation_error("attendees must be an integer")
    start = parse_timestamp(start_s)
    end = parse_timestamp(end_s)
    if end <= start:
        raise validation_error("end must be later than start")
    if end - start > MAX_DURATION:
        raise validation_error("booking must not be longer than 8 hours")
    if title.strip() == "":
        raise validation_error("title must not be empty or whitespace only")
    if len(title) > MAX_TITLE_LEN:
        raise validation_error("title must be at most 200 characters")
    if attendees < 1:
        raise validation_error("attendees must be at least 1")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def validate_idempotency_key(value):
    if value == "" or len(value) > MAX_KEY_LEN or not all(0x21 <= ord(c) <= 0x7E for c in value):
        raise validation_error("Idempotency-Key must be 1 to 255 printable ASCII characters")


def fingerprint(f):
    return json.dumps([f["room_id"], f["start"], f["end"], f["title"], f["attendees"]],
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _cursor_mac(payload):
    return hmac.new(CURSOR_KEY, payload, hashlib.sha256).digest()[:16]


def encode_cursor(start, n):
    payload = f"{start}.{n}".encode("ascii")
    raw = payload + b"." + _cursor_mac(payload)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token):
    """Returns (start, n) for a token issued by this process, else validation_error."""
    bad = validation_error("cursor is not a valid token issued by this service")
    if CURSOR_RE.fullmatch(token) is None:
        raise bad
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        raise bad
    if len(raw) < 18 or raw[-17:-16] != b".":
        raise bad
    payload, mac = raw[:-17], raw[-16:]
    if not hmac.compare_digest(mac, _cursor_mac(payload)):
        raise bad
    start_s, sep, n_s = payload.decode("ascii").partition(".")
    try:
        return int(start_s), int(n_s)
    except ValueError:
        raise bad


# ------------------------------------------------------------------- store

@dataclass(eq=False)
class Room:
    id: str
    name: str
    capacity: int


@dataclass(eq=False)
class Booking:
    id: str
    n: int
    room_id: str
    start: int
    end: int
    title: str
    attendees: int


@dataclass(eq=False)
class IdemRecord:
    fingerprint: str
    status: int
    body: bytes
    booking_id: str


def _start_of(b):
    return b.start


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}
        self.room_names = set()
        self.bookings = {}
        self.index = {}  # room_id -> bookings sorted by start (non-overlapping)
        self.idem = {}
        self.next_room = 1
        self.next_booking = 1

    def create_room(self, name, capacity):
        with self.lock:
            if name in self.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            room = Room(f"r{self.next_room}", name, capacity)
            self.next_room += 1
            self.rooms[room.id] = room
            self.room_names.add(name)
            return room

    def get_room(self, room_id):
        with self.lock:
            return self.rooms.get(room_id)

    def list_rooms(self):
        with self.lock:
            return list(self.rooms.values())

    def create_booking(self, fields, key, fp):
        """Returns (status, body bytes). Idempotency replay is checked first."""
        with self.lock:
            if key is not None:
                rec = self.idem.get(key)
                if rec is not None:
                    if rec.fingerprint == fp:
                        return rec.status, rec.body
                    raise ApiError(422, "idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
            room = self.rooms.get(fields["room_id"])
            if room is None:
                raise validation_error("room_id does not identify an existing room")
            if fields["attendees"] > room.capacity:
                raise ApiError(422, "over_capacity", "attendees exceed the room capacity")
            start, end = fields["start"], fields["end"]
            lst = self.index.setdefault(room.id, [])
            i = bisect.bisect_left(lst, end, key=_start_of)
            if i > 0 and lst[i - 1].end > start:
                raise ApiError(409, "conflict", "the room is already booked for part of this time")
            n = self.next_booking
            self.next_booking += 1
            b = Booking(f"b{n}", n, room.id, start, end, fields["title"], fields["attendees"])
            lst.insert(i, b)
            self.bookings[b.id] = b
            body = json_bytes(booking_json(b))
            if key is not None:
                self.idem[key] = IdemRecord(fp, 201, body, b.id)
            return 201, body

    def get_booking(self, booking_id):
        with self.lock:
            return self.bookings.get(booking_id)

    def delete_booking(self, booking_id):
        with self.lock:
            b = self.bookings.pop(booking_id, None)
            if b is None:
                return False
            lst = self.index[b.room_id]
            lst[:] = [x for x in lst if x is not b]
            return True

    def list_bookings(self, room_id, frm, to, limit, after):
        """Returns (page, next_token). `after` is a (start, n) sort position."""
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b.room_id != room_id:
                    continue
                if frm is not None and b.end <= frm:
                    continue
                if to is not None and b.start >= to:
                    continue
                if after is not None and (b.start, b.n) <= after:
                    continue
                items.append(b)
            items.sort(key=lambda b: (b.start, b.n))
            page = items[:limit]
            token = None
            if len(items) > limit:
                last = page[-1]
                token = encode_cursor(last.start, last.n)
            return page, token

    def free_intervals(self, room_id, day_start):
        ws = day_start + WORK_START
        we = day_start + WORK_END
        clipped = []
        for b in self.index.get(room_id, []):
            s, e = max(b.start, ws), min(b.end, we)
            if s < e:
                clipped.append((s, e))
        clipped.sort()
        merged = []
        for s, e in clipped:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        free = []
        cur = ws
        for s, e in merged:
            if s > cur:
                free.append((cur, s))
            cur = max(cur, e)
        if cur < we:
            free.append((cur, we))
        return free


# --------------------------------------------------------------- app/router

def parse_json_object(body):
    if body is TOO_LARGE:
        raise bad_json("request body is larger than 64 KiB")
    if not body:
        raise bad_json("request body is empty")
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise bad_json("request body is not valid UTF-8 JSON")
    if not isinstance(obj, dict):
        raise bad_json("request body must be a JSON object")
    return obj


def query_values(query):
    """Query parameters with blank values kept (repeats are checked by single())."""
    return parse_qs(query, keep_blank_values=True)


def single(params, name):
    vals = params.get(name, [])
    if len(vals) > 1:
        raise validation_error(f"query parameter {name} may be given only once")
    return vals[0] if vals else None


def match_route(path):
    """Returns (name, allowed_methods, arg) or None."""
    if path == "/rooms":
        return "rooms", ("GET", "POST"), None
    if path == "/bookings":
        return "bookings", ("GET", "POST"), None
    segs = path.split("/")
    if len(segs) == 3 and segs[1] == "rooms" and ROOM_ID_RE.fullmatch(segs[2]):
        return "room", ("GET",), segs[2]
    if len(segs) == 4 and segs[1] == "rooms" and segs[3] == "availability" \
            and ROOM_ID_RE.fullmatch(segs[2]):
        return "availability", ("GET",), segs[2]
    if len(segs) == 3 and segs[1] == "bookings" and BOOKING_ID_RE.fullmatch(segs[2]):
        return "booking", ("GET", "DELETE"), segs[2]
    return None


class App:
    def __init__(self):
        self.store = Store()

    def handle(self, method, target, headers, body):
        """Returns (status, body bytes or None, extra headers)."""
        try:
            return self._route(method, target, headers, body)
        except ApiError as e:
            payload = json_bytes({"error": {"code": e.code, "message": e.message}})
            return e.status, payload, e.headers

    def _route(self, method, target, headers, body):
        path, _, query = target.partition("?")
        route = match_route(path)
        if route is None:
            raise not_found("no route matches this path")
        name, allowed, arg = route
        if method not in allowed:
            raise ApiError(405, "method_not_allowed",
                           f"method {method} is not allowed for this path",
                           headers={"Allow": ", ".join(allowed)})
        if name == "rooms":
            if method == "GET":
                return self._ok(200, {"rooms": [room_json(r) for r in self.store.list_rooms()]})
            return self._create_room(body)
        if name == "room":
            room = self.store.get_room(arg)
            if room is None:
                raise not_found("room not found")
            return self._ok(200, room_json(room))
        if name == "availability":
            return self._availability(arg, query)
        if name == "bookings":
            if method == "GET":
                return self._list_bookings(query)
            return self._create_booking(headers, body)
        if name == "booking":
            if method == "DELETE":
                if not self.store.delete_booking(arg):
                    raise not_found("booking not found")
                return 204, None, {}
            b = self.store.get_booking(arg)
            if b is None:
                raise not_found("booking not found")
            return self._ok(200, booking_json(b))
        raise not_found()

    @staticmethod
    def _ok(status, obj):
        return status, json_bytes(obj), {}

    def _create_room(self, body):
        obj = parse_json_object(body)
        name, capacity = validate_room(obj)
        room = self.store.create_room(name, capacity)
        return self._ok(201, room_json(room))

    def _create_booking(self, headers, body):
        key = headers.get("Idempotency-Key")
        if key is not None:
            validate_idempotency_key(key)
        obj = parse_json_object(body)
        fields = validate_booking(obj)
        status, payload = self.store.create_booking(fields, key, fingerprint(fields))
        return status, payload, {}

    def _list_bookings(self, query):
        params = query_values(query)
        for name in ("room_id", "from", "to", "limit", "cursor"):
            single(params, name)
        room_id = single(params, "room_id")
        if room_id == "":
            raise validation_error("room_id must not be empty")
        frm_s = single(params, "from")
        to_s = single(params, "to")
        frm = parse_timestamp(frm_s, quarter_hour=False) if frm_s is not None else None
        to = parse_timestamp(to_s, quarter_hour=False) if to_s is not None else None
        if frm is not None and to is not None and frm >= to:
            raise validation_error("from must be earlier than to")
        limit = DEFAULT_LIMIT
        limit_s = single(params, "limit")
        if limit_s is not None:
            try:
                limit = int(limit_s) if re.fullmatch(r"[0-9]+", limit_s) else None
            except ValueError:  # too many digits to convert
                limit = None
            if limit is None or not 1 <= limit <= MAX_LIMIT:
                raise validation_error("limit must be an integer from 1 to 100")
        cursor_s = single(params, "cursor")
        after = decode_cursor(cursor_s) if cursor_s is not None else None
        page, token = self.store.list_bookings(room_id, frm, to, limit, after)
        return self._ok(200, {"bookings": [booking_json(b) for b in page], "next_cursor": token})

    def _availability(self, room_id, query):
        params = query_values(query)
        dates = params.get("date", [])
        if len(dates) != 1:
            raise validation_error("date must be given exactly once as YYYY-MM-DD")
        day_start = parse_date(dates[0])
        with self.store.lock:
            if self.store.get_room(room_id) is None:
                raise not_found("room not found")
            free = self.store.free_intervals(room_id, day_start)
        return self._ok(200, {"date": dates[0],
                              "free": [{"start": format_ts(s), "end": format_ts(e)} for s, e in free]})


APP = App()


# ----------------------------------------------------------------- transport

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""
    timeout = 30

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._serve("GET")

    def do_HEAD(self):
        self._serve("HEAD")

    def do_POST(self):
        self._serve("POST")

    def do_PUT(self):
        self._serve("PUT")

    def do_PATCH(self):
        self._serve("PATCH")

    def do_DELETE(self):
        self._serve("DELETE")

    def do_OPTIONS(self):
        self._serve("OPTIONS")

    def _serve(self, method):
        self.close_connection = True
        try:
            body = self._read_body() if method == "POST" else None
            status, payload, extra = APP.handle(method, self.path, self.headers, body)
        except Exception:
            traceback.print_exc()
            status, extra = 500, {}
            payload = json_bytes({"error": {"code": "internal_error",
                                            "message": "unexpected server error"}})
        self._send(method, status, payload, extra)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            remaining = length
            while remaining > 0:  # drain so the client can finish sending
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            return TOO_LARGE
        return self.rfile.read(length)

    def _send(self, method, status, payload, extra):
        try:
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            if status != 204 and payload is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if status != 204 and payload is not None and method != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError) as exc:
            print(f"client disconnected: {exc}", file=sys.stderr)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def port_arg(value):
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError(f"invalid port: {value!r}")
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port out of range 0-65535: {port}")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(prog="server.py", description="bookings meeting room API")
    parser.add_argument("--port", type=port_arg, required=True, help="TCP port to listen on (0-65535)")
    args = parser.parse_args(argv)
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
