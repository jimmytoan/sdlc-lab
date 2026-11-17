"""bookings: meeting room booking JSON API (standard library only, in-memory state)."""
import argparse
import base64
import binascii
import json
import re
import sys
import threading
from datetime import datetime, date, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

WORK_START = dtime(8, 0)
WORK_END = dtime(18, 0)
MAX_DURATION = timedelta(hours=8)
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.0+)?(Z|\+00:00)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
        self.message = message or code.replace("_", " ")
        self.status = self.STATUS[code]


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field="timestamp"):
    if not isinstance(value, str):
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), int(m[6]), tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid date/time")


def parse_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ApiError("validation_error", "date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError("validation_error", "date is not a valid calendar date")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_names = {}     # name -> id
        self.room_seq = 0
        self.bookings = {}       # id -> dict (with datetime fields under _start/_end)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body)

    # ---- rooms ----
    def create_room(self, body):
        if not isinstance(body, dict):
            raise ApiError("validation_error", "body must be a JSON object")
        name = body.get("name")
        capacity = body.get("capacity")
        if not isinstance(name, str) or not name.strip():
            raise ApiError("validation_error", "name must be a non-empty string")
        if len(name) > 50:
            raise ApiError("validation_error", "name must be at most 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            self.room_seq += 1
            rid = f"r{self.room_seq}"
            room = {"id": rid, "name": name, "capacity": capacity}
            self.rooms[rid] = room
            self.room_names[name] = rid
            return dict(room)

    def list_rooms(self):
        with self.lock:
            return [dict(r) for r in self.rooms.values()]

    def get_room(self, rid):
        with self.lock:
            if rid not in self.rooms:
                raise ApiError("not_found", "room not found")
            return dict(self.rooms[rid])

    # ---- bookings ----
    @staticmethod
    def public(b):
        return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}

    def create_booking(self, body, idem_key):
        if not isinstance(body, dict):
            raise ApiError("validation_error", "body must be a JSON object")
        room_id = body.get("room_id")
        title = body.get("title")
        attendees = body.get("attendees")
        if not isinstance(room_id, str) or not room_id:
            raise ApiError("validation_error", "room_id must be a string")
        start = parse_ts(body.get("start"), "start")
        end = parse_ts(body.get("end"), "end")
        if not isinstance(title, str) or not title.strip():
            raise ApiError("validation_error", "title must be a non-empty string")
        if not is_int(attendees) or attendees < 1:
            raise ApiError("validation_error", "attendees must be a positive integer")
        for dt, field in ((start, "start"), (end, "end")):
            if dt.minute % 15 != 0:
                raise ApiError("validation_error", f"{field} must be on a quarter hour")
        if end <= start:
            raise ApiError("validation_error", "end must be after start")
        if end - start > MAX_DURATION:
            raise ApiError("validation_error", "booking may be at most 8 hours long")

        fingerprint = json.dumps(
            {"room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
             "title": title, "attendees": attendees}, sort_keys=True)

        with self.lock:
            if idem_key is not None and idem_key in self.idem:
                fp, status, resp = self.idem[idem_key]
                if fp != fingerprint:
                    raise ApiError("idempotency_mismatch", "Idempotency-Key was already used for a different request")
                return status, dict(resp)

            if room_id not in self.rooms:
                raise ApiError("not_found", "room not found")
            room = self.rooms[room_id]
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            for other in self.bookings.values():
                if other["room_id"] == room_id and start < other["_end"] and other["_start"] < end:
                    raise ApiError("conflict", "room is already booked for this time")

            self.booking_seq += 1
            bid = f"b{self.booking_seq}"
            b = {"id": bid, "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
                 "title": title, "attendees": attendees, "_start": start, "_end": end}
            self.bookings[bid] = b
            resp = self.public(b)
            if idem_key is not None:
                self.idem[idem_key] = (fingerprint, 201, resp)
            return 201, dict(resp)

    def get_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError("not_found", "booking not found")
            return self.public(self.bookings[bid])

    def delete_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError("not_found", "booking not found")
            del self.bookings[bid]

    @staticmethod
    def _seq(bid):
        m = re.match(r"^b(\d+)$", bid)
        return int(m[1]) if m else 0

    def list_bookings(self, query):
        room_id = query.get("room_id")
        frm = parse_ts(query["from"], "from") if "from" in query else None
        to = parse_ts(query["to"], "to") if "to" in query else None
        if frm and to and frm >= to:
            raise ApiError("validation_error", "from must be before to")
        limit = 20
        if "limit" in query:
            try:
                limit = int(query["limit"])
            except ValueError:
                raise ApiError("validation_error", "limit must be an integer")
            if not (1 <= limit <= 100):
                raise ApiError("validation_error", "limit must be between 1 and 100")
        cursor = None
        if "cursor" in query:
            cursor = self._decode_cursor(query["cursor"])

        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm is not None and not (b["_end"] > frm):
                    continue
                if to is not None and not (b["_start"] < to):
                    continue
                items.append(b)
            items.sort(key=lambda b: (b["_start"], self._seq(b["id"])))
            if cursor is not None:
                items = [b for b in items if (b["_start"], self._seq(b["id"])) > cursor]
            page = items[:limit]
            next_cursor = None
            if len(items) > limit and page:
                last = page[-1]
                next_cursor = self._encode_cursor(last["_start"], self._seq(last["id"]))
            return {"bookings": [self.public(b) for b in page], "next_cursor": next_cursor}

    @staticmethod
    def _encode_cursor(start, seq):
        raw = f"{fmt_ts(start)}|{seq}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(token):
        try:
            pad = "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode((token + pad).encode()).decode()
            ts_part, seq_part = raw.split("|")
            return (parse_ts(ts_part), int(seq_part))
        except (ApiError, ValueError, binascii.Error, UnicodeDecodeError):
            raise ApiError("validation_error", "cursor is invalid")

    # ---- availability ----
    def availability(self, rid, date_str):
        day = parse_date(date_str)
        with self.lock:
            if rid not in self.rooms:
                raise ApiError("not_found", "room not found")
            day_start = datetime.combine(day, WORK_START, tzinfo=timezone.utc)
            day_end = datetime.combine(day, WORK_END, tzinfo=timezone.utc)
            busy = sorted(
                (max(b["_start"], day_start), min(b["_end"], day_end))
                for b in self.bookings.values()
                if b["room_id"] == rid and b["_start"] < day_end and b["_end"] > day_start
            )
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            cur = max(cur, e)
        if cur < day_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
        return {"date": day.isoformat(), "free": free}


STORE = Store()


def route(method, path, query, body_fn, headers):
    """Returns (status, body or None)."""
    parts = [p for p in path.split("/") if p != ""]
    if not parts:
        raise ApiError("not_found", "no such endpoint")

    def allow(*methods):
        if method not in methods:
            raise ApiError("method_not_allowed", f"method {method} not allowed here")

    if parts[0] == "rooms":
        if len(parts) == 1:
            allow("GET", "POST")
            if method == "POST":
                return 201, STORE.create_room(body_fn())
            return 200, {"rooms": STORE.list_rooms()}
        if len(parts) == 2:
            allow("GET")
            return 200, STORE.get_room(parts[1])
        if len(parts) == 3 and parts[2] == "availability":
            allow("GET")
            STORE.get_room(parts[1])  # 404 if unknown (checked before date validation below)
            if "date" not in query:
                raise ApiError("validation_error", "date query parameter is required")
            return 200, STORE.availability(parts[1], query["date"])
    elif parts[0] == "bookings":
        if len(parts) == 1:
            allow("GET", "POST")
            if method == "POST":
                key = headers.get("Idempotency-Key")
                status, resp = STORE.create_booking(body_fn(), key if key else None)
                return status, resp
            return 200, STORE.list_bookings(query)
        if len(parts) == 2:
            allow("GET", "DELETE")
            if method == "DELETE":
                STORE.delete_booking(parts[1])
                return 204, None
            return 200, STORE.get_booking(parts[1])
    raise ApiError("not_found", "no such endpoint")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        def body_fn():
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ApiError("bad_json", "request body is not valid JSON")

        try:
            parts = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            status, body = route(self.command, parts.path, query, body_fn, self.headers)
            self._send(status, body)
        except ApiError as e:
            self._send(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception as e:  # pragma: no cover
            self._send(500, {"error": {"code": "internal_error", "message": str(e)}})

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
