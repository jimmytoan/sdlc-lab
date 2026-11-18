import argparse
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

LOCK = threading.RLock()
ROOMS = {}
BOOKINGS = {}
IDEM = {}
COUNTERS = {"r": 0, "b": 0}

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def verr(msg):
    return ApiError(422, "validation_error", msg)


def parse_ts(v, field):
    if not isinstance(v, str) or not TS_RE.match(v):
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def create_room(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("name must be a non-empty string of at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("capacity must be an integer from 1 to 500")
    if any(r["name"] == name for r in ROOMS.values()):
        raise ApiError(409, "room_exists", "a room with this name already exists")
    COUNTERS["r"] += 1
    room = {"id": f"r{COUNTERS['r']}", "name": name, "capacity": cap}
    ROOMS[room["id"]] = room
    return 201, room


def create_booking(body, idem_key):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    fp = json.dumps(body, sort_keys=True)
    if idem_key is not None and idem_key in IDEM:
        ofp, status, resp = IDEM[idem_key]
        if ofp != fp:
            raise ApiError(422, "idempotency_mismatch", "Idempotency-Key was used for a different request")
        return status, resp
    rid = body.get("room_id")
    if not isinstance(rid, str) or not rid:
        raise verr("room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att) or att < 1:
        raise verr("attendees must be a positive integer")
    for dt, f in ((start, "start"), (end, "end")):
        if dt.minute % 15 or dt.second:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > timedelta(hours=8):
        raise verr("booking must be at most 8 hours long")
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if att > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    s, e = fmt(start), fmt(end)
    for b in BOOKINGS.values():
        if b["room_id"] == rid and b["start"] < e and b["end"] > s:
            raise ApiError(409, "conflict", "room is already booked in that period")
    COUNTERS["b"] += 1
    bk = {"id": f"b{COUNTERS['b']}", "room_id": rid, "start": s, "end": e, "title": title, "attendees": att}
    BOOKINGS[bk["id"]] = bk
    if idem_key is not None:
        IDEM[idem_key] = (fp, 201, bk)
    return 201, bk


def one(q, name):
    v = q.get(name)
    return v[-1] if v else None


def list_bookings(q):
    room_id = one(q, "room_id")
    frm, to = one(q, "from"), one(q, "to")
    f = fmt(parse_ts(frm, "from")) if frm is not None else None
    t = fmt(parse_ts(to, "to")) if to is not None else None
    limit = 20
    if one(q, "limit") is not None:
        try:
            limit = int(one(q, "limit"))
        except ValueError:
            raise verr("limit must be an integer")
        if not 1 <= limit <= 100:
            raise verr("limit must be between 1 and 100")
    after = None
    cur = one(q, "cursor")
    if cur is not None:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\|(\d+)$", cur)
        if not m:
            raise verr("invalid cursor")
        after = (m.group(1), int(m.group(2)))
    items = []
    for b in BOOKINGS.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if f is not None and b["end"] <= f:
            continue
        if t is not None and b["start"] >= t:
            continue
        items.append(b)
    key = lambda b: (b["start"], int(b["id"][1:]))
    items.sort(key=key)
    if after is not None:
        items = [b for b in items if key(b) > after]
    page = items[:limit]
    nxt = None
    if len(items) > limit:
        k = key(page[-1])
        nxt = f"{k[0]}|{k[1]}"
    return 200, {"bookings": page, "next_cursor": nxt}


def availability(rid, q):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    d = one(q, "date")
    if d is None or not DATE_RE.match(d):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("invalid date")
    ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
    busy = []
    for b in BOOKINGS.values():
        if b["room_id"] == rid:
            bs, be = parse_ts(b["start"], "s"), parse_ts(b["end"], "e")
            if bs < we and be > ws:
                busy.append((max(bs, ws), min(be, we)))
    busy.sort()
    free, cur = [], ws
    for bs, be in busy:
        if bs > cur:
            free.append({"start": fmt(cur), "end": fmt(bs)})
        cur = max(cur, be)
    if cur < we:
        free.append({"start": fmt(cur), "end": fmt(we)})
    return 200, {"date": d, "free": free}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")

    def route(self, method):
        u = urlsplit(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        n = len(parts)
        mine = None
        if parts[:1] == ["rooms"] and n == 1:
            mine = {"POST", "GET"}
        elif parts[:1] == ["rooms"] and n == 2:
            mine = {"GET"}
        elif parts[:1] == ["rooms"] and n == 3 and parts[2] == "availability":
            mine = {"GET"}
        elif parts[:1] == ["bookings"] and n == 1:
            mine = {"POST", "GET"}
        elif parts[:1] == ["bookings"] and n == 2:
            mine = {"GET", "DELETE"}
        if mine is None:
            raise ApiError(404, "not_found", "no such route")
        if method not in mine:
            raise ApiError(405, "method_not_allowed", "method not allowed")
        if method == "POST":
            body = self.read_body()
            with LOCK:
                if parts[0] == "rooms":
                    return create_room(body)
                return create_booking(body, self.headers.get("Idempotency-Key"))
        with LOCK:
            if parts[0] == "rooms":
                if n == 1:
                    return 200, {"rooms": list(ROOMS.values())}
                if n == 2:
                    if parts[1] not in ROOMS:
                        raise ApiError(404, "not_found", "room not found")
                    return 200, ROOMS[parts[1]]
                return availability(parts[1], q)
            if n == 1:
                return list_bookings(q)
            b = BOOKINGS.get(parts[1])
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            if method == "DELETE":
                del BOOKINGS[parts[1]]
                return 204, None
            return 200, b

    def handle_any(self, method):
        try:
            status, body = self.route(method)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, body = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        self.send(status, body)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_PATCH(self):
        self.handle_any("PATCH")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
