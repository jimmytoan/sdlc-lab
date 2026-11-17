#!/usr/bin/env python3
"""bookings: in-memory meeting room booking JSON API (standard library only)."""
import argparse
import base64
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")
MAX_DURATION = dt.timedelta(hours=8)
MIN_FREE = dt.timedelta(minutes=15)

LOCK = threading.Lock()
rooms = {}          # id -> room dict (insertion ordered)
room_names = set()  # casefolded trimmed names
bookings = {}       # id -> booking dict (insertion ordered)
idem_store = {}     # Idempotency-Key -> (request body, response payload)
counters = {"r": 0, "b": 0}


class ApiError(Exception):
    def __init__(self, status, code, message, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def verr(msg="invalid request"):
    return ApiError(422, "validation_error", msg)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_ts(s):
    """Strict canonical timestamp parse. Returns naive datetime (UTC) or None."""
    if not isinstance(s, str) or not TS_RE.fullmatch(s):
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def fmt_ts(d):
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"


def seq_of(bid):
    return int(bid[1:])


def encode_cursor(start, seq):
    raw = f"{start}|{seq}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        raw = base64.urlsafe_b64decode((c + pad).encode("ascii")).decode("utf-8")
        s, sq = raw.split("|")
        if parse_ts(s) is None or not INT_RE.fullmatch(sq):
            return None
        return (s, int(sq))
    except Exception:
        return None


def json_equal(a, b):
    """Equality of parsed JSON values where booleans never equal numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def validate_times(start_s, end_s):
    """Validates start/end timestamps. Returns (start, end) datetimes."""
    st = parse_ts(start_s)
    en = parse_ts(end_s)
    if st is None or en is None:
        raise verr("start and end must be timestamps like 2026-03-02T09:00:00Z")
    for d in (st, en):
        if d.minute not in (0, 15, 30, 45) or d.second != 0:
            raise verr("start and end must be on quarter hours with zero seconds")
    if not en > st:
        raise verr("end must be after start")
    if en - st > MAX_DURATION:
        raise verr("booking may be at most 8 hours long")
    return st, en


def validate_title_attendees(title, attendees):
    """Validates title and attendees. Returns the trimmed title."""
    if not isinstance(title, str):
        raise verr("title must be a string")
    t = title.strip()
    if not (1 <= len(t) <= 100):
        raise verr("title must be 1 to 100 characters")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be an integer of at least 1")
    return t


def has_conflict(room_id, st, en, buf, exclude=None):
    """True if [st,en) conflicts with another booking of the room, including the buffer."""
    gap = dt.timedelta(minutes=buf)
    for other in bookings.values():
        if other["room_id"] != room_id or other["id"] == exclude:
            continue
        os_ = parse_ts(other["start"])
        oe = parse_ts(other["end"])
        if st < oe + gap and os_ < en + gap:
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    # ----- plumbing -------------------------------------------------------
    def _send(self, status, payload=None, extra=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, code, message, headers=None):
        self._send(status, {"error": {"code": code, "message": message}}, headers)

    def _dispatch(self, method):
        try:
            self._route(method)
        except ApiError as e:
            self._error(e.status, e.code, e.message, e.headers)
        except Exception:
            self._error(500, "internal_error", "internal server error")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "bad_json", "request body is not valid JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "bad_json", "request body must be a JSON object")
        return obj

    def _route(self, method):
        u = urlsplit(self.path)
        parts = [p for p in u.path.split("/") if p]
        routes = None
        if parts == ["rooms"]:
            routes = {
                "GET": lambda: self.list_rooms(),
                "POST": lambda: self.create_room(),
            }
        elif len(parts) == 2 and parts[0] == "rooms":
            rid = parts[1]
            routes = {"GET": lambda: self.get_room(rid)}
        elif len(parts) == 3 and parts[0] == "rooms" and parts[2] == "availability":
            rid = parts[1]
            routes = {"GET": lambda: self.availability(rid, u.query)}
        elif parts == ["bookings"]:
            routes = {
                "GET": lambda: self.list_bookings(u.query),
                "POST": lambda: self.create_booking(),
            }
        elif len(parts) == 2 and parts[0] == "bookings":
            bid = parts[1]
            routes = {
                "GET": lambda: self.get_booking(bid),
                "PATCH": lambda: self.patch_booking(bid),
                "DELETE": lambda: self.delete_booking(bid),
            }
        if routes is None:
            raise ApiError(404, "not_found", "no such path")
        fn = routes.get(method)
        if fn is None:
            raise ApiError(405, "method_not_allowed", "method not allowed",
                           {"Allow": ", ".join(routes)})
        status, payload, extra = fn()
        self._send(status, payload, extra)

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

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def do_HEAD(self):
        self._dispatch("HEAD")

    # ----- rooms ----------------------------------------------------------
    def create_room(self):
        body = self._read_json()
        with LOCK:
            name = body.get("name")
            capacity = body.get("capacity")
            if "buffer_minutes" in body:
                buffer_minutes = body["buffer_minutes"]
                if not is_int(buffer_minutes) or not (0 <= buffer_minutes <= 60):
                    raise verr("buffer_minutes must be an integer from 0 to 60")
            else:
                buffer_minutes = 0
            if not isinstance(name, str):
                raise verr("name must be a string")
            trimmed = name.strip()
            if not (1 <= len(trimmed) <= 50):
                raise verr("name must be 1 to 50 characters")
            if not is_int(capacity) or not (1 <= capacity <= 500):
                raise verr("capacity must be an integer from 1 to 500")
            key = trimmed.casefold()
            if key in room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            counters["r"] += 1
            rid = f"r{counters['r']}"
            room = {"id": rid, "name": trimmed, "capacity": capacity,
                    "buffer_minutes": buffer_minutes}
            rooms[rid] = room
            room_names.add(key)
            return 201, dict(room), None

    def list_rooms(self):
        with LOCK:
            return 200, {"rooms": [dict(r) for r in rooms.values()]}, None

    def get_room(self, rid):
        with LOCK:
            room = rooms.get(rid)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            return 200, dict(room), None

    def availability(self, rid, query):
        q = parse_qs(query, keep_blank_values=True)
        with LOCK:
            room = rooms.get(rid)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            date_s = q["date"][0] if "date" in q else None
            if date_s is None or not DATE_RE.fullmatch(date_s):
                raise verr("date must be YYYY-MM-DD")
            try:
                d = dt.date(int(date_s[0:4]), int(date_s[5:7]), int(date_s[8:10]))
            except ValueError:
                raise verr("date is not a real calendar date")
            ws = dt.datetime(d.year, d.month, d.day, 8, 0, 0)
            we = dt.datetime(d.year, d.month, d.day, 18, 0, 0)
            buf = dt.timedelta(minutes=room["buffer_minutes"])
            intervals = []
            for b in bookings.values():
                if b["room_id"] != rid:
                    continue
                bs = parse_ts(b["start"]) - buf
                be = parse_ts(b["end"]) + buf
                if be <= ws or bs >= we:
                    continue
                intervals.append((max(bs, ws), min(be, we)))
            intervals.sort()
            gaps = []
            cur = ws
            for s, e in intervals:
                if s > cur:
                    gaps.append((cur, s))
                if e > cur:
                    cur = e
            if cur < we:
                gaps.append((cur, we))
            free = [{"start": fmt_ts(s), "end": fmt_ts(e)}
                    for s, e in gaps if e - s >= MIN_FREE]
            return 200, {"date": date_s, "free": free}, None

    # ----- bookings -------------------------------------------------------
    def create_booking(self):
        body = self._read_json()
        idem = self.headers.get("Idempotency-Key")
        if idem == "":
            idem = None
        with LOCK:
            if idem is not None and idem in idem_store:
                stored_body, stored_resp = idem_store[idem]
                if json_equal(stored_body, body):
                    return 201, dict(stored_resp), None
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was already used with a different request body")

            room_id = body.get("room_id")
            start_s = body.get("start")
            end_s = body.get("end")
            title = body.get("title")
            attendees = body.get("attendees")
            if not isinstance(room_id, str):
                raise verr("room_id must be a string")
            st, en = validate_times(start_s, end_s)
            t = validate_title_attendees(title, attendees)

            room = rooms.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "attendees exceed room capacity")
            if has_conflict(room_id, st, en, room["buffer_minutes"]):
                raise ApiError(409, "conflict", "room is already booked for this time")

            counters["b"] += 1
            bid = f"b{counters['b']}"
            booking = {
                "id": bid,
                "room_id": room_id,
                "start": start_s,
                "end": end_s,
                "title": t,
                "attendees": attendees,
            }
            bookings[bid] = booking
            resp = dict(booking)
            if idem is not None:
                idem_store[idem] = (body, dict(resp))
            return 201, resp, None

    def patch_booking(self, bid):
        body = self._read_json()
        with LOCK:
            b = bookings.get(bid)
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            room = rooms[b["room_id"]]
            start_s = body["start"] if "start" in body else b["start"]
            end_s = body["end"] if "end" in body else b["end"]
            title = body["title"] if "title" in body else b["title"]
            attendees = body["attendees"] if "attendees" in body else b["attendees"]
            st, en = validate_times(start_s, end_s)
            t = validate_title_attendees(title, attendees)
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "attendees exceed room capacity")
            if has_conflict(room["id"], st, en, room["buffer_minutes"], exclude=bid):
                raise ApiError(409, "conflict", "room is already booked for this time")
            b["start"] = start_s
            b["end"] = end_s
            b["title"] = t
            b["attendees"] = attendees
            return 200, dict(b), None

    def list_bookings(self, query):
        q = parse_qs(query, keep_blank_values=True)

        def first(name):
            v = q.get(name)
            return v[0] if v else None

        room_id = first("room_id")
        frm = first("from")
        to = first("to")
        if frm is not None and parse_ts(frm) is None:
            raise verr("from must be a timestamp like 2026-03-02T09:00:00Z")
        if to is not None and parse_ts(to) is None:
            raise verr("to must be a timestamp like 2026-03-02T09:00:00Z")
        limit = 20
        limit_s = first("limit")
        if limit_s is not None:
            if not INT_RE.fullmatch(limit_s) or not (1 <= int(limit_s) <= 100):
                raise verr("limit must be an integer from 1 to 100")
            limit = int(limit_s)
        cur = None
        cursor_s = first("cursor")
        if cursor_s is not None:
            cur = decode_cursor(cursor_s)
            if cur is None:
                raise verr("invalid cursor")

        with LOCK:
            items = []
            for b in bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm is not None and not (b["end"] > frm):
                    continue
                if to is not None and not (b["start"] < to):
                    continue
                items.append(b)
            items.sort(key=lambda b: (b["start"], seq_of(b["id"])))
            total = len(items)
            if cur is not None:
                items = [b for b in items if (b["start"], seq_of(b["id"])) > cur]
            page = items[:limit]
            next_cursor = None
            if len(items) > limit and page:
                last = page[-1]
                next_cursor = encode_cursor(last["start"], seq_of(last["id"]))
            return 200, {"bookings": [dict(b) for b in page], "next_cursor": next_cursor,
                         "total": total}, None

    def get_booking(self, bid):
        with LOCK:
            b = bookings.get(bid)
            if b is None:
                raise ApiError(404, "not_found", "booking not found")
            return 200, dict(b), None

    def delete_booking(self, bid):
        with LOCK:
            if bid not in bookings:
                raise ApiError(404, "not_found", "booking not found")
            del bookings[bid]
            return 204, None, None


def main():
    ap = argparse.ArgumentParser(description="bookings meeting room API")
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
