"""Meeting room booking API. Standard library only, in-memory state."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MAX_DURATION = timedelta(hours=8)
MIN_FREE = timedelta(minutes=15)

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


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = STATUS[code]


def parse_ts(value, field):
    if not isinstance(value, str):
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}  # id -> dict
        self.room_seq = 0
        self.bookings = {}  # id -> {"data": dict, "start": dt, "end": dt, "seq": int}
        self.booking_seq = 0
        self.idem = {}  # key -> (fingerprint, status, body)


STORE = Store()


def room_public(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"],
            "buffer_minutes": r.get("buffer_minutes", 0)}


def create_room(body):
    if not isinstance(body, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("validation_error", "name must be a non-empty string")
    if len(name) > 50:
        raise ApiError("validation_error", "name must be at most 50 characters")
    if not is_int(cap) or not (1 <= cap <= 500):
        raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
    buf = 0
    if "buffer_minutes" in body:
        buf = body["buffer_minutes"]
        if not is_int(buf) or not (0 <= buf <= 60):
            raise ApiError("validation_error", "buffer_minutes must be an integer from 0 to 60")
    key = name.strip().casefold()
    with STORE.lock:
        for r in STORE.rooms.values():
            if r["name"].strip().casefold() == key:
                raise ApiError("room_exists", "a room with this name already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": cap, "buffer_minutes": buf}
        STORE.rooms[rid] = room
        return 201, room_public(room)


def get_room(rid):
    r = STORE.rooms.get(rid)
    if r is None:
        raise ApiError("not_found", f"room {rid} not found")
    return r


def validate_booking(body):
    if not isinstance(body, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise ApiError("validation_error", "room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise ApiError("validation_error", f"{f} must be on a quarter hour")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "a booking can be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ApiError("validation_error", "title must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att) or att < 1:
        raise ApiError("validation_error", "attendees must be a positive integer")
    return room_id, start, end, title, att


def create_booking(body, idem_key):
    with STORE.lock:
        fingerprint = None
        if idem_key is not None:
            fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
            prev = STORE.idem.get(idem_key)
            if prev is not None:
                if prev[0] != fingerprint:
                    raise ApiError("idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return prev[1], prev[2]
        room_id, start, end, title, att = validate_booking(body)
        room = get_room(room_id)
        if att > room["capacity"]:
            raise ApiError("over_capacity", f"room {room_id} holds at most {room['capacity']} people")
        check_conflict(room, start, end, None)
        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        data = {"id": bid, "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
                "title": title, "attendees": att}
        STORE.bookings[bid] = {"data": data, "start": start, "end": end, "seq": STORE.booking_seq}
        result = (201, dict(data))
        if idem_key is not None:
            STORE.idem[idem_key] = (fingerprint, 201, dict(data))
        return result


def check_conflict(room, start, end, exclude_id):
    buf = timedelta(minutes=room.get("buffer_minutes", 0))
    room_id = room["id"]
    for b in STORE.bookings.values():
        if b["data"]["id"] == exclude_id or b["data"]["room_id"] != room_id:
            continue
        if b["start"] < end + buf and start - buf < b["end"]:
            raise ApiError("conflict", f"room {room_id} is already booked ({b['data']['id']})")


def patch_booking(bid, body):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", f"booking {bid} not found")
        merged = dict(b["data"])
        for f in ("start", "end", "title", "attendees"):
            if f in body:
                merged[f] = body[f]
        room_id, start, end, title, att = validate_booking(merged)
        room = get_room(room_id)
        if att > room["capacity"]:
            raise ApiError("over_capacity", f"room {room_id} holds at most {room['capacity']} people")
        check_conflict(room, start, end, bid)
        data = {"id": bid, "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
                "title": title, "attendees": att}
        b["data"] = data
        b["start"] = start
        b["end"] = end
        return 200, dict(data)


def encode_cursor(b):
    raw = f"{fmt_ts(b['start'])}|{b['seq']}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode(c + pad).decode()
        ts, seq = raw.split("|")
        start = parse_ts(ts, "cursor")
        if not seq.isdigit():
            raise ValueError
        return (start, int(seq))
    except (ValueError, ApiError, UnicodeDecodeError):
        raise ApiError("validation_error", "cursor is invalid")


def single_param(qs, name):
    vals = qs.get(name)
    if not vals:
        return None
    return vals[-1]


def list_bookings(qs):
    room_id = single_param(qs, "room_id")
    frm = single_param(qs, "from")
    to = single_param(qs, "to")
    limit_s = single_param(qs, "limit")
    cursor = single_param(qs, "cursor")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    if frm_dt is not None and to_dt is not None and to_dt <= frm_dt:
        raise ApiError("validation_error", "to must be after from")
    limit = 20
    if limit_s is not None:
        if not re.fullmatch(r"\d+", limit_s):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        limit = int(limit_s)
        if not (1 <= limit <= 100):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
    after = decode_cursor(cursor) if cursor not in (None, "") else None
    with STORE.lock:
        items = list(STORE.bookings.values())
    if room_id is not None:
        items = [b for b in items if b["data"]["room_id"] == room_id]
    if frm_dt is not None:
        items = [b for b in items if b["end"] > frm_dt]
    if to_dt is not None:
        items = [b for b in items if b["start"] < to_dt]
    items.sort(key=lambda b: (b["start"], b["seq"]))
    total = len(items)
    if after is not None:
        items = [b for b in items if (b["start"], b["seq"]) > after]
    page = items[:limit]
    next_cursor = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [dict(b["data"]) for b in page], "next_cursor": next_cursor, "total": total}


def availability(rid, qs):
    room = get_room(rid)
    buf = timedelta(minutes=room.get("buffer_minutes", 0))
    date = single_param(qs, "date")
    if date is None:
        raise ApiError("validation_error", "date is required (YYYY-MM-DD)")
    m = DATE_RE.match(date)
    if not m:
        raise ApiError("validation_error", "date must look like YYYY-MM-DD")
    try:
        day = datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", "date is not a valid date")
    ws = day + timedelta(hours=8)
    we = day + timedelta(hours=18)
    with STORE.lock:
        busy = sorted((b["start"] - buf, b["end"] + buf) for b in STORE.bookings.values()
                      if b["data"]["room_id"] == rid and b["start"] - buf < we and b["end"] + buf > ws)
    free = []
    cur = ws
    for s, e in busy:
        if s > cur and min(s, we) - cur >= MIN_FREE:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(min(s, we))})
        if e > cur:
            cur = e
        if cur >= we:
            break
    if we - cur >= MIN_FREE:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": date, "free": free}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        if status == 204:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def read_body(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length is not None else 0
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        return raw

    def parse_json(self, raw):
        if not raw.strip():
            raise ApiError("bad_json", "request body must be valid JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("bad_json", "request body must be valid JSON")

    def handle_any(self, method):
        try:
            raw = self.read_body()
            parts = urlsplit(self.path)
            path = parts.path
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/") or "/"
            qs = parse_qs(parts.query, keep_blank_values=True)
            status, body = self.route(method, path, qs, raw)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception as e:  # pragma: no cover
            status, body = 500, {"error": {"code": "internal_error", "message": str(e)}}
        self.send_json(status, body)

    def route(self, method, path, qs, raw):
        segs = [s for s in path.split("/") if s != ""]

        def allow(*methods):
            if method not in methods:
                raise ApiError("method_not_allowed", f"{method} is not allowed on {path}")

        if segs == ["rooms"]:
            allow("GET", "POST")
            if method == "GET":
                with STORE.lock:
                    return 200, {"rooms": [room_public(r) for r in STORE.rooms.values()]}
            return create_room(self.parse_json(raw))
        if len(segs) == 2 and segs[0] == "rooms":
            allow("GET")
            with STORE.lock:
                return 200, room_public(get_room(segs[1]))
        if len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
            allow("GET")
            return availability(segs[1], qs)
        if segs == ["bookings"]:
            allow("GET", "POST")
            if method == "GET":
                return list_bookings(qs)
            body = self.parse_json(raw)
            key = self.headers.get("Idempotency-Key")
            return create_booking(body, key if key else None)
        if len(segs) == 2 and segs[0] == "bookings":
            allow("GET", "DELETE", "PATCH")
            bid = segs[1]
            if method == "PATCH":
                try:
                    body = self.parse_json(raw)
                except ApiError:
                    raise ApiError("bad_json", "request body must be a JSON object")
                return patch_booking(bid, body)
            with STORE.lock:
                b = STORE.bookings.get(bid)
                if b is None:
                    raise ApiError("not_found", f"booking {bid} not found")
                if method == "GET":
                    return 200, dict(b["data"])
                del STORE.bookings[bid]
                return 204, None
        raise ApiError("not_found", f"no such path: {path}")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def do_HEAD(self):
        self.handle_any("HEAD")

    def do_OPTIONS(self):
        self.handle_any("OPTIONS")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
