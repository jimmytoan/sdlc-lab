"""Meeting room booking API. Standard library only, in-memory state."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone, date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^[+-]?\d+$")
MAX_DURATION = timedelta(hours=8)


class ApiError(Exception):
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

    def __init__(self, code, message, allow=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]
        self.allow = allow


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")
    return dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}  # id -> dict
        self.room_seq = 0
        self.bookings = {}  # id -> internal dict
        self.booking_seq = 0
        self.idem = {}  # key -> (canonical body, response body)


STATE = State()


def room_public(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_public(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}


# ---------------------------------------------------------------- handlers

def create_room(body):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str):
        raise ApiError("validation_error", "name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise ApiError("validation_error", "name must be 1 to 50 characters")
    if not is_int(cap):
        raise ApiError("validation_error", "capacity must be an integer")
    if not (1 <= cap <= 500):
        raise ApiError("validation_error", "capacity must be between 1 and 500")
    st = STATE
    folded = name.casefold()
    for r in st.rooms.values():
        if r["name"].casefold() == folded:
            raise ApiError("room_exists", "a room with this name already exists")
    st.room_seq += 1
    rid = f"r{st.room_seq}"
    room = {"id": rid, "name": name, "capacity": cap}
    st.rooms[rid] = room
    return 201, room_public(room)


def get_room(rid):
    r = STATE.rooms.get(rid)
    if r is None:
        raise ApiError("not_found", f"room {rid} not found")
    return r


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise ApiError("validation_error", "room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.second != 0 or dt.minute % 15 != 0:
            raise ApiError("validation_error", f"{field} must be on a quarter hour")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "a booking can be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise ApiError("validation_error", "title must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise ApiError("validation_error", "title must be 1 to 100 characters")
    att = body.get("attendees")
    if not is_int(att):
        raise ApiError("validation_error", "attendees must be an integer")
    if att < 1:
        raise ApiError("validation_error", "attendees must be at least 1")
    return room_id, start, end, title, att


def create_booking(body, idem_key):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    st = STATE
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if idem_key is not None and idem_key in st.idem:
        stored_canon, stored_resp = st.idem[idem_key]
        if stored_canon != canon:
            raise ApiError("idempotency_mismatch", "Idempotency-Key was already used for a different request")
        return 201, stored_resp
    room_id, start, end, title, att = validate_booking(body)
    room = get_room(room_id)
    if att > room["capacity"]:
        raise ApiError("over_capacity", f"room {room_id} holds at most {room['capacity']} people")
    for b in st.bookings.values():
        if b["room_id"] == room_id and b["start"] < end and start < b["end"]:
            raise ApiError("conflict", f"room {room_id} is already booked by {b['id']} at that time")
    st.booking_seq += 1
    bid = f"b{st.booking_seq}"
    b = {"id": bid, "seq": st.booking_seq, "room_id": room_id, "start": start, "end": end,
         "title": title, "attendees": att}
    st.bookings[bid] = b
    resp = booking_public(b)
    if idem_key is not None:
        st.idem[idem_key] = (canon, resp)
    return 201, resp


def get_booking(bid):
    b = STATE.bookings.get(bid)
    if b is None:
        raise ApiError("not_found", f"booking {bid} not found")
    return b


def single_param(q, name):
    vals = q.get(name)
    if vals is None:
        return None
    return vals[-1]


def encode_cursor(b):
    raw = f"{fmt_ts(b['start'])}|{b['seq']}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode((c + pad).encode("ascii")).decode("utf-8")
        ts, seq = raw.split("|")
        start = parse_ts(ts, "cursor")
        if not re.fullmatch(r"\d+", seq):
            raise ValueError
        return (start, int(seq))
    except Exception:
        raise ApiError("validation_error", "invalid cursor")


def list_bookings(q):
    room_id = single_param(q, "room_id")
    frm = single_param(q, "from")
    to = single_param(q, "to")
    frm = parse_ts(frm, "from") if frm is not None else None
    to = parse_ts(to, "to") if to is not None else None
    if frm is not None and to is not None and frm > to:
        raise ApiError("validation_error", "from must not be after to")
    limit_s = single_param(q, "limit")
    if limit_s is None:
        limit = 20
    else:
        if not INT_RE.match(limit_s.strip()) or limit_s.strip() != limit_s:
            raise ApiError("validation_error", "limit must be an integer between 1 and 100")
        limit = int(limit_s)
        if not (1 <= limit <= 100):
            raise ApiError("validation_error", "limit must be an integer between 1 and 100")
    cursor = single_param(q, "cursor")
    after = decode_cursor(cursor) if cursor is not None else None

    items = []
    for b in STATE.bookings.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm is not None and not (b["end"] > frm):
            continue
        if to is not None and not (b["start"] < to):
            continue
        if after is not None and (b["start"], b["seq"]) <= after:
            continue
        items.append(b)
    items.sort(key=lambda b: (b["start"], b["seq"]))
    page = items[:limit]
    next_cursor = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [booking_public(b) for b in page], "next_cursor": next_cursor}


def availability(rid, q):
    get_room(rid)
    d = single_param(q, "date")
    if d is None or not DATE_RE.match(d):
        raise ApiError("validation_error", "date must be given as YYYY-MM-DD")
    try:
        day = date_cls.fromisoformat(d)
    except ValueError:
        raise ApiError("validation_error", "date is not a valid calendar date")
    day_start = datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc)
    day_end = datetime(day.year, day.month, day.day, 18, 0, tzinfo=timezone.utc)
    busy = sorted((max(b["start"], day_start), min(b["end"], day_end))
                  for b in STATE.bookings.values()
                  if b["room_id"] == rid and b["start"] < day_end and b["end"] > day_start)
    free = []
    cur = day_start
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < day_end:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
    return 200, {"date": d, "free": free}


# ---------------------------------------------------------------- routing

ROUTES = [
    (re.compile(r"^/rooms$"), {"GET", "POST"}, "rooms"),
    (re.compile(r"^/rooms/([^/]+)$"), {"GET"}, "room"),
    (re.compile(r"^/rooms/([^/]+)/availability$"), {"GET"}, "availability"),
    (re.compile(r"^/bookings$"), {"GET", "POST"}, "bookings"),
    (re.compile(r"^/bookings/([^/]+)$"), {"GET", "DELETE"}, "booking"),
]


def parse_body(raw):
    if not raw or not raw.strip():
        raise ApiError("bad_json", "request body must be a JSON object")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(body, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    return body


def dispatch(method, target, raw, headers):
    parts = urlsplit(target)
    path = parts.path
    q = parse_qs(parts.query, keep_blank_values=True)
    for rx, methods, name in ROUTES:
        m = rx.match(path)
        if not m:
            continue
        if method not in methods:
            raise ApiError("method_not_allowed", f"method {method} not allowed on {path}",
                           allow=", ".join(sorted(methods)))
        args = m.groups()
        if name == "rooms":
            if method == "GET":
                rooms = sorted(STATE.rooms.values(), key=lambda r: int(r["id"][1:]))
                return 200, {"rooms": [room_public(r) for r in rooms]}
            return create_room(parse_body(raw))
        if name == "room":
            return 200, room_public(get_room(args[0]))
        if name == "availability":
            return availability(args[0], q)
        if name == "bookings":
            if method == "GET":
                return list_bookings(q)
            key = headers.get("Idempotency-Key")
            if key is not None and key == "":
                key = None
            return create_booking(parse_body(raw), key)
        if name == "booking":
            b = get_booking(args[0])
            if method == "GET":
                return 200, booking_public(b)
            del STATE.bookings[b["id"]]
            return 204, None
    raise ApiError("not_found", f"no route for {path}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = self.headers.get("Content-Length")
        raw = b""
        try:
            n = int(length) if length else 0
        except ValueError:
            n = 0
        if n > 0:
            raw = self.rfile.read(n)
        extra = {}
        try:
            with STATE.lock:
                status, body = dispatch(self.command, self.path, raw, self.headers)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
            if e.allow:
                extra["Allow"] = e.allow
        except Exception as e:  # pragma: no cover
            status, body = 500, {"error": {"code": "internal_error", "message": str(e)}}
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        if body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    a = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
