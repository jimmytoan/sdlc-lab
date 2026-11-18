import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

LOCK = threading.RLock()
ROOMS = {}
BOOKINGS = {}
IDEM = {}
COUNTERS = {"r": 0, "b": 0}

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FMT = "%Y-%m-%dT%H:%M:%SZ"


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def verr(msg):
    return ApiError(422, "validation_error", msg)


def parse_ts(v, field, quarter=True):
    if not isinstance(v, str) or not TS_RE.match(v):
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        d = datetime.strptime(v, FMT)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")
    if quarter and (d.minute % 15 != 0 or d.second != 0):
        raise verr(f"{field} must be on a quarter hour")
    return d


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def fmt(d):
    return d.strftime(FMT)


def num(id_):
    return int(id_[1:])


def create_room(body):
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("name must be a non-empty string of at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("capacity must be an integer from 1 to 500")
    with LOCK:
        if any(r["name"] == name for r in ROOMS.values()):
            raise ApiError(409, "room_exists", "a room with this name already exists")
        COUNTERS["r"] += 1
        room = {"id": f"r{COUNTERS['r']}", "name": name, "capacity": cap}
        ROOMS[room["id"]] = room
        return 201, dict(room)


def create_booking(body, key):
    with LOCK:
        fp = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if key:
            if key in IDEM:
                ofp, status, resp = IDEM[key]
                if ofp != fp:
                    raise ApiError(422, "idempotency_mismatch",
                                   "Idempotency-Key was used for a different request")
                return status, json.loads(json.dumps(resp))
        rid, title, att = body.get("room_id"), body.get("title"), body.get("attendees")
        if not isinstance(rid, str) or not rid:
            raise verr("room_id must be a string")
        s = parse_ts(body.get("start"), "start")
        e = parse_ts(body.get("end"), "end")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise verr("title must be a non-empty string of at most 200 characters")
        if not is_int(att) or att < 1:
            raise verr("attendees must be an integer >= 1")
        if e <= s:
            raise verr("end must be after start")
        if e - s > timedelta(hours=8):
            raise verr("booking must be at most 8 hours long")
        room = ROOMS.get(rid)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if att > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        ss, es = fmt(s), fmt(e)
        for b in BOOKINGS.values():
            if b["room_id"] == rid and b["start"] < es and ss < b["end"]:
                raise ApiError(409, "conflict", "room is already booked for this time")
        COUNTERS["b"] += 1
        bk = {"id": f"b{COUNTERS['b']}", "room_id": rid, "start": ss, "end": es,
              "title": title, "attendees": att}
        BOOKINGS[bk["id"]] = bk
        if key:
            IDEM[key] = (fp, 201, dict(bk))
        return 201, dict(bk)


def first(q, k):
    v = q.get(k)
    return v[0] if v else None


def list_bookings(q):
    room_id = first(q, "room_id")
    frm, to = first(q, "from"), first(q, "to")
    fd = parse_ts(frm, "from", False) if frm is not None else None
    td = parse_ts(to, "to", False) if to is not None else None
    if fd and td and fd >= td:
        raise verr("from must be before to")
    lim = first(q, "limit")
    limit = 20
    if lim is not None:
        if not re.fullmatch(r"-?\d+", lim.strip() if lim else ""):
            raise verr("limit must be an integer from 1 to 100")
        limit = int(lim)
        if not 1 <= limit <= 100:
            raise verr("limit must be an integer from 1 to 100")
    cur = first(q, "cursor")
    ckey = None
    if cur is not None:
        try:
            pad = "=" * (-len(cur) % 4)
            c = json.loads(base64.urlsafe_b64decode(cur + pad).decode())
            if not (isinstance(c, list) and len(c) == 2 and isinstance(c[0], str)
                    and is_int(c[1])):
                raise ValueError
            ckey = (c[0], c[1])
        except Exception:
            raise verr("invalid cursor")
    with LOCK:
        items = []
        for b in BOOKINGS.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if fd and not b["end"] > frm:
                continue
            if td and not b["start"] < to:
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start"], num(b["id"])))
        if ckey:
            items = [b for b in items if (b["start"], num(b["id"])) > ckey]
        page = items[:limit]
        nxt = None
        if len(items) > limit:
            last = page[-1]
            nxt = base64.urlsafe_b64encode(
                json.dumps([last["start"], num(last["id"])]).encode()).decode().rstrip("=")
        return 200, {"bookings": [dict(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    with LOCK:
        if rid not in ROOMS:
            raise ApiError(404, "not_found", "room not found")
        date = first(q, "date")
        if date is None or not DATE_RE.match(date):
            raise verr("date must be YYYY-MM-DD")
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise verr("date is not a valid date")
        ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
        bs = sorted((datetime.strptime(b["start"], FMT), datetime.strptime(b["end"], FMT))
                    for b in BOOKINGS.values() if b["room_id"] == rid)
        free, cur = [], ws
        for s, e in bs:
            if e <= cur:
                continue
            if s >= we:
                break
            if s > cur:
                free.append({"start": fmt(cur), "end": fmt(s)})
            cur = max(cur, e)
            if cur >= we:
                break
        if cur < we:
            free.append({"start": fmt(cur), "end": fmt(we)})
        return 200, {"date": date, "free": free}


def route(method, path, q, body_fn, headers):
    parts = [p for p in path.split("/")[1:]]
    n = len(parts)

    def need(*allowed):
        if method not in allowed:
            err = ApiError(405, "method_not_allowed", f"{method} not allowed here")
            err.allow = ", ".join(allowed)
            raise err

    if parts == ["rooms"]:
        need("GET", "POST")
        if method == "POST":
            return create_room(body_fn())
        with LOCK:
            return 200, {"rooms": [dict(r) for r in ROOMS.values()]}
    if n == 2 and parts[0] == "rooms" and parts[1]:
        need("GET")
        with LOCK:
            r = ROOMS.get(parts[1])
            if r is None:
                raise ApiError(404, "not_found", "room not found")
            return 200, dict(r)
    if n == 3 and parts[0] == "rooms" and parts[1] and parts[2] == "availability":
        need("GET")
        return availability(parts[1], q)
    if parts == ["bookings"]:
        need("GET", "POST")
        if method == "POST":
            body = body_fn()
            key = headers.get("Idempotency-Key")
            return create_booking(body, key.strip() if key else None)
        return list_bookings(q)
    if n == 2 and parts[0] == "bookings" and parts[1]:
        need("GET", "DELETE")
        with LOCK:
            b = BOOKINGS.get(parts[1])
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            if method == "DELETE":
                del BOOKINGS[parts[1]]
                return 204, None
            return 200, dict(b)
    raise ApiError(404, "not_found", "no such path")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, payload, extra=None):
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if status != 204 and self.command != "HEAD":
            self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            v = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "bad_json", "request body must be valid JSON")
        if not isinstance(v, dict):
            raise ApiError(400, "bad_json", "request body must be a JSON object")
        return v

    def _handle(self):
        extra = {}
        try:
            # drain body for non-POST so keep-alive stays sane
            sp = urlsplit(self.path)
            q = parse_qs(sp.query, keep_blank_values=True)
            if self.command in ("POST",):
                cache = []

                def body_fn():
                    if not cache:
                        cache.append(self._body())
                    return cache[0]
            else:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 0:
                    self.rfile.read(n)
                body_fn = lambda: {}
            status, payload = route(self.command, unquote(sp.path), q, body_fn, self.headers)
        except ApiError as e:
            status = e.status
            payload = {"error": {"code": e.code, "message": e.message}}
            if hasattr(e, "allow"):
                extra["Allow"] = e.allow
        except Exception as e:  # real bug
            status = 500
            payload = {"error": {"code": "internal", "message": "internal error"}}
        self._send(status, payload, extra)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
