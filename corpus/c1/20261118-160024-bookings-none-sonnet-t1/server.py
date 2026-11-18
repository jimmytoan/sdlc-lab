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


def create_booking(body, key):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
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
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    if not is_int(att) or att < 1:
        raise verr("attendees must be a positive integer")
    for d, f in ((start, "start"), (end, "end")):
        if d.minute % 15 or d.second:
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
    for b in BOOKINGS.values():
        if b["room_id"] == rid and b["_s"] < end and start < b["_e"]:
            raise ApiError(409, "conflict", "room is already booked in this time range")
    COUNTERS["b"] += 1
    bk = {"id": f"b{COUNTERS['b']}", "room_id": rid, "start": fmt(start), "end": fmt(end),
          "title": title, "attendees": att, "_s": start, "_e": end, "_n": COUNTERS["b"]}
    BOOKINGS[bk["id"]] = bk
    resp = (201, pub(bk))
    if key is not None:
        IDEM[key] = (fp, resp)
    return resp


def pub(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def list_bookings(q):
    def one(n):
        v = q.get(n)
        return v[-1] if v else None
    items = list(BOOKINGS.values())
    rid = one("room_id")
    if rid is not None:
        items = [b for b in items if b["room_id"] == rid]
    frm, to = one("from"), one("to")
    if frm is not None:
        f = parse_ts(frm, "from")
        items = [b for b in items if b["_e"] > f]
    if to is not None:
        t = parse_ts(to, "to")
        items = [b for b in items if b["_s"] < t]
    limit = 20
    lv = one("limit")
    if lv is not None:
        if not re.fullmatch(r"\d+", lv) or not 1 <= int(lv) <= 100:
            raise verr("limit must be an integer from 1 to 100")
        limit = int(lv)
    items.sort(key=lambda b: (b["_s"], b["_n"]))
    cur = one("cursor")
    if cur is not None:
        try:
            s, n = base64.urlsafe_b64decode(cur.encode()).decode().split("|")
            ck = (parse_ts(s, "cursor"), int(n))
        except Exception:
            raise verr("invalid cursor")
        items = [b for b in items if (b["_s"], b["_n"]) > ck]
    page = items[:limit]
    nxt = None
    if len(items) > limit:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(f"{fmt(last['_s'])}|{last['_n']}".encode()).decode()
    return 200, {"bookings": [pub(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    ds = q.get("date", [None])[-1]
    if ds is None or not DATE_RE.match(ds):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("date is not a valid date")
    ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
    busy = sorted((b["_s"], b["_e"]) for b in BOOKINGS.values()
                  if b["room_id"] == rid and b["_s"] < we and b["_e"] > ws)
    free, cur = [], ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt(cur), "end": fmt(s)})
        cur = max(cur, e)
    if cur < we:
        free.append({"start": fmt(cur), "end": fmt(we)})
    return 200, {"date": ds, "free": free}


def route(method, path, q, body_fn, headers):
    parts = [p for p in path.split("/") if p]
    n = len(parts)

    def allow(*ms):
        if method not in ms:
            raise ApiError(405, "method_not_allowed", f"{method} not allowed here")

    if parts == ["rooms"]:
        allow("GET", "POST")
        if method == "GET":
            return 200, {"rooms": list(ROOMS.values())}
        return create_room(body_fn())
    if n == 2 and parts[0] == "rooms":
        allow("GET")
        r = ROOMS.get(parts[1])
        if r is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, r
    if n == 3 and parts[0] == "rooms" and parts[2] == "availability":
        allow("GET")
        return availability(parts[1], q)
    if parts == ["bookings"]:
        allow("GET", "POST")
        if method == "GET":
            return list_bookings(q)
        return create_booking(body_fn(), headers.get("Idempotency-Key"))
    if n == 2 and parts[0] == "bookings":
        allow("GET", "DELETE")
        b = BOOKINGS.get(parts[1])
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        if method == "DELETE":
            del BOOKINGS[parts[1]]
            return 204, None
        return 200, pub(b)
    raise ApiError(404, "not_found", "no such path")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_any(self):
        try:
            u = urlsplit(self.path)
            q = parse_qs(u.query, keep_blank_values=True)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""

            def body_fn():
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ApiError(400, "bad_json", "request body is not valid JSON")

            with LOCK:
                status, payload = route(self.command, u.path, q, body_fn, self.headers)
        except ApiError as e:
            status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, payload = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
