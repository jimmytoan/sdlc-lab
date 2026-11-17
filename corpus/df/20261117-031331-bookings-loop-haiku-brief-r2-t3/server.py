"""bookings: meeting room booking JSON API (standard library only).

Usage: python server.py --port <N>      (listens on 127.0.0.1)
"""
import argparse
import base64
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

UTC = dt.timezone.utc
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|\+00:00)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAY_START_H, DAY_END_H = 8, 18
MAX_DURATION = dt.timedelta(hours=8)

STATUS_FOR = {
    "not_found": 404,
    "method_not_allowed": 405,
    "bad_json": 400,
    "validation_error": 422,
    "over_capacity": 422,
    "room_exists": 409,
    "conflict": 409,
    "idempotency_mismatch": 422,
}


class ApiError(Exception):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code.replace("_", " ")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_names = {}     # name -> id
        self.bookings = {}       # id -> dict (internal datetimes kept separately)
        self.idem = {}           # key -> (fingerprint, status, body)
        self.room_seq = 0
        self.booking_seq = 0


STORE = Store()


def fmt_ts(d):
    return d.strftime(TS_FMT)


def parse_ts(value, field):
    if not isinstance(value, str) or not TS_RE.match(value):
        raise ApiError("validation_error", f"{field} must be an ISO-8601 UTC timestamp like 2026-03-02T09:30:00Z")
    core = value[:-1] if value.endswith("Z") else value[:-6]
    fmt = "%Y-%m-%dT%H:%M:%S" if core.count(":") == 2 else "%Y-%m-%dT%H:%M"
    try:
        d = dt.datetime.strptime(core, fmt).replace(tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")
    return d


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def room_view(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"],
            "buffer_minutes": r["buffer_minutes"]}


def booking_view(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["_start"]),
        "end": fmt_ts(b["_end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def validate_room(body):
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("validation_error", "name must be a non-empty string")
    if len(name) > 50:
        raise ApiError("validation_error", "name must be at most 50 characters")
    if not is_int(cap) or not (1 <= cap <= 500):
        raise ApiError("validation_error", "capacity must be an integer from 1 to 500")
    if "buffer_minutes" in body:
        buf = body["buffer_minutes"]
        if not is_int(buf) or not (0 <= buf <= 60):
            raise ApiError("validation_error", "buffer_minutes must be an integer from 0 to 60")
    else:
        buf = 0
    return name, cap, buf


def check_quarter(d, field):
    if d.minute % 15 != 0 or d.second != 0:
        raise ApiError("validation_error", f"{field} must be on a quarter hour")


def validate_booking(body):
    room_id = body.get("room_id")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str) or not room_id:
        raise ApiError("validation_error", "room_id must be a string")
    start = parse_ts(body.get("start"), "start")
    end = parse_ts(body.get("end"), "end")
    if not isinstance(title, str) or not title.strip():
        raise ApiError("validation_error", "title must be a non-empty string")
    if not is_int(attendees) or attendees < 1:
        raise ApiError("validation_error", "attendees must be a positive integer")
    check_quarter(start, "start")
    check_quarter(end, "end")
    if end <= start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "booking may be at most 8 hours long")
    return room_id, start, end, title, attendees


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def conflicts_with_room(room, start, end, exclude_id=None):
    """True if [start, end) is closer than the room's buffer to another booking of the room."""
    buf = dt.timedelta(minutes=room["buffer_minutes"])
    for other in STORE.bookings.values():
        if other["room_id"] != room["id"] or other["id"] == exclude_id:
            continue
        if start < other["_end"] + buf and other["_start"] < end + buf:
            return True
    return False


def paginate_params(query):
    limit = 20
    if "limit" in query:
        raw = query["limit"][-1]
        if not re.fullmatch(r"\d+", raw) or not (1 <= int(raw) <= 100):
            raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        limit = int(raw)
    return limit


def encode_cursor(start, seq):
    raw = json.dumps([fmt_ts(start), seq]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cur):
    try:
        pad = "=" * (-len(cur) % 4)
        start_s, seq = json.loads(base64.urlsafe_b64decode(cur + pad).decode())
        start = parse_ts(start_s, "cursor")
        if not is_int(seq):
            raise ValueError
        return start, seq
    except (ApiError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error):
        raise ApiError("validation_error", "cursor is invalid")


def list_bookings(query):
    limit = paginate_params(query)
    room_id = query.get("room_id", [None])[-1]
    frm = to = None
    if "from" in query:
        frm = parse_ts(query["from"][-1], "from")
    if "to" in query:
        to = parse_ts(query["to"][-1], "to")
    if frm and to and frm >= to:
        raise ApiError("validation_error", "from must be before to")
    cursor = None
    if "cursor" in query and query["cursor"][-1] != "":
        cursor = decode_cursor(query["cursor"][-1])

    items = []
    for b in STORE.bookings.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm is not None and b["_end"] <= frm:
            continue
        if to is not None and b["_start"] >= to:
            continue
        items.append(b)
    items.sort(key=lambda b: (b["_start"], b["_seq"]))
    total = len(items)
    if cursor is not None:
        cs, cseq = cursor
        items = [b for b in items if (b["_start"], b["_seq"]) > (cs, cseq)]
    page = items[:limit]
    next_cursor = None
    if len(items) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(last["_start"], last["_seq"])
    return {"bookings": [booking_view(b) for b in page], "next_cursor": next_cursor, "total": total}


def availability(room_id, query):
    if "date" not in query or not DATE_RE.match(query["date"][-1]):
        raise ApiError("validation_error", "date must be YYYY-MM-DD")
    try:
        day = dt.date.fromisoformat(query["date"][-1])
    except ValueError:
        raise ApiError("validation_error", "date is not a valid date")
    if room_id not in STORE.rooms:
        raise ApiError("not_found", "room not found")
    day_start = dt.datetime(day.year, day.month, day.day, DAY_START_H, tzinfo=UTC)
    day_end = dt.datetime(day.year, day.month, day.day, DAY_END_H, tzinfo=UTC)
    buf = dt.timedelta(minutes=STORE.rooms[room_id]["buffer_minutes"])
    min_free = dt.timedelta(minutes=15)
    busy = []
    for b in STORE.bookings.values():
        if b["room_id"] == room_id and overlaps(b["_start"] - buf, b["_end"] + buf, day_start, day_end):
            busy.append((max(b["_start"] - buf, day_start), min(b["_end"] + buf, day_end)))
    busy.sort()
    free = []
    cur = day_start
    for s, e in busy:
        if s - cur >= min_free:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        cur = max(cur, e)
    if day_end - cur >= min_free:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
    return {"date": day.isoformat(), "free": free}


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _send(self, status, body=None):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _error(self, err):
        status = STATUS_FOR[err.code]
        self._send(status, {"error": {"code": err.code, "message": err.message}})

    def _read_raw(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _json_body(self, raw):
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body must be valid JSON")
        if not isinstance(body, dict):
            raise ApiError("validation_error", "request body must be a JSON object")
        return body

    def _route(self, method):
        parts = urlsplit(self.path)
        path = parts.path
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        query = parse_qs(parts.query, keep_blank_values=True)
        segs = [s for s in path.split("/") if s != ""]
        raw = self._read_raw() if method in ("POST", "PUT", "PATCH", "DELETE") else b""

        routes = self._routes(segs)
        if routes is None:
            raise ApiError("not_found", "no such endpoint")
        allowed, handler = routes
        if method not in allowed:
            raise ApiError("method_not_allowed", f"{method} not allowed here")
        return handler(method, query, raw)

    def _routes(self, segs):
        """Returns (allowed methods, handler) or None if path unknown."""
        if segs == ["rooms"]:
            return ({"GET", "POST"}, self._rooms_collection)
        if len(segs) == 2 and segs[0] == "rooms":
            rid = segs[1]
            return ({"GET"}, lambda m, q, r: self._get_room(rid))
        if len(segs) == 3 and segs[0] == "rooms" and segs[2] == "availability":
            rid = segs[1]
            return ({"GET"}, lambda m, q, r: (200, availability(rid, q)))
        if segs == ["bookings"]:
            return ({"GET", "POST"}, self._bookings_collection)
        if len(segs) == 2 and segs[0] == "bookings":
            bid = segs[1]
            return ({"GET", "DELETE", "PATCH"}, lambda m, q, r: self._booking_item(m, bid, r))
        return None

    def _rooms_collection(self, method, query, raw):
        if method == "GET":
            return 200, {"rooms": [room_view(r) for r in STORE.rooms.values()]}
        body = self._json_body(raw)
        name, cap, buf = validate_room(body)
        with STORE.lock:
            if name in STORE.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            STORE.room_seq += 1
            rid = f"r{STORE.room_seq}"
            room = {"id": rid, "name": name, "capacity": cap, "buffer_minutes": buf}
            STORE.rooms[rid] = room
            STORE.room_names[name] = rid
        return 201, room_view(room)

    def _get_room(self, rid):
        room = STORE.rooms.get(rid)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, room_view(room)

    def _booking_item(self, method, bid, raw=b""):
        if method == "PATCH":
            return self._patch_booking(bid, raw)
        with STORE.lock:
            b = STORE.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            if method == "DELETE":
                del STORE.bookings[bid]
                return 204, None
            return 200, booking_view(b)

    def _patch_booking(self, bid, raw):
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "request body must be valid JSON")
        if not isinstance(body, dict):
            raise ApiError("bad_json", "request body must be a JSON object")
        with STORE.lock:
            b = STORE.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            merged = {
                "room_id": b["room_id"],
                "start": fmt_ts(b["_start"]),
                "end": fmt_ts(b["_end"]),
                "title": b["title"],
                "attendees": b["attendees"],
            }
            for field in ("start", "end", "title", "attendees"):
                if field in body:
                    merged[field] = body[field]
            room_id, start, end, title, attendees = validate_booking(merged)
            room = STORE.rooms[room_id]
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            if conflicts_with_room(room, start, end, exclude_id=bid):
                raise ApiError("conflict", "room is already booked for this time")
            b["_start"], b["_end"] = start, end
            b["title"], b["attendees"] = title, attendees
            return 200, booking_view(b)

    def _bookings_collection(self, method, query, raw):
        if method == "GET":
            with STORE.lock:
                return 200, list_bookings(query)
        return self._create_booking(raw)

    def _create_booking(self, raw):
        key = self.headers.get("Idempotency-Key")
        body = self._json_body(raw)
        fingerprint = json.dumps(body, sort_keys=True)
        with STORE.lock:
            if key is not None and key in STORE.idem:
                fp, status, resp = STORE.idem[key]
                if fp != fingerprint:
                    raise ApiError("idempotency_mismatch", "Idempotency-Key was already used for a different request")
                return status, resp
            room_id, start, end, title, attendees = validate_booking(body)
            room = STORE.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            if conflicts_with_room(room, start, end):
                raise ApiError("conflict", "room is already booked for this time")
            STORE.booking_seq += 1
            bid = f"b{STORE.booking_seq}"
            booking = {
                "id": bid, "room_id": room_id, "title": title, "attendees": attendees,
                "_start": start, "_end": end, "_seq": STORE.booking_seq,
            }
            STORE.bookings[bid] = booking
            resp = booking_view(booking)
            if key is not None:
                STORE.idem[key] = (fingerprint, 201, resp)
        return 201, resp

    def _handle(self, method):
        try:
            status, body = self._route(method)
        except ApiError as err:
            self._error(err)
            return
        except Exception:  # pragma: no cover - safety net
            self._send(500, {"error": {"code": "internal_error", "message": "internal server error"}})
            return
        self._send(status, body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
