"""Meeting room booking API (stdlib only, in-memory state)."""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, unquote

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
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

    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]
        self.headers = headers or {}


def validation(msg):
    return ApiError("validation_error", msg)


def parse_ts(value, field):
    if not isinstance(value, str):
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise validation(f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}  # id -> dict
        self.room_seq = 0
        self.bookings = {}  # id -> dict with internal _start/_end/_n
        self.booking_seq = 0
        self.idem = {}  # key -> (fingerprint, status, body)


STORE = Store()


def public_booking(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def encode_cursor(b):
    raw = f"{fmt_ts(b['_start'])}|{b['_n']}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode(c + pad).decode()
        ts, n = raw.split("|")
        dt = parse_ts(ts, "cursor")
        if not n.isdigit():
            raise ValueError
        return (dt, int(n))
    except Exception:
        raise validation("cursor is invalid")


# ---------------------------------------------------------------- handlers

def create_room(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise validation("name is required and must be a non-empty string")
    if len(name) > 50:
        raise validation("name must be at most 50 characters")
    if not is_int(cap):
        raise validation("capacity is required and must be an integer")
    if cap < 1 or cap > 500:
        raise validation("capacity must be between 1 and 500")
    buf = body.get("buffer_minutes", 0)
    if not is_int(buf) or buf < 0 or buf > 60:
        raise validation("buffer_minutes must be an integer between 0 and 60")
    s = STORE
    with s.lock:
        key = name.strip().casefold()
        for r in s.rooms.values():
            if r["name"].strip().casefold() == key:
                raise ApiError("room_exists", f"a room named {name!r} already exists")
        s.room_seq += 1
        room = {"id": f"r{s.room_seq}", "name": name, "capacity": cap, "buffer_minutes": buf}
        s.rooms[room["id"]] = room
        return 201, dict(room)


def list_rooms():
    with STORE.lock:
        rooms = sorted(STORE.rooms.values(), key=lambda r: int(r["id"][1:]))
        return 200, {"rooms": [dict(r) for r in rooms]}


def get_room(rid):
    with STORE.lock:
        r = STORE.rooms.get(rid)
        if r is None:
            raise ApiError("not_found", f"room {rid} not found")
        return 200, dict(r)


def validate_booking(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise validation("room_id is required and must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise validation("title is required and must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att):
        raise validation("attendees is required and must be an integer")
    if att < 1:
        raise validation("attendees must be at least 1")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.minute % 15 or dt.second:
            raise validation(f"{f} must be on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("bookings may be at most 8 hours long")
    return room_id, start, end, title, att


def check_conflict(room, start, end, exclude_id):
    buf = timedelta(minutes=room.get("buffer_minutes", 0))
    for b in STORE.bookings.values():
        if b["id"] == exclude_id or b["room_id"] != room["id"]:
            continue
        if b["_start"] - buf < end and start < b["_end"] + buf:
            raise ApiError("conflict", f"room {room['id']} is already booked by {b['id']} at that time"
                           + (" (including the cleaning buffer)" if buf else ""))


def patch_booking(bid, body):
    if not isinstance(body, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    s = STORE
    with s.lock:
        b = s.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", f"booking {bid} not found")
        merged = {"room_id": b["room_id"], "start": b["start"], "end": b["end"],
                  "title": b["title"], "attendees": b["attendees"]}
        for k in ("start", "end", "title", "attendees"):
            if k in body:
                merged[k] = body[k]
        _, start, end, title, att = validate_booking(merged)
        room = s.rooms.get(b["room_id"])
        if room is None:
            raise ApiError("not_found", f"room {b['room_id']} not found")
        if att > room["capacity"]:
            raise ApiError("over_capacity",
                           f"{att} attendees exceed the room capacity of {room['capacity']}")
        check_conflict(room, start, end, bid)
        b.update({"start": fmt_ts(start), "end": fmt_ts(end), "title": title, "attendees": att,
                  "_start": start, "_end": end})
        return 200, public_booking(b)


def create_booking(body, idem_key):
    s = STORE
    fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with s.lock:
        if idem_key is not None:
            prev = s.idem.get(idem_key)
            if prev is not None:
                if prev[0] != fingerprint:
                    raise ApiError("idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return prev[1], json.loads(json.dumps(prev[2]))
        room_id, start, end, title, att = validate_booking(body)
        room = s.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", f"room {room_id} not found")
        if att > room["capacity"]:
            raise ApiError("over_capacity",
                           f"{att} attendees exceed the room capacity of {room['capacity']}")
        check_conflict(room, start, end, None)
        s.booking_seq += 1
        n = s.booking_seq
        b = {"id": f"b{n}", "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
             "title": title, "attendees": att, "_start": start, "_end": end, "_n": n}
        s.bookings[b["id"]] = b
        resp = public_booking(b)
        if idem_key is not None:
            s.idem[idem_key] = (fingerprint, 201, dict(resp))
        return 201, resp


def get_booking(bid):
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", f"booking {bid} not found")
        return 200, public_booking(b)


def delete_booking(bid):
    with STORE.lock:
        if STORE.bookings.pop(bid, None) is None:
            raise ApiError("not_found", f"booking {bid} not found")
        return 204, None


def list_bookings(q):
    room_id = q.get("room_id")
    frm = parse_ts(q["from"], "from") if "from" in q else None
    to = parse_ts(q["to"], "to") if "to" in q else None
    if frm is not None and to is not None and frm > to:
        raise validation("from must not be after to")
    limit = 20
    if "limit" in q:
        lv = q["limit"]
        if not re.fullmatch(r"\d+", lv or ""):
            raise validation("limit must be an integer between 1 and 100")
        limit = int(lv)
        if limit < 1 or limit > 100:
            raise validation("limit must be an integer between 1 and 100")
    after = decode_cursor(q["cursor"]) if q.get("cursor") else None
    if "cursor" in q and not q["cursor"]:
        after = None
    with STORE.lock:
        items = []
        for b in STORE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not b["_end"] > frm:
                continue
            if to is not None and not b["_start"] < to:
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_start"], b["_n"]))
        total = len(items)
        if after is not None:
            items = [b for b in items if (b["_start"], b["_n"]) > after]
        page = items[:limit]
        next_cursor = encode_cursor(page[-1]) if len(items) > limit else None
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor, "total": total}


def availability(rid, q):
    with STORE.lock:
        if rid not in STORE.rooms:
            raise ApiError("not_found", f"room {rid} not found")
    d = q.get("date")
    if d is None:
        raise validation("date is required (YYYY-MM-DD)")
    m = DATE_RE.match(d)
    if not m:
        raise validation("date must look like YYYY-MM-DD")
    try:
        day = date(*map(int, m.groups()))
    except ValueError:
        raise validation("date is not a valid date")
    ws = datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc)
    we = ws + timedelta(hours=10)
    with STORE.lock:
        buf = timedelta(minutes=STORE.rooms[rid].get("buffer_minutes", 0))
        busy = sorted((b["_start"] - buf, b["_end"] + buf) for b in STORE.bookings.values()
                      if b["room_id"] == rid)
        busy = [(bs, be) for bs, be in busy if bs < we and be > ws]
    min_free = timedelta(minutes=15)
    free = []
    cur = ws
    for bs, be in busy:
        if bs > cur and bs - cur >= min_free:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(bs)})
        if be > cur:
            cur = be
    if cur < we and we - cur >= min_free:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": day.isoformat(), "free": free}


# ---------------------------------------------------------------- HTTP

def route(path):
    """Return (allowed_methods, handler_factory) or None."""
    parts = [unquote(p) for p in path.strip("/").split("/")] if path.strip("/") else []
    if parts == ["rooms"]:
        return {"GET": lambda h: list_rooms(), "POST": lambda h: create_room(h.json_body())}
    if len(parts) == 2 and parts[0] == "rooms" and parts[1]:
        rid = parts[1]
        return {"GET": lambda h: get_room(rid)}
    if len(parts) == 3 and parts[0] == "rooms" and parts[1] and parts[2] == "availability":
        rid = parts[1]
        return {"GET": lambda h: availability(rid, h.query())}
    if parts == ["bookings"]:
        return {"GET": lambda h: list_bookings(h.query()),
                "POST": lambda h: create_booking(h.json_body(), h.headers.get("Idempotency-Key"))}
    if len(parts) == 2 and parts[0] == "bookings" and parts[1]:
        bid = parts[1]
        return {"GET": lambda h: get_booking(bid), "DELETE": lambda h: delete_booking(bid),
                "PATCH": lambda h: patch_booking(bid, h.json_body())}
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
        pass

    def read_body(self):
        if hasattr(self, "_body"):
            return self._body
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        self._body = self.rfile.read(n) if n > 0 else b""
        return self._body

    def json_body(self):
        raw = self.read_body()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body must be valid JSON")

    def query(self):
        qs = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        return {k: v[-1] for k, v in qs.items()}

    def send(self, status, body, headers=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def handle_any(self):
        try:
            self.read_body()
            path = urlsplit(self.path).path
            methods = route(path)
            if methods is None:
                raise ApiError("not_found", f"no route for {path}")
            fn = methods.get(self.command)
            if fn is None:
                raise ApiError("method_not_allowed", f"{self.command} is not allowed on {path}",
                               {"Allow": ", ".join(sorted(methods))})
            status, body = fn(self)
            self.send(status, body)
        except ApiError as e:
            self.send(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:  # pragma: no cover
            self.send(500, {"error": {"code": "internal_error", "message": str(e)}})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = handle_any


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
