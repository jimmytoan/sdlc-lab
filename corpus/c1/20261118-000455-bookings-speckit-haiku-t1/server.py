"""Meeting room booking HTTP JSON API.

Run with ``python server.py --port <N>``. Listens on 127.0.0.1 only.
Standard library only; all state (rooms, bookings, idempotency records) is kept
in memory and guarded by a single lock.
"""

import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"-?[0-9]+")
MAX_BOOKING_LENGTH = timedelta(hours=8)
DAY_START_HOUR = 8
DAY_END_HOUR = 18

STATUS_FOR_CODE = {
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
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def validation_error(message):
    return ApiError("validation_error", message)


# ---------------------------------------------------------------------------
# Value parsing helpers
# ---------------------------------------------------------------------------

def parse_ts(value, field):
    """Parse a strict UTC timestamp `YYYY-MM-DDTHH:MM:SSZ` into an aware datetime."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise validation_error(f"{field} must be a UTC timestamp like 2026-03-02T09:00:00Z")
    try:
        return datetime.strptime(value, TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise validation_error(f"{field} is not a valid date and time")


def format_ts(dt):
    return dt.strftime(TS_FMT)


def is_int(value):
    return type(value) is int


def parse_body_object(raw):
    """Parse a request body that must be a JSON object."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise validation_error("request body must be a JSON object")
    return data


def first(query, name):
    values = query.get(name)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

def encode_cursor(booking):
    payload = json.dumps([format_ts(booking["start"]), booking["n"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(text):
    """Return the (start datetime, numeric id) pair encoded in a cursor."""
    invalid = validation_error("cursor is invalid")
    try:
        std = text.replace("-", "+").replace("_", "/")
        std += "=" * (-len(std) % 4)
        data = json.loads(base64.b64decode(std.encode("ascii"), validate=True).decode("utf-8"))
    except (ValueError, RecursionError):
        raise invalid
    if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], str)
            and is_int(data[1]) and data[1] >= 0):
        raise invalid
    try:
        start = parse_ts(data[0], "cursor")
    except ApiError:
        raise invalid
    return start, data[1]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}               # id -> room dict, insertion ordered
        self.room_names = set()
        self.bookings = {}            # id -> booking record
        self.bookings_by_room = {}    # room id -> list of booking records
        self.idempotency = {}         # key -> {fingerprint, status, body}
        self.next_room_number = 1
        self.next_booking_number = 1


def booking_view(rec):
    return {
        "id": rec["id"],
        "room_id": rec["room_id"],
        "start": format_ts(rec["start"]),
        "end": format_ts(rec["end"]),
        "title": rec["title"],
        "attendees": rec["attendees"],
    }


# ---------------------------------------------------------------------------
# Handlers. Each takes the State and returns (status, payload-or-None).
# ---------------------------------------------------------------------------

def create_room(state, raw):
    body = parse_body_object(raw)
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not (1 <= len(name) <= 50):
        raise validation_error("name must be a non-empty string of at most 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise validation_error("capacity must be an integer from 1 to 500")
    with state.lock:
        if name in state.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        room_id = f"r{state.next_room_number}"
        state.next_room_number += 1
        room = {"id": room_id, "name": name, "capacity": capacity}
        state.rooms[room_id] = room
        state.room_names.add(name)
        return 201, dict(room)


def list_rooms(state):
    with state.lock:
        return 200, {"rooms": [dict(r) for r in state.rooms.values()]}


def get_room(state, room_id):
    with state.lock:
        room = state.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, dict(room)


def validate_booking_fields(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise validation_error("room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise validation_error(f"{field} must fall on a quarter hour (:00, :15, :30, :45)")
    if end <= start:
        raise validation_error("end must be after start")
    if end - start > MAX_BOOKING_LENGTH:
        raise validation_error("booking may last at most 8 hours")
    title = body.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 200):
        raise validation_error("title must be a non-empty string of at most 200 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise validation_error("attendees must be an integer of at least 1")
    return room_id, start, end, title, attendees


def create_booking(state, raw, idem_key):
    body = parse_body_object(raw)
    fingerprint = None
    if idem_key is not None:
        fingerprint = json.dumps([body.get("room_id"), body.get("start"), body.get("end"),
                                  body.get("title"), body.get("attendees")], sort_keys=True)
    with state.lock:
        if idem_key is not None and idem_key in state.idempotency:
            record = state.idempotency[idem_key]
            if record["fingerprint"] == fingerprint:
                return record["status"], json.loads(json.dumps(record["body"]))
            raise ApiError("idempotency_mismatch",
                           "Idempotency-Key was already used for a different booking request")

        room_id, start, end, title, attendees = validate_booking_fields(body)
        room = state.rooms.get(room_id)
        if room is None:
            raise validation_error("room_id does not refer to an existing room")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "attendees exceed the room capacity")
        existing = state.bookings_by_room.setdefault(room_id, [])
        for other in existing:
            if start < other["end"] and other["start"] < end:
                raise ApiError("conflict", "the room is already booked for an overlapping time")

        number = state.next_booking_number
        state.next_booking_number += 1
        rec = {"id": f"b{number}", "n": number, "room_id": room_id, "start": start,
               "end": end, "title": title, "attendees": attendees}
        state.bookings[rec["id"]] = rec
        existing.append(rec)
        view = booking_view(rec)
        if idem_key is not None:
            state.idempotency[idem_key] = {"fingerprint": fingerprint, "status": 201,
                                           "body": dict(view)}
        return 201, view


def get_booking(state, booking_id):
    with state.lock:
        rec = state.bookings.get(booking_id)
        if rec is None:
            raise ApiError("not_found", "booking not found")
        return 200, booking_view(rec)


def delete_booking(state, booking_id):
    with state.lock:
        rec = state.bookings.pop(booking_id, None)
        if rec is None:
            raise ApiError("not_found", "booking not found")
        state.bookings_by_room[rec["room_id"]].remove(rec)
        return 204, None


def list_bookings(state, query):
    limit = 20
    raw_limit = first(query, "limit")
    if raw_limit is not None:
        if not INT_RE.fullmatch(raw_limit):
            raise validation_error("limit must be an integer from 1 to 100")
        limit = int(raw_limit)
        if not (1 <= limit <= 100):
            raise validation_error("limit must be an integer from 1 to 100")

    room_id = first(query, "room_id")
    frm = parse_ts(first(query, "from"), "from") if first(query, "from") is not None else None
    to = parse_ts(first(query, "to"), "to") if first(query, "to") is not None else None
    cursor = decode_cursor(first(query, "cursor")) if first(query, "cursor") is not None else None

    with state.lock:
        items = []
        for rec in state.bookings.values():
            if room_id is not None and rec["room_id"] != room_id:
                continue
            if frm is not None and not (rec["end"] > frm):
                continue
            if to is not None and not (rec["start"] < to):
                continue
            items.append(rec)
        items.sort(key=lambda r: (r["start"], r["n"]))
        if cursor is not None:
            items = [r for r in items if (r["start"], r["n"]) > cursor]
        page = items[:limit]
        more = len(items) > limit
        next_cursor = encode_cursor(page[-1]) if more and page else None
        return 200, {"bookings": [booking_view(r) for r in page], "next_cursor": next_cursor}


def availability(state, room_id, query):
    with state.lock:
        if room_id not in state.rooms:
            raise ApiError("not_found", "room not found")
        date_text = first(query, "date")
        if date_text is None or not DATE_RE.fullmatch(date_text):
            raise validation_error("date must be YYYY-MM-DD")
        try:
            day = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise validation_error("date must be a valid YYYY-MM-DD date")
        window_start = day.replace(hour=DAY_START_HOUR)
        window_end = day.replace(hour=DAY_END_HOUR)

        busy = []
        for rec in state.bookings_by_room.get(room_id, []):
            if rec["start"] < window_end and rec["end"] > window_start:
                busy.append((max(rec["start"], window_start), min(rec["end"], window_end)))
        busy.sort()

        free = []
        cursor = window_start
        for start, end in busy:
            if start > cursor:
                free.append({"start": format_ts(cursor), "end": format_ts(start)})
            cursor = max(cursor, end)
        if cursor < window_end:
            free.append({"start": format_ts(cursor), "end": format_ts(window_end)})
        return 200, {"date": date_text, "free": free}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def resolve(path):
    """Return (handler name, path arguments, allowed methods) or None for unknown paths."""
    if not path.startswith("/"):
        return None
    parts = path[1:].split("/")
    if parts == ["rooms"]:
        return "rooms", (), {"GET", "POST"}
    if len(parts) == 2 and parts[0] == "rooms" and parts[1]:
        return "room", (parts[1],), {"GET"}
    if len(parts) == 3 and parts[0] == "rooms" and parts[1] and parts[2] == "availability":
        return "availability", (parts[1],), {"GET"}
    if parts == ["bookings"]:
        return "bookings", (), {"GET", "POST"}
    if len(parts) == 2 and parts[0] == "bookings" and parts[1]:
        return "booking", (parts[1],), {"GET", "DELETE"}
    return None


class BookingServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler_class):
        super().__init__(address, handler_class)
        self.state = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _dispatch(self, method):
        head = method == "HEAD"
        routing_method = "GET" if head else method
        try:
            url = urlsplit(self.path)
            raw = self._read_body()
            route = resolve(url.path)
            if route is None:
                raise ApiError("not_found", "no such path")
            name, args, allowed = route
            if routing_method not in allowed:
                raise ApiError("method_not_allowed", f"method {method} is not allowed here")
            status, payload = self._handle(name, routing_method, args, parse_qs(url.query, keep_blank_values=True), raw)
        except ApiError as exc:
            status = STATUS_FOR_CODE[exc.code]
            payload = {"error": {"code": exc.code, "message": exc.message}}
        except Exception:
            status = 500
            payload = {"error": {"code": "internal_error", "message": "unexpected server error"}}
        self._send(status, payload, head)

    def _handle(self, name, method, args, query, raw):
        state = self.server.state
        if name == "rooms":
            return create_room(state, raw) if method == "POST" else list_rooms(state)
        if name == "room":
            return get_room(state, args[0])
        if name == "availability":
            return availability(state, args[0], query)
        if name == "bookings":
            if method == "POST":
                key = (self.headers.get("Idempotency-Key") or "").strip()
                return create_booking(state, raw, key or None)
            return list_bookings(state, query)
        if name == "booking":
            return get_booking(state, args[0]) if method == "GET" else delete_booking(state, args[0])
        raise ApiError("not_found", "no such path")

    def _send(self, status, payload, head=False):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and not head:
            self.wfile.write(body)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API (127.0.0.1 only).")
    parser.add_argument("--port", type=int, required=True, help="TCP port to listen on")
    args = parser.parse_args(argv)
    server = BookingServer((HOST, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
