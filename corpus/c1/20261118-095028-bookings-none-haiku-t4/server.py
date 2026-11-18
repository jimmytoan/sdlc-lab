"""bookings: meeting room booking JSON API (in-memory, standard library only)."""

import argparse
import base64
import binascii
import json
import re
import sys
import threading
from datetime import datetime, date, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORK_START_H = 8
WORK_END_H = 18
MAX_DURATION = timedelta(hours=8)


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


def verr(msg):
    return ApiError("validation_error", msg)


# ---------------------------------------------------------------- helpers

def parse_ts(value, field):
    """Parse a strict UTC timestamp like 2026-03-02T09:30:00Z."""
    if not isinstance(value, str):
        raise verr(f"{field} must be a UTC timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise verr(f"{field} must be a UTC timestamp string like 2026-03-02T09:30:00Z")
    try:
        dt = datetime(*(int(g) for g in m.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid timestamp")
    return dt


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_query_ts(value, field):
    return parse_ts(value, field)


# ---------------------------------------------------------------- state

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_names = {}     # name -> id
        self.bookings = {}       # id -> dict (with datetime start/end kept in _start/_end)
        self.room_seq = 0
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body)

    # ---- rooms
    def create_room(self, body):
        if not isinstance(body, dict):
            raise verr("body must be a JSON object")
        name = body.get("name")
        capacity = body.get("capacity")
        if not isinstance(name, str) or not name.strip():
            raise verr("name must be a non-empty string")
        if len(name) > 50:
            raise verr("name must be at most 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise verr("capacity must be an integer from 1 to 500")
        if name in self.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        self.room_seq += 1
        rid = f"r{self.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        self.rooms[rid] = room
        self.room_names[name] = rid
        return 201, dict(room)

    def get_room(self, rid):
        room = self.rooms.get(rid)
        if room is None:
            raise ApiError("not_found", "room not found")
        return dict(room)

    def list_rooms(self):
        return {"rooms": [dict(r) for r in self.rooms.values()]}

    # ---- bookings
    def _public(self, b):
        return {
            "id": b["id"],
            "room_id": b["room_id"],
            "start": fmt_ts(b["_start"]),
            "end": fmt_ts(b["_end"]),
            "title": b["title"],
            "attendees": b["attendees"],
        }

    def create_booking(self, body, idem_key):
        if not isinstance(body, dict):
            raise verr("body must be a JSON object")
        room_id = body.get("room_id")
        title = body.get("title")
        attendees = body.get("attendees")
        start = parse_ts(body.get("start"), "start")
        end = parse_ts(body.get("end"), "end")
        if not isinstance(room_id, str):
            raise verr("room_id must be a string")
        if not isinstance(title, str):
            raise verr("title must be a string")
        if not is_int(attendees) or attendees < 1:
            raise verr("attendees must be a positive integer")
        for dt, field in ((start, "start"), (end, "end")):
            if dt.minute % 15 != 0 or dt.second != 0:
                raise verr(f"{field} must be on a quarter hour")
        if end <= start:
            raise verr("end must be after start")
        if end - start > MAX_DURATION:
            raise verr("booking may be at most 8 hours long")

        fingerprint = json.dumps(
            {"room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
             "title": title, "attendees": attendees},
            sort_keys=True)
        if idem_key is not None and idem_key in self.idem:
            fp, status, resp = self.idem[idem_key]
            if fp != fingerprint:
                raise ApiError("idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            return status, dict(resp)

        room = self.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for other in self.bookings.values():
            if other["room_id"] == room_id and other["_start"] < end and start < other["_end"]:
                raise ApiError("conflict", "room is already booked for this time")

        self.booking_seq += 1
        bid = f"b{self.booking_seq}"
        b = {"id": bid, "room_id": room_id, "title": title, "attendees": attendees,
             "_start": start, "_end": end}
        self.bookings[bid] = b
        resp = self._public(b)
        if idem_key is not None:
            self.idem[idem_key] = (fingerprint, 201, dict(resp))
        return 201, resp

    def get_booking(self, bid):
        b = self.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", "booking not found")
        return self._public(b)

    def delete_booking(self, bid):
        if bid not in self.bookings:
            raise ApiError("not_found", "booking not found")
        del self.bookings[bid]

    def list_bookings(self, query):
        room_id = query.get("room_id")
        frm = query.get("from")
        to = query.get("to")
        limit = query.get("limit")
        cursor = query.get("cursor")

        if room_id is not None:
            room_id = room_id[-1]
        frm_dt = parse_query_ts(frm[-1], "from") if frm is not None else None
        to_dt = parse_query_ts(to[-1], "to") if to is not None else None
        if frm_dt and to_dt and frm_dt >= to_dt:
            raise verr("from must be before to")
        if limit is not None:
            try:
                limit_n = int(limit[-1])
            except ValueError:
                raise verr("limit must be an integer from 1 to 100")
            if not (1 <= limit_n <= 100):
                raise verr("limit must be an integer from 1 to 100")
        else:
            limit_n = 20

        after = None
        if cursor is not None and cursor[-1] != "":
            after = decode_cursor(cursor[-1])

        items = []
        for b in self.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm_dt is not None and not (b["_end"] > frm_dt):
                continue
            if to_dt is not None and not (b["_start"] < to_dt):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_start"], b["id"]))
        if after is not None:
            items = [b for b in items if (b["_start"], _id_num(b["id"])) > after]

        page = items[:limit_n]
        next_cursor = None
        if len(items) > limit_n and page:
            last = page[-1]
            next_cursor = encode_cursor(last["_start"], last["id"])
        return {"bookings": [self._public(b) for b in page], "next_cursor": next_cursor}

    # ---- availability
    def availability(self, rid, date_str):
        if not isinstance(date_str, str) or not DATE_RE.match(date_str):
            raise verr("date must be YYYY-MM-DD")
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            raise verr("date must be a valid YYYY-MM-DD date")
        if rid not in self.rooms:
            raise ApiError("not_found", "room not found")
        day_start = datetime(d.year, d.month, d.day, WORK_START_H, tzinfo=timezone.utc)
        day_end = datetime(d.year, d.month, d.day, WORK_END_H, tzinfo=timezone.utc)
        busy = sorted(
            (max(b["_start"], day_start), min(b["_end"], day_end))
            for b in self.bookings.values()
            if b["room_id"] == rid and b["_start"] < day_end and day_start < b["_end"]
        )
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append((cur, s))
            if e > cur:
                cur = e
        if cur < day_end:
            free.append((cur, day_end))
        return {"date": date_str,
                "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in free]}


def _id_num(bid):
    return int(bid[1:])


def encode_cursor(start, bid):
    raw = f"{fmt_ts(start)}|{bid}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token):
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode()
        ts_part, bid = raw.split("|", 1)
        start = parse_ts(ts_part, "cursor")
        if not re.match(r"^b\d+$", bid):
            raise ValueError
        return (start, _id_num(bid))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise verr("cursor is invalid")


# ---------------------------------------------------------------- routing

STORE = Store()


def route(method, path, query, body_fn, headers):
    """Returns (status, body or None)."""
    parts = [p for p in path.split("/") if p]
    n = len(parts)

    def allow(*methods):
        if method not in methods:
            raise ApiError("method_not_allowed", "method not allowed")

    if n == 0:
        raise ApiError("not_found", "not found")

    if parts[0] == "rooms":
        if n == 1:
            allow("GET", "POST")
            if method == "GET":
                return 200, STORE.list_rooms()
            with STORE.lock:
                return STORE.create_room(body_fn())
        if n == 2:
            allow("GET")
            with STORE.lock:
                return 200, STORE.get_room(parts[1])
        if n == 3 and parts[2] == "availability":
            allow("GET")
            date_vals = query.get("date")
            date_str = date_vals[-1] if date_vals else None
            with STORE.lock:
                return 200, STORE.availability(parts[1], date_str)

    elif parts[0] == "bookings":
        if n == 1:
            allow("GET", "POST")
            if method == "GET":
                with STORE.lock:
                    return 200, STORE.list_bookings(query)
            key = headers.get("Idempotency-Key")
            if key is not None and key.strip() == "":
                key = None
            with STORE.lock:
                return STORE.create_booking(body_fn(), key)
        if n == 2:
            allow("GET", "DELETE")
            if method == "GET":
                with STORE.lock:
                    return 200, STORE.get_booking(parts[1])
            with STORE.lock:
                STORE.delete_booking(parts[1])
            return 204, None

    raise ApiError("not_found", "not found")


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep output quiet
        pass

    def _send(self, status, body):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)

        def body_fn():
            try:
                text = raw.decode("utf-8")
                return json.loads(text)
            except (UnicodeDecodeError, ValueError):
                raise ApiError("bad_json", "request body is not valid JSON")

        try:
            status, body = route(method, url.path, query, body_fn, self.headers)
            self._send(status, body)
        except ApiError as err:
            self._send_error(err)
        except Exception:  # pragma: no cover - safety net
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})

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
    p = argparse.ArgumentParser(description="bookings API server")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
