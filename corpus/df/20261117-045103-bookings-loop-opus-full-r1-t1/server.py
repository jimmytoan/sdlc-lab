"""bookings: meeting room booking API (standard library only)."""
import argparse
import base64
import binascii
import json
import re
import threading
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

TS_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
LIMIT_RE = re.compile(r"^[0-9]+$")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def parse_ts(value):
    if not isinstance(value, str) or not TS_RE.match(value):
        return None
    try:
        return datetime.strptime(value, TS_FMT)
    except ValueError:
        return None


def fmt_ts(dt):
    return dt.strftime(TS_FMT)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _reject_constant(name):
    raise ValueError("invalid constant " + name)


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}  # id -> room dict (insertion ordered)
        self.room_seq = 0
        self.bookings = {}  # id -> internal booking
        self.booking_seq = 0
        self.idem = {}  # key -> (body, response dict)


STORE = Store()


def validation(msg):
    return ApiError(422, "validation_error", msg)


def public_booking(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": b["start"], "end": b["end"],
            "title": b["title"], "attendees": b["attendees"]}


# ---------------------------------------------------------------- handlers

def create_room(body):
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str):
        raise validation("name must be a string")
    name = name.strip()
    if not 1 <= len(name) <= 50:
        raise validation("name must be 1 to 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise validation("capacity must be an integer from 1 to 500")
    with STORE.lock:
        low = name.lower()
        for r in STORE.rooms.values():
            if r["name"].lower() == low:
                raise ApiError(409, "room_exists", "a room with this name already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": cap}
        STORE.rooms[rid] = room
        return 201, dict(room)


def list_rooms():
    with STORE.lock:
        return 200, {"rooms": [dict(r) for r in STORE.rooms.values()]}


def get_room(rid):
    with STORE.lock:
        r = STORE.rooms.get(rid)
        if r is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, dict(r)


def validate_booking(body):
    room_id = body.get("room_id")
    start = body.get("start")
    end = body.get("end")
    title = body.get("title")
    att = body.get("attendees")
    if not isinstance(room_id, str):
        raise validation("room_id must be a string")
    s = parse_ts(start)
    e = parse_ts(end)
    if s is None:
        raise validation("start must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    if e is None:
        raise validation("end must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    for name, dt in (("start", s), ("end", e)):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise validation(f"{name} must be aligned to a quarter hour")
    if e <= s:
        raise validation("end must be after start")
    if e - s > timedelta(hours=8):
        raise validation("booking may be at most 8 hours long")
    if not isinstance(title, str):
        raise validation("title must be a string")
    title = title.strip()
    if not 1 <= len(title) <= 100:
        raise validation("title must be 1 to 100 characters")
    if not is_int(att) or att < 1:
        raise validation("attendees must be an integer of at least 1")
    return room_id, s, e, title, att


def canon(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def create_booking(body, key):
    with STORE.lock:
        if key is not None and key in STORE.idem:
            saved_body, saved_resp = STORE.idem[key]
            if saved_body == canon(body):
                return 201, json.loads(json.dumps(saved_resp))
            raise ApiError(422, "idempotency_mismatch",
                           "idempotency key was used with a different request body")
        room_id, s, e, title, att = validate_booking(body)
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if att > room["capacity"]:
            raise ApiError(422, "over_capacity", "attendees exceed room capacity")
        for b in STORE.bookings.values():
            if b["room_id"] == room_id and b["_s"] < e and s < b["_e"]:
                raise ApiError(409, "conflict", "booking overlaps an existing booking")
        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        b = {"id": bid, "room_id": room_id, "start": fmt_ts(s), "end": fmt_ts(e),
             "title": title, "attendees": att, "_s": s, "_e": e, "_seq": STORE.booking_seq}
        STORE.bookings[bid] = b
        resp = public_booking(b)
        if key is not None:
            STORE.idem[key] = (canon(body), resp)
        return 201, dict(resp)


def get_booking(bid):
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, public_booking(b)


def delete_booking(bid):
    with STORE.lock:
        if bid not in STORE.bookings:
            raise ApiError(404, "not_found", "booking not found")
        del STORE.bookings[bid]
        return 204, None


def encode_cursor(b):
    raw = json.dumps([b["start"], b["_seq"]]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        if not c or not re.match(r"^[A-Za-z0-9_-]+$", c):
            raise ValueError
        raw = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4))
        val = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        if not (isinstance(val, list) and len(val) == 2):
            raise ValueError
        st = parse_ts(val[0])
        if st is None or not is_int(val[1]) or val[1] < 1:
            raise ValueError
        return st, val[1]
    except (ValueError, binascii.Error, UnicodeDecodeError, TypeError):
        raise validation("invalid cursor")


def list_bookings(query):
    def first(name):
        vals = query.get(name)
        return None if vals is None else vals[0]

    room_id = first("room_id")
    frm = first("from")
    to = first("to")
    limit = first("limit")
    cursor = first("cursor")
    f_dt = t_dt = None
    if frm is not None:
        f_dt = parse_ts(frm)
        if f_dt is None:
            raise validation("invalid from")
    if to is not None:
        t_dt = parse_ts(to)
        if t_dt is None:
            raise validation("invalid to")
    if limit is None:
        n = 20
    else:
        if not LIMIT_RE.match(limit):
            raise validation("limit must be an integer from 1 to 100")
        n = int(limit)
        if not 1 <= n <= 100:
            raise validation("limit must be an integer from 1 to 100")
    after = decode_cursor(cursor) if cursor is not None else None
    with STORE.lock:
        items = []
        for b in STORE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if f_dt is not None and not b["_e"] > f_dt:
                continue
            if t_dt is not None and not b["_s"] < t_dt:
                continue
            if after is not None and (b["_s"], b["_seq"]) <= after:
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_s"], b["_seq"]))
        page = items[:n]
        nxt = encode_cursor(page[-1]) if len(items) > n else None
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": nxt}


def availability(rid, query):
    with STORE.lock:
        if rid not in STORE.rooms:
            raise ApiError(404, "not_found", "room not found")
    vals = query.get("date")
    d = vals[0] if vals else None
    if d is None or not DATE_RE.match(d):
        raise validation("date must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(d)
    except ValueError:
        raise validation("date must be a real calendar date")
    ws = datetime(day.year, day.month, day.day, 8, 0, 0)
    we = datetime(day.year, day.month, day.day, 18, 0, 0)
    with STORE.lock:
        busy = sorted((max(b["_s"], ws), min(b["_e"], we)) for b in STORE.bookings.values()
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


# ---------------------------------------------------------------- routing

def route(path):
    """Returns (route_name, params, allowed_methods) or None."""
    if path == "/rooms":
        return "rooms", (), ("GET", "POST")
    if path == "/bookings":
        return "bookings", (), ("GET", "POST")
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "" and parts[2] != "":
        seg = unquote(parts[2])
        if parts[1] == "rooms":
            return "room", (seg,), ("GET",)
        if parts[1] == "bookings":
            return "booking", (seg,), ("GET", "DELETE")
    if len(parts) == 4 and parts[0] == "" and parts[1] == "rooms" and parts[2] != "" \
            and parts[3] == "availability":
        return "availability", (unquote(parts[2]),), ("GET",)
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, *args):
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        return self.rfile.read(n) if n > 0 else b""

    def _send(self, status, payload):
        if payload is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json_body(self, raw):
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_json", "request body must be a JSON object")
        return body

    def _dispatch(self):
        raw = self._read_body()
        try:
            status, payload = self._handle(raw)
        except ApiError as e:
            status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:  # pragma: no cover
            status, payload = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        self._send(status, payload)

    def _handle(self, raw):
        parts = urlsplit(self.path)
        r = route(parts.path)
        if r is None:
            raise ApiError(404, "not_found", "no such path")
        name, params, allowed = r
        method = self.command
        if method not in allowed:
            raise ApiError(405, "method_not_allowed", f"method {method} not allowed")
        query = parse_qs(parts.query, keep_blank_values=True)
        if name == "rooms":
            if method == "GET":
                return list_rooms()
            return create_room(self._json_body(raw))
        if name == "room":
            return get_room(params[0])
        if name == "availability":
            return availability(params[0], query)
        if name == "bookings":
            if method == "GET":
                return list_bookings(query)
            body = self._json_body(raw)
            key = self.headers.get("Idempotency-Key")
            if key is not None and key == "":
                key = None
            return create_booking(body, key)
        if name == "booking":
            if method == "GET":
                return get_booking(params[0])
            return delete_booking(params[0])
        raise ApiError(404, "not_found", "no such path")


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
