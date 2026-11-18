"""Meeting room booking API (bookings).

Small JSON-over-HTTP service, standard library only, in-memory state.

Started as ``python server.py --port <N>`` and listens on 127.0.0.1 only.
Behaviour follows SPEC.md and specs/001-meeting-room-booking/ (spec, contract,
data model, research decisions). The customer acceptance harness in tests/
starts this file as a subprocess with ``--port``, polls the port until it
accepts connections, and checks response shapes and status codes.
"""

import argparse
import base64
import binascii
import datetime as dt
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# ---------------------------------------------------------------------------
# 1. Constants and error helper
# ---------------------------------------------------------------------------

ERROR_STATUS = {
    "bad_json": 400,
    "not_found": 404,
    "method_not_allowed": 405,
    "validation_error": 422,
    "over_capacity": 422,
    "idempotency_mismatch": 422,
    "room_exists": 409,
    "conflict": 409,
}

UTC = dt.timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = dt.timedelta(hours=8)
MAX_ROOM_NAME = 50
MAX_TITLE = 200
MIN_CAPACITY, MAX_CAPACITY = 1, 500
DEFAULT_LIMIT, MIN_LIMIT, MAX_LIMIT = 20, 1, 100


class ApiError(Exception):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        if code not in ERROR_STATUS:
            raise ValueError("unknown error code: %s" % code)
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message or code.replace("_", " ")


def validation(message):
    return ApiError("validation_error", message)


# ---------------------------------------------------------------------------
# 2. Timestamp, date and pagination parsing and validation
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LIMIT_RE = re.compile(r"[0-9]+")
_BOOKING_ID_RE = re.compile(r"b[0-9]+")


def parse_ts(value, field="timestamp"):
    """Strict UTC instant: YYYY-MM-DDTHH:MM:SSZ only. Returns aware datetime."""
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        raise validation("%s must be a UTC timestamp in the form YYYY-MM-DDTHH:MM:SSZ" % field)
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise validation("%s is not a valid date and time" % field)
    return parsed.replace(tzinfo=UTC)


def format_ts(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise validation("date must be in the form YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise validation("date is not a valid calendar date")


def on_quarter_hour(t):
    return t.minute % 15 == 0 and t.second == 0 and t.microsecond == 0


def encode_cursor(start_dt, booking_id):
    raw = json.dumps([format_ts(start_dt), booking_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor):
    """Returns the sort key (start datetime, numeric id) stored in the cursor."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        raise validation("cursor is malformed")
    if not (isinstance(data, list) and len(data) == 2 and all(isinstance(x, str) for x in data)):
        raise validation("cursor is malformed")
    start_s, booking_id = data
    if not _BOOKING_ID_RE.fullmatch(booking_id):
        raise validation("cursor is malformed")
    try:
        start = parse_ts(start_s, "cursor")
    except ApiError:
        raise validation("cursor is malformed")
    return start, int(booking_id[1:])


# ---------------------------------------------------------------------------
# 3. In-memory store
# ---------------------------------------------------------------------------

LOCK = threading.Lock()
ROOMS = {}          # id -> {"id", "name", "capacity"}; dict keeps creation order
BOOKINGS = {}       # numeric id -> record (see _booking_record)
IDEMPOTENCY = {}    # key -> {"fingerprint", "status", "body"}
COUNTERS = {"room": 0, "booking": 0}


def _booking_view(rec):
    return {
        "id": rec["id"],
        "room_id": rec["room_id"],
        "start": rec["start_s"],
        "end": rec["end_s"],
        "title": rec["title"],
        "attendees": rec["attendees"],
    }


def _room_name_taken(name):
    return any(r["name"] == name for r in ROOMS.values())


def _overlapping_booking(room_id, start, end):
    for rec in BOOKINGS.values():
        if rec["room_id"] == room_id and rec["start"] < end and rec["end"] > start:
            return rec
    return None


# ---------------------------------------------------------------------------
# 4. Pure functions: free slots
# ---------------------------------------------------------------------------

def free_slots(day, bookings):
    """bookings: iterable of (start, end) aware datetimes for one room.

    Window W = [day 08:00Z, day 18:00Z). Only the part of each booking inside W
    is subtracted. Returns list of (start, end) free gaps in time order, no
    zero-length gaps.
    """
    w_start = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
    w_end = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
    clipped = []
    for b_start, b_end in bookings:
        if b_start < w_end and b_end > w_start:
            clipped.append((max(b_start, w_start), min(b_end, w_end)))
    clipped.sort()
    merged = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    gaps = []
    cursor = w_start
    for s, e in merged:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < w_end:
        gaps.append((cursor, w_end))
    return gaps


# ---------------------------------------------------------------------------
# 5. Body and field validation
# ---------------------------------------------------------------------------

def parse_json_body(raw):
    try:
        text = raw.decode("utf-8")
        return json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ApiError("bad_json", "request body is not valid JSON")


def require_object(data):
    if not isinstance(data, dict):
        raise validation("request body must be a JSON object")
    return data


def validate_room(data):
    require_object(data)
    if "name" not in data:
        raise validation("name is required")
    name = data["name"]
    if not isinstance(name, str):
        raise validation("name must be a string")
    if name.strip() == "":
        raise validation("name must not be empty")
    if len(name) > MAX_ROOM_NAME:
        raise validation("name must be at most %d characters" % MAX_ROOM_NAME)
    if "capacity" not in data:
        raise validation("capacity is required")
    capacity = data["capacity"]
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise validation("capacity must be an integer")
    if not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
        raise validation("capacity must be between %d and %d" % (MIN_CAPACITY, MAX_CAPACITY))
    return name, capacity


def validate_booking(data):
    """Field-level checks only (step 1 of the booking order)."""
    require_object(data)
    for field in ("room_id", "start", "end", "title", "attendees"):
        if field not in data:
            raise validation("%s is required" % field)
    room_id = data["room_id"]
    if not isinstance(room_id, str) or room_id == "":
        raise validation("room_id must be a non-empty string")
    start = parse_ts(data["start"], "start")
    end = parse_ts(data["end"], "end")
    if not on_quarter_hour(start):
        raise validation("start must fall on a quarter hour")
    if not on_quarter_hour(end):
        raise validation("end must fall on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_BOOKING:
        raise validation("booking must be at most 8 hours long")
    title = data["title"]
    if not isinstance(title, str):
        raise validation("title must be a string")
    if title.strip() == "":
        raise validation("title must not be empty")
    if len(title) > MAX_TITLE:
        raise validation("title must be at most %d characters" % MAX_TITLE)
    attendees = data["attendees"]
    if isinstance(attendees, bool) or not isinstance(attendees, int):
        raise validation("attendees must be an integer")
    if attendees < 1:
        raise validation("attendees must be at least 1")
    return room_id, start, end, title, attendees


# ---------------------------------------------------------------------------
# 6. Endpoint handlers. Each returns (status, body_or_None).
# ---------------------------------------------------------------------------

def create_room(raw, _query, _headers, _params):
    name, capacity = validate_room(parse_json_body(raw))
    with LOCK:
        if _room_name_taken(name):
            raise ApiError("room_exists", "a room with this name already exists")
        COUNTERS["room"] += 1
        room_id = "r%d" % COUNTERS["room"]
        room = {"id": room_id, "name": name, "capacity": capacity}
        ROOMS[room_id] = room
        return 201, dict(room)


def list_rooms(_raw, _query, _headers, _params):
    with LOCK:
        return 200, {"rooms": [dict(r) for r in ROOMS.values()]}


def get_room(_raw, _query, _headers, params):
    with LOCK:
        room = ROOMS.get(params["room_id"])
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, dict(room)


def availability(_raw, query, _headers, params):
    date_values = query.get("date")
    if not date_values:
        raise validation("date is required")
    day = parse_date(date_values[0])
    with LOCK:
        if params["room_id"] not in ROOMS:
            raise ApiError("not_found", "room not found")
        spans = [(r["start"], r["end"]) for r in BOOKINGS.values()
                 if r["room_id"] == params["room_id"]]
    gaps = free_slots(day, spans)
    return 200, {
        "date": day.isoformat(),
        "free": [{"start": format_ts(s), "end": format_ts(e)} for s, e in gaps],
    }


def create_booking(raw, _query, headers, _params):
    room_id, start, end, title, attendees = validate_booking(parse_json_body(raw))
    start_s = format_ts(start)
    end_s = format_ts(end)
    key = (headers.get("Idempotency-Key") or "").strip()
    fingerprint = (room_id, start_s, end_s, title, attendees)
    with LOCK:
        if key:
            prior = IDEMPOTENCY.get(key)
            if prior is not None:
                if prior["fingerprint"] == fingerprint:
                    return prior["status"], dict(prior["body"])
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "attendees exceed the room capacity")
        if _overlapping_booking(room_id, start, end) is not None:
            raise ApiError("conflict", "the room is already booked for this time")
        COUNTERS["booking"] += 1
        n = COUNTERS["booking"]
        rec = {
            "n": n,
            "id": "b%d" % n,
            "room_id": room_id,
            "start": start,
            "end": end,
            "start_s": start_s,
            "end_s": end_s,
            "title": title,
            "attendees": attendees,
        }
        BOOKINGS[n] = rec
        body = _booking_view(rec)
        if key:
            IDEMPOTENCY[key] = {"fingerprint": fingerprint, "status": 201, "body": dict(body)}
        return 201, body


def list_bookings(_raw, query, _headers, _params):
    room_filter = query.get("room_id", [None])[0]

    raw_from = query.get("from", [None])[0]
    raw_to = query.get("to", [None])[0]
    time_from = parse_ts(raw_from, "from") if raw_from is not None else None
    time_to = parse_ts(raw_to, "to") if raw_to is not None else None
    if time_from is not None and time_to is not None and time_from >= time_to:
        raise validation("from must be before to")

    limit = DEFAULT_LIMIT
    if "limit" in query:
        raw_limit = query["limit"][0]
        if not _LIMIT_RE.fullmatch(raw_limit):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(raw_limit)
        if not MIN_LIMIT <= limit <= MAX_LIMIT:
            raise validation("limit must be an integer from 1 to 100")

    after = None
    if query.get("cursor", [None])[0] is not None:
        after = decode_cursor(query["cursor"][0])

    with LOCK:
        matches = []
        for rec in BOOKINGS.values():
            if room_filter is not None and rec["room_id"] != room_filter:
                continue
            if time_to is not None and not rec["start"] < time_to:
                continue
            if time_from is not None and not rec["end"] > time_from:
                continue
            matches.append(rec)
    matches.sort(key=lambda r: (r["start"], r["n"]))
    if after is not None:
        matches = [r for r in matches if (r["start"], r["n"]) > after]

    page = matches[:limit]
    next_cursor = None
    if len(matches) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(last["start"], "b%d" % last["n"])
    return 200, {"bookings": [_booking_view(r) for r in page], "next_cursor": next_cursor}


def get_booking(_raw, _query, _headers, params):
    n = _booking_number(params["booking_id"])
    with LOCK:
        rec = BOOKINGS.get(n) if n is not None else None
        if rec is None:
            raise ApiError("not_found", "booking not found")
        return 200, _booking_view(rec)


def delete_booking(_raw, _query, _headers, params):
    n = _booking_number(params["booking_id"])
    with LOCK:
        if n is None or n not in BOOKINGS:
            raise ApiError("not_found", "booking not found")
        del BOOKINGS[n]
        return 204, None


def _booking_number(booking_id):
    if not _BOOKING_ID_RE.fullmatch(booking_id):
        return None
    return int(booking_id[1:])


# ---------------------------------------------------------------------------
# 7. Routing
# ---------------------------------------------------------------------------

def route(method, path, query):
    """Returns (handler, params) or raises ApiError."""
    segs = path.split("/")[1:] if path.startswith("/") else [path]

    if segs == ["rooms"]:
        allowed = {"GET": list_rooms, "POST": create_room}
        params = {}
    elif len(segs) == 2 and segs[0] == "rooms":
        allowed = {"GET": get_room}
        params = {"room_id": segs[1]}
    elif len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
        allowed = {"GET": availability}
        params = {"room_id": segs[1]}
    elif segs == ["bookings"]:
        allowed = {"GET": list_bookings, "POST": create_booking}
        params = {}
    elif len(segs) == 2 and segs[0] == "bookings":
        allowed = {"GET": get_booking, "DELETE": delete_booking}
        params = {"booking_id": segs[1]}
    else:
        raise ApiError("not_found", "no such path")

    handler = allowed.get(method)
    if handler is None:
        raise ApiError("method_not_allowed", "method %s is not allowed here" % method)
    return handler, params


# ---------------------------------------------------------------------------
# 8. HTTP plumbing
# ---------------------------------------------------------------------------

class BookingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _dispatch(self, method):
        try:
            raw = self._read_body()
            split = urlsplit(self.path)
            path = split.path
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            query = parse_qs(split.query, keep_blank_values=True)
            handler, params = route(method, path, query)
            status, body = handler(raw, query, self.headers, params)
        except ApiError as err:
            status = err.status
            body = {"error": {"code": err.code, "message": err.message}}
        except Exception:  # last-resort safety net; should not happen
            status = 500
            body = {"error": {"code": "internal_error", "message": "internal server error"}}
        self._send(status, body)

    def _send(self, status, body):
        if status == 204 or body is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# 9. main()
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, default=8080, help="port to listen on (127.0.0.1)")
    args = parser.parse_args(argv)
    httpd = Server(("127.0.0.1", args.port), BookingHandler)
    print("listening on 127.0.0.1:%d" % args.port, file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
