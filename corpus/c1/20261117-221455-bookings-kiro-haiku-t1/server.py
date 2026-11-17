#!/usr/bin/env python3
"""bookings: in-memory meeting room booking JSON API.

Run as:  python server.py --port N   (listens on 127.0.0.1 only)
Spec:    .kiro/specs/bookings/ (requirements, design, tasks)
Standard library only. All state is held in memory and never written to disk.
"""

import argparse
import base64
import binascii
import datetime as _dt
import hashlib
import hmac
import json
import re
import secrets
import socket
import socketserver
import sys
import threading
import traceback
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 64 * 1024                 # bytes (R2.9)
MAX_DRAIN = 8 * 1024 * 1024          # unread request bytes we are willing to discard
IDEM_KEY_MAX = 255                   # characters (R6.9)
DEFAULT_LIMIT = 20                   # R8.11
MAX_LIMIT = 100                      # R8.13
MAX_ROOM_NAME = 50                   # R3.7
MAX_CAPACITY = 500                   # R3.14
MAX_TITLE = 200                      # R5.18
MAX_DURATION = 8 * 3600              # R5.12
QUARTER = 900                        # seconds
WORK_START = 8 * 3600                # 08:00:00Z, seconds after midnight (R9.6)
WORK_END = 18 * 3600                 # 18:00:00Z

EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
ONE_SECOND = _dt.timedelta(seconds=1)


class ApiError(Exception):
    """A rejection. Raised before any state is changed."""

    def __init__(self, status, code, message, allow=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.allow = allow


def validation_error(message):
    return ApiError(422, "validation_error", message)


def bad_json(message):
    return ApiError(400, "bad_json", message)


def not_found(message="resource not found"):
    return ApiError(404, "not_found", message)


def error_payload(code, message):
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------- timestamps

_TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")


def parse_timestamp(value, what="timestamp"):
    """Parse YYYY-MM-DDTHH:MM:SSZ into epoch seconds (R5.8, R5.9)."""
    if not isinstance(value, str):
        raise validation_error(f"{what} must be a string")
    m = _TS_RE.fullmatch(value)
    if m is None:
        raise validation_error(f"{what} must be in the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        d = _dt.datetime(*(int(g) for g in m.groups()), tzinfo=_dt.timezone.utc)
    except ValueError:
        raise validation_error(f"{what} is not a real date and time")
    return (d - EPOCH) // ONE_SECOND


def format_timestamp(epoch):
    d = EPOCH + _dt.timedelta(seconds=epoch)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"


def parse_date(value):
    """Parse YYYY-MM-DD into a real calendar date (R9.5)."""
    m = _DATE_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise validation_error("date must be in the form YYYY-MM-DD")
    try:
        return _dt.date(*(int(g) for g in m.groups()))
    except ValueError:
        raise validation_error("date is not a real calendar date")


def day_start_epoch(day):
    return (_dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc) - EPOCH) // ONE_SECOND


# ---------------------------------------------------------------- validation

def validate_room(body):
    """Return (name, capacity) or raise validation_error (R3.4 to R3.17)."""
    name = body.get("name")
    if not isinstance(name, str):
        raise validation_error("name must be a string")
    if len(name) == 0:
        raise validation_error("name must not be empty")
    if name.strip() == "":
        raise validation_error("name must not be only whitespace")
    if len(name) > MAX_ROOM_NAME:
        raise validation_error(f"name must be at most {MAX_ROOM_NAME} characters")
    capacity = body.get("capacity")
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        raise validation_error("capacity must be an integer")
    if capacity < 1 or capacity > MAX_CAPACITY:
        raise validation_error(f"capacity must be from 1 to {MAX_CAPACITY}")
    return name, capacity


@dataclass(frozen=True)
class BookingInput:
    room_id: str
    start: int
    end: int
    title: str
    attendees: int


def validate_booking(body):
    """Field-level checks for POST /bookings (R5.5 to R5.20). Room and capacity checks happen later."""
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise validation_error("room_id must be a string")
    start_raw = body.get("start")
    end_raw = body.get("end")
    if not isinstance(start_raw, str) or not isinstance(end_raw, str):
        raise validation_error("start and end must be strings")
    start = parse_timestamp(start_raw, "start")
    end = parse_timestamp(end_raw, "end")
    for value, what in ((start, "start"), (end, "end")):
        if value % QUARTER != 0:
            raise validation_error(f"{what} must fall on a quarter hour")
    if end <= start:
        raise validation_error("end must be later than start")
    if end - start > MAX_DURATION:
        raise validation_error("booking may not be longer than 8 hours")
    title = body.get("title")
    if not isinstance(title, str) or len(title) == 0:
        raise validation_error("title must be a non-empty string")
    if len(title) > MAX_TITLE:
        raise validation_error(f"title must be at most {MAX_TITLE} characters")
    attendees = body.get("attendees")
    if isinstance(attendees, bool) or not isinstance(attendees, int):
        raise validation_error("attendees must be an integer")
    if attendees < 1:
        raise validation_error("attendees must be at least 1")
    return BookingInput(room_id, start, end, title, attendees)


def single_param(query, name):
    values = query.get(name)
    if values is None:
        return None
    if len(values) > 1:
        raise validation_error(f"{name} may be given only once")
    return values[0]


def parse_limit(query):
    raw = single_param(query, "limit")
    if raw is None:
        return DEFAULT_LIMIT
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 6:
        raise validation_error("limit must be an integer from 1 to 100")
    value = int(raw)
    if value < 1 or value > MAX_LIMIT:
        raise validation_error("limit must be an integer from 1 to 100")
    return value


# ---------------------------------------------------------------- cursors

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+")


def encode_cursor(key, start, seq):
    payload = json.dumps({"s": start, "q": seq}, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key, payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + b"." + sig).decode("ascii").rstrip("=")


def decode_cursor(key, text):
    """Return (start, seq) for a cursor this process issued, else validation_error (R8.20)."""
    bad = validation_error("cursor is not a value issued by the server")
    if not _B64URL_RE.fullmatch(text) or len(text) % 4 == 1:
        raise bad
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise bad
    if len(raw) < 18 or raw[-17:-16] != b".":
        raise bad
    payload, sig = raw[:-17], raw[-16:]
    expected = hmac.new(key, payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, expected):
        raise bad
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise bad
    if not isinstance(obj, dict):
        raise bad
    s, q = obj.get("s"), obj.get("q")
    if isinstance(s, bool) or isinstance(q, bool) or not isinstance(s, int) or not isinstance(q, int):
        raise bad
    return s, q


# ---------------------------------------------------------------- availability

def free_intervals(spans, ws, we):
    """Free sub-intervals of [ws, we) not covered by spans (list of (start, end) epoch pairs).

    Spans are clipped to the window by the caller. A booking end counts as free time (R9.10),
    and zero-length gaps are omitted (R9.13).
    """
    out = []
    cur = ws
    for s, e in sorted(spans):
        if s > cur:
            out.append((cur, s))
        if e > cur:
            cur = e
    if cur < we:
        out.append((cur, we))
    return out


# ---------------------------------------------------------------- store

@dataclass(frozen=True)
class Room:
    id: str
    seq: int
    name: str
    capacity: int


@dataclass(frozen=True)
class Booking:
    id: str
    seq: int
    room_id: str
    start: int
    end: int
    title: str
    attendees: int


@dataclass(frozen=True)
class IdemRecord:
    fingerprint: tuple
    body: dict


def render_room(room):
    return {"id": room.id, "name": room.name, "capacity": room.capacity}


def render_booking(b):
    return {
        "id": b.id,
        "room_id": b.room_id,
        "start": format_timestamp(b.start),
        "end": format_timestamp(b.end),
        "title": b.title,
        "attendees": b.attendees,
    }


class Store:
    """All state. Every operation runs entirely under self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}
        self.room_order = []
        self.room_names = set()
        self.bookings = {}
        self.room_bookings = {}
        self.idem = {}
        self.next_room_seq = 1
        self.next_booking_seq = 1
        self.cursor_key = secrets.token_bytes(32)

    def create_room(self, name, capacity):
        with self.lock:
            if name in self.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            seq = self.next_room_seq
            room = Room(f"r{seq}", seq, name, capacity)
            self.next_room_seq += 1
            self.rooms[room.id] = room
            self.room_order.append(room)
            self.room_names.add(name)
            self.room_bookings[room.id] = []
            return room

    def list_rooms(self):
        with self.lock:
            return list(self.room_order)

    def get_room(self, room_id):
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise not_found("room not found")
            return room

    def create_booking(self, inp, idem_key):
        """Critical section for POST /bookings (design: Create booking)."""
        fingerprint = (inp.room_id, inp.start, inp.end, inp.title, inp.attendees)
        with self.lock:
            room = self.rooms.get(inp.room_id)
            if room is None:
                raise validation_error("room_id does not name an existing room")
            if idem_key is not None:
                rec = self.idem.get(idem_key)
                if rec is not None:
                    if rec.fingerprint == fingerprint:
                        return dict(rec.body)
                    raise ApiError(422, "idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
            if inp.attendees > room.capacity:
                raise ApiError(422, "over_capacity", "attendees exceed the room capacity")
            for other in self.room_bookings[room.id]:
                if other.start < inp.end and inp.start < other.end:
                    raise ApiError(409, "conflict", "the room is already booked for this time")
            seq = self.next_booking_seq
            booking = Booking(f"b{seq}", seq, room.id, inp.start, inp.end, inp.title, inp.attendees)
            self.next_booking_seq += 1
            self.bookings[booking.id] = booking
            self.room_bookings[room.id].append(booking)
            body = render_booking(booking)
            if idem_key is not None:
                self.idem[idem_key] = IdemRecord(fingerprint, dict(body))
            return dict(body)

    def get_booking(self, booking_id):
        with self.lock:
            b = self.bookings.get(booking_id)
            if b is None:
                raise not_found("booking not found")
            return render_booking(b)

    def delete_booking(self, booking_id):
        with self.lock:
            b = self.bookings.pop(booking_id, None)
            if b is None:
                raise not_found("booking not found")
            self.room_bookings[b.room_id] = [x for x in self.room_bookings[b.room_id] if x.id != b.id]

    def list_bookings(self, room_id, frm, to, cursor_pos, limit):
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b.room_id != room_id:
                    continue
                if frm is not None and not b.end > frm:
                    continue
                if to is not None and not b.start < to:
                    continue
                if cursor_pos is not None and (b.start, b.seq) <= cursor_pos:
                    continue
                items.append(b)
            items.sort(key=lambda b: (b.start, b.seq))
            page = items[:limit]
            next_cursor = None
            if len(items) > limit and page:
                last = page[-1]
                next_cursor = encode_cursor(self.cursor_key, last.start, last.seq)
            return [render_booking(b) for b in page], next_cursor

    def day_spans(self, room_id, ws, we):
        """Booking spans of the room clipped to [ws, we)."""
        with self.lock:
            return [(max(b.start, ws), min(b.end, we))
                    for b in self.room_bookings[room_id]
                    if b.start < we and b.end > ws]


# ---------------------------------------------------------------- HTTP layer

def _reject_constant(name):
    raise ValueError(f"invalid JSON constant {name}")


def match_route(path):
    """Return (method -> handler name, path args) or None. Exact matching, no trailing-slash tolerance."""
    segs = path.split("/")
    if segs == ["", "rooms"]:
        return {"GET": "list_rooms", "POST": "create_room"}, ()
    if len(segs) == 3 and segs[1] == "rooms" and segs[2]:
        return {"GET": "get_room"}, (segs[2],)
    if len(segs) == 4 and segs[1] == "rooms" and segs[2] and segs[3] == "availability":
        return {"GET": "availability"}, (segs[2],)
    if segs == ["", "bookings"]:
        return {"GET": "list_bookings", "POST": "create_booking"}, ()
    if len(segs) == 3 and segs[1] == "bookings" and segs[2]:
        return {"GET": "get_booking", "DELETE": "delete_booking"}, (segs[2],)
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    # protocol_version stays HTTP/1.0: every response closes the connection.

    def log_message(self, format, *args):  # keep stderr quiet
        pass

    def __getattr__(self, name):
        # Route every HTTP method (including non-standard ones) through the same dispatcher.
        if name.startswith("do_"):
            method = name[3:]
            return lambda: self._handle(method)
        raise AttributeError(name)

    def send_error(self, code, message=None, explain=None):
        # Protocol-level parse errors: keep the body JSON (R2.1).
        self._respond(400, error_payload("bad_json", "malformed HTTP request"), {})
        self.close_connection = True

    def _handle(self, method):
        self._drain_bytes = 0
        self._body_consumed = False
        extra = {}
        try:
            status, payload = self._dispatch(method)
        except ApiError as e:
            status, payload = e.status, error_payload(e.code, e.message)
            if e.allow:
                extra["Allow"] = e.allow
        except Exception:
            traceback.print_exc(file=sys.stderr)
            status, payload = 500, error_payload("internal_error", "unexpected error")
        self._respond(status, payload, extra)
        self._drain_unread()

    def _respond(self, status, payload, extra):
        body = None
        if payload is not None and status != 204:
            body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    def _drain_unread(self):
        """Discard any request body we did not read, so the client sees our response before a reset."""
        pending = self._drain_bytes
        if not pending and not self._body_consumed:
            cl = self.headers.get("Content-Length", "")
            pending = int(cl) if cl.strip().isdigit() else 0
        remaining = min(pending, MAX_DRAIN)
        if remaining <= 0:
            return
        try:
            self.connection.settimeout(2.0)
            self.connection.shutdown(socket.SHUT_WR)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    def _dispatch(self, method):
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        matched = match_route(split.path)
        if matched is None:
            raise not_found("no such path")
        table, args = matched
        name = table.get(method)
        if name is None:
            raise ApiError(405, "method_not_allowed", f"method {method} is not allowed here",
                           allow=", ".join(table))
        return getattr(self, "_" + name)(args, query)

    # ----- body and header helpers

    def _read_body(self):
        self._body_consumed = True
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            return self._read_chunked()
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        cl = cl.strip()
        if not (cl.isascii() and cl.isdigit()):
            raise bad_json("invalid Content-Length")
        n = int(cl)
        if n > MAX_BODY:
            self._drain_bytes = n
            raise bad_json("request body is larger than 64 KiB")
        return self.rfile.read(n) if n else b""

    def _read_chunked(self):
        parts = []
        total = 0
        while True:
            line = self.rfile.readline(MAX_BODY + 1)
            size_s = line.split(b";", 1)[0].strip()
            if not line or not re.fullmatch(rb"[0-9a-fA-F]+", size_s):
                raise bad_json("malformed chunked body")
            size = int(size_s, 16)
            if size == 0:
                while True:  # skip trailers
                    trailer = self.rfile.readline(MAX_BODY + 1)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > MAX_BODY:
                self._drain_bytes = MAX_BODY
                raise bad_json("request body is larger than 64 KiB")
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise bad_json("malformed chunked body")
            parts.append(chunk)
            self.rfile.readline(3)  # CRLF after each chunk
        return b"".join(parts)

    def _read_json_object(self):
        raw = self._read_body()
        if not raw:
            raise bad_json("request body is required")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise bad_json("request body is not valid UTF-8")
        try:
            value = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise bad_json("request body is not valid JSON")
        if not isinstance(value, dict):
            raise validation_error("request body must be a JSON object")
        return value

    def _idempotency_key(self):
        key = self.headers.get("Idempotency-Key")
        if key is None:
            return None
        if key == "" or len(key) > IDEM_KEY_MAX:
            raise validation_error(f"Idempotency-Key must be 1 to {IDEM_KEY_MAX} characters")
        return key

    # ----- handlers: each returns (status, payload)

    def _list_rooms(self, args, query):
        return 200, {"rooms": [render_room(r) for r in self.server.store.list_rooms()]}

    def _create_room(self, args, query):
        body = self._read_json_object()
        name, capacity = validate_room(body)
        room = self.server.store.create_room(name, capacity)
        return 201, render_room(room)

    def _get_room(self, args, query):
        return 200, render_room(self.server.store.get_room(args[0]))

    def _availability(self, args, query):
        store = self.server.store
        store.get_room(args[0])  # unknown room is 404, checked before the date
        date_raw = single_param(query, "date")
        if date_raw is None:
            raise validation_error("date query parameter is required")
        day = parse_date(date_raw)
        base = day_start_epoch(day)
        ws, we = base + WORK_START, base + WORK_END
        free = free_intervals(store.day_spans(args[0], ws, we), ws, we)
        return 200, {
            "date": date_raw,
            "free": [{"start": format_timestamp(s), "end": format_timestamp(e)} for s, e in free],
        }

    def _list_bookings(self, args, query):
        store = self.server.store
        room_id = single_param(query, "room_id")
        if room_id == "":
            raise validation_error("room_id must not be empty")
        frm_raw = single_param(query, "from")
        to_raw = single_param(query, "to")
        frm = parse_timestamp(frm_raw, "from") if frm_raw is not None else None
        to = parse_timestamp(to_raw, "to") if to_raw is not None else None
        if frm is not None and to is not None and not frm < to:
            raise validation_error("from must be earlier than to")
        limit = parse_limit(query)
        cursor_raw = single_param(query, "cursor")
        cursor_pos = None
        if cursor_raw is not None:
            if cursor_raw == "":
                raise validation_error("cursor is not a value issued by the server")
            cursor_pos = decode_cursor(store.cursor_key, cursor_raw)
        bookings, next_cursor = store.list_bookings(room_id, frm, to, cursor_pos, limit)
        return 200, {"bookings": bookings, "next_cursor": next_cursor}

    def _create_booking(self, args, query):
        body = self._read_json_object()
        inp = validate_booking(body)
        key = self._idempotency_key()
        return 201, self.server.store.create_booking(inp, key)

    def _get_booking(self, args, query):
        return 200, self.server.store.get_booking(args[0])

    def _delete_booking(self, args, query):
        self.server.store.delete_booking(args[0])
        return 204, None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # Skip the reverse-DNS lookup of HTTPServer.server_bind; we are bound to a fixed loopback address.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


# ---------------------------------------------------------------- start-up

def port_arg(text):
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: must be an integer from 1 to 65535")
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: must be an integer from 1 to 65535")
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {text!r}: must be an integer from 1 to 65535")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="server.py",
                                     description="bookings: meeting room booking API on 127.0.0.1")
    parser.add_argument("--port", type=port_arg, required=True, metavar="N",
                        help="TCP port to listen on, 1 to 65535")
    args = parser.parse_args(argv)
    try:
        httpd = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    httpd.store = Store()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
