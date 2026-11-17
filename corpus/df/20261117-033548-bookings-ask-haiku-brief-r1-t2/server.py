"""bookings: meeting room booking JSON API (standard library only).

Run with: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

UTC = timezone.utc
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
DAY_START_HOUR = 8
DAY_END_HOUR = 18
MAX_BOOKING = timedelta(hours=8)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def validation(message):
    return ApiError(422, "validation_error", message)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    """Strict form YYYY-MM-DDTHH:MM:SSZ. Returns an aware datetime or None."""
    if not isinstance(value, str):
        return None
    m = TS_RE.match(value)
    if not m:
        return None
    try:
        dt = datetime(*[int(g) for g in m.groups()], tzinfo=UTC)
    except ValueError:
        return None
    return dt


def is_quarter(dt):
    return dt.minute in (0, 15, 30, 45) and dt.second == 0 and dt.microsecond == 0


def parse_date(value):
    if not isinstance(value, str):
        return None
    m = DATE_RE.match(value)
    if not m:
        return None
    try:
        return date(*[int(g) for g in m.groups()])
    except ValueError:
        return None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> dict(id, name, capacity)
        self.room_names = set()  # normalized names
        self.room_seq = 0
        self.bookings = {}       # id -> dict (public fields + _start/_end/_seq)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, response body)

    # ---- rooms -------------------------------------------------------
    def create_room(self, body):
        name = body.get("name")
        capacity = body.get("capacity")
        if not isinstance(name, str):
            raise validation("name must be a string")
        clean = name.strip()
        if not (1 <= len(clean) <= 50):
            raise validation("name must be 1 to 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise validation("capacity must be an integer from 1 to 500")
        norm = clean.casefold()
        with self.lock:
            if norm in self.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            self.room_seq += 1
            rid = f"r{self.room_seq}"
            room = {"id": rid, "name": clean, "capacity": capacity}
            self.rooms[rid] = room
            self.room_names.add(norm)
            return dict(room)

    def list_rooms(self):
        with self.lock:
            return [dict(r) for r in self.rooms.values()]

    def get_room(self, rid):
        with self.lock:
            room = self.rooms.get(rid)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            return dict(room)

    # ---- bookings ----------------------------------------------------
    @staticmethod
    def public(b):
        return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}

    def create_booking(self, body, idem_key):
        fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
        with self.lock:
            if idem_key is not None and idem_key in self.idem:
                stored_fp, stored_resp = self.idem[idem_key]
                if stored_fp == fingerprint:
                    return dict(stored_resp)
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")

        # validation (no state touched)
        room_id = body.get("room_id")
        start_raw = body.get("start")
        end_raw = body.get("end")
        title = body.get("title")
        attendees = body.get("attendees")
        if not isinstance(room_id, str) or not room_id:
            raise validation("room_id must be a non-empty string")
        start = parse_ts(start_raw)
        if start is None:
            raise validation("start must be a UTC timestamp like 2026-03-02T09:00:00Z")
        end = parse_ts(end_raw)
        if end is None:
            raise validation("end must be a UTC timestamp like 2026-03-02T10:00:00Z")
        if not is_quarter(start) or not is_quarter(end):
            raise validation("start and end must fall on quarter hours")
        if end <= start:
            raise validation("end must be after start")
        if end - start > MAX_BOOKING:
            raise validation("booking may be at most 8 hours long")
        if not isinstance(title, str):
            raise validation("title must be a string")
        if not (1 <= len(title.strip()) <= 100):
            raise validation("title must be 1 to 100 characters")
        if not is_int(attendees) or attendees < 1:
            raise validation("attendees must be an integer of at least 1")

        with self.lock:
            # re-check idempotency under the lock (concurrent identical retries)
            if idem_key is not None and idem_key in self.idem:
                stored_fp, stored_resp = self.idem[idem_key]
                if stored_fp == fingerprint:
                    return dict(stored_resp)
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used for a different request")
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "more attendees than the room holds")
            for other in self.bookings.values():
                if other["room_id"] == room_id and other["_start"] < end and start < other["_end"]:
                    raise ApiError(409, "conflict", "the room is already booked for this time")
            self.booking_seq += 1
            bid = f"b{self.booking_seq}"
            rec = {
                "id": bid,
                "room_id": room_id,
                "start": fmt_ts(start),
                "end": fmt_ts(end),
                "title": title,
                "attendees": attendees,
                "_start": start,
                "_end": end,
                "_seq": self.booking_seq,
            }
            self.bookings[bid] = rec
            resp = self.public(rec)
            if idem_key is not None:
                self.idem[idem_key] = (fingerprint, dict(resp))
            return dict(resp)

    def get_booking(self, bid):
        with self.lock:
            rec = self.bookings.get(bid)
            if rec is None:
                raise ApiError(404, "not_found", "booking not found")
            return self.public(rec)

    def delete_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError(404, "not_found", "booking not found")
            del self.bookings[bid]

    def list_bookings(self, query):
        def single(name):
            vals = query.get(name)
            return vals[0] if vals else None

        room_id = single("room_id")
        from_raw = single("from")
        to_raw = single("to")
        limit_raw = single("limit")
        cursor_raw = single("cursor")

        date_from = None
        date_to = None
        if from_raw is not None:
            date_from = parse_ts(from_raw)
            if date_from is None:
                raise validation("from must be a UTC timestamp like 2026-03-02T09:00:00Z")
        if to_raw is not None:
            date_to = parse_ts(to_raw)
            if date_to is None:
                raise validation("to must be a UTC timestamp like 2026-03-02T09:00:00Z")
        limit = 20
        if limit_raw is not None:
            if not re.fullmatch(r"[0-9]+", limit_raw):
                raise validation("limit must be an integer from 1 to 100")
            limit = int(limit_raw)
            if not (1 <= limit <= 100):
                raise validation("limit must be an integer from 1 to 100")
        after = None
        if cursor_raw is not None:
            after = decode_cursor(cursor_raw)

        with self.lock:
            items = []
            for rec in self.bookings.values():
                if room_id is not None and rec["room_id"] != room_id:
                    continue
                if date_from is not None and not (rec["_end"] > date_from):
                    continue
                if date_to is not None and not (rec["_start"] < date_to):
                    continue
                items.append(rec)
            items.sort(key=lambda r: (r["_start"], r["_seq"]))
            if after is not None:
                items = [r for r in items if (fmt_ts(r["_start"]), r["_seq"]) > after]
            page = items[:limit]
            more = len(items) > limit
            next_cursor = None
            if more and page:
                last = page[-1]
                next_cursor = encode_cursor(fmt_ts(last["_start"]), last["_seq"])
            return {"bookings": [self.public(r) for r in page], "next_cursor": next_cursor}

    # ---- availability ------------------------------------------------
    def availability(self, rid, date_raw):
        with self.lock:
            if rid not in self.rooms:
                raise ApiError(404, "not_found", "room not found")
        day = parse_date(date_raw)
        if day is None:
            raise validation("date must be YYYY-MM-DD")
        day_start = datetime(day.year, day.month, day.day, DAY_START_HOUR, tzinfo=UTC)
        day_end = datetime(day.year, day.month, day.day, DAY_END_HOUR, tzinfo=UTC)
        with self.lock:
            spans = []
            for rec in self.bookings.values():
                if rec["room_id"] != rid:
                    continue
                s = max(rec["_start"], day_start)
                e = min(rec["_end"], day_end)
                if s < e:
                    spans.append((s, e))
        spans.sort()
        free = []
        cur = day_start
        for s, e in spans:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            if e > cur:
                cur = e
        if cur < day_end:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
        return {"date": date_raw, "free": free}


def encode_cursor(start_str, seq):
    raw = json.dumps([start_str, seq], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
        start_str, seq = data
        if not isinstance(start_str, str) or parse_ts(start_str) is None or not is_int(seq):
            raise ValueError
        return (start_str, seq)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        raise validation("cursor is malformed")


STORE = Store()

def match_route(segs):
    """Returns (handler_key, args, allowed_methods) or None when no route matches."""
    if segs == ["rooms"]:
        return "rooms", (), ("GET", "POST")
    if len(segs) == 2 and segs[0] == "rooms":
        return "room", (segs[1],), ("GET",)
    if len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
        return "availability", (segs[1],), ("GET",)
    if segs == ["bookings"]:
        return "bookings", (), ("GET", "POST")
    if len(segs) == 2 and segs[0] == "bookings":
        return "booking", (segs[1],), ("GET", "DELETE")
    return None


def parse_body(raw):
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "bad_json", "request body must be a valid JSON object")
    if not isinstance(obj, dict):
        raise ApiError(400, "bad_json", "request body must be a JSON object")
    return obj


def handle(method, target, headers, raw):
    """Returns (status, body_obj_or_None)."""
    parts = urlsplit(target)
    path = parts.path
    segs = path.strip("/").split("/") if path.strip("/") else []
    route = match_route(segs)
    if route is None:
        raise ApiError(404, "not_found", "no such endpoint")
    key, args, allowed = route
    if method not in allowed:
        raise ApiError(405, "method_not_allowed", f"{method} not allowed here")
    query = parse_qs(parts.query, keep_blank_values=True)

    if key == "rooms":
        if method == "GET":
            return 200, {"rooms": STORE.list_rooms()}
        return 201, STORE.create_room(parse_body(raw))
    if key == "room":
        return 200, STORE.get_room(args[0])
    if key == "availability":
        date_vals = query.get("date")
        date_raw = date_vals[0] if date_vals else None
        if date_raw is None:
            with STORE.lock:
                exists = args[0] in STORE.rooms
            if not exists:
                raise ApiError(404, "not_found", "room not found")
            raise validation("date query parameter is required (YYYY-MM-DD)")
        return 200, STORE.availability(args[0], date_raw)
    if key == "bookings":
        if method == "GET":
            return 200, STORE.list_bookings(query)
        idem = headers.get("Idempotency-Key")
        if idem is not None and idem.strip() == "":
            idem = None
        return 201, STORE.create_booking(parse_body(raw), idem)
    if key == "booking":
        if method == "GET":
            return 200, STORE.get_booking(args[0])
        STORE.delete_booking(args[0])
        return 204, None
    raise ApiError(404, "not_found", "no such endpoint")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def _run(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            status, body = handle(method, self.path, self.headers, raw)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:  # pragma: no cover - defensive
            status, body = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        if status == 204 or body is None:
            payload = b""
        else:
            payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_DELETE(self):
        self._run("DELETE")

    def do_PUT(self):
        self._run("PUT")

    def do_PATCH(self):
        self._run("PATCH")


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
