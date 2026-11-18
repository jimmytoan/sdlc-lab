"""Meeting room booking JSON API (standard library only).

Run: python server.py --port N   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

UTC = timezone.utc
MAX_BODY = 1024 * 1024
MAX_KEY_LEN = 255
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_DURATION = timedelta(hours=8)

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
INT_RE = re.compile(r"[0-9]+")
BOOKING_ID_RE = re.compile(r"b[0-9]+")
CONTENT_LENGTH_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def validation_error(message):
    return ApiError(422, "validation_error", message)


def fmt_ts(dt):
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_ts(value, field):
    """Strict UTC timestamp YYYY-MM-DDTHH:MM:SSZ. Returns an aware datetime."""
    if not isinstance(value, str):
        raise validation_error(f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.fullmatch(value)
    if not m:
        raise validation_error(f"{field} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise validation_error(f"{field} is not a valid date or time")


def parse_object(raw):
    if not raw:
        raise ApiError(400, "bad_json", "request body is required")

    def _reject_constant(name):
        raise ValueError(name)

    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except Exception:
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise validation_error("request body must be a JSON object")
    return data


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def encode_cursor(start_dt, booking_id):
    raw = json.dumps([fmt_ts(start_dt), booking_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(text):
    try:
        padded = text + "=" * (-len(text) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
        if not (isinstance(value, list) and len(value) == 2
                and isinstance(value[0], str) and isinstance(value[1], str)):
            raise ValueError("bad shape")
        start = parse_ts(value[0], "cursor")
        if not BOOKING_ID_RE.fullmatch(value[1]):
            raise ValueError("bad id")
        return (start, int(value[1][1:]))
    except Exception:
        raise validation_error("cursor is malformed")


def numeric_id(booking_id):
    return int(booking_id[1:])


class Service:
    """All state. Every public method is called while holding self.lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}           # id -> room dict
        self.room_order = []      # ids in creation order
        self.room_names = set()
        self.next_room = 1
        self.bookings = {}        # id -> booking dict
        self.next_booking = 1
        self.idempotency = {}     # key -> (fingerprint, status, response body)

    # ----- rooms -----
    @staticmethod
    def room_json(room):
        return {"id": room["id"], "name": room["name"], "capacity": room["capacity"]}

    def create_room(self, raw):
        data = parse_object(raw)
        if "name" not in data or "capacity" not in data:
            raise validation_error("name and capacity are required")
        name = data["name"]
        if not isinstance(name, str):
            raise validation_error("name must be a string")
        if len(name) < 1 or len(name) > 50:
            raise validation_error("name must be 1 to 50 characters")
        if not name.strip():
            raise validation_error("name must not be whitespace only")
        capacity = data["capacity"]
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise validation_error("capacity must be an integer from 1 to 500")
        if name in self.room_names:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        room_id = f"r{self.next_room}"
        self.next_room += 1
        room = {"id": room_id, "name": name, "capacity": capacity}
        self.rooms[room_id] = room
        self.room_order.append(room_id)
        self.room_names.add(name)
        return 201, self.room_json(room), {}

    def list_rooms(self):
        return 200, {"rooms": [self.room_json(self.rooms[r]) for r in self.room_order]}, {}

    def get_room(self, room_id):
        room = self.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, self.room_json(room), {}

    # ----- bookings -----
    @staticmethod
    def booking_json(b):
        return {
            "id": b["id"],
            "room_id": b["room_id"],
            "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]),
            "title": b["title"],
            "attendees": b["attendees"],
        }

    def create_booking(self, raw, idem_key):
        data = parse_object(raw)
        for field in ("room_id", "start", "end", "title", "attendees"):
            if field not in data:
                raise validation_error(f"{field} is required")
        room_id = data["room_id"]
        if not isinstance(room_id, str):
            raise validation_error("room_id must be a string")
        start = parse_ts(data["start"], "start")
        end = parse_ts(data["end"], "end")
        for dt, field in ((start, "start"), (end, "end")):
            if dt.minute % 15 != 0 or dt.second != 0:
                raise validation_error(f"{field} must be aligned to quarter hours")
        if end <= start:
            raise validation_error("end must be after start")
        if end - start > MAX_DURATION:
            raise validation_error("booking must be at most 8 hours long")
        title = data["title"]
        if not isinstance(title, str) or not (1 <= len(title) <= 200):
            raise validation_error("title must be a string of 1 to 200 characters")
        attendees = data["attendees"]
        if not is_int(attendees) or attendees < 1:
            raise validation_error("attendees must be an integer of at least 1")

        fingerprint = (room_id, start, end, title, attendees)
        if idem_key is not None:
            if idem_key in self.idempotency:
                seen_fp, status, body = self.idempotency[idem_key]
                if seen_fp == fingerprint:
                    return status, body, {}
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")

        room = self.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "more attendees than the room holds")
        for other in self.bookings.values():
            if other["room_id"] == room_id and start < other["end"] and other["start"] < end:
                raise ApiError(409, "conflict", "the room is already booked for this time")

        booking_id = f"b{self.next_booking}"
        self.next_booking += 1
        booking = {"id": booking_id, "room_id": room_id, "start": start, "end": end,
                   "title": title, "attendees": attendees}
        self.bookings[booking_id] = booking
        body = self.booking_json(booking)
        if idem_key is not None:
            self.idempotency[idem_key] = (fingerprint, 201, body)
        return 201, body, {}

    def get_booking(self, booking_id):
        b = self.bookings.get(booking_id)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, self.booking_json(b), {}

    def delete_booking(self, booking_id):
        if self.bookings.pop(booking_id, None) is None:
            raise ApiError(404, "not_found", "booking not found")
        return 204, None, {}

    def list_bookings(self, query):
        items = list(self.bookings.values())

        if "room_id" in query:
            wanted = query["room_id"]
            items = [b for b in items if b["room_id"] == wanted]

        frm = parse_ts(query["from"], "from") if "from" in query else None
        to = parse_ts(query["to"], "to") if "to" in query else None
        if frm is not None and to is not None and not (frm < to):
            raise validation_error("from must be before to")
        if frm is not None:
            items = [b for b in items if b["end"] > frm]
        if to is not None:
            items = [b for b in items if b["start"] < to]

        limit = 20
        if "limit" in query:
            text = query["limit"]
            if not INT_RE.fullmatch(text):
                raise validation_error("limit must be an integer from 1 to 100")
            limit = int(text)
            if not (1 <= limit <= 100):
                raise validation_error("limit must be an integer from 1 to 100")

        cursor_key = None
        if query.get("cursor"):
            cursor_key = decode_cursor(query["cursor"])

        items.sort(key=lambda b: (b["start"], numeric_id(b["id"])))
        if cursor_key is not None:
            items = [b for b in items
                     if (b["start"], numeric_id(b["id"])) > cursor_key]

        page = items[:limit]
        has_more = len(items) > limit
        next_cursor = encode_cursor(page[-1]["start"], page[-1]["id"]) if (has_more and page) else None
        return 200, {"bookings": [self.booking_json(b) for b in page],
                     "next_cursor": next_cursor}, {}

    def availability(self, room_id, query):
        if room_id not in self.rooms:
            raise ApiError(404, "not_found", "room not found")
        text = query.get("date")
        if text is None:
            raise validation_error("date is required (YYYY-MM-DD)")
        m = DATE_RE.fullmatch(text)
        if not m:
            raise validation_error("date must be YYYY-MM-DD")
        try:
            day = date(*(int(g) for g in m.groups()))
        except ValueError:
            raise validation_error("date is not a valid calendar date")

        work_start = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
        work_end = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
        busy = sorted(
            ((max(b["start"], work_start), min(b["end"], work_end))
             for b in self.bookings.values()
             if b["room_id"] == room_id and b["start"] < work_end and b["end"] > work_start),
            key=lambda iv: iv[0])

        free = []
        cur = work_start
        for s, e in busy:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            if e > cur:
                cur = e
        if cur < work_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(work_end)})
        return 200, {"date": text, "free": free}, {}

    # ----- routing -----
    def handle(self, method, raw_path, headers, body):
        url = urlsplit(raw_path)
        parts = url.path.split("/")
        if parts[0] != "":
            raise ApiError(404, "not_found", "no such path")
        segs = [unquote(p) for p in parts[1:]]
        query = {}
        for k, v in parse_qsl(url.query, keep_blank_values=True):
            query.setdefault(k, v)

        table = None
        if segs == ["rooms"]:
            table = {"GET": lambda: self.list_rooms(),
                     "POST": lambda: self.create_room(body)}
        elif len(segs) == 2 and segs[0] == "rooms" and segs[1] != "":
            rid = segs[1]
            table = {"GET": lambda: self.get_room(rid)}
        elif len(segs) == 3 and segs[0] == "rooms" and segs[1] != "" and segs[2] == "availability":
            rid = segs[1]
            table = {"GET": lambda: self.availability(rid, query)}
        elif segs == ["bookings"]:
            key = headers.get("Idempotency-Key")
            key = key.strip() if key is not None else ""
            if key and len(key) > MAX_KEY_LEN:
                key_error = True
            else:
                key_error = False
            table = {"GET": lambda: self.list_bookings(query),
                     "POST": lambda: self._post_booking(body, key, key_error)}
        elif len(segs) == 2 and segs[0] == "bookings" and segs[1] != "":
            bid = segs[1]
            table = {"GET": lambda: self.get_booking(bid),
                     "DELETE": lambda: self.delete_booking(bid)}

        if table is None:
            raise ApiError(404, "not_found", "no such path")
        fn = table.get(method)
        if fn is None:
            allow = ", ".join(sorted(table))
            raise ApiError(405, "method_not_allowed", "method not allowed for this path",
                           {"Allow": allow})
        with self.lock:
            return fn()

    def _post_booking(self, body, key, key_error):
        # Validate the body first so that a malformed request never touches idempotency state.
        if key_error:
            parse_object(body)  # JSON/object errors take precedence
            raise validation_error("Idempotency-Key must be at most 255 characters")
        return self.create_booking(body, key or None)


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""
    timeout = 30

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

    def _read_body(self):
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        cl = cl.strip()
        if not CONTENT_LENGTH_RE.fullmatch(cl):
            raise ApiError(400, "bad_json", "invalid Content-Length")
        n = int(cl)
        if n > MAX_BODY:
            self.close_connection = True
            raise validation_error("request body is too large")
        return self.rfile.read(n) if n else b""

    def _dispatch(self, method):
        try:
            body = self._read_body()
            status, payload, extra = self.server.service.handle(method, self.path, self.headers, body)
        except ApiError as e:
            status = e.status
            payload = {"error": {"code": e.code, "message": e.message}}
            extra = e.headers
        except Exception:
            status = 500
            payload = {"error": {"code": "internal", "message": "internal server error"}}
            extra = {}
        self._send(method, status, payload, extra)

    def _send(self, method, status, payload, extra):
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        if status == 204 or payload is None:
            self.end_headers()
            return
        data = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.service = Service()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
