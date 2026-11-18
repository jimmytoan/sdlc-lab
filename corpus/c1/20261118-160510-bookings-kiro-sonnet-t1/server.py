#!/usr/bin/env python3
"""Bookings: meeting room booking HTTP JSON API (standard library only, in-memory)."""
import argparse
import base64
import copy
import json
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STATUS = {
    "not_found": 404, "method_not_allowed": 405, "bad_json": 400,
    "validation_error": 422, "over_capacity": 422, "room_exists": 409,
    "conflict": 409, "idempotency_mismatch": 422, "internal_error": 500,
}


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = STATUS[code]
        self.message = message or code


def verr(msg):
    return ApiError("validation_error", msg)


TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z", re.ASCII)
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)


def parse_ts(s, field="timestamp"):
    if not isinstance(s, str):
        raise verr(f"{field} must be a string like 2026-03-02T09:30:00Z")
    m = TS_RE.fullmatch(s)
    if not m:
        raise verr(f"{field} must be of the form YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        raise verr(f"{field} is not a valid date/time")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return type(v) is int


def validate_room(body):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise verr("name must be a non-empty string of at most 50 characters")
    cap = body.get("capacity")
    if not is_int(cap) or cap < 1 or cap > 500:
        raise verr("capacity must be an integer between 1 and 500")
    return name, cap


def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise verr("room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    for f, d in (("start", start), ("end", end)):
        if d.minute not in (0, 15, 30, 45) or d.second != 0:
            raise verr(f"{f} must be on a quarter hour")
    if end <= start:
        raise verr("end must be after start")
    if end - start > timedelta(hours=8):
        raise verr("booking must be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise verr("title must be a non-empty string of at most 200 characters")
    att = body.get("attendees")
    if not is_int(att) or att < 1:
        raise verr("attendees must be an integer of at least 1")
    return room_id, start, end, title, att


def encode_cursor(start_str, num):
    return base64.urlsafe_b64encode(f"{start_str}|{num}".encode()).decode()


def decode_cursor(s):
    try:
        raw = base64.urlsafe_b64decode(s.encode("ascii"), ).decode("ascii")
        start_str, num = raw.split("|")
        if not re.fullmatch(r"[0-9]+", num, re.ASCII):
            raise ValueError
        return parse_ts(start_str), int(num)
    except Exception:
        raise verr("cursor is invalid")


def booking_public(b):
    return {k: b[k] for k in ("id", "room_id", "start", "end", "title", "attendees")}


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}
        self.room_names = set()
        self.bookings = {}
        self.idem = {}
        self.next_room = 1
        self.next_booking = 1

    # rooms
    def create_room(self, body):
        with self.lock:
            name, cap = validate_room(body)
            if name in self.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            rid = f"r{self.next_room}"
            self.next_room += 1
            room = {"id": rid, "name": name, "capacity": cap}
            self.rooms[rid] = room
            self.room_names.add(name)
            return 201, dict(room)

    def list_rooms(self):
        with self.lock:
            return 200, {"rooms": [dict(r) for r in self.rooms.values()]}

    def get_room(self, rid):
        with self.lock:
            r = self.rooms.get(rid)
            if r is None:
                raise ApiError("not_found", "room not found")
            return 200, dict(r)

    # bookings
    def create_booking(self, body, key):
        with self.lock:
            canon = None
            if key:
                canon = json.dumps(body, sort_keys=True)
                entry = self.idem.get(key)
                if entry is not None:
                    if entry[0] != canon:
                        raise ApiError("idempotency_mismatch",
                                       "Idempotency-Key was used with a different request")
                    return entry[1], copy.deepcopy(entry[2])
            room_id, start, end, title, att = validate_booking(body)
            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", "room not found")
            if att > room["capacity"]:
                raise ApiError("over_capacity", "attendees exceed room capacity")
            for b in self.bookings.values():
                if b["room_id"] == room_id and start < b["end_dt"] and b["start_dt"] < end:
                    raise ApiError("conflict", "the room is already booked for this time")
            num = self.next_booking
            self.next_booking += 1
            b = {"id": f"b{num}", "room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
                 "title": title, "attendees": att,
                 "start_dt": start, "end_dt": end, "num": num}
            self.bookings[b["id"]] = b
            resp = booking_public(b)
            if key:
                self.idem[key] = (canon, 201, copy.deepcopy(resp))
            return 201, resp

    def get_booking(self, bid):
        with self.lock:
            b = self.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            return 200, booking_public(b)

    def delete_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError("not_found", "booking not found")
            del self.bookings[bid]
            return 204, None

    def list_bookings(self, q):
        def first(name):
            v = q.get(name)
            return v[0] if v else None

        frm = first("from")
        to = first("to")
        frm_dt = parse_ts(frm, "from") if frm is not None else None
        to_dt = parse_ts(to, "to") if to is not None else None
        if frm_dt and to_dt and not frm_dt < to_dt:
            raise verr("from must be before to")
        lim = first("limit")
        if lim is None:
            limit = 20
        else:
            if not re.fullmatch(r"[0-9]+", lim, re.ASCII):
                raise verr("limit must be an integer between 1 and 100")
            limit = int(lim)
            if limit < 1 or limit > 100:
                raise verr("limit must be an integer between 1 and 100")
        cur = first("cursor")
        cur_key = None
        if cur is not None:
            d, n = decode_cursor(cur)
            cur_key = (d, n)
        room_id = first("room_id")
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm_dt and not b["end_dt"] > frm_dt:
                    continue
                if to_dt and not b["start_dt"] < to_dt:
                    continue
                if cur_key and not (b["start_dt"], b["num"]) > cur_key:
                    continue
                items.append(b)
            items.sort(key=lambda b: (b["start_dt"], b["num"]))
            page = items[:limit]
            nxt = None
            if len(items) > limit:
                last = page[-1]
                nxt = encode_cursor(last["start"], last["num"])
            return 200, {"bookings": [booking_public(b) for b in page], "next_cursor": nxt}

    def availability(self, rid, q):
        with self.lock:
            if rid not in self.rooms:
                raise ApiError("not_found", "room not found")
            date = (q.get("date") or [None])[0]
            m = DATE_RE.fullmatch(date) if date is not None else None
            if not m:
                raise verr("date must be of the form YYYY-MM-DD")
            try:
                day = datetime(*(int(g) for g in m.groups()))
            except ValueError:
                raise verr("date is not a real calendar date")
            ws = day + timedelta(hours=8)
            we = day + timedelta(hours=18)
            bs = sorted((b for b in self.bookings.values() if b["room_id"] == rid),
                        key=lambda b: b["start_dt"])
            free = []
            t = ws
            for b in bs:
                s = max(b["start_dt"], ws)
                e = min(b["end_dt"], we)
                if s >= e:
                    continue
                if s > t:
                    free.append({"start": fmt_ts(t), "end": fmt_ts(s)})
                t = max(t, e)
            if t < we:
                free.append({"start": fmt_ts(t), "end": fmt_ts(we)})
            return 200, {"date": date, "free": free}


STORE = Store()

ROUTES = [
    (re.compile(r"/rooms"), "rooms"),
    (re.compile(r"/rooms/([^/]+)"), "room"),
    (re.compile(r"/rooms/([^/]+)/availability"), "avail"),
    (re.compile(r"/bookings"), "bookings"),
    (re.compile(r"/bookings/([^/]+)"), "booking"),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self):
        cl = self.headers.get("Content-Length")
        try:
            n = int(cl) if cl is not None and cl.strip() != "" else 0
            if n < 0:
                raise ValueError
        except ValueError:
            raise ApiError("bad_json", "invalid Content-Length")
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError("bad_json", "request body must be valid JSON")
        if not isinstance(data, dict):
            raise ApiError("bad_json", "request body must be a JSON object")
        return data

    def _route(self, method):
        parts = urlsplit(self.path)
        path = parts.path
        q = parse_qs(parts.query, keep_blank_values=True)
        for rx, name in ROUTES:
            m = rx.fullmatch(path)
            if not m:
                continue
            args = m.groups()
            allowed = {"rooms": ("GET", "POST"), "room": ("GET",), "avail": ("GET",),
                       "bookings": ("GET", "POST"), "booking": ("GET", "DELETE")}[name]
            if method not in allowed:
                raise ApiError("method_not_allowed", f"method {method} not allowed here")
            if name == "rooms":
                return STORE.create_room(self._body()) if method == "POST" else STORE.list_rooms()
            if name == "room":
                return STORE.get_room(args[0])
            if name == "avail":
                return STORE.availability(args[0], q)
            if name == "bookings":
                if method == "POST":
                    body = self._body()
                    key = self.headers.get("Idempotency-Key")
                    return STORE.create_booking(body, key)
                return STORE.list_bookings(q)
            if method == "GET":
                return STORE.get_booking(args[0])
            return STORE.delete_booking(args[0])
        raise ApiError("not_found", "no such route")

    def _dispatch(self):
        method = self.command
        try:
            try:
                status, body = self._route(method)
            except ApiError as e:
                status, body = e.status, {"error": {"code": e.code, "message": e.message}}
            except Exception:
                status, body = 500, {"error": {"code": "internal_error",
                                               "message": "internal server error"}}
            self._send(status, body, method)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, status, body, method):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if data and method != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    srv = Server(("127.0.0.1", args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
