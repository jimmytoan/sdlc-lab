#!/usr/bin/env python3
"""bookings: meeting room booking JSON API. In-memory state, standard library only."""
import argparse
import base64
import binascii
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

UTC = dt.timezone.utc
TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z")
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DAY_START_HOUR = 8
DAY_END_HOUR = 18
MAX_BOOKING = dt.timedelta(hours=8)

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

LOCK = threading.RLock()
ROOMS = {}          # id -> room dict (public shape)
ROOM_LIST = []      # rooms in creation order
BOOKINGS = {}       # id -> record {"n", "pub", "s", "e"}
BOOKING_LIST = []   # records in creation order
IDEM = {}           # Idempotency-Key -> (fingerprint, status, response body)
COUNTERS = {"room": 0, "booking": 0}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = STATUS[code]
        self.message = message


def validation(message):
    return ApiError("validation_error", message)


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    m = TS_RE.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise validation(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        return dt.datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise validation(f"{field} is not a valid date and time")


def on_quarter_hour(d):
    return d.minute % 15 == 0 and d.second == 0 and d.microsecond == 0


def parse_body(raw):
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise validation("request body must be a JSON object")
    return obj


def is_int(v):
    return type(v) is int


# ---------------------------------------------------------------- rooms

def list_rooms():
    with LOCK:
        return 200, {"rooms": [dict(r) for r in ROOM_LIST]}


def create_room(raw):
    body = parse_body(raw)
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise validation("name must be a non-empty string of at most 50 characters")
    if not is_int(capacity) or not 1 <= capacity <= 500:
        raise validation("capacity must be an integer from 1 to 500")
    with LOCK:
        if any(r["name"] == name for r in ROOM_LIST):
            raise ApiError("room_exists", "a room with this name already exists")
        COUNTERS["room"] += 1
        room = {"id": f"r{COUNTERS['room']}", "name": name, "capacity": capacity}
        ROOMS[room["id"]] = room
        ROOM_LIST.append(room)
        return 201, dict(room)


def get_room(room_id):
    with LOCK:
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, dict(room)


def availability(room_id, query):
    with LOCK:
        if room_id not in ROOMS:
            raise ApiError("not_found", "room not found")
        m = DATE_RE.fullmatch(query.get("date") or "")
        if not m:
            raise validation("date query parameter must be YYYY-MM-DD")
        try:
            day = dt.date(*(int(g) for g in m.groups()))
        except ValueError:
            raise validation("date is not a valid calendar date")
        day_start = dt.datetime(day.year, day.month, day.day, DAY_START_HOUR, tzinfo=UTC)
        day_end = dt.datetime(day.year, day.month, day.day, DAY_END_HOUR, tzinfo=UTC)
        busy = sorted(
            (r["s"], r["e"]) for r in BOOKING_LIST
            if r["pub"]["room_id"] == room_id and r["s"] < day_end and r["e"] > day_start
        )
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(min(s, day_end))})
            cur = max(cur, e)
            if cur >= day_end:
                break
        if cur < day_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
        return 200, {"date": day.isoformat(), "free": free}


# ---------------------------------------------------------------- bookings

def create_booking(raw, headers):
    idem_key = headers.get("Idempotency-Key")
    if idem_key is not None:
        idem_key = idem_key.strip() or None
    body = parse_body(raw)

    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise validation("room_id must be a non-empty string")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise validation("title must be a non-empty string of at most 200 characters")
    if not is_int(attendees) or attendees < 1:
        raise validation("attendees must be a positive integer")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    if not on_quarter_hour(start) or not on_quarter_hour(end):
        raise validation("start and end must be on quarter hours")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_BOOKING:
        raise validation("a booking may be at most 8 hours long")

    fingerprint = json.dumps(
        {"room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
         "title": title, "attendees": attendees},
        sort_keys=True,
    )

    with LOCK:
        if idem_key is not None and idem_key in IDEM:
            fp, status, stored = IDEM[idem_key]
            if fp != fingerprint:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return status, stored

        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for r in BOOKING_LIST:
            if r["pub"]["room_id"] == room_id and r["s"] < end and start < r["e"]:
                raise ApiError("conflict", "the room is already booked for this time")

        COUNTERS["booking"] += 1
        pub = {
            "id": f"b{COUNTERS['booking']}",
            "room_id": room_id,
            "start": fmt_ts(start),
            "end": fmt_ts(end),
            "title": title,
            "attendees": attendees,
        }
        rec = {"n": COUNTERS["booking"], "pub": pub, "s": start, "e": end}
        BOOKINGS[pub["id"]] = rec
        BOOKING_LIST.append(rec)
        if idem_key is not None:
            IDEM[idem_key] = (fingerprint, 201, dict(pub))
        return 201, dict(pub)


def get_booking(booking_id):
    with LOCK:
        rec = BOOKINGS.get(booking_id)
        if rec is None:
            raise ApiError("not_found", "booking not found")
        return 200, dict(rec["pub"])


def delete_booking(booking_id):
    with LOCK:
        rec = BOOKINGS.pop(booking_id, None)
        if rec is None:
            raise ApiError("not_found", "booking not found")
        BOOKING_LIST.remove(rec)
        return 204, None


def encode_cursor(rec):
    return base64.urlsafe_b64encode(f"{fmt_ts(rec['s'])}|{rec['n']}".encode()).decode().rstrip("=")


def decode_cursor(token):
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
        ts_part, n_part = text.split("|")
        return parse_ts(ts_part, "cursor"), int(n_part)
    except (ValueError, binascii.Error, UnicodeDecodeError, ApiError):
        raise validation("cursor is invalid")


def list_bookings(query):
    limit_raw = query.get("limit")
    if limit_raw is None or limit_raw == "":
        limit = 20
    else:
        try:
            limit = int(limit_raw)
        except ValueError:
            raise validation("limit must be an integer from 1 to 100")
        if not 1 <= limit <= 100:
            raise validation("limit must be an integer from 1 to 100")

    room_id = query.get("room_id") or None
    time_from = parse_ts(query["from"], "from") if query.get("from") else None
    time_to = parse_ts(query["to"], "to") if query.get("to") else None
    if time_from and time_to and time_from >= time_to:
        raise validation("from must be before to")
    cursor_key = None
    if query.get("cursor"):
        cursor_key = decode_cursor(query["cursor"])

    with LOCK:
        items = []
        for r in BOOKING_LIST:
            if room_id is not None and r["pub"]["room_id"] != room_id:
                continue
            if time_from is not None and not r["e"] > time_from:
                continue
            if time_to is not None and not r["s"] < time_to:
                continue
            items.append(r)
        items.sort(key=lambda r: (r["s"], r["n"]))
        if cursor_key is not None:
            items = [r for r in items if (r["s"], r["n"]) > cursor_key]
        page = items[:limit]
        next_cursor = encode_cursor(page[-1]) if len(items) > limit and page else None
        return 200, {"bookings": [dict(r["pub"]) for r in page], "next_cursor": next_cursor}


# ---------------------------------------------------------------- routing

def route(method, raw_path, headers, raw_body):
    parts = urlsplit(raw_path)
    segs = [unquote(s) for s in parts.path.split("/") if s]
    query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}

    if segs == ["rooms"]:
        table = {"GET": lambda: list_rooms(), "POST": lambda: create_room(raw_body)}
    elif len(segs) == 2 and segs[0] == "rooms":
        table = {"GET": lambda: get_room(segs[1])}
    elif len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
        table = {"GET": lambda: availability(segs[1], query)}
    elif segs == ["bookings"]:
        table = {"GET": lambda: list_bookings(query),
                 "POST": lambda: create_booking(raw_body, headers)}
    elif len(segs) == 2 and segs[0] == "bookings":
        table = {"GET": lambda: get_booking(segs[1]),
                 "DELETE": lambda: delete_booking(segs[1])}
    else:
        raise ApiError("not_found", "no such route")

    fn = table.get(method)
    if fn is None:
        raise ApiError("method_not_allowed", f"method {method} not allowed here")
    return fn()


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, format, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _dispatch(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            status, body = route(method, self.path, self.headers, raw)
        except ApiError as e:
            status = e.status
            body = {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status = 500
            body = {"error": {"code": "internal_error", "message": "internal server error"}}
        self._send(status, body)

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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, default=8000)
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
