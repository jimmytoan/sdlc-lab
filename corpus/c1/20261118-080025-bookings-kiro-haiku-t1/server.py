"""bookings: in-memory meeting room booking JSON API.

Start with:  python server.py --port N     (listens on 127.0.0.1 only)
Specification: .kiro/specs/bookings/ (requirements.md, design.md, tasks.md).
Standard library only.
"""
import base64
import bisect
import hashlib
import hmac
import json
import re
import secrets
import socketserver
import sys
import threading
from datetime import date as _date
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 65536            # R1.9
MAX_DISCARD = 1 << 20       # oversize bodies up to this size are drained before answering
MAX_DURATION = 8 * 3600     # R5.12
DEFAULT_LIMIT = 20          # R10.13
MAX_LIMIT = 100             # R10.14
MAX_TITLE = 200             # R5.18
MAX_ROOM_NAME = 50          # R3.7
MAX_CAPACITY = 500          # R3.11
MAX_IDEM_KEY = 255          # R8.2
WORK_START_H = 8            # R11.5
WORK_END_H = 18

USAGE = "usage: server.py --port N"

_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INT_RE = re.compile(r"[0-9]+")
_B64_RE = re.compile(r"[A-Za-z0-9_\-=]+")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def bad_json(msg="request body is not valid JSON"):
    return ApiError(400, "bad_json", msg)


def invalid(msg):
    return ApiError(422, "validation_error", msg)


def not_found(msg="resource not found"):
    return ApiError(404, "not_found", msg)


# ---------------------------------------------------------------- domain helpers

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_ts(text):
    """Strict YYYY-MM-DDTHH:MM:SSZ, real calendar value -> epoch seconds, else None."""
    if not isinstance(text, str) or _TS_RE.fullmatch(text) is None:
        return None
    try:
        dt = datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]),
                      int(text[11:13]), int(text[14:16]), int(text[17:19]),
                      tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def format_ts(epoch):
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_date(text):
    if not isinstance(text, str) or _DATE_RE.fullmatch(text) is None:
        return None
    try:
        return _date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def is_quarter(epoch):
    """Minutes 00/15/30/45 and seconds 00 (seconds are also checked by parse_ts)."""
    return ((epoch % 3600) // 60) % 15 == 0 and epoch % 60 == 0


def overlaps(a_start, a_end, b_start, b_end):
    """Half-open interval overlap."""
    return a_start < b_end and b_start < a_end


def day_epoch(d, hour):
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp())


# ---------------------------------------------------------------- store

class Booking:
    __slots__ = ("id", "n", "room_id", "start", "end", "title", "attendees")

    def __init__(self, n, room_id, start, end, title, attendees):
        self.n = n
        self.id = f"b{n}"
        self.room_id = room_id
        self.start = start
        self.end = end
        self.title = title
        self.attendees = attendees

    def to_json(self):
        return {"id": self.id, "room_id": self.room_id, "start": format_ts(self.start),
                "end": format_ts(self.end), "title": self.title, "attendees": self.attendees}


def _sort_key(b):
    return (b.start, b.n)


class Store:
    """All state. Every state-changing operation and every read runs under self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}              # id -> {"id", "name", "capacity"}
        self.room_order = []         # ids in creation order
        self.room_names = set()      # case-sensitive names
        self.bookings = {}           # id -> Booking
        self.room_bookings = {}      # room id -> list of Booking sorted by (start, n)
        self.idem = {}               # key -> (canonical body, 201 response body)
        self.next_room_no = 1
        self.next_booking_no = 1
        self.cursor_key = secrets.token_bytes(32)

    # ---- rooms

    def create_room(self, body):
        name = body.get("name")
        if not isinstance(name, str):
            raise invalid("name must be a string")
        name = name.strip()
        if not name:
            raise invalid("name must not be empty")
        if len(name) > MAX_ROOM_NAME:
            raise invalid(f"name must be at most {MAX_ROOM_NAME} characters")
        cap = body.get("capacity")
        if not _is_int(cap) or not 1 <= cap <= MAX_CAPACITY:
            raise invalid(f"capacity must be an integer from 1 to {MAX_CAPACITY}")
        with self.lock:
            if name in self.room_names:
                raise ApiError(409, "room_exists", f"a room named {name!r} already exists")
            rid = f"r{self.next_room_no}"
            self.next_room_no += 1
            room = {"id": rid, "name": name, "capacity": cap}
            self.rooms[rid] = room
            self.room_order.append(rid)
            self.room_names.add(name)
            self.room_bookings[rid] = []
            return 201, dict(room)

    def list_rooms(self):
        with self.lock:
            return 200, {"rooms": [dict(self.rooms[r]) for r in self.room_order]}

    def get_room(self, rid):
        with self.lock:
            room = self.rooms.get(rid)
            if room is None:
                raise not_found(f"no room with id {rid}")
            return 200, dict(room)

    # ---- bookings

    def create_booking(self, body, key):
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if key is not None and (key == "" or len(key) > MAX_IDEM_KEY):
            raise invalid(f"Idempotency-Key must be 1 to {MAX_IDEM_KEY} characters")
        with self.lock:
            if key is not None and key in self.idem:
                stored_canonical, stored = self.idem[key]
                if stored_canonical == canonical:
                    return 201, dict(stored)
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used with a different request body")
            room_id, title, attendees, start, end = self._validate_booking(body)
            room = self.rooms.get(room_id)
            if room is None:
                raise not_found(f"no room with id {room_id}")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity",
                               f"attendees ({attendees}) exceed room capacity ({room['capacity']})")
            for other in self.room_bookings[room_id]:
                if overlaps(start, end, other.start, other.end):
                    raise ApiError(409, "conflict", f"room is already booked by {other.id}")
            n = self.next_booking_no
            self.next_booking_no += 1
            bk = Booking(n, room_id, start, end, title, attendees)
            self.bookings[bk.id] = bk
            bisect.insort(self.room_bookings[room_id], bk, key=_sort_key)
            response = bk.to_json()
            if key is not None:
                self.idem[key] = (canonical, response)
            return 201, dict(response)

    @staticmethod
    def _validate_booking(body):
        for field in ("room_id", "start", "end", "title", "attendees"):
            if field not in body:
                raise invalid(f"{field} is required")
        room_id = body["room_id"]
        if not isinstance(room_id, str):
            raise invalid("room_id must be a string")
        title = body["title"]
        if not isinstance(title, str):
            raise invalid("title must be a string")
        title = title.strip()
        if not title:
            raise invalid("title must not be empty")
        if len(title) > MAX_TITLE:
            raise invalid(f"title must be at most {MAX_TITLE} characters")
        attendees = body["attendees"]
        if not _is_int(attendees) or attendees < 1:
            raise invalid("attendees must be an integer of at least 1")
        start = parse_ts(body["start"])
        if start is None:
            raise invalid("start must be a real timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
        end = parse_ts(body["end"])
        if end is None:
            raise invalid("end must be a real timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
        if not (is_quarter(start) and is_quarter(end)):
            raise invalid("start and end must fall on quarter hours with seconds 00")
        if end <= start:
            raise invalid("end must be later than start")
        if end - start > MAX_DURATION:
            raise invalid("booking must not be longer than 8 hours")
        return room_id, title, attendees, start, end

    def get_booking(self, bid):
        with self.lock:
            bk = self.bookings.get(bid)
            if bk is None:
                raise not_found(f"no booking with id {bid}")
            return 200, bk.to_json()

    def delete_booking(self, bid):
        with self.lock:
            bk = self.bookings.pop(bid, None)
            if bk is None:
                raise not_found(f"no booking with id {bid}")
            self.room_bookings[bk.room_id].remove(bk)
            return 204, None

    # ---- cursors

    def _encode_cursor(self, start, n):
        payload = json.dumps({"s": start, "n": n}, separators=(",", ":")).encode()
        mac = hmac.new(self.cursor_key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + mac).decode().rstrip("=")

    def _decode_cursor(self, token):
        try:
            if not _B64_RE.fullmatch(token):
                raise ValueError("bad characters")
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            if len(raw) <= 32:
                raise ValueError("too short")
            payload, mac = raw[:-32], raw[-32:]
            expected = hmac.new(self.cursor_key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(mac, expected):
                raise ValueError("bad signature")
            obj = json.loads(payload.decode("utf-8"))
            if not (isinstance(obj, dict) and set(obj) == {"s", "n"}
                    and _is_int(obj["s"]) and _is_int(obj["n"])):
                raise ValueError("bad shape")
            return obj["s"], obj["n"]
        except (ValueError, TypeError, KeyError):
            raise invalid("cursor is not valid")

    def list_bookings(self, qs):
        def one(name):
            vals = qs.get(name)
            if vals is None:
                return None
            if len(vals) != 1:
                raise invalid(f"query parameter {name} may be given only once")
            return vals[0]

        room_id = one("room_id")
        from_s = one("from")
        to_s = one("to")
        limit_s = one("limit")
        cursor_s = one("cursor")

        if room_id == "":
            raise invalid("room_id must not be empty")
        frm = to = None
        if from_s is not None:
            frm = parse_ts(from_s)
            if frm is None:
                raise invalid("from must be a real timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
        if to_s is not None:
            to = parse_ts(to_s)
            if to is None:
                raise invalid("to must be a real timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
        if frm is not None and to is not None and frm >= to:
            raise invalid("from must be earlier than to")
        if limit_s is None:
            limit = DEFAULT_LIMIT
        else:
            if _INT_RE.fullmatch(limit_s) is None or not 1 <= int(limit_s) <= MAX_LIMIT:
                raise invalid(f"limit must be an integer from 1 to {MAX_LIMIT}")
            limit = int(limit_s)
        position = self._decode_cursor(cursor_s) if cursor_s is not None else None

        with self.lock:
            if room_id is not None:
                candidates = self.room_bookings.get(room_id, [])
            else:
                candidates = self.bookings.values()
            items = []
            for bk in candidates:
                if frm is not None and not bk.end > frm:
                    continue
                if to is not None and not bk.start < to:
                    continue
                if position is not None and not (bk.start, bk.n) > position:
                    continue
                items.append(bk)
            items.sort(key=_sort_key)
            page = items[:limit]
            next_cursor = None
            if len(items) > limit:
                last = page[-1]
                next_cursor = self._encode_cursor(last.start, last.n)
            return 200, {"bookings": [b.to_json() for b in page], "next_cursor": next_cursor}

    # ---- availability

    def availability(self, rid, qs):
        with self.lock:
            if rid not in self.rooms:
                raise not_found(f"no room with id {rid}")
            vals = qs.get("date")
            if vals is None or len(vals) != 1:
                raise invalid("date must be given exactly once in the form YYYY-MM-DD")
            d = parse_date(vals[0])
            if d is None:
                raise invalid("date must be a real date in the form YYYY-MM-DD")
            w0 = day_epoch(d, WORK_START_H)
            w1 = day_epoch(d, WORK_END_H)
            clipped = []
            for bk in self.room_bookings[rid]:
                s, e = max(bk.start, w0), min(bk.end, w1)
                if s < e:
                    clipped.append((s, e))
            clipped.sort()
            merged = []
            for s, e in clipped:
                if merged and s <= merged[-1][1]:
                    if e > merged[-1][1]:
                        merged[-1][1] = e
                else:
                    merged.append([s, e])
            free = []
            cur = w0
            for s, e in merged:
                if s > cur:
                    free.append({"start": format_ts(cur), "end": format_ts(s)})
                cur = max(cur, e)
            if cur < w1:
                free.append({"start": format_ts(cur), "end": format_ts(w1)})
            return 200, {"date": vals[0], "free": free}


# ---------------------------------------------------------------- HTTP layer

def match_route(path):
    """Returns (name, allowed methods, room/booking id) or None."""
    segs = path.split("/")
    if not segs or segs[0] != "":
        return None
    parts = segs[1:]
    if parts == ["rooms"]:
        return ("rooms", ("GET", "POST"), None)
    if len(parts) == 2 and parts[0] == "rooms" and parts[1]:
        return ("room", ("GET",), parts[1])
    if len(parts) == 3 and parts[0] == "rooms" and parts[1] and parts[2] == "availability":
        return ("availability", ("GET",), parts[1])
    if parts == ["bookings"]:
        return ("bookings", ("GET", "POST"), None)
    if len(parts) == 2 and parts[0] == "bookings" and parts[1]:
        return ("booking", ("GET", "DELETE"), parts[1])
    return None


def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


def parse_object(data):
    """Body parsing (R2.5, R2.6)."""
    if not data:
        raise bad_json("request body is empty")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise bad_json("request body is not valid UTF-8")
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise bad_json("request body is not valid JSON")
    if not isinstance(obj, dict):
        raise invalid("request body must be a JSON object")
    return obj


class BookingsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128   # listen backlog; the stdlib default of 5 resets bursts of clients

    def __init__(self, address, handler):
        self.store = Store()
        super().__init__(address, handler)

    def server_bind(self):
        # Skip HTTPServer's reverse DNS lookup; we always bind to 127.0.0.1.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings"
    sys_version = ""
    timeout = 30

    def log_message(self, format, *args):  # keep stderr quiet
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    # ---- plumbing

    def _send(self, status, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_error(self, status, code, message, headers=None):
        self.close_connection = True if status == 400 else self.close_connection
        self._send(status, {"error": {"code": code, "message": message}}, headers)

    def _read_body(self):
        """Returns (problem, data). problem is None, or a message for a 400 response."""
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
            return "chunked request bodies are not supported", None
        cl = self.headers.get("Content-Length")
        if cl is None:
            return None, b""
        cl = cl.strip()
        if _INT_RE.fullmatch(cl) is None:
            self.close_connection = True
            return "Content-Length is not a valid integer", None
        n = int(cl)
        if n > MAX_BODY:
            self.close_connection = True
            if n <= MAX_DISCARD:
                self.rfile.read(n)
            return f"request body must not exceed {MAX_BODY} bytes", None
        return None, self.rfile.read(n) if n else b""

    def _handle(self, method):
        try:
            self._serve(method)
        except ApiError as e:
            self._send_error(e.status, e.code, e.message)
        except Exception:
            try:
                self._send_error(500, "internal_error", "internal server error")
            except Exception:
                pass

    def _serve(self, method):
        url = urlsplit(self.path)
        qs = parse_qs(url.query, keep_blank_values=True)
        problem, data = self._read_body()
        route = match_route(url.path)
        if route is None:
            return self._send_error(404, "not_found", "no route matches this path")
        name, allowed, rid = route
        if method not in allowed:
            return self._send_error(405, "method_not_allowed",
                                    f"{method} is not allowed on this path",
                                    {"Allow": ", ".join(allowed)})
        if problem is not None:
            return self._send_error(400, "bad_json", problem)

        store = self.server.store
        if name == "rooms" and method == "POST":
            status, payload = store.create_room(parse_object(data))
        elif name == "rooms":
            status, payload = store.list_rooms()
        elif name == "room":
            status, payload = store.get_room(rid)
        elif name == "availability":
            status, payload = store.availability(rid, qs)
        elif name == "bookings" and method == "POST":
            body = parse_object(data)
            key = self.headers.get("Idempotency-Key")
            status, payload = store.create_booking(body, key)
        elif name == "bookings":
            status, payload = store.list_bookings(qs)
        elif method == "GET":
            status, payload = store.get_booking(rid)
        else:
            status, payload = store.delete_booking(rid)
        return self._send(status, payload)


def parse_port(argv):
    """Returns the port number, or None when the arguments are not a valid `--port N`."""
    if len(argv) == 2 and argv[0] == "--port":
        value = argv[1]
    elif len(argv) == 1 and argv[0].startswith("--port="):
        value = argv[0][len("--port="):]
    else:
        return None
    if _INT_RE.fullmatch(value) is None:
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = parse_port(argv)
    if port is None:
        print(USAGE, file=sys.stderr)
        print("N must be an integer from 1 to 65535 (there is no default port)", file=sys.stderr)
        return 2
    try:
        httpd = BookingsServer(("127.0.0.1", port), Handler)
    except OSError as e:
        print(f"server.py: cannot listen on 127.0.0.1:{port}: {e}", file=sys.stderr)
        return 1
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
