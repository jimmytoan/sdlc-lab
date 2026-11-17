"""bookings: in-memory meeting room booking JSON API (stdlib only).

Design decisions where the spec is silent:
- Validation order for POST /bookings: idempotency key lookup, JSON parse and
  shape, field validation (422), room existence (404), over_capacity (422),
  conflict (409).
- Idempotency: only successful (201) responses are stored. A retry with the
  same key and same fingerprint (method, path, canonical body) replays the
  stored status and body. Same key with a different fingerprint is
  idempotency_mismatch (422). Failed requests are not stored, so a corrected
  retry with the same key can succeed.
- Timestamps: only the strict form YYYY-MM-DDTHH:MM:SSZ is accepted. Offsets
  (+00:00), fractional seconds, and lower-case separators are rejected.
- Numbers: booleans and floats (including 8.0) are rejected for integer fields.
- Paging cursors: base64url of JSON [start, id] of the last item returned, so
  they stay valid across inserts and deletes. Next page = items strictly after
  that key in (start, numeric id) order.
- from/to: both validated as timestamps; if from >= to the window [from, to)
  is empty and the result is an empty list (200).
- room_id filter for an unknown room gives an empty list.
- Availability: room 404 is checked before the date is validated.
- Routing uses the path with the query string removed. A single trailing slash
  is ignored. Bodies on GET and DELETE are ignored.
- Empty or missing body on POST is bad_json.
- HEAD gets the same status and headers as the route, with no body.
- Idempotency-Key with an empty value is treated as absent.
"""
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

UTC = timezone.utc
TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
INT_RE = re.compile(r"[0-9]+")
WORK_START = dtime(8, 0)
WORK_END = dtime(18, 0)
MAX_DURATION = timedelta(hours=8)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation(message):
    return ApiError(422, "validation_error", message)


# ---------------------------------------------------------------- validators

def parse_timestamp(value, field="timestamp"):
    if not isinstance(value, str):
        raise validation(f"{field} must be a UTC timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.fullmatch(value)
    if not m:
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise validation(f"{field} is not a valid calendar timestamp")


def format_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value):
    if not isinstance(value, str):
        raise validation("date must be YYYY-MM-DD")
    m = DATE_RE.fullmatch(value)
    if not m:
        raise validation("date must be YYYY-MM-DD")
    try:
        return date(*(int(g) for g in m.groups()))
    except ValueError:
        raise validation("date is not a real calendar date")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_room(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 50):
        raise validation("name must be a string of 1 to 50 characters")
    cap = body.get("capacity")
    if not _is_int(cap) or not (1 <= cap <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    return name, cap


def validate_booking(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise validation("room_id must be a string")
    start = parse_timestamp(body.get("start"), "start")
    end = parse_timestamp(body.get("end"), "end")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise validation(f"{field} must fall on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise validation("title must be a string")
    attendees = body.get("attendees")
    if not _is_int(attendees) or attendees < 1:
        raise validation("attendees must be an integer of at least 1")
    return room_id, start, end, title, attendees


def parse_paging(query):
    raw_limit = query.get("limit")
    if raw_limit is None:
        limit = 20
    else:
        v = raw_limit[-1]
        if not INT_RE.fullmatch(v) or not (1 <= int(v) <= 100):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(v)
    cursor = query.get("cursor")
    cursor_key = None
    if cursor is not None:
        cursor_key = decode_cursor(cursor[-1])
    return limit, cursor_key


def encode_cursor(booking):
    payload = json.dumps([format_ts(booking["start"]), booking["_num"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(text):
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
        if not (isinstance(payload, list) and len(payload) == 2):
            raise ValueError
        start_s, num = payload
        if not _is_int(num) or num < 1:
            raise ValueError
        start = parse_timestamp(start_s, "cursor")
        return (start, num)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, ApiError):
        raise validation("cursor is malformed or unknown")


def canonical_fingerprint(method, path, body):
    return json.dumps([method, path, body], sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- store

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()
        self.room_order = []     # room ids in creation order
        self.bookings = {}       # id -> booking dict (with datetime start/end and _num)
        self.booking_order = []  # sorted by (start, num) kept by insertion sort
        self.room_bookings = {}  # room id -> list of booking dicts
        self.idem = {}           # key -> (fingerprint, status, body)
        self.next_room = 1
        self.next_booking = 1
        self.issued = {}         # booking number -> start (never removed, for cursor checks)

    # rooms
    def create_room(self, name, capacity):
        with self.lock:
            if name in self.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            rid = f"r{self.next_room}"
            self.next_room += 1
            room = {"id": rid, "name": name, "capacity": capacity}
            self.rooms[rid] = room
            self.room_names.add(name)
            self.room_order.append(rid)
            self.room_bookings[rid] = []
            return dict(room)

    def get_room(self, rid):
        with self.lock:
            room = self.rooms.get(rid)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            return dict(room)

    def list_rooms(self):
        with self.lock:
            return [dict(self.rooms[r]) for r in self.room_order]

    # bookings
    def _public(self, b):
        return {
            "id": b["id"],
            "room_id": b["room_id"],
            "start": format_ts(b["start"]),
            "end": format_ts(b["end"]),
            "title": b["title"],
            "attendees": b["attendees"],
        }

    def create_booking(self, body, validate, idem_key=None, fingerprint=None):
        """Create a booking under the lock. Returns (status, body).

        The idempotency lookup happens before validation so that a reused key
        with a different request is always idempotency_mismatch.
        """
        with self.lock:
            if idem_key is not None:
                prev = self.idem.get(idem_key)
                if prev is not None:
                    if prev[0] != fingerprint:
                        raise ApiError(422, "idempotency_mismatch",
                                       "Idempotency-Key was already used for a different request")
                    return prev[1], json.loads(json.dumps(prev[2]))
            room_id, start, end, title, attendees = validate(body)
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "attendees exceed room capacity")
            for other in self.room_bookings[room_id]:
                if start < other["end"] and other["start"] < end:
                    raise ApiError(409, "conflict", "booking overlaps an existing booking")
            num = self.next_booking
            self.next_booking += 1
            self.issued[num] = start
            b = {"id": f"b{num}", "_num": num, "room_id": room_id, "start": start, "end": end,
                 "title": title, "attendees": attendees}
            self.bookings[b["id"]] = b
            self.room_bookings[room_id].append(b)
            self._insert_sorted(b)
            body_out = self._public(b)
            if idem_key is not None:
                self.idem[idem_key] = (fingerprint, 201, body_out)
            return 201, json.loads(json.dumps(body_out))

    def _insert_sorted(self, b):
        key = (b["start"], b["_num"])
        lo, hi = 0, len(self.booking_order)
        while lo < hi:
            mid = (lo + hi) // 2
            o = self.booking_order[mid]
            if (o["start"], o["_num"]) < key:
                lo = mid + 1
            else:
                hi = mid
        self.booking_order.insert(lo, b)

    def get_booking(self, bid):
        with self.lock:
            b = self.bookings.get(bid)
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            return self._public(b)

    def delete_booking(self, bid):
        with self.lock:
            b = self.bookings.pop(bid, None)
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            self.room_bookings[b["room_id"]].remove(b)
            self.booking_order.remove(b)

    def list_bookings(self, room_id=None, frm=None, to=None, limit=20, cursor_key=None):
        with self.lock:
            if cursor_key is not None and self.issued.get(cursor_key[1]) != cursor_key[0]:
                raise validation("cursor is malformed or unknown")
            out = []
            for b in self.booking_order:
                if cursor_key is not None and (b["start"], b["_num"]) <= cursor_key:
                    continue
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm is not None and not (b["end"] > frm):
                    continue
                if to is not None and not (b["start"] < to):
                    continue
                out.append(b)
                if len(out) > limit:
                    break
            page = out[:limit]
            next_cursor = None
            if len(out) > limit and page:
                next_cursor = encode_cursor(page[-1])
            return [self._public(b) for b in page], next_cursor

    def availability(self, room_id, day):
        with self.lock:
            if room_id not in self.rooms:
                raise ApiError(404, "not_found", "room not found")
            win_start = datetime.combine(day, WORK_START, tzinfo=UTC)
            win_end = datetime.combine(day, WORK_END, tzinfo=UTC)
            busy = []
            for b in self.room_bookings[room_id]:
                s, e = max(b["start"], win_start), min(b["end"], win_end)
                if s < e:
                    busy.append((s, e))
            busy.sort()
            free = []
            cur = win_start
            for s, e in busy:
                if s > cur:
                    free.append((cur, s))
                cur = max(cur, e)
            if cur < win_end:
                free.append((cur, win_end))
            return [{"start": format_ts(s), "end": format_ts(e)} for s, e in free]


STORE = Store()


# ---------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):  # keep output quiet
        pass

    def send_json(self, status, obj=None, headers=None):
        data = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        # HEAD responses must not carry a message body (RFC 9110 9.3.2).
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def send_error_json(self, status, code, message):
        self.send_json(status, {"error": {"code": code, "message": message or code}})

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _parse_json_object(self, raw):
        if not raw.strip():
            raise ApiError(400, "bad_json", "request body must be JSON")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")

    def _dispatch(self, method):
        try:
            raw = self._read_body()
            split = urlsplit(self.path)
            path = split.path
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            query = parse_qs(split.query, keep_blank_values=True)
            parts = [p for p in path.split("/") if p != ""]
            route = self._route(method, parts)
            if route is None:
                raise ApiError(404, "not_found", "no such endpoint")
            if route == "method":
                raise ApiError(405, "method_not_allowed", "method not allowed for this endpoint")
            status, body, headers = route(raw, query, parts, path)
            self.send_json(status, body, headers)
        except ApiError as e:
            self.send_error_json(e.status, e.code, e.message)
        except Exception:  # last resort; keep the server alive
            try:
                self.send_error_json(500, "internal_error", "unexpected server error")
            except Exception:
                pass

    def _route(self, method, parts):
        """Returns a handler callable, 'method' for a known path with wrong method, or None."""
        allowed = None
        n = len(parts)
        if n == 1 and parts[0] == "rooms":
            allowed = {"POST": self.h_create_room, "GET": self.h_list_rooms}
        elif n == 2 and parts[0] == "rooms":
            allowed = {"GET": self.h_get_room}
        elif n == 3 and parts[0] == "rooms" and parts[2] == "availability":
            allowed = {"GET": self.h_availability}
        elif n == 1 and parts[0] == "bookings":
            allowed = {"POST": self.h_create_booking, "GET": self.h_list_bookings}
        elif n == 2 and parts[0] == "bookings":
            allowed = {"GET": self.h_get_booking, "DELETE": self.h_delete_booking}
        if allowed is None:
            return None
        return allowed.get(method, "method")

    # handlers: (raw, query, parts, path) -> (status, body, headers)
    def h_create_room(self, raw, query, parts, path):
        body = self._parse_json_object(raw)
        name, cap = validate_room(body)
        room = STORE.create_room(name, cap)
        return 201, room, None

    def h_list_rooms(self, raw, query, parts, path):
        return 200, {"rooms": STORE.list_rooms()}, None

    def h_get_room(self, raw, query, parts, path):
        return 200, STORE.get_room(parts[1]), None

    def h_availability(self, raw, query, parts, path):
        room_id = parts[1]
        STORE.get_room(room_id)  # 404 first
        dates = query.get("date")
        if not dates:
            raise validation("date query parameter is required")
        day = parse_date(dates[-1])
        free = STORE.availability(room_id, day)
        return 200, {"date": day.isoformat(), "free": free}, None

    def h_create_booking(self, raw, query, parts, path):
        idem_key = self.headers.get("Idempotency-Key")
        if idem_key is not None and idem_key.strip() == "":
            idem_key = None
        body = self._parse_json_object(raw)
        fingerprint = canonical_fingerprint("POST", path, body) if idem_key is not None else None
        status, out = STORE.create_booking(body, validate_booking, idem_key=idem_key,
                                           fingerprint=fingerprint)
        return status, out, None

    def h_list_bookings(self, raw, query, parts, path):
        room_id = query["room_id"][-1] if "room_id" in query else None
        frm = parse_timestamp(query["from"][-1], "from") if "from" in query else None
        to = parse_timestamp(query["to"][-1], "to") if "to" in query else None
        limit, cursor_key = parse_paging(query)
        if frm is not None and to is not None and not (frm < to):
            # [from, to) is empty: no booking can overlap it.
            return 200, {"bookings": [], "next_cursor": None}, None
        items, next_cursor = STORE.list_bookings(room_id, frm, to, limit, cursor_key)
        return 200, {"bookings": items, "next_cursor": next_cursor}, None

    def h_get_booking(self, raw, query, parts, path):
        return 200, STORE.get_booking(parts[1]), None

    def h_delete_booking(self, raw, query, parts, path):
        STORE.delete_booking(parts[1])
        return 204, None, None


for _m in ("do_GET", "do_POST", "do_DELETE", "do_PUT", "do_PATCH", "do_HEAD", "do_OPTIONS"):
    setattr(Handler, _m, lambda self, _m=_m: self._dispatch(_m[3:]))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    parser = argparse.ArgumentParser(description="bookings meeting room API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
