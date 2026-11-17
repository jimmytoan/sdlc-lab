"""bookings: in-memory meeting room booking JSON API (standard library only).

Usage: python server.py --port N        (listens on 127.0.0.1 only)

Idempotency: an Idempotency-Key header is global across the API. A key is only
stored after a successful (2xx) POST. Its fingerprint is the method, the request
path and the canonical (sorted-keys) JSON of the parsed body. A repeat with the
same fingerprint replays the stored status and body; a different fingerprint
gives 422 idempotency_mismatch. Failed requests are never stored, so the key
stays usable.
"""
import argparse
import base64
import binascii
import json
import math
import re
import sys
import threading
import urllib.parse
from datetime import datetime, date, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UTC = timezone.utc
TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
INT_RE = re.compile(r"[0-9]+")
DAY_START = dtime(8, 0)
DAY_END = dtime(18, 0)
MAX_DURATION = timedelta(hours=8)
MIN_FREE = timedelta(minutes=15)


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


def fmt_ts(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def parse_ts(value, field):
    """Strict UTC timestamp YYYY-MM-DDTHH:MM:SSZ -> aware datetime."""
    if not isinstance(value, str):
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:00:00Z")
    m = TS_RE.fullmatch(value)
    if not m:
        raise ApiError("validation_error", f"{field} must be a UTC timestamp like 2026-03-02T09:00:00Z")
    try:
        return datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid date/time")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def reject_constant(name):
    raise ValueError(f"non-finite number {name}")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict (plus internal _start/_end/_n)
        self.booking_seq = 0
        self.idempotency = {}    # key -> (fingerprint, status, body)

    # ----- rooms -----
    def create_room(self, body):
        name = body.get("name")
        capacity = body.get("capacity")
        if not isinstance(name, str) or not name.strip() or len(name) > 50:
            raise ApiError("validation_error", "name must be a non-empty string of at most 50 characters")
        if not is_int(capacity) or not (1 <= capacity <= 500):
            raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
        buffer_minutes = body.get("buffer_minutes", 0)
        if not is_int(buffer_minutes) or not (0 <= buffer_minutes <= 60):
            raise ApiError("validation_error", "buffer_minutes must be an integer from 0 to 60")
        if name in self.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        self.room_seq += 1
        rid = f"r{self.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity, "buffer_minutes": buffer_minutes}
        self.rooms[rid] = room
        self.room_names.add(name)
        return dict(room)

    def list_rooms(self):
        return {"rooms": [dict(r) for r in self.rooms.values()]}

    def get_room(self, rid):
        room = self.rooms.get(rid)
        if room is None:
            raise ApiError("not_found", "room not found")
        return dict(room)

    # ----- bookings -----
    @staticmethod
    def _validate_booking_fields(title, attendees, start_raw, end_raw):
        """Shared by POST and PATCH. Returns (start, end) as aware datetimes."""
        if not isinstance(title, str):
            raise ApiError("validation_error", "title must be a string")
        if not is_int(attendees) or attendees < 1:
            raise ApiError("validation_error", "attendees must be an integer of at least 1")
        start = parse_ts(start_raw, "start")
        end = parse_ts(end_raw, "end")
        for dt, field in ((start, "start"), (end, "end")):
            if dt.minute % 15 != 0 or dt.second != 0 or dt.microsecond != 0:
                raise ApiError("validation_error", f"{field} must fall on a quarter hour")
        if end <= start:
            raise ApiError("validation_error", "end must be after start")
        if end - start > MAX_DURATION:
            raise ApiError("validation_error", "booking may be at most 8 hours long")
        return start, end

    def _has_conflict(self, room_id, start, end, buffer_minutes, skip_id=None):
        buf = timedelta(minutes=buffer_minutes)
        for other in self.bookings.values():
            if other["id"] == skip_id or other["room_id"] != room_id:
                continue
            if start < other["_end"] + buf and other["_start"] < end + buf:
                return True
        return False

    def create_booking(self, body):
        room_id = body.get("room_id")
        if not isinstance(room_id, str) or not room_id:
            raise ApiError("validation_error", "room_id must be a string")
        start, end = self._validate_booking_fields(
            body.get("title"), body.get("attendees"), body.get("start"), body.get("end"))
        title = body.get("title")
        attendees = body.get("attendees")
        room = self.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "attendees exceed room capacity")
        if self._has_conflict(room_id, start, end, room["buffer_minutes"]):
            raise ApiError("conflict", "booking overlaps an existing booking in this room")
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
            "_n": self.booking_seq,
        }
        self.bookings[bid] = rec
        return self._public(rec)

    def update_booking(self, bid, body):
        rec = self.bookings.get(bid)
        if rec is None:
            raise ApiError("not_found", "booking not found")
        room = self.rooms[rec["room_id"]]
        # Merge: fields not sent keep their stored value (null counts as sent).
        title = body["title"] if "title" in body else rec["title"]
        attendees = body["attendees"] if "attendees" in body else rec["attendees"]
        start_raw = body["start"] if "start" in body else rec["start"]
        end_raw = body["end"] if "end" in body else rec["end"]
        start, end = self._validate_booking_fields(title, attendees, start_raw, end_raw)
        if attendees > room["capacity"]:
            raise ApiError("over_capacity", "attendees exceed room capacity")
        if self._has_conflict(rec["room_id"], start, end, room["buffer_minutes"], skip_id=bid):
            raise ApiError("conflict", "booking overlaps an existing booking in this room")
        # All checks passed: apply in one step.
        rec["title"] = title
        rec["attendees"] = attendees
        rec["start"] = fmt_ts(start)
        rec["end"] = fmt_ts(end)
        rec["_start"] = start
        rec["_end"] = end
        return self._public(rec)

    @staticmethod
    def _public(rec):
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    def get_booking(self, bid):
        rec = self.bookings.get(bid)
        if rec is None:
            raise ApiError("not_found", "booking not found")
        return self._public(rec)

    def delete_booking(self, bid):
        if bid not in self.bookings:
            raise ApiError("not_found", "booking not found")
        del self.bookings[bid]

    def list_bookings(self, query):
        room_id = query.get("room_id")
        frm = to = None
        if "from" in query:
            frm = parse_ts(query["from"], "from")
        if "to" in query:
            to = parse_ts(query["to"], "to")
        if frm is not None and to is not None and frm >= to:
            raise ApiError("validation_error", "from must be before to")
        limit = 20
        if "limit" in query:
            raw = query["limit"]
            if not INT_RE.fullmatch(raw) or not (1 <= int(raw) <= 100):
                raise ApiError("validation_error", "limit must be an integer from 1 to 100")
            limit = int(raw)
        cursor_key = None
        if "cursor" in query:
            cursor_key = decode_cursor(query["cursor"])

        items = []
        for rec in self.bookings.values():
            if room_id is not None and rec["room_id"] != room_id:
                continue
            if frm is not None and not (rec["_end"] > frm):
                continue
            if to is not None and not (rec["_start"] < to):
                continue
            items.append(rec)
        items.sort(key=lambda r: (r["_start"], r["_n"]))
        total = len(items)
        if cursor_key is not None:
            items = [r for r in items if (r["_start"], r["_n"]) > cursor_key]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last["_start"], last["_n"])
        return {"bookings": [self._public(r) for r in page], "next_cursor": next_cursor, "total": total}

    # ----- availability -----
    def availability(self, rid, date_str):
        if rid not in self.rooms:
            raise ApiError("not_found", "room not found")
        m = DATE_RE.fullmatch(date_str or "")
        if not m:
            raise ApiError("validation_error", "date must be YYYY-MM-DD")
        try:
            d = date(*(int(g) for g in m.groups()))
        except ValueError:
            raise ApiError("validation_error", "date is not a valid calendar date")
        day_start = datetime.combine(d, DAY_START, tzinfo=UTC)
        day_end = datetime.combine(d, DAY_END, tzinfo=UTC)
        room = self.rooms[rid]
        busy = []
        for rec in self.bookings.values():
            if rec["room_id"] != rid:
                continue
            buf = timedelta(minutes=room["buffer_minutes"])
            s = max(rec["_start"] - buf, day_start)
            e = min(rec["_end"] + buf, day_end)
            if s < e:
                busy.append((s, e))
        busy.sort()
        free = []
        cur = day_start
        for s, e in busy:
            if s > cur:
                free.append((cur, s))
            if e > cur:
                cur = e
        if cur < day_end:
            free.append((cur, day_end))
        return {
            "date": date_str,
            "free": [{"start": fmt_ts(s), "end": fmt_ts(e)} for s, e in free if e - s >= MIN_FREE],
        }


def encode_cursor(start, n):
    raw = json.dumps({"s": fmt_ts(start), "n": n}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token):
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
        if not isinstance(data, dict) or not is_int(data.get("n")):
            raise ValueError
        start = parse_ts(data.get("s"), "cursor")
        return (start, data["n"])
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError, ApiError):
        raise ApiError("validation_error", "invalid cursor")


STORE = Store()


def route(method, path):
    """Return (handler_name, params) or raise ApiError for 404/405."""
    if not path.startswith("/") or path.startswith("//"):
        raise ApiError("not_found", "no such path")
    segs = [s for s in path[1:].split("/") if s]
    if path.endswith("/") and len(path) > 1:
        pass  # tolerate one trailing slash
    table = None
    if segs == ["rooms"]:
        table = {"GET": "list_rooms", "POST": "create_room"}
        params = {}
    elif len(segs) == 2 and segs[0] == "rooms":
        table = {"GET": "get_room"}
        params = {"id": segs[1]}
    elif len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
        table = {"GET": "availability"}
        params = {"id": segs[1]}
    elif segs == ["bookings"]:
        table = {"GET": "list_bookings", "POST": "create_booking"}
        params = {}
    elif len(segs) == 2 and segs[0] == "bookings":
        table = {"GET": "get_booking", "PATCH": "update_booking", "DELETE": "delete_booking"}
        params = {"id": segs[1]}
    if table is None:
        raise ApiError("not_found", "no such path")
    if method not in table:
        raise ApiError("method_not_allowed", f"method {method} not allowed here")
    return table[method], params


def single(query, name):
    vals = query.get(name)
    return vals[-1] if vals else None


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    def _send(self, status, body=None):
        if status == 204 or body is None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":  # RFC 9110: HEAD responses carry no body
            self.wfile.write(data)

    def _error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            return self._read_chunked()
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _read_chunked(self):
        chunks = []
        while True:
            line = self.rfile.readline(65537)
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0].strip() or b"x", 16)
            except ValueError:
                raise ApiError("bad_json", "malformed chunked body")
            if size == 0:
                # consume optional trailers up to the blank line
                while True:
                    trailer = self.rfile.readline(65537)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline(65537)  # CRLF after chunk data
        return b"".join(chunks)

    def _handle(self, method):
        try:
            self._dispatch(method)
        except ApiError as err:
            self._error(err)
        except Exception:  # never leak a traceback to the client
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})

    def _dispatch(self, method):
        raw_body = self._read_body()
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = {k: v for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        handler, params = route(method, path)

        if method == "GET":
            with STORE.lock:
                if handler == "list_rooms":
                    return self._send(200, STORE.list_rooms())
                if handler == "get_room":
                    return self._send(200, STORE.get_room(params["id"]))
                if handler == "availability":
                    date_str = single(query, "date")
                    return self._send(200, STORE.availability(params["id"], date_str))
                if handler == "list_bookings":
                    flat = {k: v[-1] for k, v in query.items()}
                    return self._send(200, STORE.list_bookings(flat))
                if handler == "get_booking":
                    return self._send(200, STORE.get_booking(params["id"]))

        if method == "DELETE":
            with STORE.lock:
                STORE.delete_booking(params["id"])
            return self._send(204)

        if method == "PATCH":
            # Idempotency-Key is not used for PATCH: nothing stored or replayed.
            body = self._parse_json_body(raw_body)
            if not isinstance(body, dict):
                raise ApiError("bad_json", "request body must be a JSON object")
            with STORE.lock:
                resp = STORE.update_booking(params["id"], body)
            return self._send(200, resp)

        # POST
        body = self._parse_json_object(raw_body)
        key = self.headers.get("Idempotency-Key")
        if key is not None:
            key = key.strip() or None
        fingerprint = canonical({"method": method, "path": path, "body": body})
        with STORE.lock:
            if key is not None and key in STORE.idempotency:
                fp, status, resp = STORE.idempotency[key]
                if fp != fingerprint:
                    raise ApiError("idempotency_mismatch", "Idempotency-Key was used for a different request")
                return self._send(status, resp)
            if handler == "create_room":
                status, resp = 201, STORE.create_room(body)
            else:
                status, resp = 201, STORE.create_booking(body)
            if key is not None:
                STORE.idempotency[key] = (fingerprint, status, resp)
        return self._send(status, resp)

    @staticmethod
    def _parse_json_body(raw):
        try:
            text = raw.decode("utf-8")
            return json.loads(text, parse_constant=reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ApiError("bad_json", "request body is not valid JSON")

    @staticmethod
    def _parse_json_object(raw):
        obj = Handler._parse_json_body(raw)
        if not isinstance(obj, dict):
            raise ApiError("validation_error", "request body must be a JSON object")
        return obj

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

    def do_HEAD(self):
        self._handle("HEAD")

    def do_OPTIONS(self):
        self._handle("OPTIONS")


def main(argv=None):
    p = argparse.ArgumentParser(description="bookings API server")
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args(argv)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main(sys.argv[1:])
