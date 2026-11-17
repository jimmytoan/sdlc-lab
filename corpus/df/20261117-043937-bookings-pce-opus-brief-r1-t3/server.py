"""bookings: meeting room booking API (standard library only)."""
import argparse
import base64
import binascii
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOCK = threading.Lock()
rooms = {}            # id -> room dict
room_names = set()
next_room_n = 1
bookings = {}         # id -> public booking dict
booking_meta = {}     # id -> (start_dt, end_dt, n)
next_booking_n = 1
idem = {}             # key -> (fingerprint, status, body)

TS_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z\Z")
DATE_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})\Z")
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


def parse_ts(s, field):
    if not isinstance(s, str):
        raise verr(f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(s)
    if not m:
        raise verr(f"{field} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid date-time")


def fmt_ts(dt):
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_date(s):
    if not isinstance(s, str):
        raise verr("date is required (YYYY-MM-DD)")
    m = DATE_RE.match(s)
    if not m:
        raise verr("date must be YYYY-MM-DD")
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError:
        raise verr("date is not a valid calendar date")


def is_int(x):
    return type(x) is int


def booking_n(bid):
    return booking_meta[bid][2]


def encode_cursor(start_dt, n):
    raw = f"{fmt_ts(start_dt)}|{n}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode((c + pad).encode("ascii")).decode("utf-8")
        ts, n = raw.split("|")
        if not re.fullmatch(r"[1-9][0-9]*", n):
            raise ValueError
        m = TS_RE.match(ts)
        if not m:
            raise ValueError
        dt = datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
        return (dt, int(n))
    except (ValueError, binascii.Error, UnicodeError):
        raise verr("cursor is invalid")


# ---------------------------------------------------------------- handlers

def h_list_rooms(req, params, query):
    with LOCK:
        return 200, {"rooms": [dict(r) for r in rooms.values()]}


def h_create_room(req, params, query):
    global next_room_n
    body = req.parse_body()
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise verr("name must be a string")
    if not name.strip():
        raise verr("name must not be empty")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(capacity):
        raise verr("capacity must be an integer")
    if capacity < 1 or capacity > 500:
        raise verr("capacity must be between 1 and 500")
    with LOCK:
        if name in room_names:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        rid = f"r{next_room_n}"
        next_room_n += 1
        room = {"id": rid, "name": name, "capacity": capacity}
        rooms[rid] = room
        room_names.add(name)
        return 201, dict(room)


def h_get_room(req, params, query):
    with LOCK:
        room = rooms.get(params[0])
        if room is None:
            raise not_found("room not found")
        return 200, dict(room)


def h_availability(req, params, query):
    with LOCK:
        room = rooms.get(params[0])
        if room is None:
            raise not_found("room not found")
        dvals = query.get("date")
        dstr = dvals[0] if dvals else None
        d = parse_date(dstr)
        ws = datetime(d.year, d.month, d.day, 8, 0, 0, tzinfo=timezone.utc)
        we = datetime(d.year, d.month, d.day, 18, 0, 0, tzinfo=timezone.utc)
        busy = []
        for bid, b in bookings.items():
            if b["room_id"] != room["id"]:
                continue
            s, e, _ = booking_meta[bid]
            if s < we and e > ws:
                busy.append((max(s, ws), min(e, we)))
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
        return 200, {"date": dstr, "free": free}


def fingerprint(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def h_create_booking(req, params, query):
    global next_booking_n
    body = req.parse_body(require_object=False)
    key = req.headers.get("Idempotency-Key")
    with LOCK:
        fp = None
        if key:
            fp = fingerprint(body)
            if key in idem:
                stored_fp, status, resp = idem[key]
                if stored_fp != fp:
                    raise ApiError(422, "idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return status, dict(resp)
        if not isinstance(body, dict):
            raise verr("request body must be a JSON object")
        room_id = body.get("room_id")
        if not isinstance(room_id, str):
            raise verr("room_id must be a string")
        start = parse_ts(body.get("start"), "start")
        end = parse_ts(body.get("end"), "end")
        for dt, field in ((start, "start"), (end, "end")):
            if dt.minute % 15 != 0 or dt.second != 0:
                raise verr(f"{field} must be on a quarter hour")
        if end <= start:
            raise verr("end must be after start")
        if end - start > MAX_DURATION:
            raise verr("bookings can be at most 8 hours long")
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            raise verr("title must be a non-empty string")
        attendees = body.get("attendees")
        if not is_int(attendees) or attendees < 1:
            raise verr("attendees must be a positive integer")
        room = rooms.get(room_id)
        if room is None:
            raise not_found("room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        for bid, b in bookings.items():
            if b["room_id"] != room_id:
                continue
            s, e, _ = booking_meta[bid]
            if start < e and s < end:
                raise ApiError(409, "conflict", f"room is already booked ({bid})")
        n = next_booking_n
        next_booking_n += 1
        bid = f"b{n}"
        booking = {"id": bid, "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
                   "title": title, "attendees": attendees}
        bookings[bid] = booking
        booking_meta[bid] = (start, end, n)
        if key:
            idem[key] = (fp, 201, dict(booking))
        return 201, dict(booking)


def h_get_booking(req, params, query):
    with LOCK:
        b = bookings.get(params[0])
        if b is None:
            raise not_found("booking not found")
        return 200, dict(b)


def h_delete_booking(req, params, query):
    with LOCK:
        bid = params[0]
        if bid not in bookings:
            raise not_found("booking not found")
        del bookings[bid]
        del booking_meta[bid]
        return 204, None


def h_list_bookings(req, params, query):
    def q(name):
        v = query.get(name)
        return v[0] if v else None

    room_id = q("room_id")
    frm = q("from")
    to = q("to")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    if frm_dt is not None and to_dt is not None and frm_dt >= to_dt:
        raise verr("from must be before to")
    limit_s = q("limit")
    if limit_s is None:
        limit = 20
    else:
        if not re.fullmatch(r"[0-9]+", limit_s):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(limit_s)
        if limit < 1 or limit > 100:
            raise verr("limit must be an integer between 1 and 100")
    cursor = q("cursor")
    after = decode_cursor(cursor) if cursor is not None else None
    with LOCK:
        items = []
        for bid, b in bookings.items():
            s, e, n = booking_meta[bid]
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm_dt is not None and not (e > frm_dt):
                continue
            if to_dt is not None and not (s < to_dt):
                continue
            if after is not None and not ((s, n) > after):
                continue
            items.append(((s, n), b))
        items.sort(key=lambda x: x[0])
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last_key = page[-1][0]
            next_cursor = encode_cursor(*last_key)
        return 200, {"bookings": [dict(b) for _, b in page], "next_cursor": next_cursor}


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

    def log_message(self, format, *args):
        pass

    def send_json(self, status, obj, extra_headers=None):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error_json(self, status, code, message, extra_headers=None):
        self.send_json(status, {"error": {"code": code, "message": message}}, extra_headers)

    def send_empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def parse_body(self, require_object=True):
        raw = self._body
        if not raw:
            raise ApiError(400, "bad_json", "request body must be valid JSON")
        try:
            text = raw.decode("utf-8")
            body = json.loads(text)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError(400, "bad_json", "request body must be valid JSON")
        if require_object and not isinstance(body, dict):
            raise verr("request body must be a JSON object")
        return body

    def read_body(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length is not None else 0
        except ValueError:
            n = 0
        if n < 0:
            n = 0
        return self.rfile.read(n) if n else b""

    def dispatch(self, method):
        try:
            self._body = self.read_body()
            parts = urlsplit(self.path)
            path = parts.path
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            query = parse_qs(parts.query, keep_blank_values=True)
            for pattern, methods in ROUTES:
                m = pattern.match(path)
                if m:
                    handler = methods.get(method)
                    if handler is None:
                        raise ApiError(405, "method_not_allowed",
                                       f"method {method} not allowed",)
                    status, obj = handler(self, m.groups(), query)
                    if status == 204:
                        self.send_empty(204)
                    else:
                        self.send_json(status, obj)
                    return
            raise not_found("no such resource")
        except ApiError as e:
            extra = None
            if e.status == 405:
                for pattern, methods in ROUTES:
                    if pattern.match(path):
                        extra = {"Allow": ", ".join(methods)}
                        break
            self.send_error_json(e.status, e.code, e.message, extra)
        except Exception:
            try:
                self.send_error_json(500, "internal_error", "internal server error")
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

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")

    def __getattr__(self, name):
        # Any other HTTP method (TRACE, CONNECT, custom verbs, ...) is routed
        # through dispatch so it gets a JSON 404/405 instead of an HTML 501.
        if name.startswith("do_"):
            method = name[3:]
            return lambda: self.dispatch(method)
        raise AttributeError(name)

    def do_HEAD(self):
        self.dispatch("HEAD")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
