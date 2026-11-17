#!/usr/bin/env python3
"""bookings: in-memory meeting room booking JSON API (standard library only)."""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
DIGITS_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


def validation_error(msg="invalid request"):
    return ApiError(422, "validation_error", msg)


def not_found(msg="resource not found"):
    return ApiError(404, "not_found", msg)


def bad_json(msg="request body must be a JSON object"):
    return ApiError(400, "bad_json", msg)


def is_int(v):
    return type(v) is int


def parse_ts(s):
    """Parse the exact form YYYY-MM-DDTHH:MM:SSZ into an aware UTC datetime."""
    if not isinstance(s, str) or not TS_RE.fullmatch(s):
        raise ValueError("bad timestamp")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_ts(dt):
    # strftime("%Y") does not zero-pad years below 1000 on this platform.
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


_UTC_MIN = datetime.min.replace(tzinfo=timezone.utc)
_UTC_MAX = datetime.max.replace(tzinfo=timezone.utc)


def shift(dt, delta):
    """dt + delta, saturating at the datetime range limits instead of overflowing.
    A saturated bound still compares correctly against every valid booking time."""
    try:
        return dt + delta
    except OverflowError:
        return _UTC_MAX if delta > timedelta(0) else _UTC_MIN


def parse_slot_ts(s):
    """Booking boundary: valid timestamp, minutes in 00/15/30/45, seconds 00."""
    dt = parse_ts(s)
    if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
        raise ValueError("not on a quarter hour")
    return dt


def json_equal(a, b):
    """Equality of parsed JSON values: key order and whitespace are irrelevant,
    numbers compare by value (1 == 1.0), but a boolean never equals a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if type(a) is not type(b):
        return False
    return a == b


def parse_body(raw):
    if not raw:
        raise bad_json("request body is required")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise bad_json("request body is not valid JSON")
    if not isinstance(obj, dict):
        raise bad_json("request body must be a JSON object")
    return obj


def enc_cursor(start_dt, seq):
    raw = json.dumps({"s": fmt_ts(start_dt), "q": seq}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def dec_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode((c + pad).encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        raise validation_error("malformed cursor")
    if not isinstance(obj, dict) or set(obj) != {"s", "q"} or not isinstance(obj["s"], str) \
            or not is_int(obj["q"]):
        raise validation_error("malformed cursor")
    try:
        sdt = parse_ts(obj["s"])
    except ValueError:
        raise validation_error("malformed cursor")
    return sdt, obj["q"]


def one_param(qs, name):
    vals = qs.get(name)
    if vals is None:
        return None
    if len(vals) != 1:
        raise validation_error(f"query parameter {name} may be given only once")
    return vals[0]


class Store:
    def __init__(self):
        self.rooms = {}          # id -> room dict
        self.room_list = []      # rooms in creation order
        self.room_names = {}     # casefolded name -> id
        self.room_seq = 0
        self.bookings = {}       # id -> booking (internal fields included)
        self.booking_seq = 0
        self.idem = {}           # key -> (parsed request body, response dict)


STORE = Store()
LOCK = threading.Lock()


def public_booking(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start_dt"]),
            "end": fmt_ts(b["end_dt"]), "title": b["title"], "attendees": b["attendees"]}


# ---------------------------------------------------------------- rooms

def create_room(groups, query, headers, raw):
    body = parse_body(raw)
    name = body.get("name")
    if not isinstance(name, str):
        raise validation_error("name must be a string")
    name = name.strip()
    if not 1 <= len(name) <= 50:
        raise validation_error("name must be 1 to 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not 1 <= capacity <= 500:
        raise validation_error("capacity must be an integer from 1 to 500")
    buffer_minutes = body.get("buffer_minutes", 0)
    if not is_int(buffer_minutes) or not 0 <= buffer_minutes <= 60:
        raise validation_error("buffer_minutes must be an integer from 0 to 60")
    key = name.casefold()
    if key in STORE.room_names:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    STORE.room_seq += 1
    rid = f"r{STORE.room_seq}"
    room = {"id": rid, "name": name, "capacity": capacity, "buffer_minutes": buffer_minutes}
    STORE.rooms[rid] = room
    STORE.room_list.append(room)
    STORE.room_names[key] = rid
    return 201, dict(room)


def list_rooms(groups, query, headers, raw):
    return 200, {"rooms": [dict(r) for r in STORE.room_list]}


def get_room(groups, query, headers, raw):
    room = STORE.rooms.get(groups[0])
    if room is None:
        raise not_found("room not found")
    return 200, dict(room)


# ---------------------------------------------------------------- bookings

def validate_booking(body):
    for f in ("room_id", "start", "end", "title", "attendees"):
        if f not in body:
            raise validation_error(f"missing field {f}")
    room_id = body["room_id"]
    if not isinstance(room_id, str):
        raise validation_error("room_id must be a string")
    if not isinstance(body["start"], str) or not isinstance(body["end"], str):
        raise validation_error("start and end must be timestamp strings")
    try:
        start = parse_slot_ts(body["start"])
        end = parse_slot_ts(body["end"])
    except ValueError:
        raise validation_error("start and end must be quarter-hour timestamps YYYY-MM-DDTHH:MM:SSZ")
    if end <= start:
        raise validation_error("end must be after start")
    if end - start > MAX_BOOKING:
        raise validation_error("booking may be at most 8 hours long")
    title = body["title"]
    if not isinstance(title, str):
        raise validation_error("title must be a string")
    title = title.strip()
    if not 1 <= len(title) <= 100:
        raise validation_error("title must be 1 to 100 characters")
    attendees = body["attendees"]
    if not is_int(attendees) or attendees < 1:
        raise validation_error("attendees must be an integer of at least 1")
    return room_id, start, end, title, attendees


def find_conflict(room_id, start, end, buffer, exclude_id=None):
    """True if another booking of the room (skipping exclude_id) comes closer than
    `buffer` minutes to [start, end). With buffer 0 this is the half-open overlap rule."""
    buf = timedelta(minutes=buffer)
    for other in STORE.bookings.values():
        if other["room_id"] != room_id or other["id"] == exclude_id:
            continue
        if start < shift(other["end_dt"], buf) and other["start_dt"] < shift(end, buf):
            return True
    return False


def create_booking(groups, query, headers, raw):
    body = parse_body(raw)
    key = headers.get("Idempotency-Key")  # None when absent or empty
    if key is not None and key in STORE.idem:
        stored_body, stored_resp = STORE.idem[key]
        if json_equal(stored_body, body):
            return 201, dict(stored_resp)
        raise ApiError(422, "idempotency_mismatch", "Idempotency-Key was used with a different body")

    room_id, start, end, title, attendees = validate_booking(body)
    room = STORE.rooms.get(room_id)
    if room is None:
        raise not_found("room not found")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    if find_conflict(room_id, start, end, room["buffer_minutes"]):
        raise ApiError(409, "conflict", "booking overlaps an existing booking or is too close to one")

    STORE.booking_seq += 1
    seq = STORE.booking_seq
    bid = f"b{seq}"
    booking = {"id": bid, "seq": seq, "room_id": room_id, "start_dt": start, "end_dt": end,
               "title": title, "attendees": attendees}
    STORE.bookings[bid] = booking
    resp = public_booking(booking)
    if key is not None:
        STORE.idem[key] = (body, dict(resp))
    return 201, resp


def get_booking(groups, query, headers, raw):
    b = STORE.bookings.get(groups[0])
    if b is None:
        raise not_found("booking not found")
    return 200, public_booking(b)


def delete_booking(groups, query, headers, raw):
    if STORE.bookings.pop(groups[0], None) is None:
        raise not_found("booking not found")
    return 204, None


def patch_booking(groups, query, headers, raw):
    body = parse_body(raw)
    b = STORE.bookings.get(groups[0])
    if b is None:
        raise not_found("booking not found")
    merged = {
        "room_id": b["room_id"],
        "start": fmt_ts(b["start_dt"]),
        "end": fmt_ts(b["end_dt"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }
    for f in ("start", "end", "title", "attendees"):
        if f in body:
            merged[f] = body[f]
    room_id, start, end, title, attendees = validate_booking(merged)
    room = STORE.rooms.get(room_id)
    if room is None:  # pragma: no cover - rooms cannot be deleted
        raise not_found("room not found")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    if find_conflict(room_id, start, end, room["buffer_minutes"], exclude_id=b["id"]):
        raise ApiError(409, "conflict", "booking overlaps an existing booking or is too close to one")
    # Assign only after every check has passed.
    b["start_dt"] = start
    b["end_dt"] = end
    b["title"] = title
    b["attendees"] = attendees
    return 200, public_booking(b)


def list_bookings(groups, query, headers, raw):
    qs = parse_qs(query, keep_blank_values=True)
    room_id = one_param(qs, "room_id")
    from_s = one_param(qs, "from")
    to_s = one_param(qs, "to")
    limit_s = one_param(qs, "limit")
    cursor = one_param(qs, "cursor")

    from_dt = to_dt = None
    if from_s is not None:
        try:
            from_dt = parse_ts(from_s)
        except ValueError:
            raise validation_error("from must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    if to_s is not None:
        try:
            to_dt = parse_ts(to_s)
        except ValueError:
            raise validation_error("to must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    if limit_s is None:
        limit = 20
    else:
        if not DIGITS_RE.fullmatch(limit_s):
            raise validation_error("limit must be an integer from 1 to 100")
        limit = int(limit_s)
        if not 1 <= limit <= 100:
            raise validation_error("limit must be an integer from 1 to 100")
    after = dec_cursor(cursor) if cursor is not None else None

    matches = []
    empty_window = from_dt is not None and to_dt is not None and not from_dt < to_dt
    for b in ([] if empty_window else STORE.bookings.values()):
        if room_id is not None and b["room_id"] != room_id:
            continue
        if from_dt is not None and not b["end_dt"] > from_dt:
            continue
        if to_dt is not None and not b["start_dt"] < to_dt:
            continue
        matches.append(((b["start_dt"], b["seq"]), b))
    total = len(matches)
    items = [t for t in matches if after is None or t[0] > after]
    items.sort(key=lambda t: t[0])
    page = items[:limit + 1]
    has_more = len(page) > limit
    page = page[:limit]
    next_cursor = None
    if has_more:
        last = page[-1][1]
        next_cursor = enc_cursor(last["start_dt"], last["seq"])
    return 200, {"bookings": [public_booking(b) for _, b in page], "next_cursor": next_cursor,
                 "total": total}


# ---------------------------------------------------------------- availability

def availability(groups, query, headers, raw):
    room = STORE.rooms.get(groups[0])
    if room is None:
        raise not_found("room not found")
    qs = parse_qs(query, keep_blank_values=True)
    d_s = one_param(qs, "date")
    if d_s is None or not DATE_RE.fullmatch(d_s):
        raise validation_error("date must be YYYY-MM-DD")
    try:
        d = date.fromisoformat(d_s)
    except ValueError:
        raise validation_error("date must be a real calendar date")
    day_start = datetime(d.year, d.month, d.day, WORK_START_HOUR, tzinfo=timezone.utc)
    day_end = datetime(d.year, d.month, d.day, WORK_END_HOUR, tzinfo=timezone.utc)

    buf = timedelta(minutes=room["buffer_minutes"])
    busy = []
    for b in STORE.bookings.values():
        if b["room_id"] != room["id"]:
            continue
        s = max(shift(b["start_dt"], -buf), day_start)
        e = min(shift(b["end_dt"], buf), day_end)
        if s < e:
            busy.append((s, e))
    busy.sort()

    free = []
    cursor = day_start
    for s, e in busy:
        if s > cursor:
            free.append((cursor, s))
        if e > cursor:
            cursor = e
    if cursor < day_end:
        free.append((cursor, day_end))
    free = [{"start": fmt_ts(s), "end": fmt_ts(e)}
            for s, e in free if e - s >= timedelta(minutes=15)]
    return 200, {"date": d_s, "free": free}


# ---------------------------------------------------------------- routing

ROUTES = [
    (re.compile(r"/rooms"), {"GET": list_rooms, "POST": create_room}),
    (re.compile(r"/rooms/([^/]+)"), {"GET": get_room}),
    (re.compile(r"/rooms/([^/]+)/availability"), {"GET": availability}),
    (re.compile(r"/bookings"), {"GET": list_bookings, "POST": create_booking}),
    (re.compile(r"/bookings/([^/]+)"), {"GET": get_booking, "PATCH": patch_booking, "DELETE": delete_booking}),
]


def route(method, path, query, headers, raw):
    lookup = "GET" if method == "HEAD" else method
    path_matched = False
    for pattern, methods in ROUTES:
        m = pattern.fullmatch(path)
        if m is None:
            continue
        path_matched = True
        handler = methods.get(lookup)
        if handler is None:
            continue
        with LOCK:
            return handler(m.groups(), query, headers, raw)
    if path_matched:
        raise ApiError(405, "method_not_allowed", "method not allowed for this path")
    raise not_found("no such path")


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def _idempotency_key(self):
        """The header value with surrounding whitespace removed (the HTTP parser
        does this). An empty value does not name a key, so it gives None."""
        return self.headers.get("Idempotency-Key") or None

    def log_message(self, format, *args):
        pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _dispatch(self):
        method = self.command
        parts = urlsplit(self.path)
        try:
            raw = self._read_body()
            key = self._idempotency_key()
            hdrs = {"Idempotency-Key": key} if key is not None else {}
            status, payload = route(method, parts.path, parts.query, hdrs, raw)
        except ApiError as e:
            status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception as e:  # pragma: no cover - safety net
            status, payload = 500, {"error": {"code": "internal_error", "message": str(e)}}
        self._send(status, payload, head=(method == "HEAD"))

    def _send(self, status, payload, head=False):
        self.send_response(status)
        if status == 204 or payload is None:
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)


def _handler_getattr(self, name):
    # Every HTTP method name (known or not) goes through the single dispatcher,
    # so unsupported methods give 405 on known paths and 404 on unknown ones.
    if name.startswith("do_"):
        return self._dispatch
    raise AttributeError(name)


Handler.__getattr__ = _handler_getattr


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
