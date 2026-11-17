"""Meeting room booking API. Standard library only, in-memory state."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone, date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

LOCK = threading.Lock()
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^[0-9]+$")


class ApiError(Exception):
    STATUS = {
        "not_found": 404, "method_not_allowed": 405, "bad_json": 400,
        "validation_error": 422, "over_capacity": 422, "room_exists": 409,
        "conflict": 409, "idempotency_mismatch": 422,
    }

    def __init__(self, code, message, allow=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]
        self.allow = allow


def verr(msg):
    return ApiError("validation_error", msg)


class Store:
    def __init__(self):
        self.rooms = {}          # id -> room dict
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict
        self.booking_seq = 0
        self.idem = {}           # key -> (canonical body, response body)
        self.cursors = set()


STORE = Store()


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        raise verr(f"{field} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")
    return dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def booking_key(b):
    return (b["_start"], b["_num"])


def public_booking(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


# ---------------------------------------------------------------- handlers

def create_room(body):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str):
        raise verr("name must be a string")
    name = name.strip()
    if not name:
        raise verr("name must not be empty")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(cap):
        raise verr("capacity must be an integer")
    if cap < 1 or cap > 500:
        raise verr("capacity must be between 1 and 500")
    folded = name.casefold()
    for r in STORE.rooms.values():
        if r["name"].casefold() == folded:
            raise ApiError("room_exists", "a room with this name already exists")
    STORE.room_seq += 1
    rid = f"r{STORE.room_seq}"
    room = {"id": rid, "name": name, "capacity": cap}
    STORE.rooms[rid] = room
    return 201, dict(room)


def get_room(rid):
    room = STORE.rooms.get(rid)
    if room is None:
        raise ApiError("not_found", "room not found")
    return room


def validate_booking(body):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.second != 0 or dt.minute % 15 != 0:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > timedelta(hours=8):
        raise verr("booking must be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    if len(title) > 100:
        raise verr("title must be at most 100 characters")
    att = body.get("attendees")
    if not is_int(att):
        raise verr("attendees must be an integer")
    if att < 1:
        raise verr("attendees must be at least 1")
    return room_id, start, end, title, att


def create_booking(body, idem_key):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "body must be a JSON object")
    canon = canonical(body)
    if idem_key is not None and idem_key in STORE.idem:
        stored_canon, resp = STORE.idem[idem_key]
        if stored_canon != canon:
            raise ApiError("idempotency_mismatch",
                           "Idempotency-Key was already used for a different request")
        return 201, dict(resp)
    room_id, start, end, title, att = validate_booking(body)
    room = STORE.rooms.get(room_id)
    if room is None:
        raise ApiError("not_found", "room not found")
    if att > room["capacity"]:
        raise ApiError("over_capacity", "more attendees than the room holds")
    for b in STORE.bookings.values():
        if b["room_id"] == room_id and b["_start"] < end and b["_end"] > start:
            raise ApiError("conflict", f"overlaps booking {b['id']}")
    STORE.booking_seq += 1
    n = STORE.booking_seq
    b = {"id": f"b{n}", "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
         "title": title, "attendees": att, "_start": start, "_end": end, "_num": n}
    STORE.bookings[b["id"]] = b
    resp = public_booking(b)
    if idem_key is not None:
        STORE.idem[idem_key] = (canon, dict(resp))
    return 201, resp


def encode_cursor(b):
    raw = f"{fmt_ts(b['_start'])}|{b['_num']}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(c):
    if c not in STORE.cursors:
        raise verr("unknown cursor")
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode(c + pad).decode()
        s, n = raw.split("|")
        return (parse_ts(s, "cursor"), int(n))
    except Exception:
        raise verr("malformed cursor")


def single(qs, name):
    vals = qs.get(name)
    if vals is None:
        return None
    return vals[-1]


def list_bookings(qs):
    room_id = single(qs, "room_id")
    frm = single(qs, "from")
    to = single(qs, "to")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    limit_s = single(qs, "limit")
    if limit_s is None:
        limit = 20
    else:
        if not INT_RE.match(limit_s):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(limit_s)
        if limit < 1 or limit > 100:
            raise verr("limit must be between 1 and 100")
    cursor = single(qs, "cursor")
    after = decode_cursor(cursor) if cursor is not None else None
    items = []
    for b in STORE.bookings.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm_dt is not None and not (b["_end"] > frm_dt):
            continue
        if to_dt is not None and not (b["_start"] < to_dt):
            continue
        if after is not None and not (booking_key(b) > after):
            continue
        items.append(b)
    items.sort(key=booking_key)
    page = items[:limit]
    next_cursor = None
    if len(items) > limit:
        next_cursor = encode_cursor(page[-1])
        STORE.cursors.add(next_cursor)
    return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


def get_booking(bid):
    b = STORE.bookings.get(bid)
    if b is None:
        raise ApiError("not_found", "booking not found")
    return 200, public_booking(b)


def delete_booking(bid):
    if bid not in STORE.bookings:
        raise ApiError("not_found", "booking not found")
    del STORE.bookings[bid]
    return 204, None


def availability(rid, qs):
    get_room(rid)
    d = single(qs, "date")
    if d is None or not DATE_RE.match(d):
        raise verr("date must be given as YYYY-MM-DD")
    try:
        day = date_cls.fromisoformat(d)
    except ValueError:
        raise verr("date is not a valid date")
    ws = datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc)
    we = datetime(day.year, day.month, day.day, 18, tzinfo=timezone.utc)
    busy = []
    for b in STORE.bookings.values():
        if b["room_id"] != rid:
            continue
        s, e = max(b["_start"], ws), min(b["_end"], we)
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
    return 200, {"date": d, "free": free}


# ---------------------------------------------------------------- routing

def route(path):
    """Return (allowed_methods, handler_factory) or raise not_found."""
    parts = path.split("/")
    # path starts with '/', so parts[0] == ''
    if len(parts) < 2 or parts[0] != "":
        raise ApiError("not_found", "no such resource")
    segs = parts[1:]
    if segs == ["rooms"]:
        return "rooms", None
    if len(segs) == 2 and segs[0] == "rooms" and segs[1]:
        return "room", segs[1]
    if len(segs) == 3 and segs[0] == "rooms" and segs[1] and segs[2] == "availability":
        return "availability", segs[1]
    if segs == ["bookings"]:
        return "bookings", None
    if len(segs) == 2 and segs[0] == "bookings" and segs[1]:
        return "booking", segs[1]
    raise ApiError("not_found", "no such resource")


ALLOWED = {
    "rooms": ("GET", "POST"),
    "room": ("GET",),
    "availability": ("GET",),
    "bookings": ("GET", "POST"),
    "booking": ("GET", "DELETE"),
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, extra_headers=None):
        if status == 204 or body is None:
            data = b""
        else:
            data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _read_raw(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _json_body(self, raw):
        def bad_const(c):
            raise ValueError(c)
        try:
            text = raw.decode("utf-8")
            return json.loads(text, parse_constant=bad_const)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError("bad_json", "request body is not valid JSON")

    def _handle(self):
        raw = self._read_raw()
        try:
            parts = urlsplit(self.path)
            path = parts.path
            qs = parse_qs(parts.query, keep_blank_values=True)
            kind, ident = route(path)
            method = self.command
            if method not in ALLOWED[kind]:
                raise ApiError("method_not_allowed", f"{method} not allowed",
                               allow=", ".join(ALLOWED[kind]))
            with LOCK:
                if kind == "rooms":
                    if method == "GET":
                        status, body = 200, {"rooms": [dict(r) for r in STORE.rooms.values()]}
                    else:
                        status, body = create_room(self._json_body(raw))
                elif kind == "room":
                    status, body = 200, dict(get_room(ident))
                elif kind == "availability":
                    status, body = availability(ident, qs)
                elif kind == "bookings":
                    if method == "GET":
                        status, body = list_bookings(qs)
                    else:
                        key = self.headers.get("Idempotency-Key")
                        if key is not None and key == "":
                            key = None
                        status, body = create_booking(self._json_body(raw), key)
                else:
                    if method == "GET":
                        status, body = get_booking(ident)
                    else:
                        status, body = delete_booking(ident)
            self._send(status, body)
        except ApiError as e:
            hdrs = {"Allow": e.allow} if e.allow else None
            self._send(e.status, {"error": {"code": e.code, "message": e.message}}, hdrs)
        except Exception as e:  # pragma: no cover
            self._send(500, {"error": {"code": "internal", "message": str(e)}})

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _handle


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
