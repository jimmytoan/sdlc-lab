"""bookings: meeting room booking JSON API (standard library only).

Run: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

DAY_START_HOUR = 8
DAY_END_HOUR = 18
MAX_DURATION = dt.timedelta(hours=8)
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
UTC = dt.timezone.utc


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation(message):
    return ApiError(422, "validation_error", message)


def parse_ts(value, field):
    if not isinstance(value, str):
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return dt.datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise validation(f"{field} is not a valid timestamp")


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_names = {}     # name -> id
        self.room_seq = 0
        self.bookings = {}       # id -> dict (with datetime start/end kept separately)
        self.booking_times = {}  # id -> (start, end) datetimes
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, response dict)


STORE = Store()


def public_booking(b):
    return dict(b)


# ---------- handlers (all called with the store lock held) ----------

def create_room(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise validation("name must be a non-empty string of at most 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    if name in STORE.room_names:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    STORE.room_seq += 1
    rid = f"r{STORE.room_seq}"
    room = {"id": rid, "name": name, "capacity": capacity}
    STORE.rooms[rid] = room
    STORE.room_names[name] = rid
    return 201, dict(room)


def get_room(rid):
    room = STORE.rooms.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return 200, dict(room)


def list_rooms():
    return 200, {"rooms": [dict(r) for r in STORE.rooms.values()]}


def validate_booking_body(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise validation("room_id must be a non-empty string")
    if not isinstance(title, str):
        raise validation("title must be a string")
    if not is_int(attendees) or attendees < 1:
        raise validation("attendees must be a positive integer")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for d, field in ((start, "start"), (end, "end")):
        if d.minute % 15 != 0 or d.second != 0:
            raise validation(f"{field} must be on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking may not be longer than 8 hours")
    return {"room_id": room_id, "start": start, "end": end,
            "title": title, "attendees": attendees}


def booking_fingerprint(v):
    return json.dumps({"room_id": v["room_id"], "start": fmt_ts(v["start"]),
                       "end": fmt_ts(v["end"]), "title": v["title"],
                       "attendees": v["attendees"]}, sort_keys=True)


def create_booking(body, idem_key):
    v = validate_booking_body(body)
    fp = booking_fingerprint(v)
    if idem_key is not None and idem_key in STORE.idem:
        stored_fp, stored_resp = STORE.idem[idem_key]
        if stored_fp != fp:
            raise ApiError(422, "idempotency_mismatch",
                           "Idempotency-Key was already used for a different request")
        return stored_resp
    room = STORE.rooms.get(v["room_id"])
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if v["attendees"] > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    for bid, (bs, be) in STORE.booking_times.items():
        if STORE.bookings[bid]["room_id"] == room["id"] and bs < v["end"] and v["start"] < be:
            raise ApiError(409, "conflict", "the room is already booked for this time")
    STORE.booking_seq += 1
    bid = f"b{STORE.booking_seq}"
    booking = {"id": bid, "room_id": room["id"], "start": fmt_ts(v["start"]),
               "end": fmt_ts(v["end"]), "title": v["title"], "attendees": v["attendees"]}
    STORE.bookings[bid] = booking
    STORE.booking_times[bid] = (v["start"], v["end"])
    resp = (201, dict(booking))
    if idem_key is not None:
        STORE.idem[idem_key] = (fp, resp)
    return resp


def get_booking(bid):
    b = STORE.bookings.get(bid)
    if b is None:
        raise ApiError(404, "not_found", "booking not found")
    return 200, dict(b)


def delete_booking(bid):
    if bid not in STORE.bookings:
        raise ApiError(404, "not_found", "booking not found")
    del STORE.bookings[bid]
    del STORE.booking_times[bid]
    return 204, None


def _bid_num(bid):
    m = re.match(r"^b(\d+)$", bid)
    return int(m.group(1)) if m else 0


def list_bookings(query):
    params = parse_qs(query, keep_blank_values=True)

    def one(name):
        vals = params.get(name)
        return vals[-1] if vals else None

    room_id = one("room_id")
    frm = one("from")
    to = one("to")
    limit_s = one("limit")
    cursor_s = one("cursor")

    limit = 20
    if limit_s is not None:
        if not re.match(r"^\d+$", limit_s) or not (1 <= int(limit_s) <= 100):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(limit_s)
    offset = 0
    if cursor_s is not None:
        if not re.match(r"^\d+$", cursor_s):
            raise validation("cursor is invalid")
        offset = int(cursor_s)
    f_dt = parse_ts(frm, "from") if frm is not None else None
    t_dt = parse_ts(to, "to") if to is not None else None
    if f_dt is not None and t_dt is not None and f_dt >= t_dt:
        raise validation("from must be before to")

    items = []
    for bid, b in STORE.bookings.items():
        if room_id is not None and b["room_id"] != room_id:
            continue
        bs, be = STORE.booking_times[bid]
        if t_dt is not None and not (bs < t_dt):
            continue
        if f_dt is not None and not (be > f_dt):
            continue
        items.append(b)
    items.sort(key=lambda b: (b["start"], _bid_num(b["id"])))
    page = items[offset:offset + limit]
    nxt = str(offset + limit) if offset + limit < len(items) else None
    return 200, {"bookings": [dict(b) for b in page], "next_cursor": nxt}


def availability(rid, query):
    if rid not in STORE.rooms:
        raise ApiError(404, "not_found", "room not found")
    params = parse_qs(query, keep_blank_values=True)
    vals = params.get("date")
    date_s = vals[-1] if vals else None
    if date_s is None or not DATE_RE.match(date_s):
        raise validation("date must be YYYY-MM-DD")
    try:
        day = dt.date.fromisoformat(date_s)
    except ValueError:
        raise validation("date is not a valid calendar date")
    day_start = dt.datetime(day.year, day.month, day.day, DAY_START_HOUR, tzinfo=UTC)
    day_end = dt.datetime(day.year, day.month, day.day, DAY_END_HOUR, tzinfo=UTC)
    busy = []
    for bid, (bs, be) in STORE.booking_times.items():
        if STORE.bookings[bid]["room_id"] == rid and bs < day_end and be > day_start:
            busy.append((max(bs, day_start), min(be, day_end)))
    busy.sort()
    free = []
    cur = day_start
    for bs, be in busy:
        if bs > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(bs)})
        cur = max(cur, be)
    if cur < day_end:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
    return 200, {"date": date_s, "free": free}


# ---------- routing ----------

def route(method, path, query, body_fn, idem_key):
    """Returns (status, payload or None). Raises ApiError."""
    parts = [p for p in path.split("/") if p != ""]
    if not parts or parts[0] not in ("rooms", "bookings"):
        raise ApiError(404, "not_found", "no such endpoint")
    root = parts[0]
    allowed = None
    handler = None

    if root == "rooms":
        if len(parts) == 1:
            allowed = {"GET", "POST"}
            if method == "GET":
                handler = lambda: list_rooms()
            elif method == "POST":
                handler = lambda: create_room(body_fn())
        elif len(parts) == 2:
            allowed = {"GET"}
            if method == "GET":
                handler = lambda: get_room(parts[1])
        elif len(parts) == 3 and parts[2] == "availability":
            allowed = {"GET"}
            if method == "GET":
                handler = lambda: availability(parts[1], query)
        else:
            raise ApiError(404, "not_found", "no such endpoint")
    else:
        if len(parts) == 1:
            allowed = {"GET", "POST"}
            if method == "GET":
                handler = lambda: list_bookings(query)
            elif method == "POST":
                handler = lambda: create_booking(body_fn(), idem_key)
        elif len(parts) == 2:
            allowed = {"GET", "DELETE"}
            if method == "GET":
                handler = lambda: get_booking(parts[1])
            elif method == "DELETE":
                handler = lambda: delete_booking(parts[1])
        else:
            raise ApiError(404, "not_found", "no such endpoint")

    if handler is None:
        raise ApiError(405, "method_not_allowed", f"method {method} not allowed here")
    return handler()


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def _send(self, status, payload=None):
        if payload is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _read_raw(self):
        length = self.headers.get("Content-Length")
        if length is None or length.strip() == "":
            return b""
        try:
            n = int(length)
        except ValueError:
            raise ApiError(400, "bad_json", "invalid Content-Length")
        if n < 0:
            raise ApiError(400, "bad_json", "invalid Content-Length")
        return self.rfile.read(n) if n else b""

    def _handle(self, method):
        try:
            raw = self._read_raw()
            url = urlsplit(self.path)
            idem_key = self.headers.get("Idempotency-Key")
            if idem_key is not None and idem_key.strip() == "":
                idem_key = None

            def body_fn():
                if not raw.strip():
                    raise ApiError(400, "bad_json", "request body must be JSON")
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ApiError(400, "bad_json", "request body is not valid JSON")

            with STORE.lock:
                status, payload = route(method, url.path, url.query, body_fn, idem_key)
            self._send(status, payload)
        except ApiError as err:
            self._error(err)
        except Exception:  # pragma: no cover - defensive
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")


def main():
    parser = argparse.ArgumentParser(description="Meeting room bookings API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
