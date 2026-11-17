"""Meeting room booking API. Python 3.13 standard library only."""
import argparse
import base64
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qsl

LOCK = threading.Lock()
ROOMS = {}          # id -> room dict
ROOM_SEQ = [0]
BOOKINGS = {}       # id -> booking dict (with internal fields)
BOOKING_SEQ = [0]
IDEMPOTENCY = {}    # key -> (fingerprint, status, body)

WORK_START = dt.time(8, 0)
WORK_END = dt.time(18, 0)
MAX_DURATION = dt.timedelta(hours=8)

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


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
        raise verr(f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return dt.datetime(*(int(g) for g in m.groups()))
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")


def fmt_ts(d):
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def public_booking(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}


def booking_sort_key(b):
    return (b["start"], int(b["id"][1:]))


def encode_cursor(b):
    raw = json.dumps([fmt_ts(b["start"]), int(b["id"][1:])]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        data = json.loads(base64.urlsafe_b64decode(c + pad).decode())
        if not (isinstance(data, list) and len(data) == 2 and is_int(data[1])):
            raise ValueError
        return (parse_ts(data[0], "cursor"), data[1])
    except (ValueError, ApiError, UnicodeDecodeError, TypeError):
        raise verr("cursor is invalid")


# ---------------------------------------------------------------- handlers

def create_room(body):
    if not isinstance(body, dict):
        raise verr("request body must be a JSON object")
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise verr("name is required and must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(cap):
        raise verr("capacity must be an integer")
    if cap < 1 or cap > 500:
        raise verr("capacity must be between 1 and 500")
    norm = name.strip().casefold()
    for r in ROOMS.values():
        if r["name"].strip().casefold() == norm:
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


def validate_booking(body):
    if not isinstance(body, dict):
        raise verr("request body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id is required and must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for d, f in ((start, "start"), (end, "end")):
        if d.minute % 15 != 0 or d.second != 0:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_DURATION:
        raise verr("bookings can be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise verr("title is required and must be a non-empty string")
    att = body.get("attendees")
    if not is_int(att):
        raise verr("attendees must be an integer")
    if att < 1:
        raise verr("attendees must be at least 1")
    return room_id, start, end, title, att


def create_booking(body):
    room_id, start, end, title, att = validate_booking(body)
    room = ROOMS.get(room_id)
    if room is None:
        raise ApiError("not_found", f"room {room_id} not found")
    if att > room["capacity"]:
        raise ApiError("over_capacity",
                       f"room {room_id} holds {room['capacity']} people, {att} requested")
    for b in BOOKINGS.values():
        if b["room_id"] == room_id and b["start"] < end and start < b["end"]:
            raise ApiError("conflict", f"room {room_id} is already booked by {b['id']} in that time")
    BOOKING_SEQ[0] += 1
    bid = f"b{BOOKING_SEQ[0]}"
    b = {"id": bid, "room_id": room_id, "start": start, "end": end, "title": title, "attendees": att}
    BOOKINGS[bid] = b
    return 201, public_booking(b)


def list_bookings(query):
    room_id = query.get("room_id")
    frm = parse_ts(query["from"], "from") if "from" in query else None
    to = parse_ts(query["to"], "to") if "to" in query else None
    if frm is not None and to is not None and frm >= to:
        raise verr("from must be before to")
    limit = 20
    if "limit" in query:
        lv = query["limit"]
        if not re.fullmatch(r"\d+", lv or ""):
            raise verr("limit must be an integer between 1 and 100")
        limit = int(lv)
        if limit < 1 or limit > 100:
            raise verr("limit must be an integer between 1 and 100")
    after = None
    if "cursor" in query and query["cursor"] != "":
        after = decode_cursor(query["cursor"])
    items = []
    for b in BOOKINGS.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm is not None and not b["end"] > frm:
            continue
        if to is not None and not b["start"] < to:
            continue
        items.append(b)
    items.sort(key=booking_sort_key)
    if after is not None:
        items = [b for b in items if booking_sort_key(b) > after]
    page = items[:limit]
    next_cursor = encode_cursor(page[-1]) if len(items) > limit else None
    return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


def availability(rid, query):
    get_room(rid)
    date_s = query.get("date")
    if not date_s:
        raise verr("date is required (YYYY-MM-DD)")
    m = DATE_RE.match(date_s)
    if not m:
        raise verr("date must be formatted YYYY-MM-DD")
    try:
        day = dt.date(*(int(g) for g in m.groups()))
    except ValueError:
        raise verr("date is not a valid date")
    ws = dt.datetime.combine(day, WORK_START)
    we = dt.datetime.combine(day, WORK_END)
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


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, *args):
        pass

    def send_json(self, status, body, headers=None):
        if body is None:
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("bad_json", "invalid Content-Length")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            raise ApiError("bad_json", "request body must be JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")

    def handle_any(self, method):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/") or "/"
            query = {}
            for k, v in parse_qsl(parts.query, keep_blank_values=True):
                query.setdefault(k, v)
            segs = [s for s in path.split("/") if s]
            status, body, headers = self.route(method, segs, query)
            self.send_json(status, body, headers)
        except ApiError as e:
            self.send_json(e.status, {"error": {"code": e.code, "message": e.message}}, e.headers)
        except Exception as e:  # pragma: no cover
            try:
                self.send_json(500, {"error": {"code": "internal_error", "message": str(e)}})
            except Exception:
                pass

    def route(self, method, segs, query):
        def allow(*methods):
            if method not in methods:
                raise ApiError("method_not_allowed", f"{method} not allowed here",
                               {"Allow": ", ".join(methods)})

        if segs == ["rooms"]:
            allow("GET", "POST")
            if method == "GET":
                with LOCK:
                    return 200, {"rooms": [dict(r) for r in ROOMS.values()]}, None
            body = self.read_json()
            with LOCK:
                s, b = create_room(body)
            return s, b, None
        if len(segs) == 2 and segs[0] == "rooms":
            allow("GET")
            with LOCK:
                return 200, dict(get_room(segs[1])), None
        if len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
            allow("GET")
            with LOCK:
                s, b = availability(segs[1], query)
            return s, b, None
        if segs == ["bookings"]:
            allow("GET", "POST")
            if method == "GET":
                with LOCK:
                    s, b = list_bookings(query)
                return s, b, None
            body = self.read_json()
            key = self.headers.get("Idempotency-Key")
            key = key.strip() if key else ""
            with LOCK:
                if key:
                    prev = IDEMPOTENCY.get(key)
                    if prev is not None:
                        fp, ps, pb = prev
                        if fp != body:
                            raise ApiError("idempotency_mismatch",
                                           "Idempotency-Key was already used for a different request")
                        return ps, json.loads(json.dumps(pb)), None
                s, b = create_booking(body)
                if key:
                    IDEMPOTENCY[key] = (json.loads(json.dumps(body)), s, json.loads(json.dumps(b)))
            return s, b, None
        if len(segs) == 2 and segs[0] == "bookings":
            allow("GET", "DELETE")
            bid = segs[1]
            with LOCK:
                b = BOOKINGS.get(bid)
                if b is None:
                    raise ApiError("not_found", f"booking {bid} not found")
                if method == "GET":
                    return 200, public_booking(b), None
                del BOOKINGS[bid]
                return 204, None, None
        raise ApiError("not_found", "no such resource")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def do_OPTIONS(self):
        self.handle_any("OPTIONS")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
