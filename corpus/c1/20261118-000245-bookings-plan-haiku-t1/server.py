"""Meeting room booking API (stdlib only, in-memory state).

Run: python server.py --port N   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
import threading
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

MAX_BODY = 64 * 1024
MAX_KEY_LEN = 255
WORK_START = time(8, 0)
WORK_END = time(18, 0)
MAX_DURATION = timedelta(hours=8)

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")

LOCK = threading.Lock()
ROOMS = {}            # id -> room dict
ROOM_ORDER = []       # room ids in creation order
ROOM_NAMES = set()
NEXT_ROOM = 1
BOOKINGS = {}         # id -> booking record (with "_num")
NEXT_BOOKING = 1
IDEM = {}             # key -> (fingerprint, status, body)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation(message):
    return ApiError(422, "validation_error", message)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    """Strict YYYY-MM-DDTHH:MM:SSZ; returns aware UTC datetime or raises validation error."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise validation(f"{field} must be a timestamp like 2026-03-02T09:00:00Z")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise validation(f"{field} is not a valid timestamp")
    return dt.replace(tzinfo=timezone.utc)


def parse_json(raw):
    if not raw:
        raise ApiError(400, "bad_json", "request body is empty")

    def reject_constant(name):
        raise ValueError(name)

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------- rooms ----------

def create_room(raw):
    global NEXT_ROOM
    body = parse_json(raw)
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str):
        raise validation("name must be a string")
    if name.strip() == "":
        raise validation("name must not be empty")
    if len(name) > 50:
        raise validation("name must be at most 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity):
        raise validation("capacity must be an integer")
    if not 1 <= capacity <= 500:
        raise validation("capacity must be between 1 and 500")
    with LOCK:
        if name in ROOM_NAMES:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        room = {"id": f"r{NEXT_ROOM}", "name": name, "capacity": capacity}
        NEXT_ROOM += 1
        ROOMS[room["id"]] = room
        ROOM_ORDER.append(room["id"])
        ROOM_NAMES.add(name)
        return 201, dict(room)


def list_rooms():
    with LOCK:
        return 200, {"rooms": [dict(ROOMS[i]) for i in ROOM_ORDER]}


def get_room(room_id):
    with LOCK:
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, dict(room)


def availability(room_id, query):
    with LOCK:
        if room_id not in ROOMS:
            raise ApiError(404, "not_found", "room not found")
        d = query.get("date")
        if d is None or not DATE_RE.fullmatch(d):
            raise validation("date must be YYYY-MM-DD")
        try:
            day = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            raise validation("date is not a valid calendar date")
        w_start = datetime.combine(day, WORK_START, tzinfo=timezone.utc)
        w_end = datetime.combine(day, WORK_END, tzinfo=timezone.utc)
        spans = []
        for b in BOOKINGS.values():
            if b["room_id"] != room_id:
                continue
            if b["start"] < w_end and b["end"] > w_start:
                spans.append((max(b["start"], w_start), min(b["end"], w_end)))
        spans.sort()
        free = []
        cur = w_start
        for s, e in spans:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            if e > cur:
                cur = e
        if cur < w_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(w_end)})
        return 200, {"date": d, "free": free}


# ---------- bookings ----------

def booking_out(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def validate_booking(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise validation("room_id must be a non-empty string")
    title = body.get("title")
    if not isinstance(title, str) or title.strip() == "":
        raise validation("title must be a non-empty string")
    if len(title) > 200:
        raise validation("title must be at most 200 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise validation("attendees must be an integer of at least 1")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
            raise validation(f"{field} must fall on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking must be at most 8 hours long")
    return room_id, start, end, title, attendees


def fingerprint(parsed_body):
    canon = json.dumps(parsed_body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"POST\n/bookings\n{canon}".encode("utf-8")).hexdigest()


def create_booking(raw, key):
    global NEXT_BOOKING
    with LOCK:
        try:
            fp = fingerprint(parse_json(raw))
        except ApiError:
            fp = None
        if key is not None and key in IDEM:
            stored_fp, status, body = IDEM[key]
            if fp is not None and fp == stored_fp:
                return status, dict(body)
            raise ApiError(422, "idempotency_mismatch",
                           "Idempotency-Key was already used for a different request")

        parsed = parse_json(raw)
        room_id, start, end, title, attendees = validate_booking(parsed)
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "attendees exceed room capacity")
        for other in BOOKINGS.values():
            if other["room_id"] == room_id and start < other["end"] and end > other["start"]:
                raise ApiError(409, "conflict", "booking overlaps an existing booking")

        num = NEXT_BOOKING
        NEXT_BOOKING += 1
        rec = {"id": f"b{num}", "_num": num, "room_id": room_id, "start": start,
               "end": end, "title": title, "attendees": attendees}
        BOOKINGS[rec["id"]] = rec
        out = booking_out(rec)
        if key is not None:
            IDEM[key] = (fp, 201, dict(out))
        return 201, out


def get_booking(bid):
    with LOCK:
        b = BOOKINGS.get(bid)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, booking_out(b)


def delete_booking(bid):
    with LOCK:
        if bid not in BOOKINGS:
            raise ApiError(404, "not_found", "booking not found")
        del BOOKINGS[bid]
        return 204, None


def encode_cursor(start, num):
    raw = json.dumps({"s": fmt_ts(start), "i": num}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        raise validation("cursor is malformed")
    if not isinstance(obj, dict) or not isinstance(obj.get("s"), str) or not is_int(obj.get("i")):
        raise validation("cursor is malformed")
    start = parse_ts(obj["s"], "cursor")
    return start, obj["i"]


def list_bookings(query):
    room_id = query.get("room_id")
    frm = query.get("from")
    to = query.get("to")
    f_dt = parse_ts(frm, "from") if frm is not None else None
    t_dt = parse_ts(to, "to") if to is not None else None
    if f_dt is not None and t_dt is not None and f_dt >= t_dt:
        raise validation("from must be before to")

    limit_raw = query.get("limit")
    if limit_raw is None:
        limit = 20
    else:
        if not INT_RE.fullmatch(limit_raw) or not 1 <= int(limit_raw) <= 100:
            raise validation("limit must be an integer between 1 and 100")
        limit = int(limit_raw)

    cursor_key = None
    cursor_raw = query.get("cursor")
    if cursor_raw:
        cursor_key = decode_cursor(cursor_raw)

    with LOCK:
        items = []
        for b in BOOKINGS.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if t_dt is not None and not b["start"] < t_dt:
                continue
            if f_dt is not None and not b["end"] > f_dt:
                continue
            key = (b["start"], b["_num"])
            if cursor_key is not None and not key > cursor_key:
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start"], b["_num"]))
        page = items[:limit]
        more = len(items) > limit
        next_cursor = None
        if more and page:
            last = page[-1]
            next_cursor = encode_cursor(last["start"], last["_num"])
        return 200, {"bookings": [booking_out(b) for b in page], "next_cursor": next_cursor}


# ---------- HTTP layer ----------

def match_route(path):
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "":
        return None, None, None
    if parts[1] == "rooms":
        if len(parts) == 2:
            return "rooms", ("GET", "POST"), None
        if len(parts) == 3 and parts[2]:
            return "room", ("GET",), parts[2]
        if len(parts) == 4 and parts[2] and parts[3] == "availability":
            return "availability", ("GET",), parts[2]
    elif parts[1] == "bookings":
        if len(parts) == 2:
            return "bookings", ("GET", "POST"), None
        if len(parts) == 3 and parts[2]:
            return "booking", ("GET", "DELETE"), parts[2]
    return None, None, None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _send(self, status, payload=None, extra_headers=None):
        body = b""
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_error(self, err, extra_headers=None):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}}, extra_headers)

    def _read_body(self):
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        try:
            n = int(cl)
        except ValueError:
            raise ApiError(400, "bad_json", "invalid Content-Length")
        if n < 0:
            raise ApiError(400, "bad_json", "invalid Content-Length")
        if n > MAX_BODY:
            self.close_connection = True
            raise ApiError(400, "bad_json", "request body too large")
        return self.rfile.read(n) if n else b""

    def _dispatch(self):
        try:
            raw = self._read_body()
            split = urlsplit(self.path)
            query = {}
            for k, v in parse_qsl(split.query, keep_blank_values=True):
                query.setdefault(k, v)
            name, methods, arg = match_route(split.path)
            if name is None:
                raise ApiError(404, "not_found", "no such path")
            if self.command not in methods:
                allow = ", ".join(methods)
                raise ApiError(405, "method_not_allowed", f"method {self.command} not allowed")._with_allow(allow)
            status, payload, extra = self._route(name, arg, query, raw)
            self._send(status, payload, extra)
        except ApiError as err:
            extra = [("Allow", err.allow)] if getattr(err, "allow", None) else None
            self._send_error(err, extra)
        except Exception:
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})

    def _route(self, name, arg, query, raw):
        m = self.command
        if name == "rooms":
            if m == "POST":
                s, p = create_room(raw)
            else:
                s, p = list_rooms()
        elif name == "room":
            s, p = get_room(arg)
        elif name == "availability":
            s, p = availability(arg, query)
        elif name == "bookings":
            if m == "POST":
                key = self.headers.get("Idempotency-Key")
                key = key.strip() if key is not None else ""
                if key == "":
                    key = None
                elif len(key) > MAX_KEY_LEN:
                    raise validation("Idempotency-Key must be at most 255 characters")
                s, p = create_booking(raw, key)
            else:
                s, p = list_bookings(query)
        else:  # booking
            if m == "DELETE":
                s, p = delete_booking(arg)
            else:
                s, p = get_booking(arg)
        return s, p, None

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()


def _with_allow(self, allow):
    self.allow = allow
    return self


ApiError._with_allow = _with_allow


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room bookings API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
