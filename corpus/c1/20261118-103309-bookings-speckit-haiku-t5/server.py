"""Meeting room booking JSON API.

Standard library only, in-memory state, listens on 127.0.0.1 only.

Run with:  python server.py --port <N>
"""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ERROR_STATUS = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
}

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"-?[0-9]+")
BOOKING_ID_RE = re.compile(r"b[0-9]+")

WORK_START = dtime(8, 0)
WORK_END = dtime(18, 0)
MAX_DURATION = timedelta(hours=8)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# ---------------------------------------------------------------------------
# Store (in memory, guarded by one lock)
# ---------------------------------------------------------------------------

STORE_LOCK = threading.Lock()
rooms = {}            # room id -> room dict (creation order)
room_names = set()    # exact, case-sensitive
bookings = {}         # booking id -> {"pub": public dict, "start": dt, "end": dt}
room_bookings = {}    # room id -> list of booking ids
idempotency = {}      # key -> {"fingerprint", "status", "response"}
counters = {"room": 0, "booking": 0}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message

    def body(self):
        return {"error": {"code": self.code, "message": self.message}}


def api_error(code, message):
    """The only way errors are produced: returns an ApiError to be raised."""
    return ApiError(code, message)


# ---------------------------------------------------------------------------
# Helpers: JSON bodies, timestamps, ints
# ---------------------------------------------------------------------------

def read_json_object(raw):
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise api_error("bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise api_error("validation_error", "request body must be a JSON object")
    return data


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise api_error("validation_error",
                        f"{field} must be a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ form")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise api_error("validation_error", f"{field} is not a valid date and time")


def format_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def first(query, key):
    values = query.get(key)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def validate_room(body):
    name = body.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 50):
        raise api_error("validation_error", "name must be a string of 1 to 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise api_error("validation_error", "capacity must be an integer from 1 to 500")
    return name, capacity


def create_room(raw):
    name, capacity = validate_room(read_json_object(raw))
    with STORE_LOCK:
        if name in room_names:
            raise api_error("room_exists", "a room with this name already exists")
        counters["room"] += 1
        room = {"id": f"r{counters['room']}", "name": name, "capacity": capacity}
        rooms[room["id"]] = room
        room_names.add(name)
        room_bookings[room["id"]] = []
        return 201, dict(room)


def list_rooms():
    with STORE_LOCK:
        return 200, {"rooms": [dict(r) for r in rooms.values()]}


def get_room(room_id):
    with STORE_LOCK:
        room = rooms.get(room_id)
        if room is None:
            raise api_error("not_found", "room not found")
        return 200, dict(room)


def availability(room_id, query):
    d = first(query, "date")
    if d is None or not DATE_RE.fullmatch(d):
        raise api_error("validation_error", "date must be given as YYYY-MM-DD")
    try:
        day = date.fromisoformat(d)
    except ValueError:
        raise api_error("validation_error", "date is not a valid calendar date")
    window_start = datetime.combine(day, WORK_START, tzinfo=timezone.utc)
    window_end = datetime.combine(day, WORK_END, tzinfo=timezone.utc)
    with STORE_LOCK:
        if room_id not in rooms:
            raise api_error("not_found", "room not found")
        busy = sorted(
            (rec["start"], rec["end"])
            for bid in room_bookings[room_id]
            for rec in [bookings[bid]]
            if rec["start"] < window_end and rec["end"] > window_start
        )
    free = []
    cur = window_start
    for start, end in busy:
        if start > cur:
            free.append({"start": format_ts(cur), "end": format_ts(start)})
        if end > cur:
            cur = end
    if cur < window_end:
        free.append({"start": format_ts(cur), "end": format_ts(window_end)})
    return 200, {"date": d, "free": free}


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise api_error("validation_error", "room_id must be a non-empty string")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise api_error("validation_error", "title must be a non-empty string")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise api_error("validation_error", "attendees must be an integer of at least 1")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for field, dt in (("start", start), ("end", end)):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise api_error("validation_error", f"{field} must be on a quarter hour")
    if end <= start:
        raise api_error("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise api_error("validation_error", "booking must be at most 8 hours long")
    if (start.date() != end.date() or start.time() < WORK_START
            or end.time() > WORK_END):
        raise api_error("validation_error",
                        "booking must lie within 08:00-18:00 UTC on a single date")
    return room_id, start, end, title, attendees


def fingerprint(room_id, start, end, title, attendees):
    return json.dumps(
        {"room_id": room_id, "start": format_ts(start), "end": format_ts(end),
         "title": title, "attendees": attendees},
        sort_keys=True, separators=(",", ":"))


def create_booking(raw, idem_key):
    room_id, start, end, title, attendees = validate_booking(read_json_object(raw))
    fp = fingerprint(room_id, start, end, title, attendees)
    with STORE_LOCK:
        if idem_key is not None and idem_key in idempotency:
            record = idempotency[idem_key]
            if record["fingerprint"] != fp:
                raise api_error("idempotency_mismatch",
                                "Idempotency-Key was already used for a different request")
            return record["status"], dict(record["response"])
        room = rooms.get(room_id)
        if room is None:
            raise api_error("not_found", "room not found")
        if attendees > room["capacity"]:
            raise api_error("over_capacity", "attendees exceed the room capacity")
        for bid in room_bookings[room_id]:
            other = bookings[bid]
            if other["start"] < end and start < other["end"]:
                raise api_error("conflict", "booking overlaps an existing booking")
        counters["booking"] += 1
        pub = {"id": f"b{counters['booking']}", "room_id": room_id,
               "start": format_ts(start), "end": format_ts(end),
               "title": title, "attendees": attendees}
        bookings[pub["id"]] = {"pub": pub, "start": start, "end": end}
        room_bookings[room_id].append(pub["id"])
        if idem_key is not None:
            idempotency[idem_key] = {"fingerprint": fp, "status": 201, "response": dict(pub)}
        return 201, dict(pub)


def get_booking(booking_id):
    with STORE_LOCK:
        rec = bookings.get(booking_id)
        if rec is None:
            raise api_error("not_found", "booking not found")
        return 200, dict(rec["pub"])


def delete_booking(booking_id):
    with STORE_LOCK:
        rec = bookings.pop(booking_id, None)
        if rec is None:
            raise api_error("not_found", "booking not found")
        room_bookings[rec["pub"]["room_id"]].remove(booking_id)
        return 204, None


def _sort_key(rec):
    return (rec["start"], int(rec["pub"]["id"][1:]))


def encode_cursor(rec):
    payload = json.dumps([format_ts(rec["start"]), rec["pub"]["id"]],
                         separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(text):
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(payload.decode("utf-8"))
        if not (isinstance(value, list) and len(value) == 2
                and all(isinstance(v, str) for v in value)):
            raise ValueError("bad cursor shape")
        start = parse_ts(value[0], "cursor")
        if not BOOKING_ID_RE.fullmatch(value[1]):
            raise ValueError("bad cursor id")
        return start, int(value[1][1:])
    except Exception:
        raise api_error("validation_error", "cursor is invalid")


def list_bookings(query):
    room_id = first(query, "room_id")
    raw_from = first(query, "from")
    raw_to = first(query, "to")
    frm = parse_ts(raw_from, "from") if raw_from is not None else None
    to = parse_ts(raw_to, "to") if raw_to is not None else None
    if frm is not None and to is not None and frm >= to:
        raise api_error("validation_error", "from must be before to")

    raw_limit = first(query, "limit")
    if raw_limit is None:
        limit = DEFAULT_LIMIT
    else:
        if not INT_RE.fullmatch(raw_limit):
            raise api_error("validation_error", "limit must be an integer from 1 to 100")
        limit = int(raw_limit)
        if not (1 <= limit <= MAX_LIMIT):
            raise api_error("validation_error", "limit must be an integer from 1 to 100")

    raw_cursor = first(query, "cursor")
    after = decode_cursor(raw_cursor) if raw_cursor is not None else None

    with STORE_LOCK:
        items = [
            rec for rec in bookings.values()
            if (room_id is None or rec["pub"]["room_id"] == room_id)
            and (frm is None or rec["end"] > frm)
            and (to is None or rec["start"] < to)
        ]
        items.sort(key=_sort_key)
        if after is not None:
            items = [rec for rec in items if _sort_key(rec) > after]
        page = items[:limit]
        more = len(items) > limit
        next_cursor = encode_cursor(page[-1]) if more and page else None
        return 200, {"bookings": [dict(rec["pub"]) for rec in page],
                     "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def dispatch(method, raw_path, idem_key, raw_body):
    parts_url = urlsplit(raw_path)
    path = parts_url.path
    query = parse_qs(parts_url.query, keep_blank_values=True)
    parts = [unquote(p) for p in path.split("/")]
    n = len(parts)

    if n == 2 and parts[1] == "rooms":
        allowed = {
            "GET": lambda: list_rooms(),
            "POST": lambda: create_room(raw_body),
        }
    elif n == 3 and parts[1] == "rooms" and parts[2]:
        allowed = {"GET": lambda: get_room(parts[2])}
    elif n == 4 and parts[1] == "rooms" and parts[2] and parts[3] == "availability":
        allowed = {"GET": lambda: availability(parts[2], query)}
    elif n == 2 and parts[1] == "bookings":
        allowed = {
            "GET": lambda: list_bookings(query),
            "POST": lambda: create_booking(raw_body, idem_key),
        }
    elif n == 3 and parts[1] == "bookings" and parts[2]:
        allowed = {
            "GET": lambda: get_booking(parts[2]),
            "DELETE": lambda: delete_booking(parts[2]),
        }
    else:
        raise api_error("not_found", "no such path")

    handler = allowed.get(method)
    if handler is None:
        raise api_error("method_not_allowed", f"method {method} is not allowed here")
    return handler()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings"
    sys_version = ""

    def __getattr__(self, name):
        # Every HTTP method (do_GET, do_PUT, do_FOO, ...) goes through one path,
        # so unsupported methods get the JSON error shape instead of a stdlib 501.
        if name.startswith("do_"):
            return self._handle_request
        raise AttributeError(name)

    def _handle_request(self):
        try:
            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header) if length_header else 0
                if length < 0:
                    raise ValueError
            except ValueError:
                raise api_error("bad_json", "invalid Content-Length")
            raw_body = self.rfile.read(length) if length else b""
            key = self.headers.get("Idempotency-Key")
            if key is not None and not key.strip():
                key = None
            status, body = dispatch(self.command, self.path, key, raw_body)
        except ApiError as err:
            self._send(err.status, err.body())
        except Exception:  # pragma: no cover - last-resort guard
            self._send(500, {"error": {"code": "internal_error",
                                       "message": "unexpected server error"}})
        else:
            self._send(status, body)

    def _send(self, status, body):
        self.send_response(status)
        if body is None:
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        # Used by the stdlib only for malformed request lines and similar.
        if code == 400:
            body = {"error": {"code": "bad_json", "message": message or "bad request"}}
        else:
            body = {"error": {"code": "validation_error", "message": message or "bad request"}}
        status = 400 if code == 400 else 422
        self._send(status, body)

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    server = Server(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
