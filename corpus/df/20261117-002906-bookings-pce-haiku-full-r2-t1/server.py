"""bookings: in-memory meeting room booking API (standard library only).

Run with: python server.py --port <N>
"""
import argparse
import base64
import binascii
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)
MIN_FREE = timedelta(minutes=15)

_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_INT_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message or code


def validation(message):
    return ApiError(422, "validation_error", message)


def fmt_ts(dt):
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_ts(s):
    """Strict canonical timestamp parse. Returns naive datetime (UTC) or None."""
    if not isinstance(s, str):
        return None
    m = _TS_RE.fullmatch(s)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def canon(v):
    """Type-aware canonical form of parsed JSON, so that true != 1 and 2.0 != 2."""
    if isinstance(v, dict):
        return ("o", tuple(sorted((k, canon(x)) for k, x in v.items())))
    if isinstance(v, list):
        return ("a", tuple(canon(x) for x in v))
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, int):
        return ("i", v)
    if isinstance(v, float):
        return ("f", repr(v))
    if isinstance(v, str):
        return ("s", v)
    if v is None:
        return ("n",)
    return ("?", repr(v))


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict(id, name, capacity, buffer_minutes)
        self.room_order = []     # room ids in creation order
        self.room_names = set()  # casefolded trimmed names
        self.room_seq = 0
        self.bookings = {}       # numeric n -> dict
        self.booking_seq = 0
        self.idem = {}           # key -> (canon body, response dict)


STATE = State()


def booking_view(n, b):
    return {
        "id": f"b{n}",
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def encode_cursor(key):
    start, n = key
    raw = f"{fmt_ts(start)}|{n}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(s):
    """Strict decode: only a string exactly as encode_cursor produced is accepted."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", s):
        raise validation("invalid cursor")
    try:
        pad = "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode((s + pad).encode("ascii"))
        text = raw.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        raise validation("invalid cursor")
    parts = text.split("|")
    if len(parts) != 2 or not _INT_RE.fullmatch(parts[1]) or parts[1][0] == "0":
        raise validation("invalid cursor")
    start = parse_ts(parts[0])
    if start is None:
        raise validation("invalid cursor")
    key = (start, int(parts[1]))
    # Round-trip check rejects any non-canonical spelling (extra padding, alternate encodings).
    if encode_cursor(key) != s:
        raise validation("invalid cursor")
    return key


# ---------- handlers (called with STATE.lock held) ----------

def create_room(body):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise validation("name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise validation("name must be 1 to 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    buffer_minutes = 0
    if "buffer_minutes" in body:
        buffer_minutes = body["buffer_minutes"]
        if not is_int(buffer_minutes) or not (0 <= buffer_minutes <= 60):
            raise validation("buffer_minutes must be an integer from 0 to 60")
    key = name.casefold()
    if key in STATE.room_names:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    STATE.room_seq += 1
    rid = f"r{STATE.room_seq}"
    room = {"id": rid, "name": name, "capacity": capacity,
            "buffer_minutes": buffer_minutes}
    STATE.rooms[rid] = room
    STATE.room_order.append(rid)
    STATE.room_names.add(key)
    return 201, room


def list_rooms():
    return 200, {"rooms": [STATE.rooms[r] for r in STATE.room_order]}


def get_room(rid):
    room = STATE.rooms.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return 200, room


def validate_booking(body):
    room_id = body.get("room_id")
    start_s = body.get("start")
    end_s = body.get("end")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str):
        raise validation("room_id must be a string")
    if not isinstance(start_s, str) or not isinstance(end_s, str):
        raise validation("start and end must be timestamp strings")
    if not isinstance(title, str):
        raise validation("title must be a string")
    if not is_int(attendees):
        raise validation("attendees must be an integer")
    start = parse_ts(start_s)
    end = parse_ts(end_s)
    if start is None or end is None:
        raise validation("start and end must be YYYY-MM-DDTHH:MM:SSZ timestamps")
    for dt in (start, end):
        if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
            raise validation("start and end must fall on quarter hours")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_BOOKING:
        raise validation("booking may be at most 8 hours long")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise validation("title must be 1 to 100 characters")
    if attendees < 1:
        raise validation("attendees must be at least 1")
    return room_id, start, end, title, attendees


def has_conflict(room_id, start, end, buffer, exclude_n=None):
    """True if [start, end) plus `buffer` minutes on each side collides with another
    booking of the room. Gaps smaller than `buffer` are conflicts; a gap equal to
    `buffer` is allowed. Booking `exclude_n` (by id) is skipped."""
    buf = timedelta(minutes=buffer)
    for n, b in STATE.bookings.items():
        if n == exclude_n or b["room_id"] != room_id:
            continue
        if start < b["end"] + buf and b["start"] < end + buf:
            return True
    return False


def create_booking(body, idem_key):
    if idem_key is not None and idem_key in STATE.idem:
        stored_canon, stored_resp = STATE.idem[idem_key]
        if stored_canon == canon(body):
            return 201, stored_resp
        raise ApiError(422, "idempotency_mismatch",
                       "Idempotency-Key was already used with a different body")

    room_id, start, end, title, attendees = validate_booking(body)
    room = STATE.rooms.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    if has_conflict(room_id, start, end, room["buffer_minutes"]):
        raise ApiError(409, "conflict", "booking overlaps an existing booking or buffer")

    STATE.booking_seq += 1
    n = STATE.booking_seq
    rec = {"room_id": room_id, "start": start, "end": end,
           "title": title, "attendees": attendees}
    STATE.bookings[n] = rec
    resp = booking_view(n, rec)
    if idem_key is not None:
        STATE.idem[idem_key] = (canon(body), resp)
    return 201, resp


def get_booking(bid):
    n = parse_booking_id(bid)
    rec = STATE.bookings.get(n) if n is not None else None
    if rec is None:
        raise ApiError(404, "not_found", "booking not found")
    return 200, booking_view(n, rec)


def delete_booking(bid):
    n = parse_booking_id(bid)
    if n is None or n not in STATE.bookings:
        raise ApiError(404, "not_found", "booking not found")
    del STATE.bookings[n]
    return 204, None


def patch_booking(bid, body):
    n = parse_booking_id(bid)
    rec = STATE.bookings.get(n) if n is not None else None
    if rec is None:
        raise ApiError(404, "not_found", "booking not found")
    # Merged candidate: stored values overridden by the fields that were sent.
    candidate = {
        "room_id": rec["room_id"],
        "start": fmt_ts(rec["start"]),
        "end": fmt_ts(rec["end"]),
        "title": rec["title"],
        "attendees": rec["attendees"],
    }
    for field in ("start", "end", "title", "attendees"):
        if field in body:
            candidate[field] = body[field]
    room_id, start, end, title, attendees = validate_booking(candidate)
    room = STATE.rooms[room_id]
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    if has_conflict(room_id, start, end, room["buffer_minutes"], exclude_n=n):
        raise ApiError(409, "conflict", "booking overlaps an existing booking or buffer")
    # All checks passed: apply the update atomically.
    rec["start"] = start
    rec["end"] = end
    rec["title"] = title
    rec["attendees"] = attendees
    return 200, booking_view(n, rec)


def parse_booking_id(bid):
    # Only canonical ids are ever issued: b1, b2, ... (no leading zeros, no b0).
    m = re.fullmatch(r"b([1-9][0-9]*)", bid)
    if not m:
        return None
    return int(m.group(1))


def _single(params, name):
    vals = params.get(name)
    return vals[0] if vals else None


def list_bookings(params):
    room_id = _single(params, "room_id")
    from_s = _single(params, "from")
    to_s = _single(params, "to")
    limit_s = _single(params, "limit")
    cursor_s = _single(params, "cursor")

    from_dt = None
    to_dt = None
    if from_s is not None:
        from_dt = parse_ts(from_s)
        if from_dt is None:
            raise validation("from must be a YYYY-MM-DDTHH:MM:SSZ timestamp")
    if to_s is not None:
        to_dt = parse_ts(to_s)
        if to_dt is None:
            raise validation("to must be a YYYY-MM-DDTHH:MM:SSZ timestamp")
    limit = 20
    if limit_s is not None:
        if not _INT_RE.fullmatch(limit_s) or not (1 <= int(limit_s) <= 100):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(limit_s)
    cursor_key = None
    if cursor_s is not None:
        cursor_key = decode_cursor(cursor_s)

    matches = []
    for n, b in STATE.bookings.items():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if from_dt is not None and not (b["end"] > from_dt):
            continue
        if to_dt is not None and not (b["start"] < to_dt):
            continue
        matches.append(((b["start"], n), n, b))
    matches.sort(key=lambda t: t[0])
    total = len(matches)
    if cursor_key is not None:
        matches = [m for m in matches if m[0] > cursor_key]

    page = matches[:limit]
    more = len(matches) > limit
    next_cursor = encode_cursor(page[-1][0]) if (more and page) else None
    out = [booking_view(n, b) for _, n, b in page]
    return 200, {"bookings": out, "next_cursor": next_cursor, "total": total}


def availability(rid, params):
    room = STATE.rooms.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    date_s = _single(params, "date")
    if date_s is None:
        raise validation("date is required")
    m = _DATE_RE.fullmatch(date_s)
    if not m:
        raise validation("date must be YYYY-MM-DD")
    try:
        d = date(*(int(g) for g in m.groups()))
    except ValueError:
        raise validation("date must be a real calendar date")
    day_start = datetime(d.year, d.month, d.day, WORK_START_HOUR)
    day_end = datetime(d.year, d.month, d.day, WORK_END_HOUR)

    buf = timedelta(minutes=room["buffer_minutes"])
    clipped = []
    for b in STATE.bookings.values():
        if b["room_id"] != rid:
            continue
        # Expand by the buffer first, then clip to the working day.
        s = max(b["start"] - buf, day_start)
        e = min(b["end"] + buf, day_end)
        if s < e:
            clipped.append((s, e))
    clipped.sort()

    free = []
    cur = day_start

    def add_gap(a, b_):
        if b_ - a >= MIN_FREE:
            free.append({"start": fmt_ts(a), "end": fmt_ts(b_)})

    for s, e in clipped:
        if s > cur:
            add_gap(cur, s)
        if e > cur:
            cur = e
    if cur < day_end:
        add_gap(cur, day_end)
    return 200, {"date": date_s, "free": free}


# ---------- routing ----------

ROUTES = {
    ("rooms",): {"GET": "list_rooms", "POST": "create_room"},
    ("rooms", "*"): {"GET": "get_room"},
    ("rooms", "*", "availability"): {"GET": "availability"},
    ("bookings",): {"GET": "list_bookings", "POST": "create_booking"},
    ("bookings", "*"): {"GET": "get_booking", "DELETE": "delete_booking", "PATCH": "patch_booking"},
}


def match_route(path):
    parts = path.split("/")
    if not parts or parts[0] != "":
        return None, None
    segs = tuple(parts[1:])
    for pattern, methods in ROUTES.items():
        if len(pattern) != len(segs):
            continue
        args = []
        ok = True
        for p, s in zip(pattern, segs):
            if p == "*":
                args.append(s)
            elif p != s:
                ok = False
                break
        if ok:
            return methods, args
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):  # keep stdout/stderr quiet
        pass

    def send_json(self, status, obj=None):
        body = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, status, code, message):
        self.send_json(status, {"error": {"code": code, "message": message}})

    def send_error(self, code, message=None, explain=None):
        # Override so that protocol-level errors are also JSON.
        names = {400: "bad_request", 404: "not_found", 405: "method_not_allowed",
                 414: "bad_request", 501: "not_implemented"}
        self.send_error_json(code, names.get(code, "error"), message or "error")

    def read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        return self.rfile.read(n) if n > 0 else b""

    def parse_json_object(self, raw):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "bad_json", "request body must be a JSON object")
        return obj

    def handle_request(self):
        method = self.command
        parts = urlsplit(self.path)
        path = parts.path
        params = parse_qs(parts.query, keep_blank_values=True)
        raw = self.read_body() if method in ("POST", "PUT", "PATCH") else b""
        try:
            methods, args = match_route(path)
            if methods is None:
                raise ApiError(404, "not_found", "no such path")
            name = methods.get(method)
            if name is None:
                raise ApiError(405, "method_not_allowed", "method not allowed")
            with STATE.lock:
                status, obj = self.dispatch(name, args, params, raw)
            self.send_json(status, obj)
        except ApiError as e:
            self.send_error_json(e.status, e.code, e.message)

    def dispatch(self, name, args, params, raw):
        if name == "list_rooms":
            return list_rooms()
        if name == "create_room":
            return create_room(self.parse_json_object(raw))
        if name == "get_room":
            return get_room(args[0])
        if name == "availability":
            return availability(args[0], params)
        if name == "list_bookings":
            return list_bookings(params)
        if name == "create_booking":
            body = self.parse_json_object(raw)
            key = self.headers.get("Idempotency-Key")
            if key is not None:
                key = key or None  # empty header value means "no key"; otherwise used as given
            return create_booking(body, key)
        if name == "get_booking":
            return get_booking(args[0])
        if name == "delete_booking":
            return delete_booking(args[0])
        if name == "patch_booking":
            body = self.parse_json_object(raw)
            return patch_booking(args[0], body)
        raise ApiError(404, "not_found", "no such path")

    def __getattr__(self, name):
        # BaseHTTPRequestHandler answers unknown verbs with 501 before routing.
        # Route every do_<VERB> through the router so unsupported methods get 405.
        if name.startswith("do_"):
            return self.handle_request
        raise AttributeError(name)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.handle_request()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    # Large but valid JSON integers must reach field validation (422), not fail as bad_json.
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(0)
    ap = argparse.ArgumentParser(description="bookings meeting room API")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    httpd = Server((HOST, args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
