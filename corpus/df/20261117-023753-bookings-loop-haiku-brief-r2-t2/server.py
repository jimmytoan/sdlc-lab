"""bookings: meeting room booking JSON API (standard library only).

Usage: python server.py --port <N>   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

UTC = dt.timezone.utc
TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_BOOKING = dt.timedelta(hours=8)


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


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    if not isinstance(value, str):
        raise ApiError("validation_error", f"{field} must be a timestamp string like 2026-03-02T09:30:00Z")
    m = TS_RE.match(value)
    if not m:
        raise ApiError("validation_error", f"{field} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        return dt.datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid date-time")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = {}     # name -> id
        self.bookings = {}       # id -> booking dict (with internal _start/_end datetimes)
        self.room_seq = 0
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, response body)

    # ---- rooms ----
    def create_room(self, body):
        if not isinstance(body, dict):
            raise ApiError("validation_error", "body must be a JSON object")
        name = body.get("name")
        capacity = body.get("capacity")
        if not isinstance(name, str) or not name.strip() or len(name) > 50:
            raise ApiError("validation_error", "name must be a non-empty string of at most 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
        buffer_minutes = body.get("buffer_minutes", 0)
        if not is_int(buffer_minutes) or not (0 <= buffer_minutes <= 60):
            raise ApiError("validation_error", "buffer_minutes must be an integer from 0 to 60")
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            self.room_seq += 1
            rid = f"r{self.room_seq}"
            room = {"id": rid, "name": name, "capacity": capacity, "buffer_minutes": buffer_minutes}
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

    # ---- bookings ----
    @staticmethod
    def public(b):
        return {k: v for k, v in b.items() if not k.startswith("_")}

    def _validate_booking(self, body):
        if not isinstance(body, dict):
            raise ApiError("validation_error", "body must be a JSON object")
        room_id = body.get("room_id")
        title = body.get("title")
        attendees = body.get("attendees")
        if not isinstance(room_id, str) or not room_id:
            raise ApiError("validation_error", "room_id must be a string")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ApiError("validation_error", "title must be a non-empty string of at most 200 characters")
        if not is_int(attendees) or attendees < 1:
            raise ApiError("validation_error", "attendees must be a positive integer")
        start = parse_ts(body.get("start"), "start")
        end = parse_ts(body.get("end"), "end")
        for t, field in ((start, "start"), (end, "end")):
            if t.minute % 15 != 0 or t.second != 0:
                raise ApiError("validation_error", f"{field} must be on a quarter hour")
        if end <= start:
            raise ApiError("validation_error", "end must be after start")
        if end - start > MAX_BOOKING:
            raise ApiError("validation_error", "booking must be at most 8 hours long")
        return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}

    def _conflicts(self, room, start, end, exclude_id=None):
        buf = dt.timedelta(minutes=room["buffer_minutes"])
        for other in self.bookings.values():
            if other["id"] == exclude_id or other["room_id"] != room["id"]:
                continue
            if other["_start"] < end + buf and start < other["_end"] + buf:
                return True
        return False

    def create_booking(self, body, idem_key):
        with self.lock:
            fingerprint = None
            if idem_key is not None:
                fingerprint = json.dumps(body, sort_keys=True, separators=(",", ":"))
                if idem_key in self.idem:
                    stored_fp, stored_resp = self.idem[idem_key]
                    if stored_fp != fingerprint:
                        raise ApiError("idempotency_mismatch", "Idempotency-Key was already used for a different request")
                    return dict(stored_resp)
            v = self._validate_booking(body)
            room = self.rooms.get(v["room_id"])
            if room is None:
                raise ApiError("not_found", "room not found")
            if v["attendees"] > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room can hold")
            if self._conflicts(room, v["start"], v["end"]):
                raise ApiError("conflict", "the room is already booked for this time")
            self.booking_seq += 1
            bid = f"b{self.booking_seq}"
            rec = {
                "id": bid,
                "room_id": v["room_id"],
                "start": fmt_ts(v["start"]),
                "end": fmt_ts(v["end"]),
                "title": v["title"],
                "attendees": v["attendees"],
                "_start": v["start"],
                "_end": v["end"],
            }
            self.bookings[bid] = rec
            resp = self.public(rec)
            if idem_key is not None:
                self.idem[idem_key] = (fingerprint, dict(resp))
            return dict(resp)

    def get_booking(self, bid):
        with self.lock:
            b = self.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            return self.public(b)

    def patch_booking(self, bid, body):
        if not isinstance(body, dict):
            raise ApiError("bad_json", "body must be a JSON object")
        with self.lock:
            existing = self.bookings.get(bid)
            if existing is None:
                raise ApiError("not_found", "booking not found")
            merged = {
                "room_id": existing["room_id"],
                "start": fmt_ts(existing["_start"]),
                "end": fmt_ts(existing["_end"]),
                "title": existing["title"],
                "attendees": existing["attendees"],
            }
            for f in ("start", "end", "title", "attendees"):
                if f in body:
                    merged[f] = body[f]
            v = self._validate_booking(merged)
            room = self.rooms[v["room_id"]]
            if v["attendees"] > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room can hold")
            if self._conflicts(room, v["start"], v["end"], exclude_id=bid):
                raise ApiError("conflict", "the room is already booked for this time")
            existing["start"] = fmt_ts(v["start"])
            existing["end"] = fmt_ts(v["end"])
            existing["title"] = v["title"]
            existing["attendees"] = v["attendees"]
            existing["_start"] = v["start"]
            existing["_end"] = v["end"]
            return self.public(existing)

    def delete_booking(self, bid):
        with self.lock:
            if bid not in self.bookings:
                raise ApiError("not_found", "booking not found")
            del self.bookings[bid]

    def list_bookings(self, query):
        def one(name):
            vals = query.get(name)
            if not vals:
                return None
            if len(vals) > 1:
                raise ApiError("validation_error", f"{name} given more than once")
            return vals[0]

        room_id = one("room_id")
        from_s, to_s = one("from"), one("to")
        limit_s, cursor_s = one("limit"), one("cursor")

        limit = 20
        if limit_s is not None:
            if not re.fullmatch(r"-?\d+", limit_s) or not (1 <= int(limit_s) <= 100):
                raise ApiError("validation_error", "limit must be an integer from 1 to 100")
            limit = int(limit_s)

        win_from = win_to = None
        if from_s is not None:
            win_from = parse_ts(from_s, "from")
        if to_s is not None:
            win_to = parse_ts(to_s, "to")
        if win_from is not None and win_to is not None and win_to <= win_from:
            raise ApiError("validation_error", "to must be after from")

        after = None
        if cursor_s is not None:
            try:
                raw = base64.urlsafe_b64decode(cursor_s.encode("ascii") + b"=" * (-len(cursor_s) % 4)).decode("utf-8")
                start_s, num_s = raw.rsplit("|", 1)
                after = (start_s, int(num_s))
                parse_ts(start_s, "cursor")
            except (ValueError, UnicodeError, binascii.Error):
                raise ApiError("validation_error", "invalid cursor")

        with self.lock:
            matched = []
            for b in self.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if win_from is not None and not (b["_end"] > win_from):
                    continue
                if win_to is not None and not (b["_start"] < win_to):
                    continue
                matched.append(b)
            total = len(matched)
            items = []
            for b in matched:
                key = (b["start"], int(b["id"][1:]))
                if after is not None and not (key > after):
                    continue
                items.append((key, b))
            items.sort(key=lambda kb: kb[0])
            page = items[:limit]
            next_cursor = None
            if len(items) > limit:
                last_key = page[-1][0]
                raw = f"{last_key[0]}|{last_key[1]}".encode("utf-8")
                next_cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            return {"bookings": [self.public(b) for _, b in page], "total": total, "next_cursor": next_cursor}

    # ---- availability ----
    def availability(self, rid, date_s):
        if date_s is None:
            raise ApiError("validation_error", "date query parameter is required")
        m = DATE_RE.match(date_s)
        if not m:
            raise ApiError("validation_error", "date must be YYYY-MM-DD")
        try:
            day = dt.date(*(int(g) for g in m.groups()))
        except ValueError:
            raise ApiError("validation_error", "date is not a valid calendar date")
        with self.lock:
            if rid not in self.rooms:
                raise ApiError("not_found", "room not found")
            day_start = dt.datetime(day.year, day.month, day.day, WORK_START_HOUR, tzinfo=UTC)
            day_end = dt.datetime(day.year, day.month, day.day, WORK_END_HOUR, tzinfo=UTC)
            buf = dt.timedelta(minutes=self.rooms[rid]["buffer_minutes"])
            busy = sorted(
                (max(b["_start"] - buf, day_start), min(b["_end"] + buf, day_end))
                for b in self.bookings.values()
                if b["room_id"] == rid and b["_start"] - buf < day_end and b["_end"] + buf > day_start
            )
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append((cur, s))
            cur = max(cur, e)
        if cur < day_end:
            free.append((cur, day_end))
        min_free = dt.timedelta(minutes=15)
        return {"date": date_s, "free": [
            {"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in free if e - s >= min_free
        ]}


STORE = Store()


def _parse_id_path(segments):
    return segments


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings/1.0"

    def log_message(self, format, *args):  # keep quiet
        pass

    def _send(self, status, body=None):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _read_json(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            raise ApiError("bad_json", "invalid Content-Length")
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body is not valid JSON")

    def _route(self, method):
        parts = urlsplit(self.path)
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        segs = path.strip("/").split("/") if path.strip("/") else []
        query = parse_qs(parts.query, keep_blank_values=True)

        # Resolve the route: (allowed_methods, handler thunk)
        if segs == ["rooms"]:
            allowed = {"GET", "POST"}
            handler = {
                "GET": lambda: (200, {"rooms": STORE.list_rooms()}),
                "POST": lambda: (201, STORE.create_room(self._read_json())),
            }
        elif len(segs) == 2 and segs[0] == "rooms" and segs[1]:
            rid = segs[1]
            allowed = {"GET"}
            handler = {"GET": lambda: (200, STORE.get_room(rid))}
        elif len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability" and segs[1]:
            rid = segs[1]
            allowed = {"GET"}
            date_s = query.get("date", [None])[0]
            handler = {"GET": lambda: (200, STORE.availability(rid, date_s))}
        elif segs == ["bookings"]:
            allowed = {"GET", "POST"}

            def post_booking():
                body = self._read_json()
                key = self.headers.get("Idempotency-Key")
                if key is not None and key.strip() == "":
                    key = None
                return 201, STORE.create_booking(body, key)

            handler = {
                "GET": lambda: (200, STORE.list_bookings(query)),
                "POST": post_booking,
            }
        elif len(segs) == 2 and segs[0] == "bookings" and segs[1]:
            bid = segs[1]
            allowed = {"GET", "DELETE", "PATCH"}

            def delete_booking():
                STORE.delete_booking(bid)
                return 204, None

            handler = {
                "GET": lambda: (200, STORE.get_booking(bid)),
                "DELETE": delete_booking,
                "PATCH": lambda: (200, STORE.patch_booking(bid, self._read_json())),
            }
        else:
            raise ApiError("not_found", "no such endpoint")

        if method not in allowed:
            raise ApiError("method_not_allowed", f"{method} not allowed here")
        return handler[method]()

    def _dispatch(self, method):
        try:
            status, body = self._route(method)
            self._send(status, body)
        except ApiError as err:
            self._error(err)
        except Exception:  # pragma: no cover - safety net
            self._error(ApiError("internal_error", "unexpected server error"))

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


def main():
    p = argparse.ArgumentParser(description="bookings API server")
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
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
