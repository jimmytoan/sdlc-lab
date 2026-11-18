"""Meeting room booking API. Standard library only; state kept in memory."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

STATUS = {"bad_json": 400, "not_found": 404, "method_not_allowed": 405, "room_exists": 409,
          "conflict": 409, "validation_error": 422, "over_capacity": 422,
          "idempotency_mismatch": 422, "internal_error": 500}


class ApiError(Exception):
    def __init__(self, code, message, headers=None):
        super().__init__(message)
        self.status = STATUS[code]
        self.code = code
        self.message = message
        self.headers = headers or {}

    def body(self):
        return {"error": {"code": self.code, "message": self.message}}


def verr(msg):
    return ApiError("validation_error", msg)


# ---------------------------------------------------------------- helpers
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_ts(v, field, quarter=True):
    if not isinstance(v, str):
        raise verr(f"{field} must be a timestamp string")
    m = TS_RE.match(v)
    if not m:
        raise verr(f"{field} must look like 2026-03-02T09:30:00Z")
    try:
        dt = datetime(*[int(x) for x in m.groups()], tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")
    if quarter and (dt.second != 0 or dt.minute % 15 != 0):
        raise verr(f"{field} must be on a quarter hour")
    return dt


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- store
LOCK = threading.Lock()
ROOMS = {}
BOOKINGS = {}
IDEM = {}
SEQ = {"room": 0, "booking": 0}


def room_get(rid):
    r = ROOMS.get(rid)
    if r is None:
        raise ApiError("not_found", "room not found")
    return r


def booking_view(b):
    return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}


# ---------------------------------------------------------------- handlers
def create_room(body, q, h):
    name, cap = body.get("name"), body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("name must be a non-empty string of at most 50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise verr("capacity must be an integer between 1 and 500")
    with LOCK:
        if any(r["name"] == name for r in ROOMS.values()):
            raise ApiError("room_exists", "a room with this name already exists")
        SEQ["room"] += 1
        r = {"id": f"r{SEQ['room']}", "name": name, "capacity": cap}
        ROOMS[r["id"]] = r
        return 201, dict(r)


def list_rooms(body, q, h):
    with LOCK:
        return 200, {"rooms": [dict(r) for r in ROOMS.values()]}


def get_room(rid):
    def f(body, q, h):
        with LOCK:
            return 200, dict(room_get(rid))
    return f


def validate_booking(body):
    for k in ("room_id", "start", "end", "title", "attendees"):
        if k not in body:
            raise verr(f"{k} is required")
    if not isinstance(body["room_id"], str) or not body["room_id"]:
        raise verr("room_id must be a string")
    if not isinstance(body["title"], str) or not body["title"].strip():
        raise verr("title must be a non-empty string")
    if not is_int(body["attendees"]) or body["attendees"] < 0:
        raise verr("attendees must be a non-negative integer")
    s = parse_ts(body["start"], "start")
    e = parse_ts(body["end"], "end")
    if not s < e:
        raise verr("start must be before end")
    if e - s > timedelta(hours=8):
        raise verr("booking may be at most 8 hours long")
    return s, e


def _create_booking(body):
    s, e = validate_booking(body)
    room = room_get(body["room_id"])
    if body["attendees"] > room["capacity"]:
        raise ApiError("over_capacity", "more attendees than the room holds")
    for b in BOOKINGS.values():
        if b["room_id"] == room["id"] and s < b["_e"] and b["_s"] < e:
            raise ApiError("conflict", "room is already booked for that time")
    SEQ["booking"] += 1
    b = {"id": f"b{SEQ['booking']}", "room_id": room["id"], "start": fmt(s), "end": fmt(e),
         "title": body["title"], "attendees": body["attendees"], "_s": s, "_e": e,
         "_n": SEQ["booking"]}
    BOOKINGS[b["id"]] = b
    return 201, booking_view(b)


def create_booking(body, q, h):
    key = h.get("idempotency-key")
    with LOCK:
        if key is None:
            return _create_booking(body)
        fp = json.dumps(body, sort_keys=True)
        if key in IDEM:
            ofp, st, rb = IDEM[key]
            if ofp != fp:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was used with a different request")
            return st, json.loads(json.dumps(rb))
        try:
            st, rb = _create_booking(body)
        except ApiError as ex:
            if ex.code == "not_found" or ex.code in ("validation_error", "over_capacity", "conflict"):
                IDEM[key] = (fp, ex.status, ex.body())
            raise
        IDEM[key] = (fp, st, rb)
        return st, rb


def get_booking(bid):
    def f(body, q, h):
        with LOCK:
            b = BOOKINGS.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            return 200, booking_view(b)
    return f


def delete_booking(bid):
    def f(body, q, h):
        with LOCK:
            if bid not in BOOKINGS:
                raise ApiError("not_found", "booking not found")
            del BOOKINGS[bid]
            return 204, None
    return f


def qget(q, name):
    v = q.get(name)
    return v[-1] if v else None


def list_bookings(body, q, h):
    limit = 20
    if "limit" in q:
        lv = qget(q, "limit")
        if not re.fullmatch(r"[0-9]+", lv or "") or not 1 <= int(lv) <= 100:
            raise verr("limit must be an integer between 1 and 100")
        limit = int(lv)
    frm = to = None
    if "from" in q:
        frm = parse_ts(qget(q, "from"), "from", quarter=False)
    if "to" in q:
        to = parse_ts(qget(q, "to"), "to", quarter=False)
    if frm and to and frm >= to:
        raise verr("from must be before to")
    ckey = None
    if "cursor" in q:
        try:
            raw = base64.urlsafe_b64decode(qget(q, "cursor").encode("ascii")).decode("ascii")
            t, n = raw.rsplit("|", 1)
            ckey = (parse_ts(t, "cursor", quarter=False), int(n))
        except Exception:
            raise verr("invalid cursor")
    room_id = qget(q, "room_id")
    with LOCK:
        items = []
        for b in BOOKINGS.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not b["_e"] > frm:
                continue
            if to is not None and not b["_s"] < to:
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_s"], b["_n"]))
        if ckey is not None:
            items = [b for b in items if (b["_s"], b["_n"]) > ckey]
        page = items[:limit]
        nxt = None
        if len(items) > limit:
            last = page[-1]
            nxt = base64.urlsafe_b64encode(f"{last['start']}|{last['_n']}".encode()).decode()
        return 200, {"bookings": [booking_view(b) for b in page], "next_cursor": nxt}


def availability(rid):
    def f(body, q, h):
        with LOCK:
            room_get(rid)
            d = qget(q, "date")
            m = DATE_RE.match(d or "")
            if not m:
                raise verr("date must look like YYYY-MM-DD")
            try:
                day = datetime(*[int(x) for x in m.groups()], tzinfo=timezone.utc)
            except ValueError:
                raise verr("date is not a valid date")
            ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
            bs = sorted((b["_s"], b["_e"]) for b in BOOKINGS.values()
                        if b["room_id"] == rid and b["_s"] < we and b["_e"] > ws)
            free, cur = [], ws
            for s, e in bs:
                if s > cur:
                    free.append({"start": fmt(cur), "end": fmt(s)})
                cur = max(cur, e)
            if cur < we:
                free.append({"start": fmt(cur), "end": fmt(we)})
            return 200, {"date": d, "free": free}
    return f


# ---------------------------------------------------------------- router
def route(method, path):
    """Return (handler, needs_body) or raise."""
    segs = [s for s in path.split("/")[1:]]
    if path != "/" and path.endswith("/") and len(segs) > 1:
        segs = segs[:-1] if segs[-1] == "" else segs
    table = None
    if segs == ["rooms"]:
        table = {"GET": (list_rooms, False), "POST": (create_room, True)}
    elif segs == ["bookings"]:
        table = {"GET": (list_bookings, False), "POST": (create_booking, True)}
    elif len(segs) == 2 and segs[0] == "rooms" and segs[1]:
        table = {"GET": (get_room(segs[1]), False)}
    elif len(segs) == 3 and segs[0] == "rooms" and segs[1] and segs[2] == "availability":
        table = {"GET": (availability(segs[1]), False)}
    elif len(segs) == 2 and segs[0] == "bookings" and segs[1]:
        table = {"GET": (get_booking(segs[1]), False), "DELETE": (delete_booking(segs[1]), False)}
    if table is None:
        raise ApiError("not_found", "no such path")
    if method not in table:
        raise ApiError("method_not_allowed", "method not allowed",
                       {"Allow": ", ".join(sorted(table))})
    return table[method]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, body, headers=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def dispatch(self):
        try:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""
            parts = urlsplit(self.path)
            fn, needs_body = route(self.command, parts.path)
            body = None
            if needs_body:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ApiError("bad_json", "request body is not valid JSON")
                if not isinstance(body, dict):
                    raise verr("request body must be a JSON object")
            q = parse_qs(parts.query, keep_blank_values=True)
            hdrs = {k.lower(): v for k, v in self.headers.items()}
            status, out = fn(body, q, hdrs)
            self.send(status, out)
        except ApiError as e:
            self.send(e.status, e.body(), e.headers)
        except Exception:
            self.send(500, {"error": {"code": "internal_error", "message": "internal error"}})

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = dispatch


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
