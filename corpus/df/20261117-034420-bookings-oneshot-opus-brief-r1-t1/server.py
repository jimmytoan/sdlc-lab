"""Meeting room booking API. Standard library only."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

LOCK = threading.RLock()
ROOMS = {}          # id -> dict
ROOM_SEQ = [0]
BOOKINGS = {}       # id -> dict with internal datetimes
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
        self.code = code
        self.message = message
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
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def booking_public(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}


def id_num(i):
    return int(i[1:])


def sort_key(b):
    return (b["start"], id_num(b["id"]))


# ---------------- handlers ----------------

def create_room(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise verr("name is required and must be a non-empty string")
    name = name.strip()
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(cap):
        raise verr("capacity must be an integer")
    if cap < 1 or cap > 500:
        raise verr("capacity must be between 1 and 500")
    for r in ROOMS.values():
        if r["name"] == name:
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


def create_booking(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id is required and must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_DURATION:
        raise verr("bookings may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise verr("title is required and must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att):
        raise verr("attendees must be an integer")
    if att < 1:
        raise verr("attendees must be at least 1")
    room = get_room(room_id)
    if att > room["capacity"]:
        raise ApiError("over_capacity",
                       f"{att} attendees exceed room capacity of {room['capacity']}")
    for b in BOOKINGS.values():
        if b["room_id"] == room_id and b["start"] < end and start < b["end"]:
            raise ApiError("conflict", f"overlaps booking {b['id']}")
    BOOKING_SEQ[0] += 1
    bid = f"b{BOOKING_SEQ[0]}"
    b = {"id": bid, "room_id": room_id, "start": start, "end": end,
         "title": title, "attendees": att}
    BOOKINGS[bid] = b
    return 201, booking_public(b)


def first(q, name):
    v = q.get(name)
    if not v:
        return None
    return v[0]


def encode_cursor(b):
    raw = f"{fmt_ts(b['start'])}|{id_num(b['id'])}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode(c + pad).decode()
        s, n = raw.split("|")
        start = parse_ts(s, "cursor")
        if not n.isdigit():
            raise ValueError
        return (start, int(n))
    except Exception:
        raise verr("invalid cursor")


def list_bookings(q):
    room_id = first(q, "room_id")
    frm = first(q, "from")
    to = first(q, "to")
    frm_dt = parse_ts(frm, "from") if frm is not None else None
    to_dt = parse_ts(to, "to") if to is not None else None
    if frm_dt is not None and to_dt is not None and frm_dt >= to_dt:
        raise verr("from must be before to")
    limit_s = first(q, "limit")
    limit = 20
    if limit_s is not None:
        if not re.fullmatch(r"\d+", limit_s.strip() if limit_s else ""):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(limit_s)
        if limit < 1 or limit > 100:
            raise verr("limit must be an integer between 1 and 100")
    cursor = first(q, "cursor")
    after = decode_cursor(cursor) if cursor not in (None, "") else None
    items = []
    for b in BOOKINGS.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm_dt is not None and b["end"] <= frm_dt:
            continue
        if to_dt is not None and b["start"] >= to_dt:
            continue
        if after is not None and sort_key(b) <= after:
            continue
        items.append(b)
    items.sort(key=sort_key)
    page = items[:limit]
    nxt = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [booking_public(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    get_room(rid)
    d = first(q, "date")
    if d is None or not DATE_RE.match(d):
        raise verr("date is required, format YYYY-MM-DD")
    try:
        day = date.fromisoformat(d)
    except ValueError:
        raise verr("date is not a valid date")
    ws = datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc)
    we = datetime(day.year, day.month, day.day, 18, 0, tzinfo=timezone.utc)
    busy = sorted((max(b["start"], ws), min(b["end"], we)) for b in BOOKINGS.values()
                  if b["room_id"] == rid and b["start"] < we and b["end"] > ws)
    free = []
    cur = ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < we:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
    return 200, {"date": day.isoformat(), "free": free}


# ---------------- routing ----------------

def route(path):
    """Returns (route_name, params, allowed_methods) or raises not_found."""
    parts = [p for p in path.split("/") if p != ""]
    if parts == ["rooms"]:
        return "rooms", {}, ["GET", "POST"]
    if len(parts) == 2 and parts[0] == "rooms":
        return "room", {"id": parts[1]}, ["GET"]
    if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "availability":
        return "availability", {"id": parts[1]}, ["GET"]
    if parts == ["bookings"]:
        return "bookings", {}, ["GET", "POST"]
    if len(parts) == 2 and parts[0] == "bookings":
        return "booking", {"id": parts[1]}, ["GET", "DELETE"]
    raise ApiError("not_found", "no such resource")


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, obj, headers=None):
        data = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _parse_json(self, raw):
        if not raw.strip():
            raise ApiError("bad_json", "request body must be JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")

    def _handle(self):
        method = self.command
        raw = self._read_body()
        try:
            parts = urlsplit(self.path)
            q = parse_qs(parts.query, keep_blank_values=True)
            name, params, allowed = route(parts.path)
            if method not in allowed:
                raise ApiError("method_not_allowed", f"{method} not allowed",
                               {"Allow": ", ".join(allowed)})
            with LOCK:
                status, body = self._dispatch(name, params, method, q, raw)
            self._send(status, body)
        except ApiError as e:
            self._send(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:  # pragma: no cover
            self._send(500, {"error": {"code": "internal", "message": str(e)}})

    def _idempotent(self, key, path, body, fn):
        if key is None:
            return fn(body)
        fp = json.dumps({"path": path, "body": body}, sort_keys=True)
        if key in IDEMPOTENCY:
            ofp, st, ob = IDEMPOTENCY[key]
            if ofp != fp:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return st, json.loads(json.dumps(ob))
        st, ob = fn(body)
        if 200 <= st < 300:
            IDEMPOTENCY[key] = (fp, st, json.loads(json.dumps(ob)))
        return st, ob

    def _dispatch(self, name, params, method, q, raw):
        key = self.headers.get("Idempotency-Key")
        if key is not None and key == "":
            key = None
        if name == "rooms":
            if method == "GET":
                rooms = sorted(ROOMS.values(), key=lambda r: id_num(r["id"]))
                return 200, {"rooms": [dict(r) for r in rooms]}
            body = self._parse_json(raw)
            return self._idempotent(key, "/rooms", body, create_room)
        if name == "room":
            return 200, dict(get_room(params["id"]))
        if name == "availability":
            return availability(params["id"], q)
        if name == "bookings":
            if method == "GET":
                return list_bookings(q)
            body = self._parse_json(raw)
            return self._idempotent(key, "/bookings", body, create_booking)
        if name == "booking":
            bid = params["id"]
            b = BOOKINGS.get(bid)
            if b is None:
                raise ApiError("not_found", f"booking {bid} not found")
            if method == "GET":
                return 200, booking_public(b)
            del BOOKINGS[bid]
            return 204, None
        raise ApiError("not_found", "no such resource")

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
