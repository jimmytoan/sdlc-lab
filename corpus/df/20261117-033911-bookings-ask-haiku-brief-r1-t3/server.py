"""bookings: meeting room booking JSON API (standard library only, in-memory state)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

UTC = timezone.utc
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
MAX_BODY = 1024 * 1024
WORK_START = (8, 0)
WORK_END = (18, 0)
MAX_DURATION = timedelta(hours=8)


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

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.status = self.STATUS[code]
        self.message = message or code.replace("_", " ")


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()  # casefolded names
        self.bookings = {}       # id -> booking dict (internal, start/end are datetimes)
        self.idempotency = {}    # key -> (fingerprint, response body)
        self.room_seq = 0
        self.booking_seq = 0


STATE = State()


# ---------- helpers ----------

def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str):
        raise ApiError("validation_error", f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid date/time")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def numeric_id(prefix, s):
    """Return the integer part of an id like 'r12' or 'b3', or None if malformed."""
    if isinstance(s, str) and s.startswith(prefix) and s[len(prefix):].isdigit() and not s[len(prefix):].startswith("0"):
        return int(s[len(prefix):])
    return None


def encode_cursor(start_dt, seq):
    raw = json.dumps({"s": fmt_ts(start_dt), "i": seq}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        start = parse_ts(data["s"], "cursor")
        seq = data["i"]
        if not is_int(seq):
            raise ValueError
        return start, seq
    except (ApiError, ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("validation_error", "malformed cursor")


def public_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def public_room(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


# ---------- handlers ----------

def create_room(body):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise ApiError("validation_error", "name must be a string")
    name = name.strip()
    if not 1 <= len(name) <= 50:
        raise ApiError("validation_error", "name must be 1 to 50 characters")
    if not is_int(capacity) or not 1 <= capacity <= 500:
        raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
    with STATE.lock:
        if name.casefold() in STATE.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        STATE.room_seq += 1
        room = {"id": f"r{STATE.room_seq}", "name": name, "capacity": capacity}
        STATE.rooms[room["id"]] = room
        STATE.room_names.add(name.casefold())
        return 201, public_room(room)


def list_rooms(_query):
    with STATE.lock:
        return 200, {"rooms": [public_room(r) for r in STATE.rooms.values()]}


def get_room(room_id):
    with STATE.lock:
        room = STATE.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, public_room(room)


def room_availability(room_id, query):
    with STATE.lock:
        room = STATE.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        date_values = query.get("date")
        if not date_values:
            raise ApiError("validation_error", "date query parameter is required (YYYY-MM-DD)")
        ds = date_values[0]
        m = DATE_RE.match(ds)
        if not m:
            raise ApiError("validation_error", "date must be YYYY-MM-DD")
        try:
            day = datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
        except ValueError:
            raise ApiError("validation_error", "date is not a valid calendar date")
        day_start = day.replace(hour=WORK_START[0], minute=WORK_START[1])
        day_end = day.replace(hour=WORK_END[0], minute=WORK_END[1])
        busy = []
        for b in STATE.bookings.values():
            if b["room_id"] != room_id:
                continue
            if b["start"] < day_end and b["end"] > day_start:
                busy.append((max(b["start"], day_start), min(b["end"], day_end)))
        busy.sort()
        free = []
        cursor = day_start
        for s, e in busy:
            if s > cursor:
                free.append({"start": fmt_ts(cursor), "end": fmt_ts(s)})
            cursor = max(cursor, e)
        if cursor < day_end:
            free.append({"start": fmt_ts(cursor), "end": fmt_ts(day_end)})
        return 200, {"date": ds, "free": free}


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise ApiError("validation_error", "room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise ApiError("validation_error", f"{field} must be on a quarter-hour boundary")
    if start >= end:
        raise ApiError("validation_error", "start must be before end")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise ApiError("validation_error", "title must be a string")
    title = title.strip()
    if not 1 <= len(title) <= 100:
        raise ApiError("validation_error", "title must be 1 to 100 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise ApiError("validation_error", "attendees must be an integer of at least 1")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def create_booking(body, idem_key):
    fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with STATE.lock:
        if idem_key is not None and idem_key in STATE.idempotency:
            stored_fp, stored_response = STATE.idempotency[idem_key]
            if stored_fp != fingerprint:
                raise ApiError("idempotency_mismatch", "Idempotency-Key was already used for a different request")
            return 201, stored_response

        data = validate_booking(body)
        room = STATE.rooms.get(data["room_id"])
        if room is None:
            raise ApiError("not_found", "room not found")
        if data["attendees"] > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for other in STATE.bookings.values():
            if other["room_id"] == room["id"] and other["start"] < data["end"] and data["start"] < other["end"]:
                raise ApiError("conflict", "room is already booked for this time")

        STATE.booking_seq += 1
        booking = {"id": f"b{STATE.booking_seq}", **data}
        STATE.bookings[booking["id"]] = booking
        response = public_booking(booking)
        if idem_key is not None:
            STATE.idempotency[idem_key] = (fingerprint, response)
        return 201, response


def get_booking(booking_id):
    with STATE.lock:
        b = STATE.bookings.get(booking_id)
        if b is None:
            raise ApiError("not_found", "booking not found")
        return 200, public_booking(b)


def delete_booking(booking_id):
    with STATE.lock:
        if booking_id not in STATE.bookings:
            raise ApiError("not_found", "booking not found")
        del STATE.bookings[booking_id]
        return 204, None


def list_bookings(query):
    def first(name):
        vals = query.get(name)
        return vals[0] if vals else None

    limit = 20
    if first("limit") is not None:
        raw = first("limit")
        if not re.fullmatch(r"\d+", raw) or not 1 <= int(raw) <= 100:
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        limit = int(raw)

    room_id = first("room_id")
    frm = parse_ts(first("from"), "from") if first("from") is not None else None
    to = parse_ts(first("to"), "to") if first("to") is not None else None
    if frm is not None and to is not None and frm >= to:
        raise ApiError("validation_error", "from must be before to")
    after = None
    if first("cursor") is not None:
        after = decode_cursor(first("cursor"))

    with STATE.lock:
        items = []
        for b in STATE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            # Strict overlap: a booking ending exactly at `from` or starting exactly at `to` is excluded.
            if frm is not None and not b["end"] > frm:
                continue
            if to is not None and not b["start"] < to:
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start"], numeric_id("b", b["id"])))
        if after is not None:
            a_start, a_seq = after
            items = [b for b in items
                     if (b["start"], numeric_id("b", b["id"])) > (a_start, a_seq)]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last["start"], numeric_id("b", last["id"]))
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


# ---------- routing ----------

ROUTES = {
    # path pattern: {method: callable}
    ("rooms",): {"GET", "POST"},
    ("rooms", "*"): {"GET"},
    ("rooms", "*", "availability"): {"GET"},
    ("bookings",): {"GET", "POST"},
    ("bookings", "*"): {"GET", "DELETE"},
}


def match_route(segments):
    for pattern in ROUTES:
        if len(pattern) != len(segments):
            continue
        if all(p == "*" or p == s for p, s in zip(pattern, segments)):
            return pattern
    return None


def dispatch(method, raw_path, body_bytes, headers):
    parts = urlsplit(raw_path)
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    segments = [s for s in path.split("/") if s != ""]
    pattern = match_route(segments)
    if pattern is None:
        raise ApiError("not_found", "no such path")
    if method not in ROUTES[pattern]:
        raise ApiError("method_not_allowed", f"method {method} not allowed here")
    query = parse_qs(parts.query, keep_blank_values=True)

    body = None
    if method in ("POST",):
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("bad_json", "request body is not valid JSON")
        if not isinstance(body, dict):
            raise ApiError("bad_json", "request body must be a JSON object")

    if pattern == ("rooms",):
        return create_room(body) if method == "POST" else list_rooms(query)
    if pattern == ("rooms", "*"):
        return get_room(segments[1])
    if pattern == ("rooms", "*", "availability"):
        return room_availability(segments[1], query)
    if pattern == ("bookings",):
        if method == "POST":
            key = headers.get("Idempotency-Key")
            if key is not None:
                key = key.strip()
                if not key:
                    raise ApiError("validation_error", "Idempotency-Key must not be empty")
            return create_booking(body, key)
        return list_bookings(query)
    if method == "GET":
        return get_booking(segments[1])
    return delete_booking(segments[1])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def _handle(self, method):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            n = 0
        if n < 0 or n > MAX_BODY:
            n = 0
        body_bytes = self.rfile.read(n) if n else b""
        try:
            status, payload = dispatch(method, self.path, body_bytes, self.headers)
            self._send(status, payload)
        except ApiError as e:
            self._send(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception:  # pragma: no cover - last-resort safety net
            self._send(500, {"error": {"code": "internal_error", "message": "internal error"}})

    def _send(self, status, payload):
        if status == 204:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
