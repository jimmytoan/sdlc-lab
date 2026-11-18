"""bookings: meeting room booking JSON API (standard library only).

Run: python server.py --port N   (listens on 127.0.0.1)
"""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_BODY = 1024 * 1024
DAY_START_S = 8 * 3600
DAY_END_S = 18 * 3600
MAX_DURATION_S = 8 * 3600
DEFAULT_LIMIT = 20

LOCK = threading.Lock()

ROOMS = {}          # id -> room dict
ROOM_ORDER = []     # room ids in creation order
ROOM_NAMES = set()
ROOM_SEQ = [0]
BOOKINGS = {}       # id -> booking dict (internal keys prefixed with _)
BOOKING_SEQ = [0]
IDEM = {}           # key -> (fingerprint, status, body)

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def verr(message):
    return ApiError(422, "validation_error", message)


def bad_json(message="malformed JSON body"):
    return ApiError(400, "bad_json", message)


def parse_ts(value, field="timestamp", error_factory=verr):
    """Strict YYYY-MM-DDTHH:MM:SSZ -> epoch seconds (int)."""
    m = TS_RE.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise error_factory(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        raise error_factory(f"{field} is not a valid date/time")
    return int((dt - EPOCH).total_seconds())


def fmt_ts(t):
    dt = EPOCH + timedelta(seconds=t)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def check_quarter(t, field):
    dt = EPOCH + timedelta(seconds=t)
    if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
        raise verr(f"{field} must start on a quarter hour")


def json_body(raw):
    if not raw:
        raise bad_json("request body is required")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        raise bad_json()
    if not isinstance(obj, dict):
        raise bad_json("request body must be a JSON object")
    return obj


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- rooms

def room_view(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def create_room(raw, _query):
    body = json_body(raw)
    name = body.get("name")
    if not isinstance(name, str):
        raise verr("name must be a string")
    if name.strip() == "":
        raise verr("name must not be empty")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    cap = body.get("capacity")
    if not is_int(cap) or not (1 <= cap <= 500):
        raise verr("capacity must be an integer between 1 and 500")
    if name in ROOM_NAMES:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    ROOM_SEQ[0] += 1
    rid = f"r{ROOM_SEQ[0]}"
    room = {"id": rid, "name": name, "capacity": cap}
    ROOMS[rid] = room
    ROOM_ORDER.append(rid)
    ROOM_NAMES.add(name)
    return 201, room_view(room), {}


def list_rooms(_raw, _query):
    return 200, {"rooms": [room_view(ROOMS[i]) for i in ROOM_ORDER]}, {}


def get_room(room_id):
    if room_id not in ROOMS:
        raise ApiError(404, "not_found", "room not found")
    return 200, room_view(ROOMS[room_id]), {}


def availability(room_id, query):
    if room_id not in ROOMS:
        raise ApiError(404, "not_found", "room not found")
    qs = parse_qs(query, keep_blank_values=True)
    raw = qs.get("date", [None])[0]
    if raw is None or not DATE_RE.fullmatch(raw):
        raise verr("date must be YYYY-MM-DD")
    try:
        d = date(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
    except ValueError:
        raise verr("date must be a valid calendar date")
    base = int((datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - EPOCH).total_seconds())
    w_start = base + DAY_START_S
    w_end = base + DAY_END_S
    spans = []
    for b in BOOKINGS.values():
        if b["room_id"] != room_id:
            continue
        if b["_s"] < w_end and b["_e"] > w_start:
            spans.append((max(b["_s"], w_start), min(b["_e"], w_end)))
    spans.sort()
    free = []
    cur = w_start
    for s, e in spans:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        cur = max(cur, e)
    if cur < w_end:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(w_end)})
    return 200, {"date": raw, "free": free}, {}


# ---------------------------------------------------------------- bookings

def booking_view(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["_s"]),
            "end": fmt_ts(b["_e"]), "title": b["title"], "attendees": b["attendees"]}


def fingerprint(body):
    norm = [body.get("room_id"), body.get("start"), body.get("end"),
            body.get("title"), body.get("attendees")]
    return json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def create_booking(raw, headers):
    key = headers.get("Idempotency-Key")
    if key is not None and (key.strip() == "" or len(key) > 255):
        raise verr("Idempotency-Key must be 1 to 255 characters")
    body = json_body(raw)
    fp = fingerprint(body) if key is not None else None
    if key is not None and key in IDEM:
        stored_fp, status, stored_body = IDEM[key]
        if stored_fp != fp:
            raise ApiError(422, "idempotency_mismatch",
                           "Idempotency-Key was already used for a different request")
        return status, stored_body, {}

    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise verr("room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    check_quarter(start, "start")
    check_quarter(end, "end")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_DURATION_S:
        raise verr("booking must be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise verr("title must be a string")
    if title.strip() == "":
        raise verr("title must not be empty")
    if len(title) > 200:
        raise verr("title must be at most 200 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be an integer >= 1")

    room = ROOMS.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    for other in BOOKINGS.values():
        if other["room_id"] == room_id and start < other["_e"] and other["_s"] < end:
            raise ApiError(409, "conflict", "the room is already booked for this time")

    BOOKING_SEQ[0] += 1
    bid = f"b{BOOKING_SEQ[0]}"
    b = {"id": bid, "room_id": room_id, "title": title, "attendees": attendees,
         "_s": start, "_e": end, "_n": BOOKING_SEQ[0]}
    BOOKINGS[bid] = b
    view = booking_view(b)
    if key is not None:
        IDEM[key] = (fp, 201, view)
    return 201, view, {}


def get_booking(bid):
    b = BOOKINGS.get(bid)
    if b is None:
        raise ApiError(404, "not_found", "booking not found")
    return 200, booking_view(b), {}


def delete_booking(bid):
    if bid not in BOOKINGS:
        raise ApiError(404, "not_found", "booking not found")
    del BOOKINGS[bid]
    return 204, None, {}


def encode_cursor(b):
    payload = json.dumps({"s": b["_s"], "i": b["_n"]}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(raw):
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        raise verr("cursor is invalid")
    if not isinstance(payload, dict) or not is_int(payload.get("s")) or not is_int(payload.get("i")):
        raise verr("cursor is invalid")
    return payload["s"], payload["i"]


def list_bookings(_raw, query):
    qs = parse_qs(query, keep_blank_values=True)

    def get(name):
        v = qs.get(name)
        return v[0] if v else None

    room_id = get("room_id")
    raw_from = get("from")
    raw_to = get("to")
    f = parse_ts(raw_from, "from") if raw_from is not None else None
    t = parse_ts(raw_to, "to") if raw_to is not None else None
    if f is not None and t is not None and f >= t:
        raise verr("from must be before to")

    raw_limit = get("limit")
    if raw_limit is None:
        limit = DEFAULT_LIMIT
    else:
        if not INT_RE.fullmatch(raw_limit):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(raw_limit)
        if not (1 <= limit <= 100):
            raise verr("limit must be an integer between 1 and 100")

    raw_cursor = get("cursor")
    after = decode_cursor(raw_cursor) if raw_cursor else None

    items = []
    for b in BOOKINGS.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if f is not None and not (b["_e"] > f):
            continue
        if t is not None and not (b["_s"] < t):
            continue
        items.append(b)
    items.sort(key=lambda b: (b["_s"], b["_n"]))
    if after is not None:
        items = [b for b in items if (b["_s"], b["_n"]) > after]
    page = items[:limit]
    next_cursor = encode_cursor(page[-1]) if len(items) > limit and page else None
    return 200, {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor}, {}


# ---------------------------------------------------------------- routing

def match_route(parts):
    """parts: path segments without the leading ''. Returns (name, allowed methods, args) or None."""
    if parts == ["rooms"]:
        return "rooms", ("GET", "POST"), ()
    if len(parts) == 2 and parts[0] == "rooms" and parts[1] != "":
        return "room", ("GET",), (parts[1],)
    if len(parts) == 3 and parts[0] == "rooms" and parts[1] != "" and parts[2] == "availability":
        return "availability", ("GET",), (parts[1],)
    if parts == ["bookings"]:
        return "bookings", ("GET", "POST"), ()
    if len(parts) == 2 and parts[0] == "bookings" and parts[1] != "":
        return "booking", ("GET", "DELETE"), (parts[1],)
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "bookings"
    sys_version = ""

    def log_message(self, format, *args):  # silence logging
        pass

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.close_connection = True
                self._send(414, error_body("bad_json", "request line too long"))
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self._handle()
            self.wfile.flush()
        except (TimeoutError, ConnectionError, OSError):
            self.close_connection = True

    def _send(self, status, body=None, extra=None):
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _handle(self):
        parts_url = urlsplit(self.path)
        path = parts_url.path
        query = parts_url.query
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raw = b""
        else:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0:
                    raise ValueError
            except ValueError:
                self.close_connection = True
                self._send(400, error_body("bad_json", "invalid Content-Length"))
                return
            if n > MAX_BODY:
                self.close_connection = True
                self._send(400, error_body("bad_json", "request body too large"))
                return
            raw = self.rfile.read(n) if n else b""

        if not path.startswith("/"):
            self._send(404, error_body("not_found", "no such path"))
            return
        route = match_route(path[1:].split("/"))
        if route is None:
            self._send(404, error_body("not_found", "no such path"))
            return
        name, allowed, args = route
        method = self.command
        if method not in allowed:
            self._send(405, error_body("method_not_allowed", f"{method} not allowed here"),
                       {"Allow": ", ".join(allowed)})
            return

        try:
            with LOCK:
                if name == "rooms":
                    result = create_room(raw, query) if method == "POST" else list_rooms(raw, query)
                elif name == "room":
                    result = get_room(args[0])
                elif name == "availability":
                    result = availability(args[0], query)
                elif name == "bookings":
                    result = (create_booking(raw, self.headers) if method == "POST"
                              else list_bookings(raw, query))
                else:  # booking
                    result = get_booking(args[0]) if method == "GET" else delete_booking(args[0])
        except ApiError as e:
            self._send(e.status, error_body(e.code, e.message))
            return
        except Exception:
            self._send(500, error_body("internal_error", "internal server error"))
            return
        status, body, extra = result
        self._send(status, body, extra)


def error_body(code, message):
    return {"error": {"code": code, "message": message}}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
