"""Meeting room booking JSON API (in-memory, standard library only).

Run: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import json
import re
import threading
from datetime import datetime, timedelta, timezone, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

WORK_START_H = 8
WORK_END_H = 18
MAX_DURATION = timedelta(hours=8)
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


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
        self.code = code
        self.message = message
        self.status = self.STATUS[code]


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = {}     # casefolded name -> id
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict
        self.booking_order = []  # list of (start_dt, seq, id) kept sorted
        self.booking_seq = 0
        self.idem = {}           # key -> (body_canonical, status, response_body)


STORE = Store()


# ---------- helpers ----------

def parse_ts(value, field):
    """Strict UTC timestamp YYYY-MM-DDTHH:MM:SSZ. Returns aware datetime."""
    if not isinstance(value, str):
        raise ApiError("validation_error", f"{field} must be a timestamp string")
    m = TS_RE.match(value)
    if not m:
        raise ApiError("validation_error", f"{field} must look like 2026-03-02T09:30:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def encode_cursor(start_str, seq):
    raw = json.dumps([start_str, seq]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        pad = "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode((cursor + pad).encode()).decode())
        start_str, seq = data
        if not isinstance(start_str, str) or not is_int(seq):
            raise ValueError
        return parse_ts(start_str, "cursor"), seq
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("validation_error", "invalid cursor")


def booking_out(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": b["start"],
        "end": b["end"],
        "title": b["title"],
        "attendees": b["attendees"],
    }


# ---------- rooms ----------

def create_room(body):
    if not isinstance(body, dict):
        raise ApiError("validation_error", "body must be a JSON object")
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise ApiError("validation_error", "name must be a string")
    name = name.strip()
    if not 1 <= len(name) <= 50:
        raise ApiError("validation_error", "name must be 1 to 50 characters")
    if not is_int(capacity) or not 1 <= capacity <= 500:
        raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
    with STORE.lock:
        if name.casefold() in STORE.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STORE.rooms[rid] = room
        STORE.room_names[name.casefold()] = rid
        return 201, dict(room)


def list_rooms():
    with STORE.lock:
        return 200, {"rooms": [dict(r) for r in STORE.rooms.values()]}


def get_room(rid):
    with STORE.lock:
        room = STORE.rooms.get(rid)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, dict(room)


def availability(rid, query):
    dates = query.get("date")
    if not dates or len(dates) != 1:
        raise ApiError("validation_error", "date query parameter is required")
    ds = dates[0]
    m = DATE_RE.match(ds)
    if not m:
        raise ApiError("validation_error", "date must be YYYY-MM-DD")
    try:
        day = date(*(int(g) for g in m.groups()))
    except ValueError:
        raise ApiError("validation_error", "date is not a valid date")
    with STORE.lock:
        if rid not in STORE.rooms:
            raise ApiError("not_found", "room not found")
        ws = datetime(day.year, day.month, day.day, WORK_START_H, tzinfo=timezone.utc)
        we = datetime(day.year, day.month, day.day, WORK_END_H, tzinfo=timezone.utc)
        busy = []
        for _, _, bid in STORE.booking_order:
            b = STORE.bookings[bid]
            if b["room_id"] != rid:
                continue
            s, e = b["_start"], b["_end"]
            if e <= ws or s >= we:
                continue
            busy.append((max(s, ws), min(e, we)))
        busy.sort()
        free = []
        cur = ws
        for s, e in busy:
            if s > cur:
                free.append((cur, s))
            if e > cur:
                cur = e
        if cur < we:
            free.append((cur, we))
        free = [(s, e) for s, e in free if e - s >= timedelta(minutes=15)]
        return 200, {
            "date": ds,
            "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in free],
        }


# ---------- bookings ----------

def validate_booking(body):
    if not isinstance(body, dict):
        raise ApiError("validation_error", "body must be a JSON object")
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise ApiError("validation_error", "room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    title = body.get("title")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
        raise ApiError("validation_error", "title must be 1 to 100 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise ApiError("validation_error", "attendees must be an integer of at least 1")
    for dt, field in ((start, "start"), (end, "end")):
        if dt.minute % 15 or dt.second or dt.microsecond:
            raise ApiError("validation_error", f"{field} must be on a quarter hour")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "booking may be at most 8 hours")
    return room_id, start, end, title, attendees


def create_booking(body, idem_key):
    with STORE.lock:
        bcanon = canonical(body)
        if idem_key is not None and idem_key in STORE.idem:
            stored_body, status, resp = STORE.idem[idem_key]
            if stored_body != bcanon:
                raise ApiError("idempotency_mismatch", "Idempotency-Key reused with a different request")
            return status, dict(resp)

        room_id, start, end, title, attendees = validate_booking(body)
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for bid in STORE.bookings:
            other = STORE.bookings[bid]
            if other["room_id"] == room_id and other["_start"] < end and other["_end"] > start:
                raise ApiError("conflict", "room is already booked for that time")

        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        rec = {
            "id": bid,
            "room_id": room_id,
            "start": fmt_ts(start),
            "end": fmt_ts(end),
            "title": title,
            "attendees": attendees,
            "_start": start,
            "_end": end,
            "_seq": STORE.booking_seq,
        }
        STORE.bookings[bid] = rec
        STORE.booking_order.append((start, rec["_seq"], bid))
        STORE.booking_order.sort()
        out = booking_out(rec)
        if idem_key is not None:
            STORE.idem[idem_key] = (bcanon, 201, out)
        return 201, dict(out)


def get_booking(bid):
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", "booking not found")
        return 200, booking_out(b)


def delete_booking(bid):
    with STORE.lock:
        b = STORE.bookings.pop(bid, None)
        if b is None:
            raise ApiError("not_found", "booking not found")
        STORE.booking_order = [t for t in STORE.booking_order if t[2] != bid]
        return 204, None


def list_bookings(query):
    def one(name):
        vals = query.get(name)
        if vals is None:
            return None
        if len(vals) != 1:
            raise ApiError("validation_error", f"{name} must be given once")
        return vals[0]

    room_id = one("room_id")
    f_raw = one("from")
    t_raw = one("to")
    limit_raw = one("limit")
    cursor = one("cursor")

    f_dt = parse_ts(f_raw, "from") if f_raw is not None else None
    t_dt = parse_ts(t_raw, "to") if t_raw is not None else None
    if f_dt and t_dt and f_dt >= t_dt:
        raise ApiError("validation_error", "from must be before to")
    if limit_raw is None:
        limit = 20
    else:
        if not re.fullmatch(r"[0-9]+", limit_raw):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        limit = int(limit_raw)
        if not 1 <= limit <= 100:
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
    after = decode_cursor(cursor) if cursor is not None else None

    with STORE.lock:
        items = []
        for start, seq, bid in STORE.booking_order:
            b = STORE.bookings[bid]
            if room_id is not None and b["room_id"] != room_id:
                continue
            if f_dt is not None and not b["_end"] > f_dt:
                continue
            if t_dt is not None and not b["_start"] < t_dt:
                continue
            if after is not None and (b["_start"], b["_seq"]) <= after:
                continue
            items.append(b)
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last["start"], last["_seq"])
        return 200, {"bookings": [booking_out(b) for b in page], "next_cursor": next_cursor}


# ---------- routing ----------

def route(method, path, query, body_bytes, headers):
    parts = [p for p in path.split("/") if p != ""]
    if path != "/" and path.endswith("/"):
        raise ApiError("not_found", "no such endpoint")

    def allow(*methods):
        if method not in methods:
            raise ApiError("method_not_allowed", "method not allowed")

    def json_body():
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError("bad_json", "request body is not valid JSON")

    if len(parts) == 1 and parts[0] == "rooms":
        allow("GET", "POST")
        if method == "POST":
            return create_room(json_body())
        return list_rooms()

    if len(parts) == 2 and parts[0] == "rooms":
        allow("GET")
        return get_room(parts[1])

    if len(parts) == 3 and parts[0] == "rooms" and parts[2] == "availability":
        allow("GET")
        return availability(parts[1], query)

    if len(parts) == 1 and parts[0] == "bookings":
        allow("GET", "POST")
        if method == "POST":
            key = headers.get("Idempotency-Key")
            if key is not None and key.strip() == "":
                key = None
            return create_booking(json_body(), key)
        return list_bookings(query)

    if len(parts) == 2 and parts[0] == "bookings":
        allow("GET", "DELETE")
        if method == "DELETE":
            return delete_booking(parts[1])
        return get_booking(parts[1])

    raise ApiError("not_found", "no such endpoint")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep quiet
        pass

    def _handle(self):
        length = self.headers.get("Content-Length")
        body = b""
        if length:
            try:
                n = int(length)
            except ValueError:
                n = 0
            body = self.rfile.read(n) if n > 0 else b""
        try:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, keep_blank_values=True)
            status, payload = route(self.command, parts.path, query, body, self.headers)
        except ApiError as e:
            status = e.status
            payload = {"error": {"code": e.code, "message": e.message}}
        except Exception:  # defensive: never leak a traceback
            status = 500
            payload = {"error": {"code": "internal_error", "message": "internal error"}}
        self._send(status, payload)

    def _send(self, status, payload):
        if status == 204 or payload is None:
            data = b""
        else:
            data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
