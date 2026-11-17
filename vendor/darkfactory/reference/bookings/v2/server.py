"""Reference implementation of specs/bookings/SPEC.md. Never shown to a coding agent."""
import argparse
import datetime as dt
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

V2 = True
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ROOMS, BOOKINGS, IDEM = [], [], {}
COUNTERS = {"r": 0, "b": 0}
UTC = dt.timezone.utc


class Err(Exception):
    def __init__(self, status, code, message=""):
        self.status, self.code, self.message = status, code, message or code


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_ts(s):
    if not isinstance(s, str) or not TS_RE.match(s):
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def room_by_id(rid):
    for r in ROOMS:
        if r["id"] == rid:
            return r
    return None


def booking_by_id(bid):
    for b in BOOKINGS:
        if b["id"] == bid:
            return b
    return None


def public(b):
    return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}


def create_room(body):
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= 50):
        raise Err(422, "validation_error", "name")
    if not is_int(cap) or not (1 <= cap <= 500):
        raise Err(422, "validation_error", "capacity")
    buf = 0
    if V2 and "buffer_minutes" in body:
        buf = body["buffer_minutes"]
        if not is_int(buf) or not (0 <= buf <= 60):
            raise Err(422, "validation_error", "buffer_minutes")
    name = name.strip()
    if any(r["name"].lower() == name.lower() for r in ROOMS):
        raise Err(409, "room_exists")
    COUNTERS["r"] += 1
    room = {"id": f"r{COUNTERS['r']}", "name": name, "capacity": cap}
    if V2:
        room["buffer_minutes"] = buf
    ROOMS.append(room)
    return 201, room


def validate_booking(room_id, start, end, title, attendees):
    if not isinstance(room_id, str) or not isinstance(title, str) or not is_int(attendees):
        raise Err(422, "validation_error", "types")
    s, e = parse_ts(start), parse_ts(end)
    if s is None or e is None:
        raise Err(422, "validation_error", "timestamp")
    for t in (s, e):
        if t.minute % 15 or t.second:
            raise Err(422, "validation_error", "alignment")
    if e <= s or e - s > dt.timedelta(hours=8):
        raise Err(422, "validation_error", "duration")
    if not (1 <= len(title.strip()) <= 100) or attendees < 1:
        raise Err(422, "validation_error", "title/attendees")
    return s, e


def check_room_rules(room_id, s, e, attendees, ignore_id=None):
    room = room_by_id(room_id)
    if room is None:
        raise Err(404, "not_found", "room")
    if attendees > room["capacity"]:
        raise Err(422, "over_capacity")
    buf = dt.timedelta(minutes=room.get("buffer_minutes", 0))
    for b in BOOKINGS:
        if b["room_id"] == room_id and b["id"] != ignore_id and s < b["e"] + buf and b["s"] < e + buf:
            raise Err(409, "conflict")


def create_booking(body, key):
    if key and key in IDEM:
        saved_body, saved_resp = IDEM[key]
        if saved_body == body:
            return 201, saved_resp
        raise Err(422, "idempotency_mismatch")
    s, e = validate_booking(*(body.get(k) for k in ("room_id", "start", "end", "title", "attendees")))
    check_room_rules(body["room_id"], s, e, body["attendees"])
    COUNTERS["b"] += 1
    b = {"id": f"b{COUNTERS['b']}", "room_id": body["room_id"], "start": fmt(s), "end": fmt(e),
         "title": body["title"].strip(), "attendees": body["attendees"], "s": s, "e": e, "n": COUNTERS["b"]}
    BOOKINGS.append(b)
    if key:
        IDEM[key] = (body, public(b))
    return 201, public(b)


def patch_booking(bid, body):
    b = booking_by_id(bid)
    if b is None:
        raise Err(404, "not_found")
    merged = {k: body.get(k, b[k]) for k in ("start", "end", "title", "attendees")}
    s, e = validate_booking(b["room_id"], merged["start"], merged["end"], merged["title"], merged["attendees"])
    check_room_rules(b["room_id"], s, e, merged["attendees"], ignore_id=bid)
    b.update(start=fmt(s), end=fmt(e), title=merged["title"].strip(), attendees=merged["attendees"], s=s, e=e)
    return 200, public(b)


def list_bookings(q):
    def one(name):
        return q[name][0] if name in q else None

    items = list(BOOKINGS)
    if one("room_id") is not None:
        items = [b for b in items if b["room_id"] == one("room_id")]
    for name in ("from", "to"):
        if one(name) is not None:
            t = parse_ts(one(name))
            if t is None:
                raise Err(422, "validation_error", name)
            items = [b for b in items if (b["e"] > t if name == "from" else b["s"] < t)]
    limit = 20
    if one("limit") is not None:
        if not re.match(r"^\d+$", one("limit")) or not (1 <= int(one("limit")) <= 100):
            raise Err(422, "validation_error", "limit")
        limit = int(one("limit"))
    items.sort(key=lambda b: (b["s"], b["n"]))
    total = len(items)
    if one("cursor") is not None:
        m = re.match(r"^c(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)_(\d+)$", one("cursor"))
        if not m or parse_ts(m.group(1)) is None:
            raise Err(422, "validation_error", "cursor")
        after = (parse_ts(m.group(1)), int(m.group(2)))
        items = [b for b in items if (b["s"], b["n"]) > after]
    page, rest = items[:limit], items[limit:]
    nxt = f"c{page[-1]['start']}_{page[-1]['n']}" if rest else None
    out = {"bookings": [public(b) for b in page], "next_cursor": nxt}
    if V2:
        out["total"] = total
    return 200, out


def availability(rid, q):
    room = room_by_id(rid)
    if room is None:
        raise Err(404, "not_found")
    date = q.get("date", [None])[0]
    try:
        if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            raise ValueError
        day = dt.date.fromisoformat(date)
    except ValueError:
        raise Err(422, "validation_error", "date")
    lo = dt.datetime(day.year, day.month, day.day, 8, tzinfo=UTC)
    hi = dt.datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
    buf = dt.timedelta(minutes=room.get("buffer_minutes", 0))
    busy = sorted((max(b["s"] - buf, lo), min(b["e"] + buf, hi)) for b in BOOKINGS
                  if b["room_id"] == rid and b["s"] - buf < hi and b["e"] + buf > lo)
    free, cur = [], lo
    for s, e in busy:
        if s > cur:
            free.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        free.append((cur, hi))
    if V2:
        free = [(s, e) for s, e in free if e - s >= dt.timedelta(minutes=15)]
    return 200, {"date": date, "free": [{"start": fmt(s), "end": fmt(e)} for s, e in free]}


ROUTES = [
    (re.compile(r"^/rooms$"), {"GET", "POST"}),
    (re.compile(r"^/rooms/([^/]+)$"), {"GET"}),
    (re.compile(r"^/rooms/([^/]+)/availability$"), {"GET"}),
    (re.compile(r"^/bookings$"), {"GET", "POST"}),
    (re.compile(r"^/bookings/([^/]+)$"), {"GET", "DELETE"} | ({"PATCH"} if V2 else set())),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, body):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        url = urlparse(self.path)
        q = parse_qs(url.query, keep_blank_values=True)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            for i, (rx, methods) in enumerate(ROUTES):
                m = rx.match(url.path)
                if not m:
                    continue
                if self.command not in methods:
                    raise Err(405, "method_not_allowed")
                body = None
                if self.command in ("POST", "PATCH"):
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise Err(400, "bad_json")
                    if not isinstance(body, dict):
                        raise Err(400, "bad_json")
                if i == 0:
                    return self.send(*(create_room(body) if body is not None else (200, {"rooms": ROOMS})))
                if i == 1:
                    room = room_by_id(m.group(1))
                    if room is None:
                        raise Err(404, "not_found")
                    return self.send(200, room)
                if i == 2:
                    return self.send(*availability(m.group(1), q))
                if i == 3:
                    if body is not None:
                        return self.send(*create_booking(body, self.headers.get("Idempotency-Key")))
                    return self.send(*list_bookings(q))
                b = booking_by_id(m.group(1))
                if self.command == "PATCH":
                    return self.send(*patch_booking(m.group(1), body))
                if b is None:
                    raise Err(404, "not_found")
                if self.command == "DELETE":
                    BOOKINGS.remove(b)
                    return self.send(204, None)
                return self.send(200, public(b))
            raise Err(404, "not_found")
        except Err as e:
            self.send(e.status, {"error": {"code": e.code, "message": e.message}})

    do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = do_HEAD = handle_any


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ThreadingHTTPServer(("127.0.0.1", ap.parse_args().port), Handler).serve_forever()
