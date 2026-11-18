import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

STATUS = {
    "not_found": 404, "method_not_allowed": 405, "bad_json": 400,
    "validation_error": 422, "over_capacity": 422, "room_exists": 409,
    "conflict": 409, "idempotency_mismatch": 422,
}


class ApiError(Exception):
    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.status = STATUS[code]
        self.code = code
        self.message = message
        self.headers = headers or {}


def verr(msg):
    return ApiError("validation_error", msg)


LOCK = threading.Lock()
rooms = {}
room_names = set()
bookings = {}
idem = {}
counters = {"room": 0, "booking": 0}

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def create_room(body):
    name, cap = body.get("name"), body.get("capacity")
    if not nonempty_str(name):
        raise verr("name must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("capacity must be an integer from 1 to 500")
    if name in room_names:
        raise ApiError("room_exists", "a room with this name already exists")
    counters["room"] += 1
    room = {"id": f"r{counters['room']}", "name": name, "capacity": cap}
    rooms[room["id"]] = room
    room_names.add(name)
    return 201, room


def create_booking(body, key):
    if key:
        try:
            fp = json.dumps(body, sort_keys=True)
        except Exception:
            fp = repr(body)
        if key in idem:
            ofp, st, rb = idem[key]
            if ofp != fp:
                raise ApiError("idempotency_mismatch", "Idempotency-Key was used with a different request")
            return st, rb
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise verr("room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title = body.get("title")
    if not nonempty_str(title):
        raise verr("title must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att) or att < 1:
        raise verr("attendees must be an integer of at least 1")
    for d, n in ((start, "start"), (end, "end")):
        if d.minute not in (0, 15, 30, 45) or d.second != 0:
            raise verr(f"{n} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > timedelta(hours=8):
        raise verr("booking must be at most 8 hours long")
    room = rooms.get(room_id)
    if room is None:
        raise ApiError("not_found", "room not found")
    if att > room["capacity"]:
        raise ApiError("over_capacity", "attendees exceed room capacity")
    for b in bookings.values():
        if b["room_id"] == room_id and b["_s"] < end and start < b["_e"]:
            raise ApiError("conflict", "room is already booked for that time")
    counters["booking"] += 1
    bid = f"b{counters['booking']}"
    rec = {"id": bid, "room_id": room_id, "start": fmt(start), "end": fmt(end),
           "title": title, "attendees": att, "_s": start, "_e": end, "_n": counters["booking"]}
    bookings[bid] = rec
    out = public(rec)
    if key:
        idem[key] = (fp, 201, out)
    return 201, out


def public(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def one(q, name):
    v = q.get(name)
    return v[-1] if v else None


def list_bookings(q):
    room_id = one(q, "room_id")
    f, t = one(q, "from"), one(q, "to")
    fd = parse_ts(f, "from") if f is not None else None
    td = parse_ts(t, "to") if t is not None else None
    if fd and td and td <= fd:
        raise verr("to must be after from")
    lim = one(q, "limit")
    limit = 20
    if lim is not None:
        if not re.match(r"^\d+$", lim) or not 1 <= int(lim) <= 100:
            raise verr("limit must be an integer from 1 to 100")
        limit = int(lim)
    cur = one(q, "cursor")
    pos = None
    if cur is not None:
        try:
            raw = base64.urlsafe_b64decode(cur.encode("ascii") + b"=" * (-len(cur) % 4)).decode()
            s, n = raw.split("|")
            pos = (parse_ts(s, "cursor"), int(n))
        except Exception:
            raise verr("invalid cursor")
    items = []
    for b in bookings.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if td and not b["_s"] < td:
            continue
        if fd and not b["_e"] > fd:
            continue
        items.append(b)
    items.sort(key=lambda b: (b["_s"], b["_n"]))
    if pos:
        items = [b for b in items if (b["_s"], b["_n"]) > pos]
    page = items[:limit]
    nxt = None
    if len(items) > limit:
        last = page[-1]
        nxt = base64.urlsafe_b64encode(f"{last['start']}|{last['_n']}".encode()).decode()
    return 200, {"bookings": [public(b) for b in page], "next_cursor": nxt}


def availability(room_id, q):
    room = rooms.get(room_id)
    if room is None:
        raise ApiError("not_found", "room not found")
    d = one(q, "date")
    if d is None or not DATE_RE.match(d):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise verr("date is not valid")
    ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
    bs = sorted((b["_s"], b["_e"]) for b in bookings.values()
                if b["room_id"] == room_id and b["_s"] < we and b["_e"] > ws)
    free = []
    cur = ws
    for s, e in bs:
        if s > cur:
            free.append({"start": fmt(cur), "end": fmt(s)})
        if e > cur:
            cur = e
    if cur < we:
        free.append({"start": fmt(cur), "end": fmt(we)})
    return 200, {"date": d, "free": free}


def route(method, parts, q, body_fn, headers):
    """Returns (status, body)."""
    n = len(parts)
    if n >= 1 and parts[0] == "rooms" and n <= 3:
        if n == 1:
            allowed = ("GET", "POST")
        elif n == 2:
            allowed = ("GET",)
        elif parts[2] == "availability":
            allowed = ("GET",)
        else:
            allowed = None
    elif n >= 1 and parts[0] == "bookings" and n <= 2:
        allowed = ("GET", "POST") if n == 1 else ("GET", "DELETE")
    else:
        allowed = None
    if allowed is None or "" in parts:
        raise ApiError("not_found", "no such path")
    if method not in allowed:
        raise ApiError("method_not_allowed", "method not allowed",
                       {"Allow": ", ".join(allowed)})
    if parts[0] == "rooms":
        if n == 1:
            if method == "POST":
                body = body_fn()
                with LOCK:
                    return create_room(body)
            with LOCK:
                return 200, {"rooms": list(rooms.values())}
        with LOCK:
            if n == 3:
                return availability(parts[1], q)
            r = rooms.get(parts[1])
            if r is None:
                raise ApiError("not_found", "room not found")
            return 200, r
    if n == 1:
        if method == "POST":
            body = body_fn()
            key = headers.get("Idempotency-Key")
            with LOCK:
                return create_booking(body, key)
        with LOCK:
            return list_bookings(q)
    with LOCK:
        b = bookings.get(parts[1])
        if b is None:
            raise ApiError("not_found", "booking not found")
        if method == "DELETE":
            del bookings[parts[1]]
            return 204, None
        return 200, public(b)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, status, body, extra=None):
        data = b"" if status == 204 else json.dumps(body).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        self._read = True
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")
        if not isinstance(obj, dict):
            raise verr("request body must be a JSON object")
        return obj

    def handle_any(self):
        try:
            self._read = False
            try:
                u = urlsplit(self.path)
                parts = u.path.split("/")[1:]
                q = parse_qs(u.query, keep_blank_values=True)
                status, body = route(self.command, parts, q, self.read_body, self.headers)
                self.send_json(status, body)
            except ApiError as e:
                self.drain()
                self.send_json(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self.send_json(500, {"error": {"code": "internal_error", "message": "internal error"}})
            except Exception:
                pass

    def drain(self):
        if not getattr(self, "_read", False):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 0:
                    self.rfile.read(n)
            except Exception:
                pass

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = handle_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    a = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
