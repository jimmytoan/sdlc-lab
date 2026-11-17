"""Meeting room booking API (standard library only, in-memory state)."""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

LOCK = threading.Lock()
ROOMS = {}          # id -> room dict
ROOM_SEQ = [0]
BOOKINGS = {}       # id -> booking dict (with internal datetimes kept separately)
BOOKING_SEQ = [0]
IDEMPOTENCY = {}    # key -> (fingerprint, status, body)

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MAX_DURATION = timedelta(hours=8)


class ApiError(Exception):
    STATUS = {
        "not_found": 404, "method_not_allowed": 405, "bad_json": 400,
        "validation_error": 422, "over_capacity": 422, "room_exists": 409,
        "conflict": 409, "idempotency_mismatch": 422,
    }

    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.status = self.STATUS[code]
        self.headers = headers or {}


def verr(msg):
    return ApiError("validation_error", msg)


def parse_ts(value, field):
    if not isinstance(value, str):
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def id_num(i):
    return int(i[1:])


# ---------------------------------------------------------------- rooms

def create_room(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise verr("name is required and must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("capacity must be an integer from 1 to 500")
    key = name.strip().casefold()
    for r in ROOMS.values():
        if r["name"].strip().casefold() == key:
            raise ApiError("room_exists", f"a room named {name!r} already exists")
    ROOM_SEQ[0] += 1
    rid = f"r{ROOM_SEQ[0]}"
    room = {"id": rid, "name": name, "capacity": cap}
    ROOMS[rid] = room
    return 201, dict(room)


def get_room(rid):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError("not_found", f"room {rid} not found")
    return room


# ---------------------------------------------------------------- bookings

def public_booking(b):
    return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}


def create_booking(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id is required and must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise verr("title is required and must be a non-empty string")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be a positive integer")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.minute % 15 or dt.second:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_DURATION:
        raise verr("bookings may be at most 8 hours long")
    room = ROOMS.get(room_id)
    if room is None:
        raise ApiError("not_found", f"room {room_id} not found")
    if attendees > room["capacity"]:
        raise ApiError("over_capacity",
                       f"room {room_id} holds {room['capacity']} people, {attendees} requested")
    for b in BOOKINGS.values():
        if b["room_id"] == room_id and b["_start"] < end and start < b["_end"]:
            raise ApiError("conflict", f"overlaps booking {b['id']}")
    BOOKING_SEQ[0] += 1
    bid = f"b{BOOKING_SEQ[0]}"
    b = {"id": bid, "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
         "title": title, "attendees": attendees, "_start": start, "_end": end}
    BOOKINGS[bid] = b
    return 201, public_booking(b)


def encode_cursor(b):
    raw = f"{b['_start'].timestamp():.0f}:{id_num(b['id'])}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode(c + pad).decode()
        a, b = raw.split(":")
        if not re.fullmatch(r"-?\d+", a) or not re.fullmatch(r"\d+", b):
            raise ValueError
        return int(a), int(b)
    except Exception:
        raise verr("cursor is invalid")


def list_bookings(q):
    room_id = q.get("room_id")
    frm = parse_ts(q["from"], "from") if "from" in q else None
    to = parse_ts(q["to"], "to") if "to" in q else None
    if frm is not None and to is not None and frm >= to:
        raise verr("from must be before to")
    limit = 20
    if "limit" in q:
        s = q["limit"]
        if not re.fullmatch(r"\d+", s or ""):
            raise verr("limit must be an integer from 1 to 100")
        limit = int(s)
        if not 1 <= limit <= 100:
            raise verr("limit must be an integer from 1 to 100")
    after = None
    if "cursor" in q and q["cursor"] != "":
        after = decode_cursor(q["cursor"])
    items = []
    for b in BOOKINGS.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm is not None and not b["_end"] > frm:
            continue
        if to is not None and not b["_start"] < to:
            continue
        items.append(b)
    items.sort(key=lambda b: (b["_start"], id_num(b["id"])))
    if after is not None:
        items = [b for b in items
                 if (int(b["_start"].timestamp()), id_num(b["id"])) > after]
    page = items[:limit]
    nxt = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    room = get_room(rid)
    d = q.get("date")
    if d is None:
        raise verr("date is required (YYYY-MM-DD)")
    m = DATE_RE.match(d)
    if not m:
        raise verr("date must look like YYYY-MM-DD")
    try:
        day = date(*map(int, m.groups()))
    except ValueError:
        raise verr("date is not a valid date")
    ws = datetime(day.year, day.month, day.day, 8, tzinfo=timezone.utc)
    we = datetime(day.year, day.month, day.day, 18, tzinfo=timezone.utc)
    busy = sorted((b["_start"], b["_end"]) for b in BOOKINGS.values()
                  if b["room_id"] == room["id"] and b["_start"] < we and b["_end"] > ws)
    free, cur = [], ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(min(s, we))})
        cur = max(cur, e)
        if cur >= we:
            break
    if cur < we:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": day.isoformat(), "free": free}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, *a):
        pass

    def send_json(self, status, body, headers=None):
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

    def read_body(self):
        n = self.headers.get("Content-Length")
        try:
            n = int(n) if n else 0
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")

    def handle_any(self):
        try:
            # Always drain the body so keep-alive connections stay in sync.
            self._raw_body_consumed = False
            status, body, headers = self.route()
            self.send_json(status, body, headers)
        except ApiError as e:
            self.drain()
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:  # pragma: no cover
            self.drain()
            self.send_json(500, {"error": {"code": "internal", "message": str(e)}})

    def drain(self):
        if getattr(self, "_raw_body_consumed", True):
            return
        self._raw_body_consumed = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(n)
        except Exception:
            pass

    def body(self):
        self._raw_body_consumed = True
        return self.read_body()

    def route(self):
        parts = urlsplit(self.path)
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        segs = [unquote(s) for s in path.strip("/").split("/")] if path.strip("/") else []
        m = self.command

        def allow(*methods):
            if m not in methods:
                raise ApiError("method_not_allowed", f"{m} not allowed on {path}",
                               {"Allow": ", ".join(methods)})

        if segs == ["rooms"]:
            allow("GET", "POST")
            if m == "GET":
                with LOCK:
                    rooms = sorted(ROOMS.values(), key=lambda r: id_num(r["id"]))
                    return 200, {"rooms": [dict(r) for r in rooms]}, None
            body = self.body()
            with LOCK:
                s, b = create_room(body)
            return s, b, {"Location": f"/rooms/{b['id']}"}
        if len(segs) == 2 and segs[0] == "rooms":
            allow("GET")
            with LOCK:
                return 200, dict(get_room(segs[1])), None
        if len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
            allow("GET")
            with LOCK:
                s, b = availability(segs[1], q)
            return s, b, None
        if segs == ["bookings"]:
            allow("GET", "POST")
            if m == "GET":
                with LOCK:
                    s, b = list_bookings(q)
                return s, b, None
            body = self.body()
            key = self.headers.get("Idempotency-Key")
            with LOCK:
                if key:
                    fp = json.dumps(body, sort_keys=True, separators=(",", ":"))
                    prev = IDEMPOTENCY.get(key)
                    if prev is not None:
                        if prev[0] != fp:
                            raise ApiError("idempotency_mismatch",
                                           "Idempotency-Key was already used for a different request")
                        return prev[1], prev[2], {"Location": f"/bookings/{prev[2]['id']}",
                                                  "Idempotent-Replayed": "true"}
                s, b = create_booking(body)
                if key:
                    IDEMPOTENCY[key] = (fp, s, b)
            return s, b, {"Location": f"/bookings/{b['id']}"}
        if len(segs) == 2 and segs[0] == "bookings":
            allow("GET", "DELETE")
            bid = segs[1]
            with LOCK:
                b = BOOKINGS.get(bid)
                if b is None:
                    raise ApiError("not_found", f"booking {bid} not found")
                if m == "GET":
                    return 200, public_booking(b), None
                del BOOKINGS[bid]
                return 204, None, None
        raise ApiError("not_found", f"no route for {path}")

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
