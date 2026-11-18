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
COUNTER = {"r": 0, "b": 0}

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def verr(msg):
    return ApiError(422, "validation_error", msg)


def parse_ts(s):
    if not isinstance(s, str) or not TS_RE.match(s):
        raise verr("invalid timestamp")
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("invalid timestamp")


def fmt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def create_room(body, headers):
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("invalid name")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("invalid capacity")
    with LOCK:
        if any(r["name"] == name for r in ROOMS.values()):
            raise ApiError(409, "room_exists", "room name already exists")
        COUNTER["r"] += 1
        room = {"id": f"r{COUNTER['r']}", "name": name, "capacity": cap}
        ROOMS[room["id"]] = room
        return 201, dict(room)


def get_room(rid):
    r = ROOMS.get(rid)
    if not r:
        raise ApiError(404, "not_found", "room not found")
    return r


def validate_booking(body):
    rid = body.get("room_id")
    title = body.get("title")
    att = body.get("attendees")
    if not isinstance(rid, str) or not rid:
        raise verr("invalid room_id")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise verr("invalid title")
    if not is_int(att) or att < 1:
        raise verr("invalid attendees")
    s = parse_ts(body.get("start"))
    e = parse_ts(body.get("end"))
    for d in (s, e):
        if d.minute % 15 or d.second:
            raise verr("times must be on quarter hours")
    if e <= s:
        raise verr("end must be after start")
    if e - s > timedelta(hours=8):
        raise verr("booking too long")
    return rid, s, e, title, att


def create_booking(body, headers):
    key = headers.get("Idempotency-Key")
    with LOCK:
        fp = None
        if key:
            fp = json.dumps(body, sort_keys=True, separators=(",", ":"))
            if key in IDEM:
                ofp, resp = IDEM[key]
                if ofp != fp:
                    raise ApiError(422, "idempotency_mismatch", "idempotency key reused for a different request")
                return resp[0], json.loads(resp[1])
        rid, s, e, title, att = validate_booking(body)
        room = ROOMS.get(rid)
        if not room:
            raise ApiError(404, "not_found", "room not found")
        if att > room["capacity"]:
            raise ApiError(422, "over_capacity", "too many attendees for room")
        ss, es = fmt(s), fmt(e)
        for b in BOOKINGS.values():
            if b["room_id"] == rid and b["start"] < es and ss < b["end"]:
                raise ApiError(409, "conflict", "booking overlaps an existing booking")
        COUNTER["b"] += 1
        b = {"id": f"b{COUNTER['b']}", "room_id": rid, "start": ss, "end": es,
             "title": title, "attendees": att}
        BOOKINGS[b["id"]] = b
        if key:
            IDEM[key] = (fp, (201, json.dumps(b)))
        return 201, dict(b)


def num(b):
    return int(b["id"][1:])


def list_bookings(q):
    def first(k):
        v = q.get(k)
        return v[0] if v else None

    room_id = first("room_id")
    frm, to = first("from"), first("to")
    fd = parse_ts(frm).__format__("") if False else None
    fd = fmt(parse_ts(frm)) if frm is not None else None
    td = fmt(parse_ts(to)) if to is not None else None
    if fd and td and fd >= td:
        raise verr("from must be before to")
    limit = 20
    lv = first("limit")
    if lv is not None:
        if not re.fullmatch(r"[0-9]+", lv) or not 1 <= int(lv) <= 100:
            raise verr("invalid limit")
        limit = int(lv)
    after = None
    cv = first("cursor")
    if cv is not None:
        try:
            pad = cv + "=" * (-len(cv) % 4)
            c = json.loads(base64.urlsafe_b64decode(pad.encode()).decode())
            if not (isinstance(c, list) and len(c) == 2 and isinstance(c[0], str) and is_int(c[1])):
                raise ValueError
            after = (c[0], c[1])
        except Exception:
            raise verr("invalid cursor")
    with LOCK:
        items = [b for b in BOOKINGS.values()
                 if (room_id is None or b["room_id"] == room_id)
                 and (fd is None or b["end"] > fd)
                 and (td is None or b["start"] < td)]
        items.sort(key=lambda b: (b["start"], num(b)))
        if after is not None:
            items = [b for b in items if (b["start"], num(b)) > after]
        page = items[:limit]
        nxt = None
        if len(items) > limit:
            last = page[-1]
            nxt = base64.urlsafe_b64encode(
                json.dumps([last["start"], num(last)]).encode()).decode().rstrip("=")
        return 200, {"bookings": [dict(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    with LOCK:
        room = get_room(rid)
        dv = q.get("date", [None])[0]
        if dv is None or not DATE_RE.match(dv):
            raise verr("invalid date")
        try:
            day = datetime.strptime(dv, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise verr("invalid date")
        ws, we = fmt(day + timedelta(hours=8)), fmt(day + timedelta(hours=18))
        bs = sorted((b for b in BOOKINGS.values()
                     if b["room_id"] == room["id"] and b["start"] < we and b["end"] > ws),
                    key=lambda b: b["start"])
        free = []
        cur = ws
        for b in bs:
            if b["start"] > cur:
                free.append({"start": cur, "end": b["start"]})
            if b["end"] > cur:
                cur = b["end"]
        if cur < we:
            free.append({"start": cur, "end": we})
        return 200, {"date": dv, "free": free}


def route(method, path, q, body_fn, headers):
    """Returns (status, body)."""
    parts = path.split("/")
    allowed = None
    handler = None
    if path == "/rooms":
        allowed = ["GET", "POST"]
        if method == "GET":
            handler = lambda: (200, {"rooms": [dict(r) for r in ROOMS.values()]})
        elif method == "POST":
            handler = lambda: create_room(body_fn(), headers)
    elif re.fullmatch(r"/rooms/[^/]+", path):
        allowed = ["GET"]
        if method == "GET":
            handler = lambda: (200, dict(get_room(parts[2])))
    elif re.fullmatch(r"/rooms/[^/]+/availability", path):
        allowed = ["GET"]
        if method == "GET":
            handler = lambda: availability(parts[2], q)
    elif path == "/bookings":
        allowed = ["GET", "POST"]
        if method == "GET":
            handler = lambda: list_bookings(q)
        elif method == "POST":
            handler = lambda: create_booking(body_fn(), headers)
    elif re.fullmatch(r"/bookings/[^/]+", path):
        allowed = ["GET", "DELETE"]
        if method == "GET":
            def h():
                with LOCK:
                    b = BOOKINGS.get(parts[2])
                    if not b:
                        raise ApiError(404, "not_found", "booking not found")
                    return 200, dict(b)
            handler = h
        elif method == "DELETE":
            def h():
                with LOCK:
                    if parts[2] not in BOOKINGS:
                        raise ApiError(404, "not_found", "booking not found")
                    del BOOKINGS[parts[2]]
                    return 204, None
            handler = h
    if allowed is None:
        raise ApiError(404, "not_found", "no such path")
    if handler is None:
        raise ApiError(405, "method_not_allowed", "method not allowed", {"Allow": ", ".join(allowed)})
    return handler()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, body, extra=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def handle_any(self):
        try:
            u = urlsplit(self.path)
            q = parse_qs(u.query, keep_blank_values=True)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""

            def body_fn():
                try:
                    v = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ApiError(400, "bad_json", "request body is not valid JSON")
                if not isinstance(v, dict):
                    raise ApiError(400, "bad_json", "request body must be a JSON object")
                return v

            status, body = route(self.command, u.path, q, body_fn, self.headers)
            self.send(status, body)
        except ApiError as e:
            self.send(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:
            self.send(500, {"error": {"code": "internal_error", "message": "internal error"}})

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
