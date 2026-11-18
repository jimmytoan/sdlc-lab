#!/usr/bin/env python3
"""bookings: meeting room booking JSON API (in-memory, stdlib only)."""
import argparse
import json
import re
import threading
from datetime import datetime, date, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

UTC = timezone.utc
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
WORK_START = dtime(8, 0)
WORK_END = dtime(18, 0)
MAX_DURATION = timedelta(hours=8)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation(msg):
    return ApiError(422, "validation_error", msg)


def parse_ts(value, field):
    if not isinstance(value, str):
        raise validation(f"{field} must be a UTC timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise validation(f"{field} must be a UTC timestamp string like 2026-03-02T09:30:00Z")
    y, mo, d, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), tzinfo=UTC)
    except ValueError:
        raise validation(f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}       # id -> dict (with datetime fields under _start/_end)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body)


STORE = Store()


def room_public(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_public(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["_start"]),
        "end": fmt_ts(b["_end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def booking_sort_key(b):
    return (b["_start"], int(b["id"][1:]))


# ---------- handlers (all called with STORE.lock held) ----------

def create_room(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise validation("name must be a non-empty string")
    if len(name) > 50:
        raise validation("name must be at most 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    if name in STORE.room_names:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    STORE.room_seq += 1
    rid = f"r{STORE.room_seq}"
    room = {"id": rid, "name": name, "capacity": capacity}
    STORE.rooms[rid] = room
    STORE.room_names.add(name)
    return 201, room_public(room)


def get_room(rid):
    room = STORE.rooms.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return 200, room_public(room)


def list_rooms():
    return 200, {"rooms": [room_public(r) for r in STORE.rooms.values()]}


def find_conflict(room_id, start, end, exclude_id=None):
    for b in STORE.bookings.values():
        if b["room_id"] != room_id or b["id"] == exclude_id:
            continue
        if b["_start"] < end and start < b["_end"]:
            return b
    return None


def create_booking(body):
    if not isinstance(body, dict):
        raise validation("body must be a JSON object")
    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise validation("room_id must be a non-empty string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    if not isinstance(title, str):
        raise validation("title must be a string")
    if not is_int(attendees) or attendees < 1:
        raise validation("attendees must be a positive integer")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 != 0 or dt.second != 0:
            raise validation(f"{field} must be on a quarter hour")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking must be at most 8 hours long")
    room = STORE.rooms.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "more attendees than the room holds")
    if find_conflict(room_id, start, end):
        raise ApiError(409, "conflict", "the room is already booked for this time")
    STORE.booking_seq += 1
    bid = f"b{STORE.booking_seq}"
    b = {"id": bid, "room_id": room_id, "title": title, "attendees": attendees,
         "_start": start, "_end": end}
    STORE.bookings[bid] = b
    return 201, booking_public(b)


def get_booking(bid):
    b = STORE.bookings.get(bid)
    if b is None:
        raise ApiError(404, "not_found", "booking not found")
    return 200, booking_public(b)


def delete_booking(bid):
    if bid not in STORE.bookings:
        raise ApiError(404, "not_found", "booking not found")
    del STORE.bookings[bid]
    return 204, None


def parse_limit(qs):
    raw = qs.get("limit", ["20"])[0]
    if not re.fullmatch(r"\d+", raw):
        raise validation("limit must be an integer from 1 to 100")
    v = int(raw)
    if not (1 <= v <= 100):
        raise validation("limit must be an integer from 1 to 100")
    return v


def parse_cursor(qs):
    raw = qs.get("cursor", [None])[0]
    if raw is None or raw == "":
        return 0
    if not re.fullmatch(r"\d+", raw):
        raise validation("cursor is invalid")
    return int(raw)


def list_bookings(qs):
    limit = parse_limit(qs)
    offset = parse_cursor(qs)
    room_id = qs.get("room_id", [None])[0]
    frm = qs.get("from", [None])[0]
    to = qs.get("to", [None])[0]
    f_dt = parse_ts(frm, "from") if frm is not None else None
    t_dt = parse_ts(to, "to") if to is not None else None
    if f_dt and t_dt and t_dt <= f_dt:
        raise validation("to must be after from")
    items = []
    for b in sorted(STORE.bookings.values(), key=booking_sort_key):
        if room_id is not None and b["room_id"] != room_id:
            continue
        if f_dt is not None and not (b["_end"] > f_dt):
            continue
        if t_dt is not None and not (b["_start"] < t_dt):
            continue
        items.append(b)
    page = items[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(items) else None
    return 200, {"bookings": [booking_public(b) for b in page], "next_cursor": next_cursor}


def availability(rid, qs):
    room = STORE.rooms.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    raw = qs.get("date", [None])[0]
    if raw is None or not DATE_RE.match(raw):
        raise validation("date must be YYYY-MM-DD")
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        raise validation("date must be a valid YYYY-MM-DD date")
    win_start = datetime.combine(d, WORK_START, tzinfo=UTC)
    win_end = datetime.combine(d, WORK_END, tzinfo=UTC)
    busy = sorted(
        (max(b["_start"], win_start), min(b["_end"], win_end))
        for b in STORE.bookings.values()
        if b["room_id"] == rid and b["_start"] < win_end and b["_end"] > win_start
    )
    free = []
    cur = win_start
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < win_end:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(win_end)})
    return 200, {"date": raw, "free": free}


# ---------- routing ----------

def route(method, path, qs, raw_body, idem_key):
    parts = [p for p in path.split("/") if p != ""]

    def not_allowed():
        return ApiError(405, "method_not_allowed", f"method {method} not allowed here")

    def not_found():
        return ApiError(404, "not_found", "no such endpoint")

    def parse_body():
        if not raw_body or not raw_body.strip():
            raise ApiError(400, "bad_json", "request body must be JSON")
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")

    if not parts:
        raise not_found()
    head = parts[0]

    if head == "rooms":
        if len(parts) == 1:
            if method == "GET":
                with STORE.lock:
                    return list_rooms()
            if method == "POST":
                body = parse_body()
                with STORE.lock:
                    return create_room(body)
            raise not_allowed()
        if len(parts) == 2:
            if method != "GET":
                raise not_allowed()
            with STORE.lock:
                return get_room(parts[1])
        if len(parts) == 3 and parts[2] == "availability":
            if method != "GET":
                raise not_allowed()
            with STORE.lock:
                return availability(parts[1], qs)
        raise not_found()

    if head == "bookings":
        if len(parts) == 1:
            if method == "GET":
                with STORE.lock:
                    return list_bookings(qs)
            if method == "POST":
                return create_booking_idem(parse_body(), raw_body, idem_key)
            raise not_allowed()
        if len(parts) == 2:
            if method == "GET":
                with STORE.lock:
                    return get_booking(parts[1])
            if method == "DELETE":
                with STORE.lock:
                    return delete_booking(parts[1])
            raise not_allowed()
        raise not_found()

    raise not_found()


def create_booking_idem(body, raw_body, idem_key):
    if idem_key is None:
        with STORE.lock:
            return create_booking(body)
    fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with STORE.lock:
        prev = STORE.idem.get(idem_key)
        if prev is not None:
            if prev[0] != fingerprint:
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return prev[1], prev[2]
        status, payload = create_booking(body)
        STORE.idem[idem_key] = (fingerprint, status, payload)
        return status, payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep quiet
        pass

    def _handle(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        parts = urlsplit(self.path)
        qs = parse_qs(parts.query, keep_blank_values=True)
        idem = self.headers.get("Idempotency-Key")
        if idem is not None and idem.strip() == "":
            idem = None
        try:
            status, payload = route(self.command, parts.path, qs, raw, idem)
        except ApiError as e:
            status, payload = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:  # pragma: no cover - safety net
            status, payload = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        if status == 204:
            body = b""
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
