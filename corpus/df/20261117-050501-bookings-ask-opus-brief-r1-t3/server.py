"""Meeting room booking API. Standard library only, in-memory state."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BOOKING = timedelta(hours=8)
WORK_START = 8
WORK_END = 18


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

    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]
        self.headers = headers or {}


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")
    return dt


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}  # id -> dict
        self.room_seq = 0
        self.bookings = {}  # id -> record
        self.booking_seq = 0
        self.idem = {}  # key -> (canonical body, response body)


STORE = Store()


def room_view(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_view(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


# ---------------------------------------------------------------- handlers

def create_room(body):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise ApiError("validation_error", "name is required and must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise ApiError("validation_error", "name must be 1 to 50 characters")
    if not is_int(capacity):
        raise ApiError("validation_error", "capacity is required and must be an integer")
    if not (1 <= capacity <= 500):
        raise ApiError("validation_error", "capacity must be between 1 and 500")
    with STORE.lock:
        low = name.casefold()
        for r in STORE.rooms.values():
            if r["name"].casefold() == low:
                raise ApiError("room_exists", f"a room named {name!r} already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STORE.rooms[rid] = room
        return 201, room_view(room)


def list_rooms():
    with STORE.lock:
        rooms = sorted(STORE.rooms.values(), key=lambda r: int(r["id"][1:]))
        return 200, {"rooms": [room_view(r) for r in rooms]}


def get_room(rid):
    with STORE.lock:
        r = STORE.rooms.get(rid)
        if r is None:
            raise ApiError("not_found", f"room {rid} not found")
        return 200, room_view(r)


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise ApiError("validation_error", "room_id is required and must be a string")
    if "start" not in body:
        raise ApiError("validation_error", "start is required")
    if "end" not in body:
        raise ApiError("validation_error", "end is required")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.second != 0 or dt.minute % 15 != 0:
            raise ApiError("validation_error", f"{field} must be on a quarter hour")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_BOOKING:
        raise ApiError("validation_error", "a booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise ApiError("validation_error", "title is required and must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise ApiError("validation_error", "title must be 1 to 100 characters")
    attendees = body.get("attendees")
    if not is_int(attendees):
        raise ApiError("validation_error", "attendees is required and must be an integer")
    if attendees < 1:
        raise ApiError("validation_error", "attendees must be at least 1")
    return room_id, start, end, title, attendees


def create_booking(body, idem_key):
    with STORE.lock:
        canon = canonical(body)
        if idem_key is not None and idem_key in STORE.idem:
            stored_canon, stored_resp = STORE.idem[idem_key]
            if stored_canon == canon:
                return 201, json.loads(json.dumps(stored_resp))
            raise ApiError("idempotency_mismatch",
                           "this Idempotency-Key was already used for a different request")
        room_id, start, end, title, attendees = validate_booking(body)
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", f"room {room_id} not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity",
                           f"room {room_id} holds {room['capacity']} people, got {attendees} attendees")
        for b in STORE.bookings.values():
            if b["room_id"] == room_id and b["start"] < end and start < b["end"]:
                raise ApiError("conflict", f"room {room_id} is already booked by {b['id']} at that time")
        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        rec = {"id": bid, "seq": STORE.booking_seq, "room_id": room_id, "start": start, "end": end,
               "title": title, "attendees": attendees}
        STORE.bookings[bid] = rec
        resp = booking_view(rec)
        if idem_key is not None:
            STORE.idem[idem_key] = (canon, json.loads(json.dumps(resp)))
        return 201, resp


def get_booking(bid):
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", f"booking {bid} not found")
        return 200, booking_view(b)


def delete_booking(bid):
    with STORE.lock:
        if bid not in STORE.bookings:
            raise ApiError("not_found", f"booking {bid} not found")
        del STORE.bookings[bid]
        return 204, None


def encode_cursor(b):
    raw = f"{fmt_ts(b['start'])}|{b['seq']}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode((c + pad).encode("ascii")).decode("ascii")
        ts, seq = raw.split("|")
        if not TS_RE.match(ts) or not seq.isdigit():
            raise ValueError
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (dt, int(seq))
    except Exception:
        raise ApiError("validation_error", "cursor is invalid")


def single(params, name):
    vals = params.get(name)
    if vals is None:
        return None
    return vals[-1]


def list_bookings(params):
    room_id = single(params, "room_id")
    frm = single(params, "from")
    to = single(params, "to")
    limit_s = single(params, "limit")
    cursor = single(params, "cursor")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    if frm_dt is not None and to_dt is not None and frm_dt > to_dt:
        raise ApiError("validation_error", "from must not be after to")
    if limit_s is None:
        limit = 20
    else:
        if not re.fullmatch(r"\d+", limit_s):
            raise ApiError("validation_error", "limit must be an integer between 1 and 100")
        limit = int(limit_s)
        if not (1 <= limit <= 100):
            raise ApiError("validation_error", "limit must be an integer between 1 and 100")
    after = decode_cursor(cursor) if cursor is not None else None
    with STORE.lock:
        items = list(STORE.bookings.values())
    if room_id is not None:
        items = [b for b in items if b["room_id"] == room_id]
    if frm_dt is not None:
        items = [b for b in items if b["end"] > frm_dt]
    if to_dt is not None:
        items = [b for b in items if b["start"] < to_dt]
    items.sort(key=lambda b: (b["start"], b["seq"]))
    if after is not None:
        items = [b for b in items if (b["start"], b["seq"]) > after]
    page = items[:limit]
    next_cursor = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor}


def availability(rid, params):
    with STORE.lock:
        if rid not in STORE.rooms:
            raise ApiError("not_found", f"room {rid} not found")
        items = [b for b in STORE.bookings.values() if b["room_id"] == rid]
    date_s = single(params, "date")
    if date_s is None or not DATE_RE.match(date_s):
        raise ApiError("validation_error", "date is required, like 2026-03-02")
    try:
        day = datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", "date is not a valid date")
    ws = day + timedelta(hours=WORK_START)
    we = day + timedelta(hours=WORK_END)
    busy = sorted((max(b["start"], ws), min(b["end"], we)) for b in items
                  if b["start"] < we and b["end"] > ws)
    free = []
    cur = ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < we:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": date_s, "free": free}


# ---------------------------------------------------------------- routing

def route(path):
    """Return (allowed methods, route name, args) or None."""
    if path == "/rooms":
        return ("GET", "POST"), "rooms", ()
    if path == "/bookings":
        return ("GET", "POST"), "bookings", ()
    m = re.fullmatch(r"/rooms/([^/]+)", path)
    if m:
        return ("GET",), "room", (m.group(1),)
    m = re.fullmatch(r"/rooms/([^/]+)/availability", path)
    if m:
        return ("GET",), "availability", (m.group(1),)
    m = re.fullmatch(r"/bookings/([^/]+)", path)
    if m:
        return ("GET", "DELETE"), "booking", (m.group(1),)
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def send_json(self, status, body, headers=None):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def read_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def parse_json(self, raw):
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body must be valid JSON")
        if not isinstance(body, dict):
            raise ApiError("bad_json", "request body must be a JSON object")
        return body

    def handle_any(self):
        raw = self.read_body()
        try:
            parts = urlsplit(self.path)
            path = parts.path
            params = parse_qs(parts.query, keep_blank_values=True)
            r = route(path)
            if r is None:
                raise ApiError("not_found", f"no such path: {path}")
            allowed, name, args = r
            method = self.command
            if method not in allowed:
                raise ApiError("method_not_allowed", f"{method} is not allowed on {path}",
                               headers={"Allow": ", ".join(allowed)})
            if name == "rooms":
                if method == "GET":
                    status, body = list_rooms()
                else:
                    status, body = create_room(self.parse_json(raw))
            elif name == "room":
                status, body = get_room(*args)
            elif name == "availability":
                status, body = availability(args[0], params)
            elif name == "bookings":
                if method == "GET":
                    status, body = list_bookings(params)
                else:
                    payload = self.parse_json(raw)
                    status, body = create_booking(payload, self.headers.get("Idempotency-Key"))
            elif name == "booking":
                if method == "GET":
                    status, body = get_booking(*args)
                else:
                    status, body = delete_booking(*args)
            else:  # pragma: no cover
                raise ApiError("not_found", "not found")
            self.send_json(status, body)
        except ApiError as e:
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:  # pragma: no cover
            data = json.dumps({"error": {"code": "internal_error", "message": str(e)}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
