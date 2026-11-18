#!/usr/bin/env python3
"""bookings: meeting room booking API (standard library only)."""
import argparse
import base64
import json
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

STATUS = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
}

STATE_LOCK = threading.Lock()


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = STATUS[code]
        self.message = message


def verr(msg):
    return ApiError("validation_error", msg)


# ---------- pure helpers ----------
TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def is_int(v):
    return type(v) is int


def parse_ts(s):
    if not isinstance(s, str) or not TS_RE.match(s):
        return None
    try:
        return datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]),
                        int(s[11:13]), int(s[14:16]), int(s[17:19]),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_ts(dt):
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (dt.year, dt.month, dt.day,
                                               dt.hour, dt.minute, dt.second)


def parse_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return None
    try:
        return datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), tzinfo=timezone.utc)
    except ValueError:
        return None


def validate_room_payload(obj):
    name = obj.get("name")
    cap = obj.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("name must be a non-blank string of at most 50 characters")
    if not is_int(cap) or cap < 1 or cap > 500:
        raise verr("capacity must be an integer between 1 and 500")
    return name, cap


def validate_booking_payload(obj):
    room_id = obj.get("room_id")
    if not isinstance(room_id, str):
        raise verr("room_id must be a string")
    times = []
    for f in ("start", "end"):
        dt = parse_ts(obj.get(f))
        if dt is None:
            raise verr("%s must be a timestamp like 2026-03-02T09:30:00Z" % f)
        if dt.second != 0 or dt.minute not in (0, 15, 30, 45):
            raise verr("%s must fall on a quarter hour" % f)
        times.append(dt)
    start, end = times
    if end <= start:
        raise verr("end must be after start")
    if end - start > timedelta(hours=8):
        raise verr("booking must be at most 8 hours long")
    title = obj.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise verr("title must be a non-blank string of at most 200 characters")
    att = obj.get("attendees")
    if not is_int(att) or att < 1:
        raise verr("attendees must be an integer of at least 1")
    return room_id, start, end, title, att


def encode_cursor(start, num):
    raw = json.dumps([fmt_ts(start), num]).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(c):
    try:
        if not c or not c.isascii():
            raise ValueError
        raw = base64.b64decode(c.encode("ascii"), altchars=b"-_", validate=True)
        v = json.loads(raw.decode("utf-8"))
        if not (isinstance(v, list) and len(v) == 2 and is_int(v[1]) and v[1] >= 0):
            raise ValueError
        st = parse_ts(v[0])
        if st is None:
            raise ValueError
        return st, v[1]
    except Exception:
        raise verr("invalid cursor")


def compute_free(bookings, date):
    """bookings: iterable of (start, end) datetimes; date: aware datetime at midnight."""
    w0 = date + timedelta(hours=8)
    w1 = date + timedelta(hours=18)
    clipped = []
    for s, e in bookings:
        s2, e2 = max(s, w0), min(e, w1)
        if s2 < e2:
            clipped.append((s2, e2))
    clipped.sort()
    free = []
    cur = w0
    for s, e in clipped:
        if s > cur:
            free.append((cur, s))
        cur = max(cur, e)
    if cur < w1:
        free.append((cur, w1))
    return free


# ---------- store ----------
class Store:
    def __init__(self):
        self.rooms = {}
        self.room_names = set()
        self.next_room = 1
        self.bookings = {}
        self.next_booking = 1
        self.idem = {}

    def booking_json(self, b):
        return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
                "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}

    def room_bookings(self, room_id):
        return [b for b in self.bookings.values() if b["room_id"] == room_id]


STORE = Store()


# ---------- handlers ----------
def h_list_rooms(req, m, q, body):
    return 200, {"rooms": [dict(r) for r in STORE.rooms.values()]}


def h_create_room(req, m, q, body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name, cap = validate_room_payload(body)
    if name in STORE.room_names:
        raise ApiError("room_exists", "a room with this name already exists")
    rid = "r%d" % STORE.next_room
    STORE.next_room += 1
    room = {"id": rid, "name": name, "capacity": cap}
    STORE.rooms[rid] = room
    STORE.room_names.add(name)
    return 201, dict(room)


def h_get_room(req, m, q, body):
    r = STORE.rooms.get(m.group(1))
    if r is None:
        raise ApiError("not_found", "room not found")
    return 200, dict(r)


def h_availability(req, m, q, body):
    r = STORE.rooms.get(m.group(1))
    if r is None:
        raise ApiError("not_found", "room not found")
    vals = q.get("date")
    date = parse_date(vals[0]) if vals else None
    if date is None:
        raise verr("date must be a real date of the form YYYY-MM-DD")
    free = compute_free([(b["start"], b["end"]) for b in STORE.room_bookings(r["id"])], date)
    return 200, {"date": vals[0],
                 "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in free]}


def canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def h_create_booking(req, m, q, body):
    key = req.headers.get("Idempotency-Key")
    if key == "":
        key = None
    canonical = None
    if key is not None:
        canonical = canon(body)
        prev = STORE.idem.get(key)
        if prev is not None:
            if prev[0] == canonical:
                return prev[1], json.loads(json.dumps(prev[2]))
            raise ApiError("idempotency_mismatch",
                           "Idempotency-Key was already used with a different request")
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    room_id, start, end, title, att = validate_booking_payload(body)
    room = STORE.rooms.get(room_id)
    if room is None:
        raise ApiError("not_found", "room not found")
    if att > room["capacity"]:
        raise ApiError("over_capacity", "attendees exceed room capacity")
    for b in STORE.room_bookings(room_id):
        if start < b["end"] and b["start"] < end:
            raise ApiError("conflict", "room is already booked in that time range")
    num = STORE.next_booking
    STORE.next_booking += 1
    b = {"id": "b%d" % num, "num": num, "room_id": room_id, "start": start,
         "end": end, "title": title, "attendees": att}
    STORE.bookings[b["id"]] = b
    out = STORE.booking_json(b)
    if key is not None:
        STORE.idem[key] = (canonical, 201, json.loads(json.dumps(out)))
    return 201, out


def h_get_booking(req, m, q, body):
    b = STORE.bookings.get(m.group(1))
    if b is None:
        raise ApiError("not_found", "booking not found")
    return 200, STORE.booking_json(b)


def h_delete_booking(req, m, q, body):
    if STORE.bookings.pop(m.group(1), None) is None:
        raise ApiError("not_found", "booking not found")
    return 204, None


def first(q, name):
    v = q.get(name)
    return v[0] if v else None


def h_list_bookings(req, m, q, body):
    room_id = first(q, "room_id")
    frm = to = None
    if "from" in q:
        frm = parse_ts(first(q, "from"))
        if frm is None:
            raise verr("from must be a timestamp like 2026-03-02T09:30:00Z")
    if "to" in q:
        to = parse_ts(first(q, "to"))
        if to is None:
            raise verr("to must be a timestamp like 2026-03-02T09:30:00Z")
    if frm is not None and to is not None and to <= frm:
        raise verr("to must be after from")
    limit = 20
    if "limit" in q:
        ls = first(q, "limit")
        if not re.fullmatch(r"[0-9]{1,6}", ls or ""):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(ls)
        if limit < 1 or limit > 100:
            raise verr("limit must be an integer between 1 and 100")
    cur = None
    if "cursor" in q:
        cur = decode_cursor(first(q, "cursor"))
    items = []
    if room_id is None or room_id in STORE.rooms:
        for b in STORE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if to is not None and not b["start"] < to:
                continue
            if frm is not None and not b["end"] > frm:
                continue
            if cur is not None and not (b["start"], b["num"]) > cur:
                continue
            items.append(b)
    items.sort(key=lambda b: (b["start"], b["num"]))
    page = items[:limit]
    nxt = None
    if len(items) > limit:
        nxt = encode_cursor(page[-1]["start"], page[-1]["num"])
    return 200, {"bookings": [STORE.booking_json(b) for b in page], "next_cursor": nxt}


ROUTES = [
    (re.compile(r"/rooms"), {"GET": h_list_rooms, "POST": h_create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": h_get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": h_availability}),
    (re.compile(r"/bookings"), {"GET": h_list_bookings, "POST": h_create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": h_get_booking, "DELETE": h_delete_booking}),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        try:
            if obj is None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                data = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _error(self, status, code, message):
        self._send(status, {"error": {"code": code, "message": message}})

    def _handle(self):
        try:
            target = self.path
            target = target.split("#", 1)[0]
            path, _, query = target.partition("?")
            q = parse_qs(query, keep_blank_values=True)
            handlers = None
            match = None
            for rx, hs in ROUTES:
                mm = rx.fullmatch(path)
                if mm:
                    handlers, match = hs, mm
                    break
            if handlers is None:
                raise ApiError("not_found", "no such route")
            fn = handlers.get(self.command)
            if fn is None:
                raise ApiError("method_not_allowed", "method not allowed on this path")
            body = None
            if self.command == "POST":
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = 0
                raw = self.rfile.read(n) if n > 0 else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    raise ApiError("bad_json", "request body is not valid JSON")
            with STATE_LOCK:
                status, out = fn(self, match, q, body)
            self._send(status, out)
        except ApiError as e:
            self._error(e.status, e.code, e.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._error(500, "internal_error", "internal server error")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
