"""Meeting room booking API (standard library only)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOCK = threading.Lock()
rooms = {}
bookings = {}
booking_dt = {}  # id -> (start_dt, end_dt)
idem = {}
room_seq = 0
booking_seq = 0

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_DURATION = timedelta(hours=8)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def verr(msg):
    return ApiError(422, "validation_error", msg)


def not_found(msg="not found"):
    return ApiError(404, "not_found", msg)


def is_int(v):
    return type(v) is int


def parse_ts(s, field):
    if not isinstance(s, str) or not TS_RE.match(s):
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise verr(f"{field} is not a valid date/time")
    return dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def bkey(b):
    return (booking_dt[b["id"]][0], int(b["id"][1:]))


def encode_cursor(key):
    raw = f"{fmt_ts(key[0])}|{key[1]}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode((c + pad).encode("ascii")).decode("utf-8")
        ts, num = raw.split("|")
        if not TS_RE.match(ts) or not re.fullmatch(r"\d+", num):
            raise ValueError
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (dt, int(num))
    except Exception:
        raise verr("cursor is invalid")


# ---------------------------------------------------------------- handlers

def h_list_rooms(req, m, q):
    return 200, {"rooms": list(rooms.values())}


def h_create_room(req, m, q):
    global room_seq
    body = req.read_json_body()
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise verr("name must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    cap = body.get("capacity")
    if not is_int(cap) or not (1 <= cap <= 500):
        raise verr("capacity must be an integer from 1 to 500")
    if any(r["name"] == name for r in rooms.values()):
        raise ApiError(409, "room_exists", "a room with this name already exists")
    room_seq += 1
    rid = f"r{room_seq}"
    room = {"id": rid, "name": name, "capacity": cap}
    rooms[rid] = room
    return 201, room


def h_get_room(req, m, q):
    room = rooms.get(m[0])
    if room is None:
        raise not_found("room not found")
    return 200, room


def h_availability(req, m, q):
    room = rooms.get(m[0])
    if room is None:
        raise not_found("room not found")
    date = q.get("date")
    if date is None or not DATE_RE.match(date):
        raise verr("date must be YYYY-MM-DD")
    try:
        d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("date is not a valid date")
    ws = d + timedelta(hours=8)
    we = d + timedelta(hours=18)
    busy = []
    for bid, b in bookings.items():
        if b["room_id"] != room["id"]:
            continue
        s, e = booking_dt[bid]
        s, e = max(s, ws), min(e, we)
        if s < e:
            busy.append((s, e))
    busy.sort()
    free = []
    cur = ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < we:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": date, "free": free}


def h_create_booking(req, m, q):
    global booking_seq
    body = req.read_json_body()
    key = req.headers.get("Idempotency-Key")
    canon = None
    if key:
        canon = json.dumps(body, sort_keys=True)
        if key in idem:
            stored_canon, status, resp = idem[key]
            if stored_canon == canon:
                return status, resp
            raise ApiError(422, "idempotency_mismatch",
                           "Idempotency-Key was already used for a different request")
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.minute % 15 or dt.second:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_DURATION:
        raise verr("bookings may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att) or att < 1:
        raise verr("attendees must be a positive integer")
    room = rooms.get(room_id)
    if room is None:
        raise not_found("room not found")
    if att > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    for bid, b in bookings.items():
        if b["room_id"] != room_id:
            continue
        s, e = booking_dt[bid]
        if s < end and start < e:
            raise ApiError(409, "conflict", f"overlaps booking {bid}")
    booking_seq += 1
    bid = f"b{booking_seq}"
    b = {"id": bid, "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
         "title": title, "attendees": att}
    bookings[bid] = b
    booking_dt[bid] = (start, end)
    if key:
        idem[key] = (canon, 201, dict(b))
    return 201, b


def h_list_bookings(req, m, q):
    room_id = q.get("room_id")
    frm = q.get("from")
    to = q.get("to")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    if frm_dt and to_dt and frm_dt >= to_dt:
        raise verr("from must be before to")
    limit_s = q.get("limit")
    limit = 20
    if limit_s is not None:
        if not re.fullmatch(r"[0-9]+", limit_s):
            raise verr("limit must be an integer from 1 to 100")
        limit = int(limit_s)
        if not (1 <= limit <= 100):
            raise verr("limit must be an integer from 1 to 100")
    cursor = q.get("cursor")
    ckey = decode_cursor(cursor) if cursor is not None else None
    items = []
    for bid, b in bookings.items():
        if room_id is not None and b["room_id"] != room_id:
            continue
        s, e = booking_dt[bid]
        if to_dt is not None and not (s < to_dt):
            continue
        if frm_dt is not None and not (e > frm_dt):
            continue
        items.append(b)
    items.sort(key=bkey)
    if ckey is not None:
        items = [b for b in items if bkey(b) > ckey]
    page = items[:limit]
    nxt = encode_cursor(bkey(page[-1])) if len(items) > limit else None
    return 200, {"bookings": page, "next_cursor": nxt}


def h_get_booking(req, m, q):
    b = bookings.get(m[0])
    if b is None:
        raise not_found("booking not found")
    return 200, b


def h_delete_booking(req, m, q):
    bid = m[0]
    if bid not in bookings:
        raise not_found("booking not found")
    del bookings[bid]
    del booking_dt[bid]
    return 204, None


ROUTES = [
    (re.compile(r"^/rooms$"), {"GET": h_list_rooms, "POST": h_create_room}),
    (re.compile(r"^/rooms/([^/]+)$"), {"GET": h_get_room}),
    (re.compile(r"^/rooms/([^/]+)/availability$"), {"GET": h_availability}),
    (re.compile(r"^/bookings$"), {"GET": h_list_bookings, "POST": h_create_booking}),
    (re.compile(r"^/bookings/([^/]+)$"), {"GET": h_get_booking, "DELETE": h_delete_booking}),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, *args):
        pass

    def read_json_body(self):
        if isinstance(self._parsed, ApiError):
            raise self._parsed
        return self._parsed[0]

    def _read_json_body(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length is not None else 0
        except ValueError:
            n = 0
        if n <= 0:
            raise ApiError(400, "bad_json", "request body must be JSON")
        raw = self.rfile.read(n)
        self._body_read = True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(400, "bad_json", "request body is not valid UTF-8")

        def reject(c):
            raise ValueError(c)
        try:
            return json.loads(text, parse_constant=reject)
        except (ValueError, RecursionError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")

    def _drain(self):
        if getattr(self, "_body_read", False):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > 0:
            self.rfile.read(n)
        self._body_read = True

    def send_json(self, status, obj):
        self._drain()
        if status == 204:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def dispatch(self, method):
        self._body_read = False
        self._parsed = ApiError(400, "bad_json", "request body must be JSON")
        try:
            parts = urlsplit(self.path)
            path = parts.path
            q = {}
            for k, vs in parse_qs(parts.query, keep_blank_values=True).items():
                q[k] = vs[0]
            for pat, methods in ROUTES:
                mt = pat.match(path)
                if mt:
                    lookup = "GET" if method == "HEAD" else method
                    fn = methods.get(lookup)
                    if fn is None:
                        raise ApiError(405, "method_not_allowed",
                                       f"method {method} not allowed on {path}")
                    if lookup == "POST":
                        try:
                            self._parsed = (self._read_json_body(),)
                        except ApiError as pe:
                            self._parsed = pe
                    with LOCK:
                        status, obj = fn(self, mt.groups(), q)
                    self.send_json(status, obj)
                    return
            raise not_found(f"no route for {path}")
        except ApiError as e:
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception as e:  # noqa: BLE001
            try:
                self.send_json(500, {"error": {"code": "internal_error",
                                               "message": f"internal error: {type(e).__name__}"}})
            except Exception:
                pass

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

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")


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
