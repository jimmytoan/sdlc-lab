#!/usr/bin/env python3
"""bookings: meeting room booking JSON API (in-memory, stdlib only).

Usage: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

UTC = timezone.utc
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^-?\d+$")

WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING_MINUTES = 8 * 60
MAX_NAME_LEN = 50
MAX_TITLE_LEN = 200


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

    def __init__(self, code, message):
        super().__init__(message)
        self.status = self.STATUS[code]
        self.code = code
        self.message = message


def verr(message):
    return ApiError("validation_error", message)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        raise verr(f"{field} must be a UTC timestamp like 2026-03-02T09:30:00Z")
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")
    return dt


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_names = {}     # name -> id
        self.room_seq = 0
        self.bookings = {}       # id -> dict (with datetime fields under _start/_end)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, payload)


STATE = State()


def public_room(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def public_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["_start"]),
        "end": fmt_ts(b["_end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def num_id(prefix, ident):
    return int(ident[len(prefix):])


# ---------- rooms ----------

def create_room(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise verr("name is required")
    if len(name) > MAX_NAME_LEN:
        raise verr(f"name must be at most {MAX_NAME_LEN} characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise verr("capacity must be an integer from 1 to 500")
    with STATE.lock:
        if name in STATE.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        STATE.room_seq += 1
        rid = f"r{STATE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STATE.rooms[rid] = room
        STATE.room_names[name] = rid
        return 201, public_room(room)


def list_rooms():
    with STATE.lock:
        return 200, {"rooms": [public_room(r) for r in
                               sorted(STATE.rooms.values(), key=lambda r: num_id("r", r["id"]))]}


def get_room(rid):
    with STATE.lock:
        room = STATE.rooms.get(rid)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, public_room(room)


def availability(rid, query):
    dates = query.get("date")
    if not dates or len(dates) != 1:
        raise verr("date query parameter is required (YYYY-MM-DD)")
    ds = dates[0]
    if not DATE_RE.match(ds):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(ds)
    except ValueError:
        raise verr("date is not a valid calendar date")
    with STATE.lock:
        if rid not in STATE.rooms:
            raise ApiError("not_found", "room not found")
        day_start = datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
        day_end = datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
        busy = sorted(
            (b["_start"], b["_end"]) for b in STATE.bookings.values()
            if b["room_id"] == rid and b["_start"] < day_end and b["_end"] > day_start
        )
        free = []
        cursor = day_start
        for s, e in busy:
            if s > cursor:
                free.append({"start": fmt_ts(cursor), "end": fmt_ts(min(s, day_end))})
            if e > cursor:
                cursor = e
            if cursor >= day_end:
                break
        if cursor < day_end:
            free.append({"start": fmt_ts(cursor), "end": fmt_ts(day_end)})
        return 200, {"date": ds, "free": free}


# ---------- bookings ----------

def validate_booking(body):
    if not isinstance(body, dict):
        raise verr("body must be a JSON object")
    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise verr("room_id is required")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    if not isinstance(title, str) or not title.strip():
        raise verr("title is required")
    if len(title) > MAX_TITLE_LEN:
        raise verr(f"title must be at most {MAX_TITLE_LEN} characters")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be a positive integer")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 or dt.second:
            raise verr(f"{field} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if (end - start) > timedelta(minutes=MAX_BOOKING_MINUTES):
        raise verr("booking may be at most 8 hours long")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def create_booking(body, idem_key):
    data = validate_booking(body)
    fingerprint = json.dumps(
        {"room_id": data["room_id"], "start": fmt_ts(data["start"]),
         "end": fmt_ts(data["end"]), "title": data["title"], "attendees": data["attendees"]},
        sort_keys=True)
    with STATE.lock:
        if idem_key is not None and idem_key in STATE.idem:
            old_fp, status, payload = STATE.idem[idem_key]
            if old_fp != fingerprint:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return status, payload

        room = STATE.rooms.get(data["room_id"])
        if room is None:
            raise verr("room_id does not refer to an existing room")
        if data["attendees"] > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for other in STATE.bookings.values():
            if other["room_id"] == room["id"] and other["_start"] < data["end"] and data["start"] < other["_end"]:
                raise ApiError("conflict", "room is already booked for this time")

        STATE.booking_seq += 1
        bid = f"b{STATE.booking_seq}"
        booking = {
            "id": bid,
            "room_id": room["id"],
            "title": data["title"],
            "attendees": data["attendees"],
            "_start": data["start"],
            "_end": data["end"],
        }
        STATE.bookings[bid] = booking
        result = (201, public_booking(booking))
        if idem_key is not None:
            STATE.idem[idem_key] = (fingerprint, result[0], result[1])
        return result


def get_booking(bid):
    with STATE.lock:
        b = STATE.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", "booking not found")
        return 200, public_booking(b)


def delete_booking(bid):
    with STATE.lock:
        if bid not in STATE.bookings:
            raise ApiError("not_found", "booking not found")
        del STATE.bookings[bid]
        return 204, None


def parse_limit(query):
    vals = query.get("limit")
    if vals is None:
        return 20
    if len(vals) != 1 or not INT_RE.match(vals[0]):
        raise verr("limit must be an integer from 1 to 100")
    n = int(vals[0])
    if not (1 <= n <= 100):
        raise verr("limit must be an integer from 1 to 100")
    return n


def parse_cursor(query):
    vals = query.get("cursor")
    if vals is None:
        return 0
    if len(vals) != 1 or not re.fullmatch(r"\d+", vals[0]):
        raise verr("cursor is invalid")
    return int(vals[0])


def list_bookings(query):
    limit = parse_limit(query)
    offset = parse_cursor(query)

    room_id = None
    if "room_id" in query:
        if len(query["room_id"]) != 1 or not query["room_id"][0]:
            raise verr("room_id must be a single value")
        room_id = query["room_id"][0]
    time_from = parse_ts(query["from"][0], "from") if "from" in query else None
    time_to = parse_ts(query["to"][0], "to") if "to" in query else None
    if time_from and time_to and time_from >= time_to:
        raise verr("from must be before to")

    with STATE.lock:
        items = []
        for b in STATE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if time_from is not None and not (b["_end"] > time_from):
                continue
            if time_to is not None and not (b["_start"] < time_to):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_start"], num_id("b", b["id"])))
        page = items[offset:offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


# ---------- routing ----------

def route(method, raw_path, query, headers, body_bytes):
    path = urlsplit(raw_path).path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    parts = [unquote(p) for p in path.split("/") if p != ""]

    def body_json():
        if not body_bytes or not body_bytes.strip():
            raise ApiError("bad_json", "request body must be a JSON document")
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")

    def allow(*methods):
        if method not in methods:
            raise ApiError("method_not_allowed", f"method {method} not allowed here")

    if parts == ["rooms"]:
        allow("GET", "POST")
        if method == "GET":
            return list_rooms()
        return create_room(body_json())

    if len(parts) == 2 and parts[0] == "rooms":
        allow("GET")
        return get_room(parts[1])

    if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "availability":
        allow("GET")
        return availability(parts[1], query)

    if parts == ["bookings"]:
        allow("GET", "POST")
        if method == "GET":
            return list_bookings(query)
        key = headers.get("Idempotency-Key")
        if key is not None:
            key = key.strip()
            if not key:
                key = None
        return create_booking(body_json(), key)

    if len(parts) == 2 and parts[0] == "bookings":
        allow("GET", "DELETE")
        if method == "GET":
            return get_booking(parts[1])
        return delete_booking(parts[1])

    raise ApiError("not_found", "no such endpoint")


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep the output quiet
        pass

    def _handle(self):
        length = self.headers.get("Content-Length")
        body = b""
        if length:
            try:
                n = int(length)
            except ValueError:
                n = 0
            if n > 0:
                body = self.rfile.read(n)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            status, payload = route(self.command, self.path, query, self.headers, body)
        except ApiError as e:
            status = e.status
            payload = {"error": {"code": e.code, "message": e.message}}
        except Exception:  # pragma: no cover - defensive
            status = 500
            payload = {"error": {"code": "internal_error", "message": "internal server error"}}
        if status == 204:
            data = b""
        else:
            data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main(argv=None):
    p = argparse.ArgumentParser(description="bookings API server")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
