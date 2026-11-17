"""Meeting room booking API (standard library only)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone, date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

LOCK = threading.RLock()
STATE = {
    "rooms": {},        # id -> room dict
    "room_seq": 0,
    "bookings": {},     # id -> booking dict
    "booking_seq": 0,
    "idem": {},         # key -> (fingerprint, status, body)
}

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MAX_DURATION = timedelta(hours=8)
MIN_FREE = timedelta(minutes=15)


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


def room_public(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"],
            "buffer_minutes": r["buffer_minutes"]}


def booking_public(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}


def booking_sort_key(b):
    return (b["start"], b["num"])


# ---------------------------------------------------------------- handlers

def create_room(body):
    if not isinstance(body, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("validation_error", "name is required and must be a non-empty string")
    if len(name) > 50:
        raise ApiError("validation_error", "name must be at most 50 characters")
    if not is_int(capacity):
        raise ApiError("validation_error", "capacity is required and must be an integer")
    if capacity < 1 or capacity > 500:
        raise ApiError("validation_error", "capacity must be between 1 and 500")
    buffer_minutes = 0
    if "buffer_minutes" in body:
        buffer_minutes = body["buffer_minutes"]
        if not is_int(buffer_minutes) or buffer_minutes < 0 or buffer_minutes > 60:
            raise ApiError("validation_error", "buffer_minutes must be an integer between 0 and 60")
    with LOCK:
        key = name.strip().casefold()
        for r in STATE["rooms"].values():
            if r["name"].strip().casefold() == key:
                raise ApiError("room_exists", f"a room named {name!r} already exists")
        STATE["room_seq"] += 1
        rid = f"r{STATE['room_seq']}"
        room = {"id": rid, "name": name, "capacity": capacity, "buffer_minutes": buffer_minutes}
        STATE["rooms"][rid] = room
        return 201, room_public(room)


def get_room(rid):
    room = STATE["rooms"].get(rid)
    if room is None:
        raise ApiError("not_found", f"room {rid} not found")
    return room


def validate_booking(body):
    if not isinstance(body, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise ApiError("validation_error", "room_id is required and must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise ApiError("validation_error", f"{field} must be on a quarter hour")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "a booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ApiError("validation_error", "title is required and must be a non-empty string")
    attendees = body.get("attendees")
    if not is_int(attendees):
        raise ApiError("validation_error", "attendees is required and must be an integer")
    if attendees < 1:
        raise ApiError("validation_error", "attendees must be at least 1")
    return room_id, start, end, title, attendees


def check_conflict(room, start, end, exclude_id=None):
    buf = timedelta(minutes=room.get("buffer_minutes", 0))
    for b in STATE["bookings"].values():
        if b["room_id"] != room["id"] or b["id"] == exclude_id:
            continue
        if b["start"] < end + buf and b["end"] > start - buf:
            raise ApiError("conflict", f"room {room['id']} is already booked by {b['id']} in that time")


def create_booking(body, idem_key):
    with LOCK:
        fingerprint = None
        if idem_key is not None:
            fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
            stored = STATE["idem"].get(idem_key)
            if stored is not None:
                if stored[0] != fingerprint:
                    raise ApiError("idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return stored[1], json.loads(json.dumps(stored[2]))
        room_id, start, end, title, attendees = validate_booking(body)
        room = get_room(room_id)
        if attendees > room["capacity"]:
            raise ApiError("over_capacity",
                           f"room {room_id} holds {room['capacity']} people, got {attendees} attendees")
        check_conflict(room, start, end)
        STATE["booking_seq"] += 1
        num = STATE["booking_seq"]
        bk = {"id": f"b{num}", "num": num, "room_id": room_id, "start": start, "end": end,
              "title": title, "attendees": attendees}
        STATE["bookings"][bk["id"]] = bk
        resp = booking_public(bk)
        if idem_key is not None:
            STATE["idem"][idem_key] = (fingerprint, 201, json.loads(json.dumps(resp)))
        return 201, resp


def patch_booking(bid, body):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    with LOCK:
        b = STATE["bookings"].get(bid)
        if b is None:
            raise ApiError("not_found", f"booking {bid} not found")
        merged = {
            "room_id": b["room_id"],
            "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]),
            "title": b["title"],
            "attendees": b["attendees"],
        }
        for f in ("start", "end", "title", "attendees"):
            if f in body:
                merged[f] = body[f]
        _, start, end, title, attendees = validate_booking(merged)
        room = get_room(b["room_id"])
        if attendees > room["capacity"]:
            raise ApiError("over_capacity",
                           f"room {room['id']} holds {room['capacity']} people, got {attendees} attendees")
        check_conflict(room, start, end, exclude_id=bid)
        b["start"] = start
        b["end"] = end
        b["title"] = title
        b["attendees"] = attendees
        return 200, booking_public(b)


def encode_cursor(b):
    raw = json.dumps([fmt_ts(b["start"]), b["num"]]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        data = json.loads(base64.urlsafe_b64decode(c + pad).decode())
        if not (isinstance(data, list) and len(data) == 2 and is_int(data[1])):
            raise ValueError
        start = parse_ts(data[0], "cursor")
        return (start, data[1])
    except (ValueError, ApiError, UnicodeDecodeError, TypeError):
        raise ApiError("validation_error", "cursor is invalid")


def single(q, name):
    vals = q.get(name)
    if not vals:
        return None
    return vals[-1]


def list_bookings(q):
    room_id = single(q, "room_id")
    frm = single(q, "from")
    to = single(q, "to")
    limit_s = single(q, "limit")
    cursor = single(q, "cursor")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    if frm_dt is not None and to_dt is not None and to_dt <= frm_dt:
        raise ApiError("validation_error", "to must be after from")
    limit = 20
    if limit_s is not None:
        if not re.fullmatch(r"\d+", limit_s.strip() or "x"):
            raise ApiError("validation_error", "limit must be an integer between 1 and 100")
        limit = int(limit_s.strip())
        if limit < 1 or limit > 100:
            raise ApiError("validation_error", "limit must be an integer between 1 and 100")
    after = decode_cursor(cursor) if cursor is not None and cursor != "" else None
    if cursor == "":
        after = None
    with LOCK:
        items = []
        total = 0
        for b in STATE["bookings"].values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm_dt is not None and b["end"] <= frm_dt:
                continue
            if to_dt is not None and b["start"] >= to_dt:
                continue
            total += 1
            if after is not None and booking_sort_key(b) <= after:
                continue
            items.append(b)
        items.sort(key=booking_sort_key)
        page = items[:limit]
        next_cursor = encode_cursor(page[-1]) if len(items) > limit else None
        return 200, {"bookings": [booking_public(b) for b in page], "next_cursor": next_cursor,
                     "total": total}


def availability(rid, q):
    with LOCK:
        room = get_room(rid)
        d = single(q, "date")
        if d is None:
            raise ApiError("validation_error", "date is required (YYYY-MM-DD)")
        m = DATE_RE.match(d)
        if not m:
            raise ApiError("validation_error", "date must look like YYYY-MM-DD")
        try:
            day = date_cls(*(int(g) for g in m.groups()))
        except ValueError:
            raise ApiError("validation_error", "date is not a valid date")
        ws = datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc)
        we = datetime(day.year, day.month, day.day, 18, 0, tzinfo=timezone.utc)
        buf = timedelta(minutes=room.get("buffer_minutes", 0))
        busy = []
        for b in STATE["bookings"].values():
            if b["room_id"] != rid:
                continue
            bs = b["start"] - buf
            be = b["end"] + buf
            if bs < we and be > ws:
                busy.append((max(bs, ws), min(be, we)))
        busy.sort()
        free = []

        def add(s, e):
            if e - s >= MIN_FREE:
                free.append({"start": fmt_ts(s), "end": fmt_ts(e)})

        cur = ws
        for s, e in busy:
            if s > cur:
                add(cur, s)
            if e > cur:
                cur = e
        if cur < we:
            add(cur, we)
        return 200, {"date": day.isoformat(), "free": free}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
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

    def read_json(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw.strip():
            raise ApiError("bad_json", "request body must be valid JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("bad_json", "request body must be valid JSON")

    def route(self):
        parts = urlsplit(self.path)
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        q = parse_qs(parts.query, keep_blank_values=True)
        segs = [s for s in path.split("/") if s] if path != "/" else []
        m = self.command

        def allow(*methods):
            if m not in methods:
                raise ApiError("method_not_allowed", f"method {m} not allowed on {path}",
                               {"Allow": ", ".join(methods)})

        if segs == ["rooms"]:
            allow("GET", "POST")
            if m == "GET":
                with LOCK:
                    rooms = sorted(STATE["rooms"].values(), key=lambda r: int(r["id"][1:]))
                    return 200, {"rooms": [room_public(r) for r in rooms]}
            return create_room(self.read_json())
        if len(segs) == 2 and segs[0] == "rooms":
            allow("GET")
            with LOCK:
                return 200, room_public(get_room(segs[1]))
        if len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
            allow("GET")
            return availability(segs[1], q)
        if segs == ["bookings"]:
            allow("GET", "POST")
            if m == "GET":
                return list_bookings(q)
            key = self.headers.get("Idempotency-Key")
            if key is not None and key.strip() == "":
                key = None
            return create_booking(self.read_json(), key)
        if len(segs) == 2 and segs[0] == "bookings":
            allow("GET", "DELETE", "PATCH")
            bid = segs[1]
            if m == "PATCH":
                return patch_booking(bid, self.read_json())
            with LOCK:
                b = STATE["bookings"].get(bid)
                if b is None:
                    raise ApiError("not_found", f"booking {bid} not found")
                if m == "GET":
                    return 200, booking_public(b)
                del STATE["bookings"][bid]
                return 204, None
        raise ApiError("not_found", f"no such resource: {path}")

    def handle_any(self):
        try:
            status, body = self.route()
            self.send_json(status, body)
        except ApiError as e:
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:  # pragma: no cover
            self.send_json(500, {"error": {"code": "internal_error", "message": str(e)}})

    do_GET = handle_any
    do_POST = handle_any
    do_DELETE = handle_any
    do_PUT = handle_any
    do_PATCH = handle_any
    do_OPTIONS = handle_any
    do_HEAD = handle_any


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
