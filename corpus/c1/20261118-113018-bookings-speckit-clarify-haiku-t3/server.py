"""Meeting room booking API (single file, standard library only).

Run as:  python server.py --port <N>      (binds 127.0.0.1 only)

Notes on the customer acceptance harness (tests/helpers.py, read-only):
  * It starts `python server.py --port <N>` with cwd set to the repository root,
    with stdout/stderr discarded, on a free port it picked by binding port 0.
  * Readiness is detected by repeatedly opening a TCP connection to 127.0.0.1:<N>
    (10 second deadline), so no startup line or extra flag is required.
  * It then speaks plain HTTP/1.x with http.client and JSON bodies, and kills the
    process at teardown. Hence the server must not need any argument other than
    --port, and state is in memory only (a restart clears everything).

Design (see specs/001-room-booking-api/ for the full spec):
  * One process-wide threading.Lock guards all state; overlap checks and inserts
    happen atomically under it (FR-025).
  * Idempotency records are service-wide and kept for the process lifetime; a
    request with an in-progress key waits for the first request to finish (FR-017).
  * Booking validation order: format -> unknown room -> over capacity -> overlap
    (FR-026).
"""

import argparse
import base64
import binascii
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DIGITS_RE = re.compile(r"[0-9]+")
ID_NUM_RE = re.compile(r"[bB]?[0-9]+")

UTC = timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING_LENGTH = timedelta(hours=8)

# FR-003: error code -> HTTP status.
ERROR_STATUS = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
    "internal_error": 500,
}


class ApiError(Exception):
    def __init__(self, code, message=None):
        super().__init__(code)
        self.code = code
        self.status = ERROR_STATUS[code]
        self.message = message or code.replace("_", " ")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------- helpers


def parse_timestamp(value, field):
    """Accepts only YYYY-MM-DDTHH:MM:SSZ (FR-009); returns an aware UTC datetime."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise ApiError("validation_error",
                       f"{field} must be a UTC timestamp in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(value, TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid calendar timestamp")


def format_timestamp(dt):
    return dt.strftime(TS_FORMAT)


def parse_json_object(raw):
    """Invalid JSON / invalid UTF-8 -> bad_json; valid non-object (or empty) -> validation_error."""
    if not raw:
        raise ApiError("validation_error", "request body must be a JSON object")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    return obj


def first_param(query, name):
    values = query.get(name)
    return values[0] if values else None


def json_bytes(obj):
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- validation


def validate_room_input(body):
    name = body.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 50):
        raise ApiError("validation_error", "name must be a string of 1 to 50 characters")
    capacity = body.get("capacity")
    if not _is_int(capacity) or not (1 <= capacity <= 500):
        raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
    return {"name": name, "capacity": capacity}


def _check_quarter_hour(dt, field):
    if dt.minute % 15 != 0 or dt.second != 0:
        raise ApiError("validation_error",
                       f"{field} must fall on a quarter hour (minutes 00, 15, 30, 45; seconds 00)")


def validate_booking_input(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise ApiError("validation_error", "room_id must be a non-empty string")
    start = parse_timestamp(body.get("start"), "start")
    end = parse_timestamp(body.get("end"), "end")
    _check_quarter_hour(start, "start")
    _check_quarter_hour(end, "end")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_BOOKING_LENGTH:
        raise ApiError("validation_error", "booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 200):
        raise ApiError("validation_error", "title must be a string of 1 to 200 characters")
    attendees = body.get("attendees")
    if not _is_int(attendees) or attendees < 1:
        raise ApiError("validation_error", "attendees must be an integer of at least 1")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def booking_numeric_id(booking_id):
    return int(booking_id[1:])


def booking_sort_key(booking):
    return (booking["start"], booking_numeric_id(booking["id"]))


def public_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": format_timestamp(b["start"]),
        "end": format_timestamp(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def public_room(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_fingerprint(body):
    """Canonical JSON of the five booking fields as received (research.md §4)."""
    keys = ("room_id", "start", "end", "title", "attendees")
    return json.dumps({k: body.get(k) for k in keys}, sort_keys=True, separators=(",", ":"))


class IdempotencyRecord:
    __slots__ = ("fingerprint", "status", "body", "event")

    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.status = None
        self.body = None          # None while in progress
        self.event = threading.Event()


# ---------------------------------------------------------------- store


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> room dict, insertion ordered (creation order)
        self.bookings = {}       # id -> booking dict
        self.idempotency = {}    # raw Idempotency-Key -> IdempotencyRecord
        self.room_seq = 0
        self.booking_seq = 0

    # rooms -----------------------------------------------------------

    def create_room(self, data):
        with self.lock:
            if any(r["name"] == data["name"] for r in self.rooms.values()):
                raise ApiError("room_exists", "a room with this name already exists")
            self.room_seq += 1
            room = {"id": f"r{self.room_seq}", "name": data["name"], "capacity": data["capacity"]}
            self.rooms[room["id"]] = room
            return public_room(room)

    def list_rooms(self):
        with self.lock:
            return [public_room(r) for r in self.rooms.values()]

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", f"room {room_id} not found")
            return public_room(room)

    # bookings --------------------------------------------------------

    def _create_booking_locked(self, data):
        """Checks in FR-026 order and inserts. Caller must hold self.lock."""
        room = self.rooms.get(data["room_id"])
        if room is None:
            raise ApiError("not_found", f"room {data['room_id']} not found")
        if data["attendees"] > room["capacity"]:
            raise ApiError("over_capacity", "attendees exceed room capacity")
        for other in self.bookings.values():
            if (other["room_id"] == room["id"]
                    and other["start"] < data["end"]
                    and other["end"] > data["start"]):
                raise ApiError("conflict", "booking overlaps an existing booking in this room")
        self.booking_seq += 1
        booking = {"id": f"b{self.booking_seq}", **data}
        self.bookings[booking["id"]] = booking
        return public_booking(booking)

    def create_booking(self, body, idem_key):
        """Returns (status, body)."""
        data = validate_booking_input(body)  # format checks first; no state touched
        if idem_key is None:
            with self.lock:
                return 201, self._create_booking_locked(data)

        fingerprint = booking_fingerprint(body)
        while True:
            with self.lock:
                record = self.idempotency.get(idem_key)
                if record is None:
                    record = IdempotencyRecord(fingerprint)
                    self.idempotency[idem_key] = record
                    try:
                        result = self._create_booking_locked(data)
                    except BaseException:
                        # Failed requests are not recorded; let the key be retried.
                        if self.idempotency.get(idem_key) is record:
                            del self.idempotency[idem_key]
                        record.event.set()
                        raise
                    record.status = 201
                    record.body = result
                    record.event.set()
                    return 201, result
                if record.body is not None:
                    if record.fingerprint != fingerprint:
                        raise ApiError("idempotency_mismatch",
                                       "Idempotency-Key was already used for a different request")
                    return record.status, record.body
                waiting_on = record.event
            # First request with this key is still in progress: wait, then re-check.
            waiting_on.wait()

    def get_booking(self, booking_id):
        with self.lock:
            booking = self.bookings.get(booking_id)
            if booking is None:
                raise ApiError("not_found", f"booking {booking_id} not found")
            return public_booking(booking)

    def delete_booking(self, booking_id):
        with self.lock:
            if self.bookings.pop(booking_id, None) is None:
                raise ApiError("not_found", f"booking {booking_id} not found")

    def list_bookings(self, room_id, window_from, window_to, limit, after_key):
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if window_to is not None and not (b["start"] < window_to):
                    continue
                if window_from is not None and not (b["end"] > window_from):
                    continue
                items.append(b)
            items.sort(key=booking_sort_key)
            if after_key is not None:
                items = [b for b in items if booking_sort_key(b) > after_key]
            page = items[:limit]
            has_more = len(items) > limit
            next_cursor = encode_cursor(page[-1]) if (has_more and page) else None
            return [public_booking(b) for b in page], next_cursor

    def availability(self, room_id, day):
        with self.lock:
            if room_id not in self.rooms:
                raise ApiError("not_found", f"room {room_id} not found")
            window_start = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
            window_end = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
            busy = []
            for b in self.bookings.values():
                if b["room_id"] != room_id:
                    continue
                if b["start"] < window_end and b["end"] > window_start:
                    busy.append((max(b["start"], window_start), min(b["end"], window_end)))
        busy.sort()
        free = []
        cursor = window_start
        for start, end in busy:
            if start > cursor:
                free.append((cursor, start))
            if end > cursor:
                cursor = end
        if cursor < window_end:
            free.append((cursor, window_end))
        return [{"start": format_timestamp(s), "end": format_timestamp(e)} for s, e in free]


# ---------------------------------------------------------------- cursors


def encode_cursor(booking):
    payload = json.dumps([format_timestamp(booking["start"]), booking["id"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(text):
    """Returns the (start, numeric id) sort key the cursor points at, or validation_error."""
    bad = ApiError("validation_error", "cursor is not valid")
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        raise bad
    if not (isinstance(payload, list) and len(payload) == 2
            and all(isinstance(x, str) for x in payload)):
        raise bad
    start_text, booking_id = payload
    if not ID_NUM_RE.fullmatch(booking_id):
        raise bad
    try:
        start = parse_timestamp(start_text, "cursor")
    except ApiError:
        raise bad
    return (start, int(booking_id.lstrip("bB")))


# ---------------------------------------------------------------- handlers


class Request:
    def __init__(self, store, raw, headers, query):
        self.store = store
        self.raw = raw
        self.headers = headers
        self.query = query


def h_create_room(req):
    data = validate_room_input(parse_json_object(req.raw))
    return 201, req.store.create_room(data)


def h_list_rooms(req):
    return 200, {"rooms": req.store.list_rooms()}


def h_get_room(req, room_id):
    return 200, req.store.get_room(room_id)


def h_availability(req, room_id):
    date_text = first_param(req.query, "date")
    if date_text is None or not DATE_RE.fullmatch(date_text):
        raise ApiError("validation_error", "date must be given as YYYY-MM-DD")
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        raise ApiError("validation_error", "date is not a valid calendar date")
    free = req.store.availability(room_id, day)
    return 200, {"date": date_text, "free": free}


def h_create_booking(req):
    body = parse_json_object(req.raw)
    key = req.headers.get("Idempotency-Key")
    if key is not None and key == "":
        key = None
    return req.store.create_booking(body, key)


def h_list_bookings(req):
    q = req.query
    room_id = first_param(q, "room_id")
    window_from = first_param(q, "from")
    window_to = first_param(q, "to")
    window_from = parse_timestamp(window_from, "from") if window_from is not None else None
    window_to = parse_timestamp(window_to, "to") if window_to is not None else None

    limit_text = first_param(q, "limit")
    if limit_text is None:
        limit = 20
    else:
        if not DIGITS_RE.fullmatch(limit_text) or not (1 <= int(limit_text) <= 100):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        limit = int(limit_text)

    cursor_text = first_param(q, "cursor")
    after_key = decode_cursor(cursor_text) if cursor_text else None

    bookings, next_cursor = req.store.list_bookings(room_id, window_from, window_to, limit, after_key)
    return 200, {"bookings": bookings, "next_cursor": next_cursor}


def h_get_booking(req, booking_id):
    return 200, req.store.get_booking(booking_id)


def h_delete_booking(req, booking_id):
    req.store.delete_booking(booking_id)
    return 204, None


ROUTES = {
    "rooms": {"GET": h_list_rooms, "POST": h_create_room},
    "rooms/{id}": {"GET": h_get_room},
    "rooms/{id}/availability": {"GET": h_availability},
    "bookings": {"GET": h_list_bookings, "POST": h_create_booking},
    "bookings/{id}": {"GET": h_get_booking, "DELETE": h_delete_booking},
}


def resolve(parts):
    """Returns (methods table, path args) or (None, ())."""
    if parts == ["rooms"]:
        return ROUTES["rooms"], ()
    if len(parts) == 2 and parts[0] == "rooms":
        return ROUTES["rooms/{id}"], (parts[1],)
    if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "availability":
        return ROUTES["rooms/{id}/availability"], (parts[1],)
    if parts == ["bookings"]:
        return ROUTES["bookings"], ()
    if len(parts) == 2 and parts[0] == "bookings":
        return ROUTES["bookings/{id}"], (parts[1],)
    return None, ()


def dispatch(store, method, target, raw, headers):
    """Returns (status, body-or-None). Raises ApiError for every failure."""
    parts_url = urlsplit(target)
    parts = [p for p in parts_url.path.split("/") if p]
    table, args = resolve(parts)
    if table is None:
        raise ApiError("not_found", "no such path")
    handler = table.get(method)
    if handler is None:
        raise ApiError("method_not_allowed", f"{method} is not allowed on this path")
    query = parse_qs(parts_url.query, keep_blank_values=True)
    return handler(Request(store, raw, headers, query), *args)


# ---------------------------------------------------------------- HTTP plumbing


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings"
    sys_version = ""

    def log_message(self, format, *args):  # keep the process quiet
        pass

    def _handle(self):
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            status, body = dispatch(self.server.store, self.command, self.path, raw, self.headers)
            if body is None:
                self._send(status, None)
            else:
                self._send(status, body)
        except ApiError as err:
            self._send(err.status, {"error": {"code": err.code, "message": err.message}})
        except Exception:  # pragma: no cover - defensive
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})

    def _send(self, status, body):
        payload = b"" if body is None else json_bytes(body)
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on (127.0.0.1)")
    args = parser.parse_args(argv)
    if not (0 <= args.port <= 65535):
        parser.error("--port must be between 0 and 65535")
    server = Server(("127.0.0.1", args.port), Handler)
    server.store = Store()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
