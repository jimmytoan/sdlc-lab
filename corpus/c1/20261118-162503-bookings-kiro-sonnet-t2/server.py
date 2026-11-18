#!/usr/bin/env python3
"""Meeting room booking API. Standard library only, in-memory state."""
import argparse
import base64
import copy
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

UTC = timezone.utc
STATUS = {
    "not_found": 404, "method_not_allowed": 405, "bad_json": 400,
    "validation_error": 422, "over_capacity": 422, "room_exists": 409,
    "conflict": 409, "idempotency_mismatch": 422, "internal_error": 500,
}


class ApiError(Exception):
    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.status = STATUS[code]
        self.code = code
        self.message = message
        self.headers = headers or {}


def render_error(err):
    return {"error": {"code": err.code, "message": err.message}}


def validation(msg):
    return ApiError("validation_error", msg)


def not_found(msg="not found"):
    return ApiError("not_found", msg)


# ---------------------------------------------------------------- helpers
def is_int(v):
    return type(v) is int


def _bad_const(c):
    raise ValueError(c)


def parse_json_object(raw):
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text, parse_constant=_bad_const)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    return obj


TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z", re.ASCII)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)


def parse_ts(s, aligned, name="timestamp"):
    m = TS_RE.fullmatch(s) if isinstance(s, str) else None
    if not m:
        raise validation(f"{name} must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        dt = datetime(*map(int, m.groups()), tzinfo=UTC)
    except ValueError:
        raise validation(f"{name} is not a real date/time")
    if aligned and (dt.second != 0 or dt.minute % 15 != 0):
        raise validation(f"{name} must be on a quarter hour")
    return dt


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(s):
    m = DATE_RE.fullmatch(s) if isinstance(s, str) else None
    if not m:
        raise validation("date must be YYYY-MM-DD")
    try:
        return date(*map(int, m.groups()))
    except ValueError:
        raise validation("date is not a real date")


def free_slots(bookings, win_start, win_end):
    """bookings: iterable of (start, end). Returns list of (start, end)."""
    clipped = []
    for s, e in bookings:
        if s < win_end and e > win_start:
            clipped.append((max(s, win_start), min(e, win_end)))
    clipped.sort()
    out = []
    t = win_start
    for s, e in clipped:
        if s > t:
            out.append((t, s))
        t = max(t, e)
    if t < win_end:
        out.append((t, win_end))
    return out


def encode_cursor(b):
    raw = json.dumps([fmt_ts(b.start), fmt_ts(b.end), b.seq]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", c):
            raise ValueError
        raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4))
        v = json.loads(raw.decode())
        if not (isinstance(v, list) and len(v) == 3 and is_int(v[2]) and v[2] >= 1):
            raise ValueError
        s = parse_ts(v[0], False)
        e = parse_ts(v[1], False)
        return (s, e, v[2])
    except (ValueError, ApiError, UnicodeDecodeError):
        raise validation("invalid cursor")


# ------------------------------------------------------------------ store
class Room:
    def __init__(self, id, name, capacity):
        self.id, self.name, self.capacity = id, name, capacity

    def to_json(self):
        return {"id": self.id, "name": self.name, "capacity": self.capacity}


class Booking:
    def __init__(self, id, seq, room_id, start, end, title, attendees):
        self.id, self.seq, self.room_id = id, seq, room_id
        self.start, self.end, self.title, self.attendees = start, end, title, attendees

    def to_json(self):
        return {"id": self.id, "room_id": self.room_id, "start": fmt_ts(self.start),
                "end": fmt_ts(self.end), "title": self.title, "attendees": self.attendees}


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}
        self.room_names = set()
        self.bookings = {}
        self.room_bookings = {}
        self.next_room = 1
        self.next_booking = 1
        self.idem = {}


store = Store()


class Req:
    def __init__(self, method, params, query, headers, raw_body):
        self.method, self.params, self.query = method, params, query
        self.headers, self.raw_body = headers, raw_body


# -------------------------------------------------------------- endpoints
def create_room(req):
    body = parse_json_object(req.raw_body)
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise validation("name must be a non-blank string of at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise validation("capacity must be an integer from 1 to 500")
    if name in store.room_names:
        raise ApiError("room_exists", "a room with this name already exists")
    room = Room(f"r{store.next_room}", name, cap)
    store.next_room += 1
    store.rooms[room.id] = room
    store.room_names.add(name)
    store.room_bookings[room.id] = []
    return 201, room.to_json(), {}


def list_rooms(req):
    return 200, {"rooms": [r.to_json() for r in store.rooms.values()]}, {}


def get_room(req):
    room = store.rooms.get(req.params[0])
    if room is None:
        raise not_found("room not found")
    return 200, room.to_json(), {}


def _create_booking_core(body):
    room_id, title, att = body.get("room_id"), body.get("title"), body.get("attendees")
    if not isinstance(room_id, str):
        raise validation("room_id must be a string")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise validation("title must be a non-blank string of at most 200 characters")
    if not is_int(att) or att < 1:
        raise validation("attendees must be an integer >= 1")
    start = parse_ts(body.get("start"), True, "start")
    end = parse_ts(body.get("end"), True, "end")
    if end <= start:
        raise validation("end must be after start")
    if end - start > timedelta(hours=8):
        raise validation("booking may not exceed 8 hours")
    room = store.rooms.get(room_id)
    if room is None:
        raise not_found("room not found")
    if att > room.capacity:
        raise ApiError("over_capacity", "attendees exceed room capacity")
    for b in store.room_bookings[room_id]:
        if start < b.end and b.start < end:
            raise ApiError("conflict", "room is already booked for this time")
    seq = store.next_booking
    store.next_booking += 1
    b = Booking(f"b{seq}", seq, room_id, start, end, title, att)
    store.bookings[b.id] = b
    store.room_bookings[room_id].append(b)
    return 201, b.to_json()


def create_booking(req):
    body = parse_json_object(req.raw_body)
    key = req.headers.get("Idempotency-Key")
    fp = None
    if key:
        fp = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        rec = store.idem.get(key)
        if rec is not None:
            if rec[0] != fp:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was used with a different request body")
            return rec[1], copy.deepcopy(rec[2]), {}
    try:
        status, out = _create_booking_core(body)
    except ApiError as e:
        if key and e.code in ("validation_error", "not_found", "over_capacity", "conflict"):
            store.idem[key] = (fp, e.status, render_error(e))
        raise
    if key:
        store.idem[key] = (fp, status, copy.deepcopy(out))
    return status, out, {}


def get_booking(req):
    b = store.bookings.get(req.params[0])
    if b is None:
        raise not_found("booking not found")
    return 200, b.to_json(), {}


def delete_booking(req):
    b = store.bookings.pop(req.params[0], None)
    if b is None:
        raise not_found("booking not found")
    store.room_bookings[b.room_id].remove(b)
    return 204, None, {}


def list_bookings(req):
    q = req.query
    for k in ("limit", "from", "to", "cursor", "room_id"):
        if k in q and q[k] == "":
            raise validation(f"{k} must not be empty")
    limit = 20
    if "limit" in q:
        if not re.fullmatch(r"[0-9]+", q["limit"], re.ASCII):
            raise validation("limit must be an integer")
        limit = int(q["limit"])
        if not 1 <= limit <= 100:
            raise validation("limit must be between 1 and 100")
    lo = parse_ts(q["from"], False, "from") if "from" in q else None
    hi = parse_ts(q["to"], False, "to") if "to" in q else None
    if lo is not None and hi is not None and hi <= lo:
        raise validation("to must be after from")
    cur = decode_cursor(q["cursor"]) if "cursor" in q else None
    items = []
    for b in store.bookings.values():
        if "room_id" in q and b.room_id != q["room_id"]:
            continue
        if hi is not None and not b.start < hi:
            continue
        if lo is not None and not b.end > lo:
            continue
        if cur is not None and not (b.start, b.end, b.seq) > cur:
            continue
        items.append(b)
    items.sort(key=lambda b: (b.start, b.end, b.seq))
    page = items[:limit]
    nxt = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [b.to_json() for b in page], "next_cursor": nxt}, {}


def availability(req):
    room = store.rooms.get(req.params[0])
    if room is None:
        raise not_found("room not found")
    d = parse_date(req.query.get("date"))
    ws = datetime(d.year, d.month, d.day, 8, tzinfo=UTC)
    we = datetime(d.year, d.month, d.day, 18, tzinfo=UTC)
    slots = free_slots([(b.start, b.end) for b in store.room_bookings[room.id]], ws, we)
    return 200, {"date": d.isoformat(),
                 "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in slots]}, {}


ROUTES = [
    (re.compile(r"/rooms"), {"GET": list_rooms, "POST": create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": availability}),
    (re.compile(r"/bookings"), {"GET": list_bookings, "POST": create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": get_booking, "DELETE": delete_booking}),
]


def resolve(path, method):
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    for rx, methods in ROUTES:
        m = rx.fullmatch(path)
        if m:
            fn = methods.get(method)
            if fn is None:
                raise ApiError("method_not_allowed", "method not allowed",
                               {"Allow": ", ".join(sorted(methods))})
            return fn, m.groups()
    raise not_found("no such route")


def handle(method, target, headers, raw_body):
    """Returns (status, body_or_None, extra_headers)."""
    try:
        path, _, qs = target.partition("?")
        fn, params = resolve(path, method)
        query = {}
        for k, v in parse_qsl(qs, keep_blank_values=True):
            query.setdefault(k, v)
        req = Req(method, params, query, headers, raw_body)
        with store.lock:
            return fn(req)
    except ApiError as e:
        return e.status, render_error(e), e.headers
    except Exception:
        err = ApiError("internal_error", "internal server error")
        return err.status, render_error(err), {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _dispatch(self):
        try:
            raw = b""
            cl = self.headers.get("Content-Length")
            if cl is not None and cl.strip().isdigit():
                raw = self.rfile.read(int(cl))
            status, body, extra = handle(self.command, self.path, self.headers, raw)
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and data:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--port")
    args, _ = p.parse_known_args(argv)
    if args.port is None or not re.fullmatch(r"[0-9]+", args.port, re.ASCII) \
            or int(args.port) > 65535:
        print("error: --port must be an integer from 0 to 65535", file=sys.stderr)
        sys.exit(2)
    ThreadingHTTPServer.daemon_threads = True
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", int(args.port)), Handler)
    except OSError as e:
        print(f"error: cannot bind: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
