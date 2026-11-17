"""bookings: meeting room booking JSON API (in-memory, standard library only).

Run: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import datetime as dt
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
DAY_START_H = 8
DAY_END_H = 18
MAX_DURATION = dt.timedelta(hours=8)
QUARTER = dt.timedelta(minutes=15)


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


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str):
        raise verr(f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise verr(f"{field} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        return dt.datetime(*(int(g) for g in m.groups()), tzinfo=dt.timezone.utc)
    except ValueError:
        raise verr(f"{field} is not a valid date/time")


def parse_date(value):
    if not isinstance(value, str):
        raise verr("date must be YYYY-MM-DD")
    m = DATE_RE.match(value)
    if not m:
        raise verr("date must be YYYY-MM-DD")
    try:
        return dt.date(*(int(g) for g in m.groups()))
    except ValueError:
        raise verr("date is not a valid calendar date")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = {}     # name -> id
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict (internal datetimes kept separately)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, response dict)

    # ----- rooms -----
    def create_room(self, body):
        if not isinstance(body, dict):
            raise verr("body must be a JSON object")
        name = body.get("name")
        cap = body.get("capacity")
        if not isinstance(name, str) or not (1 <= len(name) <= 50) or not name.strip():
            raise verr("name must be a non-empty string of at most 50 characters")
        if not is_int(cap) or not (1 <= cap <= 500):
            raise verr("capacity must be an integer from 1 to 500")
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            self.room_seq += 1
            rid = f"r{self.room_seq}"
            room = {"id": rid, "name": name, "capacity": cap}
            self.rooms[rid] = room
            self.room_names[name] = rid
            return dict(room)

    def list_rooms(self):
        with self.lock:
            return [dict(r) for r in self.rooms.values()]

    def get_room(self, rid):
        with self.lock:
            room = self.rooms.get(rid)
            if room is None:
                raise ApiError("not_found", "room not found")
            return dict(room)

    # ----- bookings -----
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
        fingerprint = json.dumps(body, sort_keys=True)
        with self.lock:
            if idem_key is not None and idem_key in self.idem:
                fp, resp = self.idem[idem_key]
                if fp != fingerprint:
                    raise ApiError("idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return resp["status"], dict(resp["body"])

            room_id = body.get("room_id")
            title = body.get("title")
            attendees = body.get("attendees")
            if not isinstance(room_id, str) or not room_id:
                raise verr("room_id must be a string")
            start = parse_ts(body.get("start"), "start")
            end = parse_ts(body.get("end"), "end")
            if not isinstance(title, str) or not (1 <= len(title) <= 200):
                raise verr("title must be a string of 1 to 200 characters")
            if not is_int(attendees) or attendees < 1:
                raise verr("attendees must be a positive integer")
            for t, field in ((start, "start"), (end, "end")):
                if t.minute % 15 != 0 or t.second != 0:
                    raise verr(f"{field} must be on a quarter hour")
            if end <= start:
                raise verr("end must be after start")
            if end - start > MAX_DURATION:
                raise verr("booking may be at most 8 hours long")

            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            for other in self.bookings.values():
                if other["room_id"] == room_id and start < other["_end"] and other["_start"] < end:
                    raise ApiError("conflict", "room is already booked for this time")

            self.booking_seq += 1
            bid = f"b{self.booking_seq}"
            b = {"id": bid, "room_id": room_id, "_start": start, "_end": end,
                 "title": title, "attendees": attendees}
            self.bookings[bid] = b
            pub = self._public(b)
            if idem_key is not None:
                self.idem[idem_key] = (fingerprint, {"status": 201, "body": pub})
            return 201, dict(pub)

    def get_booking(self, bid):
        with self.lock:
            b = self.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            return self._public(b)

    def delete_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError("not_found", "booking not found")
            del self.bookings[bid]

    def list_bookings(self, query):
        room_id = query.get("room_id")
        frm = query.get("from")
        to = query.get("to")
        limit = 20
        cursor = 0
        if "limit" in query:
            try:
                limit = int(query["limit"])
            except ValueError:
                raise verr("limit must be an integer from 1 to 100")
            if not (1 <= limit <= 100):
                raise verr("limit must be an integer from 1 to 100")
        if "cursor" in query:
            try:
                cursor = int(query["cursor"])
            except ValueError:
                raise verr("invalid cursor")
            if cursor < 0:
                raise verr("invalid cursor")
        f_ts = parse_ts(frm, "from") if frm is not None else None
        t_ts = parse_ts(to, "to") if to is not None else None
        if f_ts and t_ts and f_ts >= t_ts:
            raise verr("from must be before to")
        with self.lock:
            items = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if f_ts is not None and not (b["_end"] > f_ts):
                    continue
                if t_ts is not None and not (b["_start"] < t_ts):
                    continue
                items.append(b)
            items.sort(key=lambda b: (b["_start"], b["_end"], b["id"]))
            page = items[cursor:cursor + limit]
            nxt = cursor + limit if cursor + limit < len(items) else None
            return {"bookings": [self._public(b) for b in page],
                    "next_cursor": str(nxt) if nxt is not None else None}

    def availability(self, rid, date_str):
        d = parse_date(date_str)
        with self.lock:
            if rid not in self.rooms:
                raise ApiError("not_found", "room not found")
            day_start = dt.datetime(d.year, d.month, d.day, DAY_START_H, tzinfo=dt.timezone.utc)
            day_end = dt.datetime(d.year, d.month, d.day, DAY_END_H, tzinfo=dt.timezone.utc)
            busy = sorted(
                (b["_start"], b["_end"]) for b in self.bookings.values()
                if b["room_id"] == rid and b["_start"] < day_end and b["_end"] > day_start
            )
            free = []
            cur = day_start
            for s, e in busy:
                s = max(s, day_start)
                e = min(e, day_end)
                if s > cur:
                    free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
                cur = max(cur, e)
            if cur < day_end:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
            return {"date": d.isoformat(), "free": free}


STORE = Store()


def route(method, path, query, body_fn, headers):
    """Returns (status, body_or_None). Raises ApiError."""
    parts = [p for p in path.split("/") if p]
    n = len(parts)

    def allow(*methods):
        if method not in methods:
            raise ApiError("method_not_allowed", f"method {method} not allowed here")

    if n == 1 and parts[0] == "rooms":
        allow("GET", "POST")
        if method == "GET":
            return 200, {"rooms": STORE.list_rooms()}
        return 201, STORE.create_room(body_fn())
    if n == 2 and parts[0] == "rooms":
        allow("GET")
        return 200, STORE.get_room(parts[1])
    if n == 3 and parts[0] == "rooms" and parts[2] == "availability":
        allow("GET")
        date = query.get("date")
        if date is None:
            raise verr("date query parameter is required")
        return 200, STORE.availability(parts[1], date)
    if n == 1 and parts[0] == "bookings":
        allow("GET", "POST")
        if method == "GET":
            return 200, STORE.list_bookings(query)
        key = headers.get("Idempotency-Key")
        if key is not None and key.strip() == "":
            key = None
        return STORE.create_booking(body_fn(), key)
    if n == 2 and parts[0] == "bookings":
        allow("GET", "DELETE")
        if method == "GET":
            return 200, STORE.get_booking(parts[1])
        STORE.delete_booking(parts[1])
        return 204, None
    raise ApiError("not_found", "no such endpoint")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, fmt, *args):
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

        def body_fn():
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ApiError("bad_json", "request body is not valid JSON")

        try:
            parsed = urllib.parse.urlsplit(self.path)
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True).items()}
            status, body = route(method, parsed.path, query, body_fn, self.headers)
        except ApiError as err:
            self._send_error(err)
            return
        except Exception:  # pragma: no cover - safety net
            self._send(500, {"error": {"code": "internal_error", "message": "internal error"}})
            return
        self._send(status, body)

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


def main():
    ap = argparse.ArgumentParser(description="bookings API server")
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
