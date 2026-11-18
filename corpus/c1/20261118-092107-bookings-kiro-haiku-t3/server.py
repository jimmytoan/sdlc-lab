#!/usr/bin/env python3
"""bookings: in-memory meeting room booking JSON API.

Start with:  python server.py --port <0-65535>
Listens on 127.0.0.1 only. Standard library only (Python 3.13).
"""
import base64
import bisect
import calendar
import datetime as dt
import hashlib
import json
import re
import socketserver
import sys
import threading
import traceback
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
MAX_BODY = 64 * 1024              # R5.5: bodies above 64 KiB are rejected
DRAIN_LIMIT = 1024 * 1024         # extra bytes read and discarded after an oversized body
CONN_TIMEOUT = 10.0               # R1.7: a stalled client only blocks its own thread
IDEM_MAX_LEN = 255                # R7.7
ROOM_NAME_MAX = 50                # R3.6
CAP_MIN, CAP_MAX = 1, 500         # R3.10
TITLE_MAX = 200                   # R5.15
BOOKING_MAX_SECONDS = 8 * 3600    # R5.13
DEFAULT_LIMIT = 20                # R9.10
LIMIT_MIN, LIMIT_MAX = 1, 100     # R9.11
DAY_OPEN_SECONDS = 8 * 3600       # working day 08:00Z
DAY_CLOSE_SECONDS = 18 * 3600     # working day 18:00Z

_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_INT_RE = re.compile(r"-?[0-9]+")
_PORT_RE = re.compile(r"[0-9]{1,5}")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_json(message="Request body must be a JSON object."):
    return ApiError(400, "bad_json", message)


def validation_error(message):
    return ApiError(422, "validation_error", message)


def not_found(message="Resource not found."):
    return ApiError(404, "not_found", message)


def error_payload(code, message):
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------- codec

def _reject_constant(name):
    raise ValueError("non-finite number not allowed: " + name)


def decode_json_object(raw):
    """Strict UTF-8 JSON decode. Empty, invalid or non-object bodies -> bad_json."""
    if not raw or not raw.strip():
        raise bad_json("Request body is empty.")
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise bad_json("Request body is not valid JSON.")
    if not isinstance(obj, dict):
        raise bad_json("Request body must be a JSON object.")
    return obj


def parse_timestamp(value):
    """Exact form YYYY-MM-DDTHH:MM:SSZ and a real calendar date/time. Returns epoch seconds or None."""
    if not isinstance(value, str):
        return None
    m = _TS_RE.fullmatch(value)
    if not m:
        return None
    try:
        d = dt.datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    return calendar.timegm(d.timetuple())


def format_timestamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    """YYYY-MM-DD and a real calendar date. Returns datetime.date or None."""
    if not isinstance(value, str):
        return None
    m = _DATE_RE.fullmatch(value)
    if not m:
        return None
    try:
        return dt.date(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_fingerprint(method, path, raw):
    """R7.3: method, path and parsed JSON body. Unparseable bodies use the raw bytes."""
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return "raw:" + hashlib.sha256(raw).hexdigest()
    text = method + " " + path + "\n" + _canonical(parsed)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def filter_digest(room_id, frm, to):
    return hashlib.sha256(_canonical([room_id, frm, to]).encode("utf-8")).hexdigest()


def encode_cursor(digest, start, seq):
    payload = json.dumps({"f": digest, "s": start, "i": seq}, sort_keys=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_cursor(token, digest):
    """Returns (start, seq). Any problem, including a digest for other filters, -> validation_error."""
    bad = validation_error("cursor is not valid for these filters.")
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.b64decode((token + pad).encode("ascii"), altchars=b"-_", validate=True)
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, TypeError):
        raise bad
    if not isinstance(obj, dict) or set(obj) != {"f", "s", "i"}:
        raise bad
    s, i = obj["s"], obj["i"]
    if obj["f"] != digest or type(s) is not int or type(i) is not int:
        raise bad
    return s, i


# ---------------------------------------------------------------- models and store

@dataclass(frozen=True)
class Room:
    id: str
    name: str
    capacity: int
    seq: int


@dataclass(frozen=True)
class Booking:
    id: str
    seq: int
    room_id: str
    start: int
    end: int
    title: str
    attendees: int


@dataclass(frozen=True)
class IdemRecord:
    key: str
    fingerprint: str
    status: int
    body: dict


def _booking_key(b):
    return (b.start, b.seq)


def _insert_sorted(lst, b):
    bisect.insort(lst, b, key=_booking_key)


def _remove_sorted(lst, b):
    i = bisect.bisect_left(lst, _booking_key(b), key=_booking_key)
    if i >= len(lst) or lst[i] is not b:
        raise RuntimeError("booking index out of sync")
    del lst[i]


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}              # id -> Room
        self.room_names = {}         # exact name -> id
        self.room_order = []         # ids in creation order
        self.bookings = {}           # live bookings by id
        self.bookings_by_room = {}   # room id -> list sorted by (start, seq)
        self.booking_order = []      # all live bookings sorted by (start, seq)
        self.next_room_seq = 1
        self.next_booking_seq = 1
        self.idem = {}               # key -> IdemRecord (server-wide, never deleted)


def room_json(room):
    return {"id": room.id, "name": room.name, "capacity": room.capacity}


def booking_json(b):
    return {
        "id": b.id,
        "room_id": b.room_id,
        "start": format_timestamp(b.start),
        "end": format_timestamp(b.end),
        "title": b.title,
        "attendees": b.attendees,
    }


# ---------------------------------------------------------------- services

@dataclass
class Req:
    query: dict
    body: bytes
    too_large: bool
    idem_key: object  # str or None


def create_room(store, req):
    if req.too_large:
        raise bad_json("Request body is larger than 64 KiB.")
    obj = decode_json_object(req.body)
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        raise validation_error("name must be a non-empty string.")
    if len(name) > ROOM_NAME_MAX:
        raise validation_error("name must be at most 50 characters.")
    capacity = obj.get("capacity")
    if type(capacity) is not int or not (CAP_MIN <= capacity <= CAP_MAX):
        raise validation_error("capacity must be an integer from 1 to 500.")
    with store.lock:
        if name in store.room_names:
            raise ApiError(409, "room_exists", "A room with this name already exists.")
        seq = store.next_room_seq
        store.next_room_seq += 1
        room = Room(id=f"r{seq}", name=name, capacity=capacity, seq=seq)
        store.rooms[room.id] = room
        store.room_names[name] = room.id
        store.room_order.append(room.id)
    return 201, room_json(room)


def list_rooms(store, req):
    with store.lock:
        rooms = [room_json(store.rooms[rid]) for rid in store.room_order]
    return 200, {"rooms": rooms}


def get_room(store, room_id):
    with store.lock:
        room = store.rooms.get(room_id)
        if room is None:
            raise not_found("Room not found.")
        return 200, room_json(room)


def _validate_booking_body(obj):
    room_id = obj.get("room_id")
    if not isinstance(room_id, str):
        raise validation_error("room_id must be a string.")
    start_raw = obj.get("start")
    end_raw = obj.get("end")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        raise validation_error("start and end must be strings.")
    start = parse_timestamp(start_raw)
    end = parse_timestamp(end_raw)
    if start is None or end is None:
        raise validation_error("start and end must be real timestamps in the form YYYY-MM-DDTHH:MM:SSZ.")
    if start % 900 or end % 900:
        raise validation_error("start and end must fall on a quarter hour.")
    if end <= start:
        raise validation_error("end must be later than start.")
    if end - start > BOOKING_MAX_SECONDS:
        raise validation_error("a booking may last at most 8 hours.")
    title = obj.get("title")
    if not isinstance(title, str) or not title.strip():
        raise validation_error("title must be a non-empty string.")
    if len(title) > TITLE_MAX:
        raise validation_error("title must be at most 200 characters.")
    attendees = obj.get("attendees")
    if type(attendees) is not int:
        raise validation_error("attendees must be an integer.")
    if attendees < 1:
        raise validation_error("attendees must be at least 1.")
    return room_id, start, end, title, attendees


def create_booking(store, req):
    if req.too_large:
        raise bad_json("Request body is larger than 64 KiB.")
    key = req.idem_key
    if key is not None and (not key.strip() or len(key) > IDEM_MAX_LEN):
        raise validation_error("Idempotency-Key must be 1 to 255 non-blank characters.")
    fp = request_fingerprint("POST", "/bookings", req.body)
    with store.lock:
        if key is not None and key in store.idem:
            rec = store.idem[key]
            if rec.fingerprint == fp:
                return rec.status, json.loads(json.dumps(rec.body))
            raise ApiError(422, "idempotency_mismatch", "Idempotency-Key was used for a different request.")
        obj = decode_json_object(req.body)
        room_id, start, end, title, attendees = _validate_booking_body(obj)
        room = store.rooms.get(room_id)
        if room is None:
            raise not_found("Room not found.")
        if attendees > room.capacity:
            raise ApiError(422, "over_capacity", "More attendees than the room holds.")
        for other in store.bookings_by_room.get(room_id, []):
            if start < other.end and end > other.start:
                raise ApiError(409, "conflict", "The room is already booked for part of this time.")
        seq = store.next_booking_seq
        store.next_booking_seq += 1
        booking = Booking(id=f"b{seq}", seq=seq, room_id=room_id, start=start, end=end,
                          title=title, attendees=attendees)
        store.bookings[booking.id] = booking
        _insert_sorted(store.bookings_by_room.setdefault(room_id, []), booking)
        _insert_sorted(store.booking_order, booking)
        body = booking_json(booking)
        if key is not None:
            store.idem[key] = IdemRecord(key=key, fingerprint=fp, status=201, body=body)
    return 201, body


def get_booking(store, booking_id):
    with store.lock:
        b = store.bookings.get(booking_id)
        if b is None:
            raise not_found("Booking not found.")
        return 200, booking_json(b)


def delete_booking(store, booking_id):
    with store.lock:
        b = store.bookings.pop(booking_id, None)
        if b is None:
            raise not_found("Booking not found.")
        _remove_sorted(store.bookings_by_room[b.room_id], b)
        _remove_sorted(store.booking_order, b)
    return 204, None


def list_bookings(store, req):
    q = req.query
    frm_raw = q.get("from")
    to_raw = q.get("to")
    frm = to = None
    if frm_raw is not None:
        frm = parse_timestamp(frm_raw)
        if frm is None:
            raise validation_error("from must be a timestamp in the form YYYY-MM-DDTHH:MM:SSZ.")
    if to_raw is not None:
        to = parse_timestamp(to_raw)
        if to is None:
            raise validation_error("to must be a timestamp in the form YYYY-MM-DDTHH:MM:SSZ.")
    if frm is not None and to is not None and frm >= to:
        raise validation_error("from must be earlier than to.")
    limit_raw = q.get("limit")
    if limit_raw is None:
        limit = DEFAULT_LIMIT
    else:
        if not _INT_RE.fullmatch(limit_raw):
            raise validation_error("limit must be an integer from 1 to 100.")
        limit = int(limit_raw)
        if not (LIMIT_MIN <= limit <= LIMIT_MAX):
            raise validation_error("limit must be an integer from 1 to 100.")
    room_id = q.get("room_id")
    digest = filter_digest(room_id, frm_raw, to_raw)
    cursor_pos = None
    if "cursor" in q:
        cursor_pos = decode_cursor(q["cursor"], digest)

    with store.lock:
        source = store.booking_order if room_id is None else store.bookings_by_room.get(room_id, [])
        idx = 0
        if cursor_pos is not None:
            idx = bisect.bisect_right(source, cursor_pos, key=_booking_key)
        out = []
        more = False
        for b in source[idx:]:
            if frm is not None and not (b.end > frm):
                continue
            if to is not None and not (b.start < to):
                continue
            if len(out) == limit:
                more = True
                break
            out.append(b)
    next_cursor = encode_cursor(digest, out[-1].start, out[-1].seq) if more else None
    return 200, {"bookings": [booking_json(b) for b in out], "next_cursor": next_cursor}


def availability(store, room_id, req):
    date_raw = req.query.get("date")
    day = parse_date(date_raw)
    if day is None:
        raise validation_error("date must be a real date in the form YYYY-MM-DD.")
    day_start = calendar.timegm(day.timetuple()) + DAY_OPEN_SECONDS
    day_end = calendar.timegm(day.timetuple()) + DAY_CLOSE_SECONDS
    with store.lock:
        if room_id not in store.rooms:
            raise not_found("Room not found.")
        clipped = []
        for b in store.bookings_by_room.get(room_id, []):
            if b.start < day_end and b.end > day_start:
                clipped.append((max(b.start, day_start), min(b.end, day_end)))
    clipped.sort()
    free = []
    cursor = day_start
    for s, e in clipped:
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < day_end:
        free.append((cursor, day_end))
    return 200, {
        "date": date_raw,
        "free": [{"start": format_timestamp(a), "end": format_timestamp(b)} for a, b in free],
    }


# ---------------------------------------------------------------- router

def route(store, method, path, query_str, body, too_large, idem_key):
    """Returns (status, payload). Raises ApiError for every client error."""
    if not path.startswith("/"):
        raise not_found()
    segs = path[1:].split("/")
    # Trailing slash or empty segment never matches a route (R2.5).
    if not segs or any(s == "" for s in segs):
        raise not_found()
    query = {}
    for k, v in urllib.parse.parse_qsl(query_str, keep_blank_values=True):
        query.setdefault(k, v)
    req = Req(query=query, body=body, too_large=too_large, idem_key=idem_key)

    if segs == ["rooms"]:
        table = {"GET": lambda: list_rooms(store, req), "POST": lambda: create_room(store, req)}
    elif len(segs) == 2 and segs[0] == "rooms":
        rid = segs[1]
        table = {"GET": lambda: get_room(store, rid)}
    elif len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
        rid = segs[1]
        table = {"GET": lambda: availability(store, rid, req)}
    elif segs == ["bookings"]:
        table = {"GET": lambda: list_bookings(store, req), "POST": lambda: create_booking(store, req)}
    elif len(segs) == 2 and segs[0] == "bookings":
        bid = segs[1]
        table = {"GET": lambda: get_booking(store, bid), "DELETE": lambda: delete_booking(store, bid)}
    else:
        raise not_found()

    fn = table.get(method)
    if fn is None:
        raise ApiError(405, "method_not_allowed", "Method not allowed for this resource.")
    return fn()


def dispatch(store, method, raw_path, body, too_large, idem_key):
    path, _, query_str = raw_path.partition("?")
    try:
        status, payload = route(store, method, path, query_str, body, too_large, idem_key)
    except ApiError as e:
        return e.status, error_payload(e.code, e.message)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 500, error_payload("internal_error", "An unexpected error occurred.")
    if status == 204:
        return status, None
    return status, payload


# ---------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings"
    sys_version = ""
    timeout = CONN_TIMEOUT  # socket timeout per connection (R1.7)

    def log_message(self, format, *args):
        pass  # nothing on stdout/stderr per request

    def log_error(self, format, *args):
        pass

    def _send(self, status, payload, head=False):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status != 204:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body and not head:
                self.wfile.write(body)
        except OSError:
            self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        # Protocol-level errors (bad request line, unknown method, ...) also get the JSON envelope.
        status = int(code)
        if status == 404:
            out_status, err_code = 404, "not_found"
        elif status in (405, 501):
            out_status, err_code = 405, "method_not_allowed"
        else:
            # Malformed request line, headers or version: a client error, not an internal failure.
            out_status, err_code = 400, "bad_json"
        self.close_connection = True
        self._send(out_status, error_payload(err_code, message or "Bad request."))

    def _read_body(self):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not raw_len.strip():
            return b"", False, 0
        raw_len = raw_len.strip()
        if not raw_len.isdigit():
            return b"", True, 0
        n = int(raw_len)
        if n > MAX_BODY:
            return self.rfile.read(MAX_BODY + 1), True, n
        return self.rfile.read(n), False, n

    def _drain(self, declared, consumed):
        remaining = min(max(declared - consumed, 0), DRAIN_LIMIT)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _handle(self, method):
        try:
            body, too_large, declared = self._read_body()
        except OSError:  # timeout or client gone while sending
            self.close_connection = True
            return
        idem_key = self.headers.get("Idempotency-Key")
        status, payload = dispatch(self.server.store, method, self.path, body, too_large, idem_key)
        self._send(status, payload, head=(method == "HEAD"))
        if too_large:
            self.close_connection = True
            try:
                self._drain(declared, len(body))
            except OSError:
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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def server_bind(self):
        # Skip HTTPServer's getfqdn() lookup; the address is fixed to loopback.
        socketserver.TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]


def make_server(port):
    srv = Server((HOST, port), Handler)
    srv.store = Store()
    return srv


USAGE = "usage: python server.py --port <0-65535>"


def parse_port(argv):
    """Returns the port, or None if the arguments are not valid."""
    port_val = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--port":
            if i + 1 >= len(argv):
                return None
            port_val = argv[i + 1]
            i += 2
        elif arg.startswith("--port="):
            port_val = arg[len("--port="):]
            i += 1
        else:
            return None
    if port_val is None or not _PORT_RE.fullmatch(port_val):
        return None
    port = int(port_val)
    if not (0 <= port <= 65535):
        return None
    return port


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    port = parse_port(argv)
    if port is None:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        srv = make_server(port)
    except OSError as e:
        print(f"bookings: cannot listen on {HOST}:{port}: {e}", file=sys.stderr)
        return 1
    print(f"bookings: listening on {HOST}:{srv.server_address[1]}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
