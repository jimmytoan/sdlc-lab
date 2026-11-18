import argparse
import base64
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
COUNT = {"r": 0, "b": 0}

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def verr(msg):
    return ApiError(422, "validation_error", msg)


def notfound(msg="not found"):
    return ApiError(404, "not_found", msg)


def parse_ts(v, name):
    if not isinstance(v, str) or not TS_RE.match(v):
        raise verr(f"{name} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{name} is not a valid timestamp")


def fmt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def create_room(body, _h):
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("name must be a non-empty string of at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("capacity must be an integer from 1 to 500")
    if any(r["name"] == name for r in ROOMS.values()):
        raise ApiError(409, "room_exists", "a room with this name exists")
    COUNT["r"] += 1
    room = {"id": f"r{COUNT['r']}", "name": name, "capacity": cap}
    ROOMS[room["id"]] = room
    return 201, dict(room)


def create_booking(body, headers):
    key = headers.get("Idempotency-Key")
    fp = json.dumps(body, sort_keys=True, separators=(",", ":")) if key else None
    if key:
        if key in IDEM:
            ofp, st, resp = IDEM[key]
            if ofp != fp:
                raise ApiError(422, "idempotency_mismatch", "idempotency key reused with a different request")
            return st, json.loads(json.dumps(resp))
    rid = body.get("room_id")
    if not isinstance(rid, str) or not rid:
        raise verr("room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title, att = body.get("title"), body.get("attendees")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise verr("title must be a non-empty string of at most 200 characters")
    if not is_int(att) or att < 1:
        raise verr("attendees must be a positive integer")
    for d in (start, end):
        if d.minute % 15 or d.second:
            raise verr("times must be on quarter hours")
    if end <= start:
        raise verr("end must be after start")
    if end - start > timedelta(hours=8):
        raise verr("booking may be at most 8 hours long")
    room = ROOMS.get(rid)
    if room is None:
        raise notfound("room not found")
    if att > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than room capacity")
    for b in BOOKINGS.values():
        if b["room_id"] == rid and b["_s"] < end and start < b["_e"]:
            raise ApiError(409, "conflict", "room already booked in that period")
    COUNT["b"] += 1
    n = COUNT["b"]
    b = {"id": f"b{n}", "room_id": rid, "start": fmt(start), "end": fmt(end), "title": title,
         "attendees": att, "_s": start, "_e": end, "_n": n}
    BOOKINGS[b["id"]] = b
    resp = pub(b)
    if key:
        IDEM[key] = (fp, 201, json.loads(json.dumps(resp)))
    return 201, resp


def pub(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def list_bookings(q):
    def one(name):
        v = q.get(name)
        return v[-1] if v else None

    room_id = one("room_id")
    frm, to = one("from"), one("to")
    f = parse_ts(frm, "from") if frm is not None else None
    t = parse_ts(to, "to") if to is not None else None
    if f and t and f >= t:
        raise verr("from must be before to")
    limit = 20
    lv = one("limit")
    if lv is not None:
        if not re.fullmatch(r"[0-9]+", lv) or not 1 <= int(lv) <= 100:
            raise verr("limit must be an integer from 1 to 100")
        limit = int(lv)
    after = None
    cur = one("cursor")
    if cur is not None:
        try:
            s, n = base64.urlsafe_b64decode(cur.encode("ascii") + b"=" * (-len(cur) % 4)).decode().split("|")
            after = (datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), int(n))
        except Exception:
            raise verr("invalid cursor")
    items = [b for b in BOOKINGS.values()
             if (room_id is None or b["room_id"] == room_id)
             and (f is None or b["_e"] > f) and (t is None or b["_s"] < t)]
    items.sort(key=lambda b: (b["_s"], b["_n"]))
    if after:
        items = [b for b in items if (b["_s"], b["_n"]) > after]
    page = items[:limit]
    nxt = None
    if len(items) > limit:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(f"{fmt(last['_s'])}|{last['_n']}".encode()).decode().rstrip("=")
    return 200, {"bookings": [pub(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    room = ROOMS.get(rid)
    if room is None:
        raise notfound("room not found")
    dv = q.get("date", [None])[-1]
    if dv is None or not DATE_RE.match(dv):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(dv, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("invalid date")
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
    return 200, {"date": dv, "free": free}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, payload=None, extra=None):
        data = b"" if status == 204 else json.dumps(payload).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")
        if not isinstance(obj, dict):
            raise verr("request body must be a JSON object")
        return obj

    def handle_any(self):
        try:
            try:
                status, payload, extra = self.route()
            except ApiError as e:
                status, payload, extra = e.status, {"error": {"code": e.code, "message": e.message}}, e.headers
            except Exception:
                status, payload, extra = 500, {"error": {"code": "internal_error", "message": "internal error"}}, {}
            self.send(status, payload, extra)
        except (BrokenPipeError, ConnectionError):
            pass

    def route(self):
        m = self.command
        u = urlsplit(self.path)
        path = u.path
        q = parse_qs(u.query, keep_blank_values=True)
        routes = [
            (r"/rooms", {"GET", "POST"}),
            (r"/rooms/([^/]+)", {"GET"}),
            (r"/rooms/([^/]+)/availability", {"GET"}),
            (r"/bookings", {"GET", "POST"}),
            (r"/bookings/([^/]+)", {"GET", "DELETE"}),
        ]
        for pat, methods in routes:
            mt = re.fullmatch(pat, path)
            if not mt:
                continue
            if m not in methods:
                raise ApiError(405, "method_not_allowed", "method not allowed",
                               {"Allow": ", ".join(sorted(methods))})
            a = mt.groups()
            with LOCK:
                if pat == r"/rooms":
                    if m == "POST":
                        return (*create_room(self.body(), self.headers), {})
                    return 200, {"rooms": [dict(r) for r in ROOMS.values()]}, {}
                if pat == r"/rooms/([^/]+)":
                    if a[0] not in ROOMS:
                        raise notfound("room not found")
                    return 200, dict(ROOMS[a[0]]), {}
                if pat.endswith("availability"):
                    return (*availability(a[0], q), {})
                if pat == r"/bookings":
                    if m == "POST":
                        return (*create_booking(self.body(), self.headers), {})
                    return (*list_bookings(q), {})
                b = BOOKINGS.get(a[0])
                if b is None:
                    raise notfound("booking not found")
                if m == "DELETE":
                    del BOOKINGS[a[0]]
                    return 204, None, {}
                return 200, pub(b), {}
        raise notfound("no such path")

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = do_OPTIONS = handle_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
