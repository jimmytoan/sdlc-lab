#!/usr/bin/env python3
"""Meeting room booking API.

In-memory, standard library only. Run with:

    python server.py --port <N>

The server listens on 127.0.0.1 only. All bodies are JSON; errors use the form
{"error": {"code": "...", "message": "..."}}.
"""
import argparse
import base64
import copy
import json
import re
import threading
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

HOST = "127.0.0.1"
EPOCH = datetime(1970, 1, 1)
WINDOW_START = time(8, 0)
WINDOW_END = time(18, 0)
MAX_DURATION = timedelta(hours=8)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

STATUS = {
    "bad_json": 400,
    "not_found": 404,
    "method_not_allowed": 405,
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
        self.status = STATUS[code]
        self.message = message


def validation(message):
    return ApiError("validation_error", message)


# ---------------------------------------------------------------- timestamps

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")


def parse_ts(value, field):
    """Parse YYYY-MM-DDTHH:MM:SSZ into a naive UTC datetime."""
    m = TS_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        raise validation(f"{field} is not a valid calendar timestamp")


def fmt_ts(t):
    return (f"{t.year:04d}-{t.month:02d}-{t.day:02d}"
            f"T{t.hour:02d}:{t.minute:02d}:{t.second:02d}Z")


def epoch_secs(t):
    return int((t - EPOCH).total_seconds())


def check_quarter_hour(t, field):
    if t.minute not in (0, 15, 30, 45) or t.second != 0 or t.microsecond != 0:
        raise validation(f"{field} must start on a quarter hour (minutes 00, 15, 30 or 45)")


# ------------------------------------------------------------------- cursors

CURSOR_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
CURSOR_PAYLOAD_RE = re.compile(r"(-?[0-9]+):([0-9]+)")


def encode_cursor(key):
    raw = f"{key[0]}:{key[1]}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token):
    if not CURSOR_TOKEN_RE.fullmatch(token):
        raise validation("cursor is not valid")
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeError):
        raise validation("cursor is not valid")
    m = CURSOR_PAYLOAD_RE.fullmatch(raw)
    if m is None:
        raise validation("cursor is not valid")
    return (int(m.group(1)), int(m.group(2)))


# ------------------------------------------------------------------- helpers

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_body(raw):
    if not raw:
        raise ApiError("bad_json", "request body is empty; a JSON body is required")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError("bad_json", "request body is not valid JSON")


def require_object(body):
    if not isinstance(body, dict):
        raise validation("request body must be a JSON object")
    return body


# --------------------------------------------------------------------- store

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = []              # room dicts in creation order
        self.rooms_by_id = {}
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}           # booking id -> record
        self.room_bookings = {}      # room id -> list of booking records
        self.booking_seq = 0
        self.idem = {}               # Idempotency-Key -> {fingerprint, status, body}

    @staticmethod
    def render_booking(b):
        return {
            "id": b["id"],
            "room_id": b["room_id"],
            "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]),
            "title": b["title"],
            "attendees": b["attendees"],
        }

    # rooms
    def create_room(self, name, capacity):
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            self.room_seq += 1
            room = {"id": f"r{self.room_seq}", "name": name, "capacity": capacity}
            self.rooms.append(room)
            self.rooms_by_id[room["id"]] = room
            self.room_names.add(name)
            self.room_bookings[room["id"]] = []
            return dict(room)

    def list_rooms(self):
        with self.lock:
            return [dict(r) for r in self.rooms]

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms_by_id.get(room_id)
            return dict(room) if room is not None else None

    # bookings
    def create_booking(self, body, key, fingerprint):
        with self.lock:
            if key is not None and key in self.idem:
                rec = self.idem[key]
                if rec["fingerprint"] == fingerprint:
                    return rec["status"], copy.deepcopy(rec["body"])
                raise ApiError("idempotency_mismatch",
                               "this Idempotency-Key was already used with a different request")

            body = require_object(body)
            room_id = body.get("room_id")
            if not isinstance(room_id, str):
                raise validation("room_id must be a string")
            title = body.get("title")
            if not isinstance(title, str) or len(title) < 1:
                raise validation("title must be a non-empty string")
            attendees = body.get("attendees")
            if not is_int(attendees) or attendees < 1:
                raise validation("attendees must be an integer of at least 1")
            start = parse_ts(body.get("start"), "start")
            end = parse_ts(body.get("end"), "end")
            check_quarter_hour(start, "start")
            check_quarter_hour(end, "end")
            if end <= start:
                raise validation("end must be after start")
            if end - start > MAX_DURATION:
                raise validation("a booking may last at most 8 hours")

            room = self.rooms_by_id.get(room_id)
            if room is None:
                raise ApiError("not_found", "room does not exist")
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "attendees exceed the room capacity")
            for other in self.room_bookings[room_id]:
                if other["start"] < end and start < other["end"]:
                    raise ApiError("conflict", "the room is already booked for this time")

            self.booking_seq += 1
            rec = {
                "id": f"b{self.booking_seq}",
                "n": self.booking_seq,
                "room_id": room_id,
                "start": start,
                "end": end,
                "title": title,
                "attendees": attendees,
            }
            self.bookings[rec["id"]] = rec
            self.room_bookings[room_id].append(rec)
            out = self.render_booking(rec)
            if key is not None:
                self.idem[key] = {"fingerprint": fingerprint, "status": 201,
                                  "body": copy.deepcopy(out)}
            return 201, out

    def get_booking(self, booking_id):
        with self.lock:
            rec = self.bookings.get(booking_id)
            return self.render_booking(rec) if rec is not None else None

    def delete_booking(self, booking_id):
        with self.lock:
            rec = self.bookings.pop(booking_id, None)
            if rec is None:
                return False
            self.room_bookings[rec["room_id"]].remove(rec)
            return True

    def list_bookings(self, room_id, frm, to, limit, cursor):
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm is not None and not b["end"] > frm:
                    continue
                if to is not None and not b["start"] < to:
                    continue
                key = (epoch_secs(b["start"]), b["n"])
                if cursor is not None and key <= cursor:
                    continue
                items.append((key, b))
            items.sort(key=lambda item: item[0])
            page = items[:limit]
            has_more = len(items) > limit
            next_key = page[-1][0] if has_more else None
            return [self.render_booking(b) for _, b in page], next_key

    def free_slots(self, room_id, day):
        ws = datetime.combine(day, WINDOW_START)
        we = datetime.combine(day, WINDOW_END)
        with self.lock:
            if room_id not in self.rooms_by_id:
                raise ApiError("not_found", "room does not exist")
            busy = sorted(
                ((b["start"], b["end"]) for b in self.room_bookings[room_id]
                 if b["start"] < we and b["end"] > ws),
                key=lambda pair: pair[0],
            )
        slots = []
        cursor = ws
        for s, e in busy:
            if s > cursor:
                slots.append((cursor, min(s, we)))
            cursor = max(cursor, e)
        if cursor < we:
            slots.append((cursor, we))
        return slots


# ------------------------------------------------------------------ handlers

class Request:
    def __init__(self, method, raw, headers, query, store):
        self.method = method
        self.raw = raw
        self.headers = headers
        self.query = query
        self.store = store


def h_create_room(req):
    body = require_object(parse_body(req.raw))
    name = body.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 50:
        raise validation("name must be a string of 1 to 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not 1 <= capacity <= 500:
        raise validation("capacity must be an integer from 1 to 500")
    return 201, req.store.create_room(name, capacity)


def h_list_rooms(req):
    return 200, {"rooms": req.store.list_rooms()}


def h_get_room(req, room_id):
    room = req.store.get_room(room_id)
    if room is None:
        raise ApiError("not_found", "room does not exist")
    return 200, room


def h_availability(req, room_id):
    day_text = req.query.get("date")
    if day_text is None or not DATE_RE.fullmatch(day_text):
        raise validation("date must be a YYYY-MM-DD date")
    try:
        day = date_cls(*(int(p) for p in day_text.split("-")))
    except ValueError:
        raise validation("date must be a real calendar date in YYYY-MM-DD form")
    slots = req.store.free_slots(room_id, day)
    return 200, {
        "date": day_text,
        "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in slots],
    }


def h_create_booking(req):
    body = parse_body(req.raw)
    key = req.headers.get("Idempotency-Key")
    if key is not None and key.strip() == "":
        raise validation("Idempotency-Key must not be empty")
    fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return req.store.create_booking(body, key, fingerprint)


def h_list_bookings(req):
    q = req.query
    limit = DEFAULT_LIMIT
    if "limit" in q:
        if not INT_RE.fullmatch(q["limit"]) or not 1 <= int(q["limit"]) <= MAX_LIMIT:
            raise validation("limit must be an integer from 1 to 100")
        limit = int(q["limit"])
    frm = parse_ts(q["from"], "from") if "from" in q else None
    to = parse_ts(q["to"], "to") if "to" in q else None
    if frm is not None and to is not None and to <= frm:
        raise validation("to must be after from")
    cursor = decode_cursor(q["cursor"]) if "cursor" in q else None
    items, next_key = req.store.list_bookings(q.get("room_id"), frm, to, limit, cursor)
    return 200, {
        "bookings": items,
        "next_cursor": encode_cursor(next_key) if next_key is not None else None,
    }


def h_get_booking(req, booking_id):
    b = req.store.get_booking(booking_id)
    if b is None:
        raise ApiError("not_found", "booking does not exist")
    return 200, b


def h_delete_booking(req, booking_id):
    if not req.store.delete_booking(booking_id):
        raise ApiError("not_found", "booking does not exist")
    return 204, None


ROUTES = [
    (re.compile(r"/rooms"), {"GET": h_list_rooms, "POST": h_create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": h_get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": h_availability}),
    (re.compile(r"/bookings"), {"GET": h_list_bookings, "POST": h_create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": h_get_booking, "DELETE": h_delete_booking}),
]


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def _content_length(self):
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _dispatch(self, method):
        try:
            length = self._content_length()
            raw = self.rfile.read(length) if length else b""
            path, _, query_text = self.path.partition("?")
            path = unquote(path)
            matched = None
            for pattern, methods in ROUTES:
                m = pattern.fullmatch(path)
                if m is not None:
                    matched = (methods, m.groups())
                    break
            if matched is None:
                raise ApiError("not_found", "no such path")
            methods, groups = matched
            handler = methods.get(method)
            if handler is None:
                raise ApiError("method_not_allowed", f"{method} is not allowed on this path")
            query = {k: v[0] for k, v in parse_qs(query_text, keep_blank_values=True).items()}
            req = Request(method, raw, self.headers, query, self.server.store)
            status, body = handler(req, *groups)
            self._send(status, body)
        except ApiError as e:
            self._send(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception:
            self._send(500, {"error": {"code": "internal_error",
                                       "message": "internal server error"}})

    def _send(self, status, body):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, required=True, help="port to listen on")
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((HOST, args.port), Handler)
    server.daemon_threads = True
    server.store = Store()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
