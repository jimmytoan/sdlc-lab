"""bookings: meeting room booking JSON API (standard library only, in-memory state)."""
import argparse
import datetime as dt
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

WORK_START_H = 8
WORK_END_H = 18
MAX_DURATION_MIN = 8 * 60
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class ApiError(Exception):
    STATUS = {
        "not_found": 404,
        "method_not_allowed": 405,
        "bad_json": 400,
        "validation_error": 422,
        "over_capacity": 422,
        "room_exists": 409,
        "conflict": 409,
        "idempotency_mismatch": 422,
    }

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = self.STATUS[code]


def verr(msg):
    return ApiError("validation_error", msg)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body)


STATE = State()


# ---------- helpers ----------

def parse_ts(value, field):
    if not isinstance(value, str):
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        d = dt.datetime(*(int(x) for x in m.groups()), tzinfo=dt.timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")
    return d


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def require_object(body):
    if not isinstance(body, dict):
        raise verr("request body must be a JSON object")
    return body


def parse_query_int(qs, name, default, lo, hi):
    if name not in qs:
        return default
    raw = qs[name][0]
    if not re.fullmatch(r"-?\d+", raw):
        raise verr(f"{name} must be an integer")
    v = int(raw)
    if v < lo or v > hi:
        raise verr(f"{name} must be between {lo} and {hi}")
    return v


def room_view(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_view(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start_dt"]),
        "end": fmt_ts(b["end_dt"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


# ---------- handlers (return (status, body)) ----------

def create_room(body):
    body = require_object(body)
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise verr("name must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise verr("capacity must be an integer from 1 to 500")
    with STATE.lock:
        if name in STATE.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        STATE.room_seq += 1
        rid = f"r{STATE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STATE.rooms[rid] = room
        STATE.room_names.add(name)
        return 201, room_view(room)


def list_rooms():
    with STATE.lock:
        return 200, {"rooms": [room_view(r) for r in STATE.rooms.values()]}


def get_room(rid):
    with STATE.lock:
        r = STATE.rooms.get(rid)
        if r is None:
            raise ApiError("not_found", "room not found")
        return 200, room_view(r)


def validate_booking(body):
    body = require_object(body)
    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id must be a string")
    if not isinstance(title, str) or not title.strip():
        raise verr("title must be a non-empty string")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be a positive integer")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for d, name in ((start, "start"), (end, "end")):
        if d.minute % 15 != 0 or d.second != 0:
            raise verr(f"{name} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    duration = (end - start).total_seconds() / 60
    if duration > MAX_DURATION_MIN:
        raise verr("booking must be at most 8 hours long")
    return {
        "room_id": room_id,
        "start_dt": start,
        "end_dt": end,
        "title": title,
        "attendees": attendees,
    }


def fingerprint(data):
    return json.dumps({
        "room_id": data["room_id"],
        "start": fmt_ts(data["start_dt"]),
        "end": fmt_ts(data["end_dt"]),
        "title": data["title"],
        "attendees": data["attendees"],
    }, sort_keys=True)


def create_booking(body, idem_key):
    data = validate_booking(body)
    fp = fingerprint(data)
    with STATE.lock:
        if idem_key is not None and idem_key in STATE.idem:
            old_fp, status, resp = STATE.idem[idem_key]
            if old_fp != fp:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return status, resp
        room = STATE.rooms.get(data["room_id"])
        if room is None:
            raise ApiError("not_found", "room not found")
        if data["attendees"] > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for b in STATE.bookings.values():
            if b["room_id"] == room["id"] and b["start_dt"] < data["end_dt"] and data["start_dt"] < b["end_dt"]:
                raise ApiError("conflict", "the room is already booked for this time")
        STATE.booking_seq += 1
        bid = f"b{STATE.booking_seq}"
        booking = dict(data, id=bid)
        STATE.bookings[bid] = booking
        resp = booking_view(booking)
        if idem_key is not None:
            STATE.idem[idem_key] = (fp, 201, resp)
        return 201, resp


def get_booking(bid):
    with STATE.lock:
        b = STATE.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", "booking not found")
        return 200, booking_view(b)


def delete_booking(bid):
    with STATE.lock:
        if bid not in STATE.bookings:
            raise ApiError("not_found", "booking not found")
        del STATE.bookings[bid]
        return 204, None


def list_bookings(qs):
    limit = parse_query_int(qs, "limit", 20, 1, 100)
    offset = parse_query_int(qs, "cursor", 0, 0, 10**9)
    room_id = qs["room_id"][0] if "room_id" in qs else None
    frm = parse_ts(qs["from"][0], "from") if "from" in qs else None
    to = parse_ts(qs["to"][0], "to") if "to" in qs else None
    with STATE.lock:
        items = []
        for b in STATE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not (b["end_dt"] > frm):
                continue
            if to is not None and not (b["start_dt"] < to):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start_dt"], int(b["id"][1:])))
        page = items[offset:offset + limit]
        nxt = offset + limit
        next_cursor = str(nxt) if nxt < len(items) else None
        return 200, {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor}


def availability(rid, qs):
    if "date" not in qs:
        raise verr("date is required (YYYY-MM-DD)")
    raw = qs["date"][0]
    m = DATE_RE.match(raw)
    if not m:
        raise verr("date must be YYYY-MM-DD")
    try:
        day = dt.date(*(int(x) for x in m.groups()))
    except ValueError:
        raise verr("date is not a valid calendar date")
    with STATE.lock:
        if rid not in STATE.rooms:
            raise ApiError("not_found", "room not found")
        day_start = dt.datetime(day.year, day.month, day.day, WORK_START_H, tzinfo=dt.timezone.utc)
        day_end = dt.datetime(day.year, day.month, day.day, WORK_END_H, tzinfo=dt.timezone.utc)
        busy = sorted(
            (max(b["start_dt"], day_start), min(b["end_dt"], day_end))
            for b in STATE.bookings.values()
            if b["room_id"] == rid and b["start_dt"] < day_end and day_start < b["end_dt"]
        )
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            cur = max(cur, e)
        if cur < day_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
        return 200, {"date": raw, "free": free}


# ---------- routing ----------

def route(method, path, qs, read_body, headers):
    """Returns (status, body) or raises ApiError."""
    parts = [p for p in path.split("/") if p != ""]
    if path.endswith("/") and path != "/":
        raise ApiError("not_found", "no such path")

    def need(allowed):
        if method not in allowed:
            raise ApiError("method_not_allowed", f"method {method} not allowed here")

    def json_body():
        raw = read_body()
        if not raw.strip():
            raise ApiError("bad_json", "request body must be JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")

    if len(parts) == 0:
        raise ApiError("not_found", "no such path")

    if parts[0] == "rooms":
        if len(parts) == 1:
            if method == "GET":
                return list_rooms()
            if method == "POST":
                return create_room(json_body())
            need(("GET", "POST"))
        if len(parts) == 2:
            need(("GET",))
            return get_room(parts[1])
        if len(parts) == 3 and parts[2] == "availability":
            need(("GET",))
            return availability(parts[1], qs)
        raise ApiError("not_found", "no such path")

    if parts[0] == "bookings":
        if len(parts) == 1:
            if method == "GET":
                return list_bookings(qs)
            if method == "POST":
                key = headers.get("Idempotency-Key")
                if key is not None and key.strip() == "":
                    key = None
                return create_booking(json_body(), key)
            need(("GET", "POST"))
        if len(parts) == 2:
            if method == "GET":
                return get_booking(parts[1])
            if method == "DELETE":
                return delete_booking(parts[1])
            need(("GET", "DELETE"))
        raise ApiError("not_found", "no such path")

    raise ApiError("not_found", "no such path")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body):
        if body is None:
            data = b""
        else:
            data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _handle(self):
        parsed = urlsplit(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        length_hdr = self.headers.get("Content-Length")
        cache = {}

        def read_body():
            if "raw" not in cache:
                try:
                    n = int(length_hdr or 0)
                except ValueError:
                    n = 0
                cache["raw"] = self.rfile.read(n) if n > 0 else b""
            return cache["raw"]

        try:
            status, body = route(self.command, parsed.path, qs, read_body, self.headers)
        except ApiError as e:
            self._error(e)
            return
        except Exception:  # pragma: no cover - safety net
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})
            return
        self._send(status, body)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_PATCH(self):
        self._handle()


def main(argv=None):
    p = argparse.ArgumentParser(description="bookings API server")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
