"""bookings: meeting room booking API (stdlib only, in-memory state)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOCK = threading.Lock()
rooms = {}
room_seq = 0
bookings = {}
booking_seq = 0
idem = {}

TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
LIMIT_RE = re.compile(r"^[0-9]+$")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def verr(msg):
    return ApiError(422, "validation_error", msg)


def parse_ts(s):
    if not isinstance(s, str) or not TS_RE.fullmatch(s):
        return None
    try:
        return datetime.strptime(s, TS_FMT)
    except ValueError:
        return None


def fmt_ts(dt):
    return dt.strftime(TS_FMT)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def public_booking(b):
    return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}


def booking_key(b):
    return (b["_s"], int(b["id"][1:]))


def encode_cursor(b):
    raw = json.dumps([b["start"], int(b["id"][1:])]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        data = json.loads(base64.urlsafe_b64decode((c + pad).encode()).decode("utf-8"))
        if not (isinstance(data, list) and len(data) == 2):
            raise ValueError
        dt = parse_ts(data[0])
        if dt is None or not is_int(data[1]):
            raise ValueError
        return (dt, data[1])
    except Exception:
        raise verr("invalid cursor")


# ---------------------------------------------------------------- handlers

def create_room(body):
    global room_seq
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise verr("name must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(cap):
        raise verr("capacity must be an integer")
    if cap < 1 or cap > 500:
        raise verr("capacity must be between 1 and 500")
    if any(r["name"] == name for r in rooms.values()):
        raise ApiError(409, "room_exists", "a room with this name already exists")
    room_seq += 1
    room = {"id": f"r{room_seq}", "name": name, "capacity": cap}
    rooms[room["id"]] = room
    return 201, room


def get_room(rid):
    room = rooms.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return room


def create_booking(body, key):
    global booking_seq
    fingerprint = None
    if key:
        fingerprint = json.dumps(body, sort_keys=True)
        if key in idem:
            fp, status, resp = idem[key]
            if fp != fingerprint:
                raise ApiError(422, "idempotency_mismatch",
                               "idempotency key was used for a different request")
            return status, resp
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    room_id = body.get("room_id")
    start, end = body.get("start"), body.get("end")
    title, att = body.get("title"), body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id must be a string")
    s = parse_ts(start)
    if s is None:
        raise verr("start must be a UTC timestamp like 2026-03-02T09:30:00Z")
    e = parse_ts(end)
    if e is None:
        raise verr("end must be a UTC timestamp like 2026-03-02T09:30:00Z")
    for name, dt in (("start", s), ("end", e)):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise verr(f"{name} must be on a quarter hour")
    if e <= s:
        raise verr("end must be after start")
    if e - s > timedelta(hours=8):
        raise verr("bookings may be at most 8 hours long")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    if not is_int(att) or att < 1:
        raise verr("attendees must be a positive integer")
    room = rooms.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if att > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    for b in bookings.values():
        if b["room_id"] == room_id and s < b["_e"] and b["_s"] < e:
            raise ApiError(409, "conflict", f"overlaps booking {b['id']}")
    booking_seq += 1
    b = {"id": f"b{booking_seq}", "room_id": room_id, "start": start, "end": end,
         "title": title, "attendees": att, "_s": s, "_e": e}
    bookings[b["id"]] = b
    resp = public_booking(b)
    if key:
        idem[key] = (fingerprint, 201, resp)
    return 201, resp


def list_bookings(q):
    room_id = q.get("room_id")
    frm = to = None
    if "from" in q:
        frm = parse_ts(q["from"])
        if frm is None:
            raise verr("from must be a UTC timestamp like 2026-03-02T09:30:00Z")
    if "to" in q:
        to = parse_ts(q["to"])
        if to is None:
            raise verr("to must be a UTC timestamp like 2026-03-02T09:30:00Z")
    if frm is not None and to is not None and frm >= to:
        raise verr("from must be before to")
    limit = 20
    if "limit" in q:
        lv = q["limit"]
        if not LIMIT_RE.fullmatch(lv) or not (1 <= int(lv) <= 100):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(lv)
    after = None
    if "cursor" in q:
        after = decode_cursor(q["cursor"])
    items = []
    for b in bookings.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if to is not None and not b["_s"] < to:
            continue
        if frm is not None and not b["_e"] > frm:
            continue
        if after is not None and not booking_key(b) > after:
            continue
        items.append(b)
    items.sort(key=booking_key)
    page = items[:limit]
    nxt = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    get_room(rid)
    d = q.get("date")
    if not isinstance(d, str) or not DATE_RE.fullmatch(d):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise verr("date must be a valid date")
    ws, we = day.replace(hour=8), day.replace(hour=18)
    busy = sorted((max(b["_s"], ws), min(b["_e"], we)) for b in bookings.values()
                  if b["room_id"] == rid and b["_s"] < we and b["_e"] > ws)
    free = []
    cur = ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < we:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": d, "free": free}


# ---------------------------------------------------------------- HTTP

ROUTES = [
    (re.compile(r"^/rooms$"), ("GET", "POST"), "rooms"),
    (re.compile(r"^/rooms/([^/]+)$"), ("GET",), "room"),
    (re.compile(r"^/rooms/([^/]+)/availability$"), ("GET",), "avail"),
    (re.compile(r"^/bookings$"), ("GET", "POST"), "bookings"),
    (re.compile(r"^/bookings/([^/]+)$"), ("GET", "DELETE"), "booking"),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, *args):
        pass

    def send_json(self, status, obj, extra=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_err(self, status, code, message, extra=None):
        self.send_json(status, {"error": {"code": code, "message": message}}, extra)

    def send_204(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    break
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline(65537)
            return b"".join(chunks)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        return raw

    def parse_json(self, raw):
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "bad_json", "request body must be valid JSON")

    def dispatch(self, method):
        raw = b""
        try:
            raw = self.read_body()
        except Exception:
            pass
        try:
            parts = urlsplit(self.path)
            path = parts.path
            q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            for rx, methods, name in ROUTES:
                m = rx.match(path)
                if not m:
                    continue
                if method not in methods:
                    raise _MethodNotAllowed(methods)
                with LOCK:
                    self.handle_route(name, method, m.groups(), q, raw)
                return
            raise ApiError(404, "not_found", "no such resource")
        except _MethodNotAllowed as e:
            self.send_err(405, "method_not_allowed", f"method {method} not allowed",
                          {"Allow": ", ".join(e.methods)})
        except ApiError as e:
            self.send_err(e.status, e.code, e.message)
        except Exception as e:  # pragma: no cover
            try:
                self.send_err(500, "internal", f"internal error: {type(e).__name__}")
            except Exception:
                pass

    def handle_route(self, name, method, groups, q, raw):
        if name == "rooms":
            if method == "GET":
                return self.send_json(200, {"rooms": list(rooms.values())})
            status, obj = create_room(self.parse_json(raw))
            return self.send_json(status, obj)
        if name == "room":
            return self.send_json(200, get_room(groups[0]))
        if name == "avail":
            status, obj = availability(groups[0], q)
            return self.send_json(status, obj)
        if name == "bookings":
            if method == "GET":
                status, obj = list_bookings(q)
                return self.send_json(status, obj)
            body = self.parse_json(raw)
            key = self.headers.get("Idempotency-Key") or ""
            status, obj = create_booking(body, key)
            return self.send_json(status, obj)
        if name == "booking":
            b = bookings.get(groups[0])
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            if method == "GET":
                return self.send_json(200, public_booking(b))
            del bookings[groups[0]]
            return self.send_204()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def __getattr__(self, name):
        # Route any other method (TRACE, CONNECT, custom verbs) through
        # dispatch so clients always get a JSON 404/405 instead of a 501 page.
        if name.startswith("do_") and len(name) > 3:
            method = name[3:]
            return lambda: self.dispatch(method)
        raise AttributeError(name)


class _MethodNotAllowed(Exception):
    def __init__(self, methods):
        super().__init__("method not allowed")
        self.methods = methods


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
