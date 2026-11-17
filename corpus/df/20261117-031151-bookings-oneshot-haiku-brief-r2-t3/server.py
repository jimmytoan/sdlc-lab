"""bookings: meeting room booking JSON API (standard library only).

Run: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import json
import re
import sys
import threading
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DIGITS_RE = re.compile(r"^\d+$")

WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)
MIN_FREE = timedelta(minutes=15)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation_error(message):
    return ApiError(422, "validation_error", message)


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        raise validation_error(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise validation_error(f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.strftime(TS_FORMAT)


def parse_json_bytes(raw):
    if raw is None or not raw.strip():
        raise ApiError(400, "bad_json", "request body must be a JSON object")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")


def parse_body(raw):
    body = parse_json_bytes(raw)
    if not isinstance(body, dict):
        raise validation_error("request body must be a JSON object")
    return body


def parse_object_body_bad_json(raw):
    body = parse_json_bytes(raw)
    if not isinstance(body, dict):
        raise ApiError(400, "bad_json", "request body must be a JSON object")
    return body


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_booking_fields(title, attendees, start_raw, end_raw):
    """Shared validation for POST and PATCH. Returns (start_dt, end_dt)."""
    if not isinstance(title, str) or not title.strip():
        raise validation_error("title must be a non-empty string")
    if not is_int(attendees) or attendees < 1:
        raise validation_error("attendees must be a positive integer")
    start = parse_ts(start_raw, "start")
    end = parse_ts(end_raw, "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise validation_error(f"{field} must be on a quarter hour")
    if end <= start:
        raise validation_error("end must be after start")
    if end - start > MAX_BOOKING:
        raise validation_error("booking may be at most 8 hours long")
    return start, end


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> record
        self.room_names = set()
        self.bookings = {}       # id -> record (insertion order == creation order)
        self.next_room = 1
        self.next_booking = 1
        self.idem = {}           # key -> (fingerprint, response payload)

    # ----- rooms -----
    def create_room(self, body):
        name = body.get("name")
        capacity = body.get("capacity")
        if not isinstance(name, str) or not name.strip() or len(name) > 50:
            raise validation_error("name must be a non-empty string of at most 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise validation_error("capacity must be an integer from 1 to 500")
        buffer_minutes = 0
        if "buffer_minutes" in body:
            buffer_minutes = body["buffer_minutes"]
            if not is_int(buffer_minutes) or not (0 <= buffer_minutes <= 60):
                raise validation_error("buffer_minutes must be an integer from 0 to 60")
        with self.lock:
            if name in self.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            rid = f"r{self.next_room}"
            self.next_room += 1
            rec = {"id": rid, "name": name, "capacity": capacity, "buffer_minutes": buffer_minutes}
            self.rooms[rid] = rec
            self.room_names.add(name)
            return dict(rec)

    def list_rooms(self):
        with self.lock:
            return [dict(r) for r in self.rooms.values()]

    def get_room(self, rid):
        with self.lock:
            rec = self.rooms.get(rid)
            if rec is None:
                raise ApiError(404, "not_found", "room not found")
            return rec

    # ----- bookings -----
    @staticmethod
    def public_booking(rec):
        return {
            "id": rec["id"],
            "room_id": rec["room_id"],
            "start": fmt_ts(rec["start_dt"]),
            "end": fmt_ts(rec["end_dt"]),
            "title": rec["title"],
            "attendees": rec["attendees"],
        }

    def _conflicts(self, room_id, buffer_minutes, start, end, exclude_id=None):
        buf = timedelta(minutes=buffer_minutes)
        for other in self.bookings.values():
            if other["room_id"] != room_id or other["id"] == exclude_id:
                continue
            if other["start_dt"] < end + buf and start < other["end_dt"] + buf:
                return True
        return False

    def create_booking(self, body):
        room_id = body.get("room_id")
        if not isinstance(room_id, str) or not room_id:
            raise validation_error("room_id must be a non-empty string")
        title = body.get("title")
        attendees = body.get("attendees")
        start, end = validate_booking_fields(title, attendees, body.get("start"), body.get("end"))
        with self.lock:
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "more attendees than the room holds")
            if self._conflicts(room_id, room["buffer_minutes"], start, end):
                raise ApiError(409, "conflict", "room is already booked for this time")
            bid = f"b{self.next_booking}"
            self.next_booking += 1
            rec = {
                "id": bid,
                "room_id": room_id,
                "start_dt": start,
                "end_dt": end,
                "title": title,
                "attendees": attendees,
            }
            self.bookings[bid] = rec
            return self.public_booking(rec)

    def update_booking(self, bid, body):
        with self.lock:
            rec = self.bookings.get(bid)
            if rec is None:
                raise ApiError(404, "not_found", "booking not found")
            start_raw = body["start"] if "start" in body else fmt_ts(rec["start_dt"])
            end_raw = body["end"] if "end" in body else fmt_ts(rec["end_dt"])
            title = body["title"] if "title" in body else rec["title"]
            attendees = body["attendees"] if "attendees" in body else rec["attendees"]
            start, end = validate_booking_fields(title, attendees, start_raw, end_raw)
            room = self.rooms[rec["room_id"]]
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "more attendees than the room holds")
            if self._conflicts(rec["room_id"], room["buffer_minutes"], start, end, exclude_id=bid):
                raise ApiError(409, "conflict", "room is already booked for this time")
            rec["start_dt"] = start
            rec["end_dt"] = end
            rec["title"] = title
            rec["attendees"] = attendees
            return self.public_booking(rec)

    def get_booking(self, bid):
        with self.lock:
            rec = self.bookings.get(bid)
            if rec is None:
                raise ApiError(404, "not_found", "booking not found")
            return self.public_booking(rec)

    def delete_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError(404, "not_found", "booking not found")
            del self.bookings[bid]

    def list_bookings(self, query):
        def first(name):
            vals = query.get(name)
            return vals[0] if vals else None

        room_id = first("room_id")
        from_raw = first("from")
        to_raw = first("to")
        from_dt = parse_ts(from_raw, "from") if from_raw is not None else None
        to_dt = parse_ts(to_raw, "to") if to_raw is not None else None
        if from_dt is not None and to_dt is not None and from_dt >= to_dt:
            raise validation_error("from must be before to")

        limit_raw = first("limit")
        if limit_raw is None:
            limit = 20
        else:
            if not DIGITS_RE.match(limit_raw) or not (1 <= int(limit_raw) <= 100):
                raise validation_error("limit must be an integer from 1 to 100")
            limit = int(limit_raw)

        cursor_raw = first("cursor")
        if cursor_raw is None:
            offset = 0
        else:
            if not DIGITS_RE.match(cursor_raw):
                raise validation_error("cursor is invalid")
            offset = int(cursor_raw)

        with self.lock:
            items = []
            for rec in self.bookings.values():
                if room_id is not None and rec["room_id"] != room_id:
                    continue
                if from_dt is not None and not (rec["end_dt"] > from_dt):
                    continue
                if to_dt is not None and not (rec["start_dt"] < to_dt):
                    continue
                items.append(rec)
            items.sort(key=lambda r: (r["start_dt"], int(r["id"][1:])))
            total = len(items)
            page = items[offset:offset + limit]
            next_cursor = str(offset + limit) if offset + limit < total else None
            return {
                "bookings": [self.public_booking(r) for r in page],
                "next_cursor": next_cursor,
                "total": total,
            }

    # ----- availability -----
    def availability(self, room_id, date_raw):
        if date_raw is None or not DATE_RE.match(date_raw):
            raise validation_error("date must be YYYY-MM-DD")
        try:
            d = datetime.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            raise validation_error("date is not a valid calendar date")
        day_start = datetime(d.year, d.month, d.day, WORK_START_HOUR, tzinfo=timezone.utc)
        day_end = datetime(d.year, d.month, d.day, WORK_END_HOUR, tzinfo=timezone.utc)
        with self.lock:
            room = self.get_room(room_id)
            buf = timedelta(minutes=room["buffer_minutes"])
            busy = []
            for rec in self.bookings.values():
                if rec["room_id"] != room_id:
                    continue
                bs = rec["start_dt"] - buf
                be = rec["end_dt"] + buf
                if bs < day_end and be > day_start:
                    busy.append((max(bs, day_start), min(be, day_end)))
        busy.sort()
        free = []
        cur = day_start

        def add_free(s, e):
            if e - s >= MIN_FREE:
                free.append({"start": fmt_ts(s), "end": fmt_ts(e)})

        for s, e in busy:
            if s > cur:
                add_free(cur, s)
            if e > cur:
                cur = e
        if cur < day_end:
            add_free(cur, day_end)
        return {"date": date_raw, "free": free}


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, format, *args):
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

    def _read_raw(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _dispatch(self, method):
        try:
            raw = self._read_raw()
            status, payload = self._route(method, raw)
        except ApiError as e:
            status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, payload = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        self._send(status, payload)

    def _send(self, status, payload):
        self.send_response(status)
        if payload is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _route(self, method, raw):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        segs = [s for s in parts.path.split("/") if s]

        def not_allowed():
            return ApiError(405, "method_not_allowed", f"method {method} not allowed here")

        if not segs:
            raise ApiError(404, "not_found", "no such endpoint")

        head = segs[0]
        if head == "rooms":
            if len(segs) == 1:
                if method == "GET":
                    return 200, {"rooms": STORE.list_rooms()}
                if method == "POST":
                    body = parse_body(raw)
                    return 201, STORE.create_room(body)
                raise not_allowed()
            if len(segs) == 2:
                if method != "GET":
                    raise not_allowed()
                rec = STORE.get_room(segs[1])
                return 200, dict(rec)
            if len(segs) == 3 and segs[2] == "availability":
                if method != "GET":
                    raise not_allowed()
                date_raw = query.get("date", [None])[0]
                return 200, STORE.availability(segs[1], date_raw)
            raise ApiError(404, "not_found", "no such endpoint")

        if head == "bookings":
            if len(segs) == 1:
                if method == "GET":
                    return 200, STORE.list_bookings(query)
                if method == "POST":
                    return self._create_booking(raw)
                raise not_allowed()
            if len(segs) == 2:
                if method == "GET":
                    return 200, STORE.get_booking(segs[1])
                if method == "DELETE":
                    STORE.delete_booking(segs[1])
                    return 204, None
                if method == "PATCH":
                    body = parse_object_body_bad_json(raw)
                    return 200, STORE.update_booking(segs[1], body)
                raise not_allowed()
            raise ApiError(404, "not_found", "no such endpoint")

        raise ApiError(404, "not_found", "no such endpoint")

    def _create_booking(self, raw):
        body = parse_body(raw)
        key = self.headers.get("Idempotency-Key")
        fingerprint = "POST /bookings " + json.dumps(body, sort_keys=True)
        with STORE.lock:
            if key:
                prev = STORE.idem.get(key)
                if prev is not None:
                    if prev[0] != fingerprint:
                        raise ApiError(422, "idempotency_mismatch",
                                       "Idempotency-Key was already used for a different request")
                    return 201, prev[1]
            result = STORE.create_booking(body)
            if key:
                STORE.idem[key] = (fingerprint, result)
            return 201, result


def main(argv=None):
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
