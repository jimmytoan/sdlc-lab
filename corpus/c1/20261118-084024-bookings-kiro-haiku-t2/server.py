"""bookings: in-memory meeting room booking HTTP API.

Run as `python server.py --port <N>`. Listens on 127.0.0.1 only and uses only
the Python standard library. See .kiro/specs/bookings/ for the specification.
"""
import base64
import bisect
import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import parse_qsl

MAX_BODY = 65536                 # 12.9: larger bodies are refused with 422
DRAIN_MAX = 8 * 1024 * 1024      # bodies up to this size are read and discarded, then refused
UTC = dt.timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_DURATION = dt.timedelta(hours=8)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_IDEM_KEY = 255
MAX_NAME = 50
MAX_TITLE = 200

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")

USAGE = "usage: python server.py --port <N>   (N is an integer from 1 to 65535)"


class ApiError(Exception):
    def __init__(self, status, code, message, headers=()):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = tuple(headers)


def invalid(message):
    return ApiError(422, "validation_error", message)


def j(obj):
    return json.dumps(obj).encode("utf-8")


def fmt_ts(t):
    return f"{t.year:04d}-{t.month:02d}-{t.day:02d}T{t.hour:02d}:{t.minute:02d}:{t.second:02d}Z"


def _build_dt(s):
    try:
        return dt.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]),
                           int(s[11:13]), int(s[14:16]), int(s[17:19]), tzinfo=UTC)
    except ValueError:
        raise invalid("timestamp is not a real date and time")


def parse_booking_ts(v):
    """Strict booking timestamp: exact form, real date, quarter hour, zero seconds."""
    if not isinstance(v, str) or not TS_RE.fullmatch(v):
        raise invalid("timestamp must be in the form YYYY-MM-DDTHH:MM:SSZ")
    t = _build_dt(v)
    if t.minute not in (0, 15, 30, 45) or t.second != 0:
        raise invalid("timestamp must fall on a quarter hour with zero seconds")
    return t


def parse_query_ts(v):
    """from/to query timestamp: exact form and real date only (9.8)."""
    if not isinstance(v, str) or not TS_RE.fullmatch(v):
        raise invalid("timestamp must be in the form YYYY-MM-DDTHH:MM:SSZ")
    return _build_dt(v)


def parse_name(v):
    if not isinstance(v, str):
        raise invalid("name must be a string")
    s = v.strip()
    if not 1 <= len(s) <= MAX_NAME:
        raise invalid("name must be 1 to 50 characters after trimming whitespace")
    return s


def parse_capacity(v):
    if type(v) is not int or not 1 <= v <= 500:
        raise invalid("capacity must be an integer from 1 to 500")
    return v


def parse_attendees(v):
    if type(v) is not int or v < 1:
        raise invalid("attendees must be an integer of at least 1")
    return v


def parse_title(v):
    if not isinstance(v, str):
        raise invalid("title must be a string")
    if not 1 <= len(v.strip()) <= MAX_TITLE:
        raise invalid("title must be 1 to 200 characters after trimming whitespace")
    return v  # stored exactly as sent (3.7)


def parse_str_field(body, name):
    v = body.get(name)
    if not isinstance(v, str):
        raise invalid(f"{name} is required and must be a string")
    return v


def parse_idem_header(headers):
    values = headers.get_all("Idempotency-Key")
    if values is None:
        return None
    if len(values) != 1:
        raise invalid("Idempotency-Key header must be sent exactly once")
    key = values[0].strip()
    if not key or len(key) > MAX_IDEM_KEY:
        raise invalid("Idempotency-Key must be 1 to 255 characters after trimming whitespace")
    return key


def parse_query(qs):
    out = {}
    for k, v in parse_qsl(qs, keep_blank_values=True):
        out.setdefault(k, []).append(v)
    return out


def single(query, name):
    vals = query.get(name)
    if not vals:
        return None
    if len(vals) > 1:
        raise invalid(f"query parameter {name} may be given only once")
    return vals[0]


def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


def parse_json_body(raw):
    if not raw:
        raise ApiError(400, "bad_json", "request body is empty")
    try:
        text = raw.decode("utf-8")
        return json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")


# ---------------------------------------------------------------- store

class Room:
    __slots__ = ("id", "num", "name", "capacity")

    def __init__(self, num, name, capacity):
        self.num = num
        self.id = f"r{num}"
        self.name = name
        self.capacity = capacity

    def to_json(self):
        return {"id": self.id, "name": self.name, "capacity": self.capacity}


class Booking:
    __slots__ = ("num", "id", "room_id", "start", "end", "title", "attendees")

    def __init__(self, num, room_id, start, end, title, attendees):
        self.num = num
        self.id = f"b{num}"
        self.room_id = room_id
        self.start = start
        self.end = end
        self.title = title
        self.attendees = attendees

    def to_json(self):
        return {"id": self.id, "room_id": self.room_id, "start": fmt_ts(self.start),
                "end": fmt_ts(self.end), "title": self.title, "attendees": self.attendees}


def _sort_key(b):
    return (int(b.start.timestamp()), b.num)


def _cursor_encode(key, start_epoch, num, fp):
    payload = json.dumps({"s": start_epoch, "i": num, "f": fp},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")
    return base64.urlsafe_b64encode(payload + b"." + sig).decode("ascii").rstrip("=")


def _cursor_decode(key, token, fp):
    bad = invalid("cursor was not issued by this server")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload, sep, sig = raw.rpartition(b".")
        if not sep:
            raise bad
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest().encode("ascii")
        if not hmac.compare_digest(sig, expected):
            raise bad
        data = json.loads(payload.decode("utf-8"))
        s, i, f = data["s"], data["i"], data["f"]
    except ApiError:
        raise
    except Exception:
        raise bad
    if type(s) is not int or type(i) is not int or type(f) is not str:
        raise bad
    if f != fp:
        raise invalid("cursor was issued for different room_id, from or to values")
    return s, i


class Store:
    """All mutable state. Every method takes the lock and changes nothing until all checks pass."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cursor_key = secrets.token_bytes(32)
        self.rooms = {}          # id -> Room
        self.room_list = []      # rooms in creation (= numeric id) order
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}       # id -> Booking (live bookings only)
        self.by_room = {}        # room id -> bookings sorted by (start, num)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body bytes)

    def create_room(self, name, capacity):
        with self.lock:
            if name in self.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            num = self.room_seq + 1
            room = Room(num, name, capacity)
            self.room_seq = num
            self.rooms[room.id] = room
            self.room_list.append(room)
            self.room_names.add(name)
            return room

    def list_rooms(self):
        with self.lock:
            return list(self.room_list)

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return room

    def create_booking(self, room_id, start, end, title, attendees, idem_key):
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise invalid("room_id does not name an existing room")
            fp = (room_id, start, end, title, attendees)
            if idem_key is not None and idem_key in self.idem:
                rec_fp, status, body = self.idem[idem_key]
                if rec_fp == fp:
                    return status, body
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            if attendees > room.capacity:
                raise ApiError(422, "over_capacity", "attendees exceed the room capacity")
            lst = self.by_room.setdefault(room_id, [])
            for other in lst:
                if other.start < end and start < other.end:
                    raise ApiError(409, "conflict", "the room is already booked for this time")
            # Commit: nothing below can fail.
            num = self.booking_seq + 1
            booking = Booking(num, room_id, start, end, title, attendees)
            body = j(booking.to_json())
            self.booking_seq = num
            bisect.insort(lst, booking, key=_sort_key)
            self.bookings[booking.id] = booking
            if idem_key is not None:
                self.idem[idem_key] = (fp, 201, body)
            return 201, body

    def get_booking(self, booking_id):
        with self.lock:
            b = self.bookings.get(booking_id)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return b

    def delete_booking(self, booking_id):
        with self.lock:
            b = self.bookings.pop(booking_id, None)
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            self.by_room[b.room_id].remove(b)

    def list_bookings(self, room_id, frm, to, limit, cursor, fp):
        after = _cursor_decode(self.cursor_key, cursor, fp) if cursor is not None else None
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b.room_id != room_id:
                    continue
                if frm is not None and not b.end > frm:
                    continue
                if to is not None and not b.start < to:
                    continue
                items.append(b)
            items.sort(key=_sort_key)
            if after is not None:
                items = [b for b in items if _sort_key(b) > after]
            page = items[:limit]
            next_cursor = None
            if len(items) > limit:
                last = _sort_key(page[-1])
                next_cursor = _cursor_encode(self.cursor_key, last[0], last[1], fp)
            return [b.to_json() for b in page], next_cursor

    def availability(self, room_id, day):
        with self.lock:
            if room_id not in self.rooms:
                raise ApiError(404, "not_found", "room not found")
            ds = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
            de = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
            spans = []
            for b in self.by_room.get(room_id, []):
                if b.start < de and b.end > ds:
                    spans.append((max(b.start, ds), min(b.end, de)))
        spans.sort()
        free = []
        cur = ds
        for s, e in spans:
            if s > cur:
                free.append((cur, s))
            if e > cur:
                cur = e
        if cur < de:
            free.append((cur, de))
        return free


# ---------------------------------------------------------------- endpoints

class Request:
    __slots__ = ("store", "params", "query", "headers", "body")

    def __init__(self, store, params, query, headers):
        self.store = store
        self.params = params
        self.query = query
        self.headers = headers
        self.body = None


def ep_create_room(req):
    body = req.body
    name = parse_name(body.get("name"))
    capacity = parse_capacity(body.get("capacity"))
    room = req.store.create_room(name, capacity)
    return 201, j(room.to_json())


def ep_list_rooms(req):
    return 200, j({"rooms": [r.to_json() for r in req.store.list_rooms()]})


def ep_get_room(req):
    return 200, j(req.store.get_room(req.params[0]).to_json())


def ep_availability(req):
    raw = single(req.query, "date")
    if raw is None or not DATE_RE.fullmatch(raw):
        raise invalid("date is required and must be in the form YYYY-MM-DD")
    try:
        day = dt.date(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
    except ValueError:
        raise invalid("date is not a real calendar date")
    free = req.store.availability(req.params[0], day)
    body = {"date": raw,
            "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in free]}
    return 200, j(body)


def ep_create_booking(req):
    body = req.body
    room_id = parse_str_field(body, "room_id")
    title = parse_title(body.get("title"))
    attendees = parse_attendees(body.get("attendees"))
    start = parse_booking_ts(body.get("start"))
    end = parse_booking_ts(body.get("end"))
    if end <= start:
        raise invalid("end must be later than start")
    if end - start > MAX_DURATION:
        raise invalid("booking may last at most 8 hours")
    key = parse_idem_header(req.headers)
    return req.store.create_booking(room_id, start, end, title, attendees, key)


def ep_list_bookings(req):
    q = req.query
    room_id = single(q, "room_id")
    raw_from = single(q, "from")
    raw_to = single(q, "to")
    raw_limit = single(q, "limit")
    cursor = single(q, "cursor")
    frm = parse_query_ts(raw_from) if raw_from is not None else None
    to = parse_query_ts(raw_to) if raw_to is not None else None
    if frm is not None and to is not None and frm >= to:
        raise invalid("from must be earlier than to")
    if raw_limit is None:
        limit = DEFAULT_LIMIT
    else:
        if not INT_RE.fullmatch(raw_limit):
            raise invalid("limit must be a base-10 integer")
        try:
            limit = int(raw_limit)
        except ValueError:
            raise invalid("limit must be a base-10 integer")
        if not 1 <= limit <= MAX_LIMIT:
            raise invalid("limit must be from 1 to 100")
    fp = json.dumps({"room_id": room_id, "from": raw_from, "to": raw_to},
                    sort_keys=True, separators=(",", ":"))
    bookings, next_cursor = req.store.list_bookings(room_id, frm, to, limit, cursor, fp)
    return 200, j({"bookings": bookings, "next_cursor": next_cursor})


def ep_get_booking(req):
    return 200, j(req.store.get_booking(req.params[0]).to_json())


def ep_delete_booking(req):
    req.store.delete_booking(req.params[0])
    return 204, None


def resolve(path, method):
    """Return (endpoint, reads_body, params) or raise ApiError 404/405. Path is matched raw."""
    segs = path.split("/")
    table = None
    params = ()
    if segs == ["", "rooms"]:
        table = {"GET": (ep_list_rooms, False), "POST": (ep_create_room, True)}
    elif len(segs) == 3 and segs[1] == "rooms" and segs[2]:
        table = {"GET": (ep_get_room, False)}
        params = (segs[2],)
    elif len(segs) == 4 and segs[1] == "rooms" and segs[2] and segs[3] == "availability":
        table = {"GET": (ep_availability, False)}
        params = (segs[2],)
    elif segs == ["", "bookings"]:
        table = {"GET": (ep_list_bookings, False), "POST": (ep_create_booking, True)}
    elif len(segs) == 3 and segs[1] == "bookings" and segs[2]:
        table = {"GET": (ep_get_booking, False), "DELETE": (ep_delete_booking, False)}
        params = (segs[2],)
    if table is None:
        raise ApiError(404, "not_found", "no route matches this path")
    if method not in table:
        raise ApiError(405, "method_not_allowed", f"method {method} is not allowed for this path",
                       headers=[("Allow", ", ".join(sorted(table)))])
    fn, reads_body = table[method]
    return fn, reads_body, params


# ---------------------------------------------------------------- transport

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._handle()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_GET

    def send_error(self, code, message=None, explain=None):
        # Called by the base class for protocol-level problems. Keep responses JSON.
        if code == 501:  # unsupported method name: route it like any other method
            self._handle()
            return
        self.close_connection = True
        err_code = "bad_json" if code == 400 else "validation_error"
        self._send(code, j({"error": {"code": err_code,
                                      "message": message or "bad request"}}))

    # -- body handling
    def _read_body(self, limit):
        """Return the body bytes, or None if it is larger than `limit` (already consumed)."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            total = 0
            too_big = False
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    self.close_connection = True
                    raise ApiError(400, "bad_json", "request body is truncated")
                size_s = line.split(b";", 1)[0].strip()
                if not re.fullmatch(rb"[0-9a-fA-F]+", size_s):
                    self.close_connection = True
                    raise ApiError(400, "bad_json", "invalid chunk size")
                size = int(size_s, 16)
                if size == 0:
                    while True:  # trailers
                        t = self.rfile.readline(65537)
                        if not t or t in (b"\r\n", b"\n"):
                            break
                    break
                total += size
                if total > DRAIN_MAX:
                    self.close_connection = True
                    too_big = True
                    break
                data = self.rfile.read(size)
                if len(data) != size:
                    self.close_connection = True
                    raise ApiError(400, "bad_json", "request body is truncated")
                self.rfile.readline(65537)  # CRLF after chunk
                chunks.append(data)
            if too_big or total > limit:
                return None
            return b"".join(chunks)
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        if not re.fullmatch(r"[0-9]+", cl.strip()):
            self.close_connection = True
            raise ApiError(400, "bad_json", "invalid Content-Length")
        n = int(cl.strip())
        if n > DRAIN_MAX:
            self.close_connection = True
            return None
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            self.close_connection = True
            raise ApiError(400, "bad_json", "request body is truncated")
        if n > limit:
            return None
        return data

    def _discard_body(self):
        """Consume a body we will not use, so the connection can be kept alive."""
        try:
            self._read_body(DRAIN_MAX)
        except ApiError:
            self.close_connection = True

    # -- dispatch
    def _handle(self):
        consumed = False
        try:
            path, _, qs = self.path.partition("?")
            endpoint, reads_body, params = resolve(path, self.command)
            req = Request(self.server.store, params, parse_query(qs), self.headers)
            if reads_body:
                consumed = True
                raw = self._read_body(MAX_BODY)
                if raw is None:
                    raise invalid("request body is larger than 65536 bytes")
                req.body = parse_json_body(raw)
                if not isinstance(req.body, dict):
                    raise invalid("request body must be a JSON object")
            else:
                consumed = True
                self._discard_body()
            status, payload = endpoint(req)
            self._send(status, payload)
        except ApiError as e:
            if not consumed:
                self._discard_body()
            self._send(e.status, j({"error": {"code": e.code, "message": e.message}}), e.headers)
        except Exception:
            if not consumed:
                self._discard_body()
            traceback.print_exc(file=sys.stderr)
            self._send(500, j({"error": {"code": "internal_error",
                                         "message": "internal server error"}}))

    def _send(self, status, payload, headers=()):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in headers:
                self.send_header(k, v)
            if status != 204 and status >= 200:
                self.send_header("Content-Length", str(len(payload or b"")))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if payload and self.command != "HEAD" and status != 204:
                self.wfile.write(payload)
        except OSError:
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def server_bind(self):
        # Skip the reverse-DNS lookup done by HTTPServer.server_bind.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def make_server(host, port, store=None):
    server = Server((host, port), Handler)
    server.store = store if store is not None else Store()
    return server


def parse_port(argv):
    """Return the port as an int, or None if the arguments are not valid."""
    value = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--port":
            if i + 1 >= len(argv) or value is not None:
                return None
            value = argv[i + 1]
            i += 2
        elif arg.startswith("--port=") and value is None:
            value = arg[len("--port="):]
            i += 1
        else:
            return None
    if value is None or not INT_RE.fullmatch(value):
        return None
    try:
        port = int(value)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def main(argv=None):
    port = parse_port(sys.argv[1:] if argv is None else argv)
    if port is None:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        server = make_server("127.0.0.1", port)
    except OSError as e:
        print(f"error: cannot listen on 127.0.0.1:{port}: {e.strerror or e}", file=sys.stderr)
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
