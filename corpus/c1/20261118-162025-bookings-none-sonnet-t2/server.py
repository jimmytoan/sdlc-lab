import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

LOCK = threading.Lock()
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


def fmt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def create_room(body, headers):
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


def overlaps(b, s, e):
    return b["_s"] < e and b["_e"] > s


def pub(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def create_booking(body, headers):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    key = headers.get("Idempotency-Key")
    fp = json.dumps(body, sort_keys=True)
    if key is not None and key in IDEM:
        ofp, resp = IDEM[key]
        if ofp != fp:
            raise ApiError(422, "idempotency_mismatch", "Idempotency-Key was used for a different request")
        return resp
    rid = body.get("room_id")
    title = body.get("title")
    att = body.get("attendees")
    if not isinstance(rid, str) or not rid:
        raise verr("room_id must be a string")
    s = parse_ts(body.get("start"), "start")
    e = parse_ts(body.get("end"), "end")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    if not is_int(att) or att < 1:
        raise verr("attendees must be a positive integer")
    for d, f in ((s, "start"), (e, "end")):
        if d.minute % 15 or d.second:
            raise verr(f"{f} must be on a quarter hour")
    if e <= s:
        raise verr("end must be after start")
    if e - s > timedelta(hours=8):
        raise verr("booking must be at most 8 hours long")
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if att > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    for b in BOOKINGS.values():
        if b["room_id"] == rid and overlaps(b, s, e):
            raise ApiError(409, "conflict", "room is already booked for that time")
    COUNTERS["b"] += 1
    n = COUNTERS["b"]
    b = {"id": f"b{n}", "room_id": rid, "start": fmt(s), "end": fmt(e), "title": title,
         "attendees": att, "_s": s, "_e": e, "_n": n}
    BOOKINGS[b["id"]] = b
    resp = (201, pub(b))
    if key is not None:
        IDEM[key] = (fp, resp)
    return resp


def list_bookings(q):
    def one(name):
        v = q.get(name)
        return v[-1] if v else None

    room_id = one("room_id")
    f, t = one("from"), one("to")
    fd = parse_ts(f, "from") if f is not None else None
    td = parse_ts(t, "to") if t is not None else None
    limit = 20
    lv = one("limit")
    if lv is not None:
        if not re.fullmatch(r"\d+", lv) or not 1 <= int(lv) <= 100:
            raise verr("limit must be an integer from 1 to 100")
        limit = int(lv)
    after = None
    cur = one("cursor")
    if cur is not None:
        try:
            ds, dn = base64.urlsafe_b64decode(cur.encode()).decode().split("|")
            after = (parse_ts(ds, "cursor"), int(dn))
        except Exception:
            raise verr("invalid cursor")
    items = sorted(BOOKINGS.values(), key=lambda b: (b["_s"], b["_n"]))
    out = []
    for b in items:
        if room_id is not None and b["room_id"] != room_id:
            continue
        if fd is not None and b["_e"] <= fd:
            continue
        if td is not None and b["_s"] >= td:
            continue
        if after is not None and (b["_s"], b["_n"]) <= after:
            continue
        out.append(b)
    page = out[:limit]
    nxt = None
    if len(out) > limit:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(f"{fmt(last['_s'])}|{last['_n']}".encode()).decode()
    return 200, {"bookings": [pub(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    v = q.get("date")
    v = v[-1] if v else None
    if v is None or not DATE_RE.match(v):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("date is not a valid date")
    ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
    busy = sorted((b["_s"], b["_e"]) for b in BOOKINGS.values()
                  if b["room_id"] == rid and overlaps(b, ws, we))
    free, cur = [], ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt(cur), "end": fmt(min(s, we))})
        cur = max(cur, e)
    if cur < we:
        free.append({"start": fmt(cur), "end": fmt(we)})
    return 200, {"date": v, "free": free}


def route(method, path, q, body_fn, headers):
    parts = [p for p in path.split("/") if p]
    allowed = None
    if parts == ["rooms"]:
        allowed = {"GET", "POST"}
    elif len(parts) == 2 and parts[0] == "rooms":
        allowed = {"GET"}
    elif len(parts) == 3 and parts[0] == "rooms" and parts[2] == "availability":
        allowed = {"GET"}
    elif parts == ["bookings"]:
        allowed = {"GET", "POST"}
    elif len(parts) == 2 and parts[0] == "bookings":
        allowed = {"GET", "DELETE"}
    if allowed is None:
        raise ApiError(404, "not_found", "no such path")
    if method not in allowed:
        raise ApiError(405, "method_not_allowed", f"{method} not allowed here")
    with LOCK:
        if parts == ["rooms"]:
            if method == "GET":
                return 200, {"rooms": list(ROOMS.values())}
            return create_room(body_fn(), headers)
        if parts[0] == "rooms" and len(parts) == 2:
            r = ROOMS.get(parts[1])
            if r is None:
                raise ApiError(404, "not_found", "room not found")
            return 200, r
        if parts[0] == "rooms":
            return availability(parts[1], q)
        if parts == ["bookings"]:
            if method == "GET":
                return list_bookings(q)
            return create_booking(body_fn(), headers)
        b = BOOKINGS.get(parts[1])
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        if method == "DELETE":
            del BOOKINGS[parts[1]]
            return 204, None
        return 200, pub(b)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_any(self):
        try:
            u = urlsplit(self.path)
            q = parse_qs(u.query, keep_blank_values=True)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""

            def body_fn():
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ApiError(400, "bad_json", "request body is not valid JSON")

            status, data = route(self.command, u.path, q, body_fn, self.headers)
        except ApiError as e:
            status, data = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, data = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        payload = b"" if data is None else json.dumps(data).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
