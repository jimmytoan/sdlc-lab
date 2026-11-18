"""bookings: meeting room booking JSON API (in-memory, standard library only)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, date, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

UTC = timezone.utc
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORK_START = dtime(8, 0)
WORK_END = dtime(18, 0)
MAX_DURATION = timedelta(hours=8)

STATUS_TEXT = {
    400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    422: "Unprocessable Entity", 201: "Created", 200: "OK", 204: "No Content",
}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validation(message):
    return ApiError(422, "validation_error", message)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str):
        raise validation(f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    y, mo, d, h, mi, s = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=UTC)
    except ValueError:
        raise validation(f"{field} is not a valid date/time")
    return dt


def parse_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise validation("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise validation("date must be a valid YYYY-MM-DD date")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()
        self.bookings = {}       # id -> booking dict (internal datetimes kept in _start/_end)
        self.room_seq = 0
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body)


STORE = Store()


def num_of(prefix, ident):
    if isinstance(ident, str) and ident.startswith(prefix) and ident[len(prefix):].isdigit():
        return int(ident[len(prefix):])
    return None


def public_booking(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["_start"]),
            "end": fmt_ts(b["_end"]), "title": b["title"], "attendees": b["attendees"]}


def encode_cursor(start, num):
    raw = f"{fmt_ts(start)}|{num}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + pad).encode()).decode()
        start_s, num_s = raw.rsplit("|", 1)
        return parse_ts(start_s, "cursor"), int(num_s)
    except ApiError:
        raise validation("cursor is invalid")
    except Exception:
        raise validation("cursor is invalid")


def read_int_param(params, name, default, lo, hi):
    if name not in params:
        return default
    raw = params[name]
    if not re.fullmatch(r"-?\d+", raw):
        raise validation(f"{name} must be an integer")
    v = int(raw)
    if v < lo or v > hi:
        raise validation(f"{name} must be between {lo} and {hi}")
    return v


# ---------- handlers ----------

def create_room(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise validation("name must be a non-empty string of at most 50 characters")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or not (1 <= capacity <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    with STORE.lock:
        if name in STORE.room_names:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STORE.rooms[rid] = room
        STORE.room_names.add(name)
    return 201, dict(room)


def get_room(room_id):
    with STORE.lock:
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, dict(room)


def list_rooms():
    with STORE.lock:
        return 200, {"rooms": [dict(r) for r in STORE.rooms.values()]}


def validate_booking_body(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise validation("room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.second != 0 or dt.minute % 15 != 0:
            raise validation(f"{field} must be on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise validation("title must be a string")
    attendees = body.get("attendees")
    if isinstance(attendees, bool) or not isinstance(attendees, int) or attendees < 1:
        raise validation("attendees must be a positive integer")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def create_booking(body, idem_key):
    req = validate_booking_body(body)
    fingerprint = json.dumps({"room_id": req["room_id"], "start": fmt_ts(req["start"]),
                              "end": fmt_ts(req["end"]), "title": req["title"],
                              "attendees": req["attendees"]}, sort_keys=True)
    with STORE.lock:
        if idem_key is not None and idem_key in STORE.idem:
            fp, status, stored = STORE.idem[idem_key]
            if fp != fingerprint:
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return status, json.loads(json.dumps(stored))
        room = STORE.rooms.get(req["room_id"])
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if req["attendees"] > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        for other in STORE.bookings.values():
            if other["room_id"] == room["id"] and req["start"] < other["_end"] and other["_start"] < req["end"]:
                raise ApiError(409, "conflict", "the room is already booked for this time")
        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        booking = {"id": bid, "room_id": room["id"], "title": req["title"],
                   "attendees": req["attendees"], "_start": req["start"], "_end": req["end"]}
        STORE.bookings[bid] = booking
        result = public_booking(booking)
        if idem_key is not None:
            STORE.idem[idem_key] = (fingerprint, 201, result)
        return 201, dict(result)


def get_booking(bid):
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, public_booking(b)


def delete_booking(bid):
    with STORE.lock:
        if bid not in STORE.bookings:
            raise ApiError(404, "not_found", "booking not found")
        del STORE.bookings[bid]
        return 204, None


def list_bookings(params):
    limit = read_int_param(params, "limit", 20, 1, 100)
    room_id = params.get("room_id")
    frm = parse_ts(params["from"], "from") if "from" in params else None
    to = parse_ts(params["to"], "to") if "to" in params else None
    if frm is not None and to is not None and to <= frm:
        raise validation("to must be after from")
    cursor = decode_cursor(params["cursor"]) if "cursor" in params else None
    with STORE.lock:
        items = []
        for b in STORE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not (b["_end"] > frm):
                continue
            if to is not None and not (b["_start"] < to):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_start"], num_of("b", b["id"])))
        if cursor is not None:
            cs, cn = cursor
            items = [b for b in items if (b["_start"], num_of("b", b["id"])) > (cs, cn)]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last["_start"], num_of("b", last["id"]))
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


def availability(room_id, params):
    if "date" not in params:
        raise validation("date is required")
    day = parse_date(params["date"])
    with STORE.lock:
        if room_id not in STORE.rooms:
            raise ApiError(404, "not_found", "room not found")
        day_start = datetime.combine(day, WORK_START, tzinfo=UTC)
        day_end = datetime.combine(day, WORK_END, tzinfo=UTC)
        busy = []
        for b in STORE.bookings.values():
            if b["room_id"] != room_id:
                continue
            s, e = max(b["_start"], day_start), min(b["_end"], day_end)
            if s < e:
                busy.append((s, e))
        busy.sort()
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            cur = max(cur, e)
        if cur < day_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
        return 200, {"date": day.isoformat(), "free": free}


# ---------- routing ----------

def route(method, path, params, body_fn, headers):
    parts = [p for p in path.split("/") if p != ""]
    not_found = ApiError(404, "not_found", "no such endpoint")
    if not parts:
        raise not_found
    head = parts[0]
    if head == "rooms":
        if len(parts) == 1:
            if method == "POST":
                return create_room(body_fn())
            if method == "GET":
                return list_rooms()
            raise ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 2:
            if method == "GET":
                return get_room(parts[1])
            raise ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 3 and parts[2] == "availability":
            if method == "GET":
                return availability(parts[1], params)
            raise ApiError(405, "method_not_allowed", "method not allowed")
        raise not_found
    if head == "bookings":
        if len(parts) == 1:
            if method == "POST":
                return create_booking(body_fn(), headers.get("Idempotency-Key") or None)
            if method == "GET":
                return list_bookings(params)
            raise ApiError(405, "method_not_allowed", "method not allowed")
        if len(parts) == 2:
            if method == "GET":
                return get_booking(parts[1])
            if method == "DELETE":
                return delete_booking(parts[1])
            raise ApiError(405, "method_not_allowed", "method not allowed")
        raise not_found
    raise not_found


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send(self, status, payload):
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            parts = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}

            def body_fn():
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise ApiError(400, "bad_json", "request body is not valid JSON")

            status, payload = route(method, parts.path, params, body_fn, self.headers)
            self._send(status, payload)
        except ApiError as err:
            self._error(err)
        except Exception:
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
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
