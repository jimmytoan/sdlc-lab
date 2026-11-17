#!/usr/bin/env python3
"""Meeting room booking JSON API (standard library only, in-memory state).

Run: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

UTC = timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)
QUARTER = 15


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
        "internal_error": 500,
    }

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = self.STATUS[code]
        self.message = message


def verr(message):
    return ApiError("validation_error", message)


# ---------------------------------------------------------------- helpers

TS_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?(?:Z|\+00:00)$")


def parse_ts(value, field):
    """Strict UTC timestamp parser: YYYY-MM-DDTHH:MM[:SS]Z."""
    if not isinstance(value, str):
        raise verr(f"{field} must be a UTC timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    y, mo, d, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=UTC)
    except ValueError:
        raise verr(f"{field} is not a valid date/time")


def fmt_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_quarter(dt):
    return dt.minute % QUARTER == 0 and dt.second == 0 and dt.microsecond == 0


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_json_object(raw):
    try:
        text = raw.decode("utf-8")
        body = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ApiError("bad_json", "request body must be valid JSON")
    if not isinstance(body, dict):
        raise verr("request body must be a JSON object")
    return body


# ---------------------------------------------------------------- state

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()
        self.bookings = {}       # id -> booking dict (with datetime fields under _start/_end)
        self.room_seq = 0
        self.booking_seq = 0
        self.idempotency = {}    # key -> (fingerprint, response)


STORE = Store()


def public_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["_start"]),
        "end": fmt_ts(b["_end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def num_of(prefix_id):
    return int(prefix_id[1:])


# ---------------------------------------------------------------- rooms

def create_room(raw, headers, query):
    body = parse_json_object(raw)
    name = body.get("name")
    if not isinstance(name, str) or name.strip() == "":
        raise verr("name is required and must be a non-empty string")
    if len(name) > 50:
        raise verr("name must be at most 50 characters")
    capacity = body.get("capacity")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise verr("capacity must be an integer from 1 to 500")
    with STORE.lock:
        if name in STORE.room_names:
            raise ApiError("room_exists", f"a room named '{name}' already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STORE.rooms[rid] = room
        STORE.room_names.add(name)
    return 201, dict(room)


def list_rooms(raw, headers, query):
    with STORE.lock:
        rooms = [dict(r) for r in STORE.rooms.values()]
    rooms.sort(key=lambda r: num_of(r["id"]))
    return 200, {"rooms": rooms}


def get_room(room_id):
    def handler(raw, headers, query):
        with STORE.lock:
            room = STORE.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", f"room {room_id} not found")
            return 200, dict(room)
    return handler


def availability(room_id):
    def handler(raw, headers, query):
        with STORE.lock:
            if room_id not in STORE.rooms:
                raise ApiError("not_found", f"room {room_id} not found")
        dates = query.get("date", [])
        if len(dates) != 1 or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", dates[0]):
            raise verr("query parameter date is required, format YYYY-MM-DD")
        try:
            day = date.fromisoformat(dates[0])
        except ValueError:
            raise verr("date is not a valid calendar date")
        with STORE.lock:
            day_start = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
            day_end = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
            busy = sorted(
                (b["_start"], b["_end"]) for b in STORE.bookings.values()
                if b["room_id"] == room_id and b["_start"] < day_end and b["_end"] > day_start
            )
        free = []
        cursor = day_start
        for s, e in busy:
            if s > cursor:
                free.append({"start": fmt_ts(cursor), "end": fmt_ts(s)})
            cursor = max(cursor, e)
        if cursor < day_end:
            free.append({"start": fmt_ts(cursor), "end": fmt_ts(day_end)})
        return 200, {"date": day.isoformat(), "free": free}
    return handler


# ---------------------------------------------------------------- bookings

def create_booking(raw, headers, query):
    idem_key = headers.get("Idempotency-Key")
    if idem_key is not None and idem_key.strip() == "":
        idem_key = None
    body = parse_json_object(raw)

    room_id = body.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id is required and must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title = body.get("title")
    if not isinstance(title, str) or title.strip() == "":
        raise verr("title is required and must be a non-empty string")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be an integer of at least 1")
    if not is_quarter(start) or not is_quarter(end):
        raise verr("start and end must fall on quarter hours (:00, :15, :30, :45)")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_BOOKING:
        raise verr("a booking may be at most 8 hours long")

    normalized = {
        "room_id": room_id,
        "start": fmt_ts(start),
        "end": fmt_ts(end),
        "title": title,
        "attendees": attendees,
    }
    fingerprint = json.dumps(normalized, sort_keys=True)

    with STORE.lock:
        if idem_key is not None and idem_key in STORE.idempotency:
            stored_fp, stored_resp = STORE.idempotency[idem_key]
            if stored_fp != fingerprint:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return 201, dict(stored_resp)

        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", f"room {room_id} not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity",
                           f"{attendees} attendees exceed room capacity of {room['capacity']}")
        for other in STORE.bookings.values():
            if other["room_id"] == room_id and other["_start"] < end and other["_end"] > start:
                raise ApiError("conflict", f"room is already booked by {other['id']} at that time")

        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        rec = {"id": bid, "room_id": room_id, "title": title, "attendees": attendees,
               "_start": start, "_end": end}
        STORE.bookings[bid] = rec
        response = public_booking(rec)
        if idem_key is not None:
            STORE.idempotency[idem_key] = (fingerprint, dict(response))
    return 201, response


def get_booking(booking_id):
    def handler(raw, headers, query):
        with STORE.lock:
            rec = STORE.bookings.get(booking_id)
            if rec is None:
                raise ApiError("not_found", f"booking {booking_id} not found")
            return 200, public_booking(rec)
    return handler


def delete_booking(booking_id):
    def handler(raw, headers, query):
        with STORE.lock:
            if booking_id not in STORE.bookings:
                raise ApiError("not_found", f"booking {booking_id} not found")
            del STORE.bookings[booking_id]
        return 204, None
    return handler


def encode_cursor(start_dt, num):
    raw = f"{fmt_ts(start_dt)}|{num}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded.encode()).decode()
        ts_part, num_part = text.split("|")
        if not re.fullmatch(r"[0-9]+", num_part):
            raise ValueError("bad cursor id")
        return parse_ts(ts_part, "cursor"), int(num_part)
    except (ValueError, UnicodeDecodeError, TypeError):
        raise verr("cursor is invalid")


def single(query, name):
    vals = query.get(name)
    if vals is None:
        return None
    if len(vals) != 1:
        raise verr(f"query parameter {name} must be given once")
    return vals[0]


def list_bookings(raw, headers, query):
    limit_s = single(query, "limit")
    if limit_s is None:
        limit = 20
    else:
        if not re.fullmatch(r"[0-9]+", limit_s):
            raise verr("limit must be an integer from 1 to 100")
        limit = int(limit_s)
        if not (1 <= limit <= 100):
            raise verr("limit must be an integer from 1 to 100")

    room_id = single(query, "room_id")
    from_s, to_s = single(query, "from"), single(query, "to")
    t_from = parse_ts(from_s, "from") if from_s is not None else None
    t_to = parse_ts(to_s, "to") if to_s is not None else None
    if t_from is not None and t_to is not None and t_to <= t_from:
        raise verr("to must be after from")

    cursor_s = single(query, "cursor")
    after = decode_cursor(cursor_s) if cursor_s else None

    with STORE.lock:
        items = []
        for rec in STORE.bookings.values():
            if room_id is not None and rec["room_id"] != room_id:
                continue
            if t_from is not None and not rec["_end"] > t_from:
                continue
            if t_to is not None and not rec["_start"] < t_to:
                continue
            items.append(rec)
        items.sort(key=lambda r: (r["_start"], num_of(r["id"])))
        if after is not None:
            a_start, a_num = after
            items = [r for r in items if (r["_start"], num_of(r["id"])) > (a_start, a_num)]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last["_start"], num_of(last["id"]))
        result = [public_booking(r) for r in page]
    return 200, {"bookings": result, "next_cursor": next_cursor}


# ---------------------------------------------------------------- routing

def match_route(method, parts):
    """Returns handler factory result or raises. parts: non-empty path segments."""
    if parts and parts[0] == "rooms":
        if len(parts) == 1:
            table = {"GET": list_rooms, "POST": create_room}
        elif len(parts) == 2:
            table = {"GET": get_room(parts[1])}
        elif len(parts) == 3 and parts[2] == "availability":
            table = {"GET": availability(parts[1])}
        else:
            table = None
    elif parts and parts[0] == "bookings":
        if len(parts) == 1:
            table = {"GET": list_bookings, "POST": create_booking}
        elif len(parts) == 2:
            table = {"GET": get_booking(parts[1]), "DELETE": delete_booking(parts[1])}
        else:
            table = None
    else:
        table = None

    if table is None:
        raise ApiError("not_found", "no such endpoint")
    if method not in table:
        raise ApiError("method_not_allowed", f"method {method} not allowed here")
    return table[method]


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    def _send(self, status, payload=None):
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_error_json(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            url = urlsplit(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = parse_qs(url.query, keep_blank_values=True)
            fn = match_route(method, parts)
            status, payload = fn(raw, self.headers, query)
            self._send(status, payload)
        except ApiError as err:
            self._send_error_json(err)
        except Exception:  # pragma: no cover - defensive
            self._send_error_json(ApiError("internal_error", "internal server error"))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")


def main(argv=None):
    p = argparse.ArgumentParser(description="Meeting room booking API")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
