"""bookings: meeting room booking JSON API (standard library only)."""
import argparse
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

UTC = timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)
MIN_FREE = timedelta(minutes=15)

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|\+00:00)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DIGITS_RE = re.compile(r"^[0-9]+$")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def err(status, code, message):
    return ApiError(status, code, message)


def fmt_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str):
        raise err(422, "validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise err(422, "validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    parts = [int(x) for x in m.groups()[:6]]
    try:
        return datetime(*parts, tzinfo=UTC)
    except ValueError:
        raise err(422, "validation_error", f"{field} is not a valid timestamp")


def parse_body(raw):
    if not raw or not raw.strip():
        raise err(400, "bad_json", "request body must be a JSON object")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise err(400, "bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise err(422, "validation_error", "request body must be a JSON object")
    return data


def parse_object_body_strict(raw):
    """For PATCH: anything that is not a JSON object is bad_json (400)."""
    if not raw or not raw.strip():
        raise err(400, "bad_json", "request body must be a JSON object")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise err(400, "bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise err(400, "bad_json", "request body must be a JSON object")
    return data


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_booking_fields(start, end, title, attendees):
    """Shared validation for POST and PATCH; start/end are parsed datetimes."""
    if not isinstance(title, str) or not (1 <= len(title) <= 200):
        raise err(422, "validation_error", "title must be a string of 1 to 200 characters")
    if not is_int(attendees) or attendees < 1:
        raise err(422, "validation_error", "attendees must be a positive integer")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise err(422, "validation_error", f"{field} must be on a quarter hour")
    if end <= start:
        raise err(422, "validation_error", "end must be after start")
    if end - start > MAX_BOOKING:
        raise err(422, "validation_error", "booking may be at most 8 hours long")


class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> room dict
        self.room_order = []     # ids in creation order
        self.room_names = set()
        self.bookings = {}       # id -> booking record
        self.idem = {}           # key -> (fingerprint, response body)
        self.room_seq = 0
        self.booking_seq = 0

    def handle(self, method, path, headers, raw):
        with self.lock:
            return self._route(method, path, headers, raw)

    # ----- routing -------------------------------------------------------
    def _route(self, method, path, headers, raw):
        url = urlsplit(path)
        segs = [unquote(s) for s in url.path.split("/") if s]
        query = parse_qs(url.query, keep_blank_values=True)

        def q(name):
            vals = query.get(name)
            return vals[0] if vals else None

        if not segs:
            raise err(404, "not_found", "no such path")

        head = segs[0]
        if head == "rooms":
            if len(segs) == 1:
                self._allow(method, ("GET", "POST"))
                if method == "GET":
                    return 200, self.list_rooms()
                return 201, self.create_room(raw)
            if len(segs) == 2:
                self._allow(method, ("GET",))
                return 200, self.get_room(segs[1])
            if len(segs) == 3 and segs[2] == "availability":
                self._allow(method, ("GET",))
                return 200, self.availability(segs[1], q("date"))
        elif head == "bookings":
            if len(segs) == 1:
                self._allow(method, ("GET", "POST"))
                if method == "GET":
                    return 200, self.list_bookings(q)
                return 201, self.create_booking(raw, headers, method, path)
            if len(segs) == 2:
                self._allow(method, ("GET", "PATCH", "DELETE"))
                if method == "GET":
                    return 200, self.get_booking(segs[1])
                if method == "PATCH":
                    return 200, self.patch_booking(segs[1], raw)
                self.delete_booking(segs[1])
                return 204, None
        raise err(404, "not_found", "no such path")

    @staticmethod
    def _allow(method, allowed):
        if method not in allowed:
            raise err(405, "method_not_allowed", f"method {method} not allowed here")

    # ----- rooms ---------------------------------------------------------
    def room_json(self, room):
        return {"id": room["id"], "name": room["name"], "capacity": room["capacity"],
                "buffer_minutes": room["buffer_minutes"]}

    def create_room(self, raw):
        data = parse_body(raw)
        name = data.get("name")
        capacity = data.get("capacity")
        if not isinstance(name, str) or not name.strip() or len(name) > 50:
            raise err(422, "validation_error", "name must be a non-empty string of at most 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise err(422, "validation_error", "capacity must be an integer from 1 to 500")
        buffer_minutes = 0
        if "buffer_minutes" in data:
            buffer_minutes = data["buffer_minutes"]
            if not is_int(buffer_minutes) or not (0 <= buffer_minutes <= 60):
                raise err(422, "validation_error", "buffer_minutes must be an integer from 0 to 60")
        if name in self.room_names:
            raise err(409, "room_exists", "a room with this name already exists")
        self.room_seq += 1
        rid = f"r{self.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity, "buffer_minutes": buffer_minutes}
        self.rooms[rid] = room
        self.room_order.append(rid)
        self.room_names.add(name)
        return self.room_json(room)

    def list_rooms(self):
        return {"rooms": [self.room_json(self.rooms[r]) for r in self.room_order]}

    def get_room(self, rid):
        room = self.rooms.get(rid)
        if room is None:
            raise err(404, "not_found", "room not found")
        return self.room_json(room)

    # ----- bookings ------------------------------------------------------
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

    def _sorted_bookings(self):
        return sorted(self.bookings.values(), key=lambda b: (b["start"], int(b["id"][1:])))

    def _check_conflict(self, room_id, start, end, buffer_minutes, exclude=None):
        pad = timedelta(minutes=buffer_minutes)
        for other in self.bookings.values():
            if other["id"] == exclude or other["room_id"] != room_id:
                continue
            if other["start"] < end + pad and start < other["end"] + pad:
                raise err(409, "conflict", "the room is already booked for this time")

    def create_booking(self, raw, headers, method, path):
        key = headers.get("Idempotency-Key") if headers is not None else None
        if key is not None and key.strip() == "":
            key = None

        data = parse_body(raw)
        fingerprint = method + " " + path + " " + json.dumps(data, sort_keys=True, separators=(",", ":"))
        if key is not None and key in self.idem:
            stored_fp, stored_body = self.idem[key]
            if stored_fp != fingerprint:
                raise err(422, "idempotency_mismatch", "Idempotency-Key was already used for a different request")
            return dict(stored_body)

        room_id = data.get("room_id")
        title = data.get("title")
        attendees = data.get("attendees")
        if not isinstance(room_id, str) or not room_id:
            raise err(422, "validation_error", "room_id must be a non-empty string")
        start = parse_ts(data.get("start"), "start")
        end = parse_ts(data.get("end"), "end")
        check_booking_fields(start, end, title, attendees)

        room = self.rooms.get(room_id)
        if room is None:
            raise err(422, "validation_error", "room_id does not refer to an existing room")
        if attendees > room["capacity"]:
            raise err(422, "over_capacity", "more attendees than the room holds")
        self._check_conflict(room_id, start, end, room["buffer_minutes"])

        self.booking_seq += 1
        bid = f"b{self.booking_seq}"
        rec = {"id": bid, "room_id": room_id, "start": start, "end": end,
               "title": title, "attendees": attendees}
        self.bookings[bid] = rec
        body = self.booking_json(rec)
        if key is not None:
            self.idem[key] = (fingerprint, dict(body))
        return body

    def get_booking(self, bid):
        b = self.bookings.get(bid)
        if b is None:
            raise err(404, "not_found", "booking not found")
        return self.booking_json(b)

    def patch_booking(self, bid, raw):
        data = parse_object_body_strict(raw)
        b = self.bookings.get(bid)
        if b is None:
            raise err(404, "not_found", "booking not found")

        start = parse_ts(data["start"], "start") if "start" in data else b["start"]
        end = parse_ts(data["end"], "end") if "end" in data else b["end"]
        title = data["title"] if "title" in data else b["title"]
        attendees = data["attendees"] if "attendees" in data else b["attendees"]
        check_booking_fields(start, end, title, attendees)

        room = self.rooms[b["room_id"]]
        if attendees > room["capacity"]:
            raise err(422, "over_capacity", "more attendees than the room holds")
        self._check_conflict(b["room_id"], start, end, room["buffer_minutes"], exclude=bid)

        b["start"] = start
        b["end"] = end
        b["title"] = title
        b["attendees"] = attendees
        return self.booking_json(b)

    def delete_booking(self, bid):
        if bid not in self.bookings:
            raise err(404, "not_found", "booking not found")
        del self.bookings[bid]

    def list_bookings(self, q):
        room_id = q("room_id")
        from_raw = q("from")
        to_raw = q("to")
        frm = parse_ts(from_raw, "from") if from_raw is not None else None
        to = parse_ts(to_raw, "to") if to_raw is not None else None

        limit_raw = q("limit")
        if limit_raw is None:
            limit = 20
        else:
            if not DIGITS_RE.match(limit_raw) or not (1 <= int(limit_raw) <= 100):
                raise err(422, "validation_error", "limit must be an integer from 1 to 100")
            limit = int(limit_raw)

        cursor_raw = q("cursor")
        if cursor_raw is None or cursor_raw == "":
            offset = 0
        else:
            if not DIGITS_RE.match(cursor_raw):
                raise err(422, "validation_error", "cursor is invalid")
            offset = int(cursor_raw)

        items = []
        for b in self._sorted_bookings():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not (b["end"] > frm):
                continue
            if to is not None and not (b["start"] < to):
                continue
            items.append(b)

        page = items[offset:offset + limit]
        next_offset = offset + limit
        next_cursor = str(next_offset) if next_offset < len(items) else None
        return {"bookings": [self.booking_json(b) for b in page],
                "next_cursor": next_cursor,
                "total": len(items)}

    # ----- availability --------------------------------------------------
    def availability(self, rid, date_str):
        room = self.rooms.get(rid)
        if room is None:
            raise err(404, "not_found", "room not found")
        if date_str is None or not DATE_RE.match(date_str):
            raise err(422, "validation_error", "date must be YYYY-MM-DD")
        try:
            y, mo, d = (int(x) for x in date_str.split("-"))
            day = datetime(y, mo, d, tzinfo=UTC)
        except ValueError:
            raise err(422, "validation_error", "date is not a valid calendar date")

        day_start = day.replace(hour=WORK_START_HOUR)
        day_end = day.replace(hour=WORK_END_HOUR)
        pad = timedelta(minutes=room["buffer_minutes"])

        busy = []
        for b in self.bookings.values():
            if b["room_id"] != rid:
                continue
            bs = b["start"] - pad
            be = b["end"] + pad
            if bs < day_end and be > day_start:
                busy.append((max(bs, day_start), min(be, day_end)))
        busy.sort()

        free = []

        def add_free(s, e):
            if e - s >= MIN_FREE:
                free.append({"start": fmt_ts(s), "end": fmt_ts(e)})

        cur = day_start
        for s, e in busy:
            if s > cur:
                add_free(cur, s)
            if e > cur:
                cur = e
        if cur < day_end:
            add_free(cur, day_end)
        return {"date": date_str, "free": free}


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, format, *args):
        pass

    def _read_raw(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _dispatch(self):
        raw = self._read_raw()
        try:
            status, body = self.server.app.handle(self.command, self.path, self.headers, raw)
        except ApiError as e:
            status = e.status
            body = {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status = 500
            body = {"error": {"code": "internal_error", "message": "internal server error"}}
        self._send(status, body)

    def _send(self, status, body):
        self.send_response(status)
        if status == 204 or body is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = Server(("127.0.0.1", args.port), Handler)
    httpd.app = App()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
