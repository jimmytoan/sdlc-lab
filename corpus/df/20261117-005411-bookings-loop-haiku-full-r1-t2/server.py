"""bookings: meeting room booking HTTP JSON API (standard library only)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
INT_RE = re.compile(r"-?[0-9]+\Z")
DAY_START = (8, 0)
DAY_END = (18, 0)
MAX_BOOKING = timedelta(hours=8)


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message or code


def parse_ts(value):
    """Return naive UTC datetime for an exact-form timestamp, else None."""
    if not isinstance(value, str) or not TS_RE.match(value):
        return None
    try:
        return datetime.strptime(value, TS_FORMAT)
    except ValueError:
        return None


def fmt_ts(dt):
    return dt.strftime(TS_FORMAT)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_constant(name):
    raise ValueError("invalid constant " + name)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()  # casefolded names
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict (insertion ordered)
        self.booking_seq = 0
        self.idem = {}           # key -> (body, response)


STORE = Store()


def validation(message):
    return ApiError(422, "validation_error", message)


# ---------- rooms ----------

def create_room(body):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise validation("name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise validation("name must be 1 to 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    with STORE.lock:
        key = name.casefold()
        if key in STORE.room_names:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        STORE.room_seq += 1
        room_id = "r%d" % STORE.room_seq
        room = {"id": room_id, "name": name, "capacity": capacity}
        STORE.rooms[room_id] = room
        STORE.room_names.add(key)
    return 201, room


def list_rooms():
    with STORE.lock:
        return 200, {"rooms": list(STORE.rooms.values())}


def get_room(room_id):
    with STORE.lock:
        room = STORE.rooms.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return 200, room


def availability(room_id, query):
    with STORE.lock:
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        dates = query.get("date")
        if not dates:
            raise validation("date is required")
        date_str = dates[-1]
        if not DATE_RE.match(date_str):
            raise validation("date must be YYYY-MM-DD")
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise validation("date must be a real calendar date")
        ds = day.replace(hour=DAY_START[0], minute=DAY_START[1])
        de = day.replace(hour=DAY_END[0], minute=DAY_END[1])
        relevant = sorted(
            (b["_start"], b["_end"]) for b in STORE.bookings.values()
            if b["room_id"] == room_id and b["_end"] > ds and b["_start"] < de
        )
        free = []
        cur = ds
        for s, e in relevant:
            s = max(s, ds)
            e = min(e, de)
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            if e > cur:
                cur = e
        if cur < de:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(de)})
    return 200, {"date": date_str, "free": free}


# ---------- bookings ----------

def validate_booking(body):
    room_id = body.get("room_id")
    start_s = body.get("start")
    end_s = body.get("end")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str):
        raise validation("room_id must be a string")
    if not isinstance(start_s, str) or not isinstance(end_s, str):
        raise validation("start and end must be timestamp strings")
    start = parse_ts(start_s)
    end = parse_ts(end_s)
    if start is None or end is None:
        raise validation("start and end must be timestamps like 2026-03-02T09:30:00Z")
    for dt in (start, end):
        if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
            raise validation("start and end must be on quarter-hour boundaries")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_BOOKING:
        raise validation("booking must be at most 8 hours long")
    if not isinstance(title, str):
        raise validation("title must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise validation("title must be 1 to 100 characters")
    if not is_int(attendees) or attendees < 1:
        raise validation("attendees must be an integer of at least 1")
    return room_id, start_s, end_s, start, end, title, attendees


def public_booking(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def create_booking(body, idem_key):
    with STORE.lock:
        if idem_key is not None and idem_key in STORE.idem:
            stored_body, stored_resp = STORE.idem[idem_key]
            if stored_body == body:
                return 201, stored_resp
            raise ApiError(422, "idempotency_mismatch",
                           "Idempotency-Key was used with a different request body")

        room_id, start_s, end_s, start, end, title, attendees = validate_booking(body)
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "attendees exceed room capacity")
        for other in STORE.bookings.values():
            if other["room_id"] == room_id and other["_start"] < end and start < other["_end"]:
                raise ApiError(409, "conflict", "booking overlaps an existing booking")

        STORE.booking_seq += 1
        bid = "b%d" % STORE.booking_seq
        booking = {
            "id": bid,
            "room_id": room_id,
            "start": start_s,
            "end": end_s,
            "title": title,
            "attendees": attendees,
            "_start": start,
            "_end": end,
            "_seq": STORE.booking_seq,
        }
        STORE.bookings[bid] = booking
        resp = public_booking(booking)
        if idem_key is not None:
            STORE.idem[idem_key] = (body, resp)
    return 201, resp


def get_booking(bid):
    with STORE.lock:
        b = STORE.bookings.get(bid)
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, public_booking(b)


def delete_booking(bid):
    with STORE.lock:
        if bid not in STORE.bookings:
            raise ApiError(404, "not_found", "booking not found")
        del STORE.bookings[bid]
    return 204, None


def _single(query, name):
    vals = query.get(name)
    return vals[-1] if vals else None


def encode_cursor(start_s, seq):
    raw = json.dumps([start_s, seq], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + pad).encode())
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise validation("invalid cursor")
    if (not isinstance(data, list) or len(data) != 2 or not isinstance(data[0], str)
            or not is_int(data[1])):
        raise validation("invalid cursor")
    return data[0], data[1]


def list_bookings(query):
    room_id = _single(query, "room_id")
    from_s = _single(query, "from")
    to_s = _single(query, "to")
    limit_s = _single(query, "limit")
    cursor_s = _single(query, "cursor")

    from_dt = None
    to_dt = None
    if from_s is not None:
        from_dt = parse_ts(from_s)
        if from_dt is None:
            raise validation("from must be a timestamp")
    if to_s is not None:
        to_dt = parse_ts(to_s)
        if to_dt is None:
            raise validation("to must be a timestamp")
    limit = 20
    if limit_s is not None:
        if not INT_RE.match(limit_s) or not (1 <= int(limit_s) <= 100):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(limit_s)
    after = None
    if cursor_s is not None:
        after = decode_cursor(cursor_s)

    with STORE.lock:
        items = []
        for b in STORE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if from_dt is not None and not (b["_end"] > from_dt):
                continue
            if to_dt is not None and not (b["_start"] < to_dt):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start"], b["_seq"]))
        if after is not None:
            items = [b for b in items if (b["start"], b["_seq"]) > after]
        page = items[:limit]
        more = len(items) > limit
    next_cursor = None
    if more and page:
        last = page[-1]
        next_cursor = encode_cursor(last["start"], last["_seq"])
    return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


# ---------- routing ----------

def route(method, path):
    """Return (handler_name, args) or raise ApiError 404/405."""
    parts = path.split("/")
    # path looks like /a, /a/b, /a/b/c
    if len(parts) >= 2 and parts[0] == "":
        segs = parts[1:]
    else:
        segs = None
    table = None
    args = []
    if segs is not None:
        if len(segs) == 1 and segs[0] in ("rooms", "bookings"):
            table = {"rooms": {"GET": "list_rooms", "POST": "create_room"},
                     "bookings": {"GET": "list_bookings", "POST": "create_booking"}}[segs[0]]
        elif len(segs) == 2 and segs[0] == "rooms" and segs[1] != "":
            table = {"GET": "get_room"}
            args = [segs[1]]
        elif len(segs) == 2 and segs[0] == "bookings" and segs[1] != "":
            table = {"GET": "get_booking", "DELETE": "delete_booking"}
            args = [segs[1]]
        elif len(segs) == 3 and segs[0] == "rooms" and segs[1] != "" and segs[2] == "availability":
            table = {"GET": "availability"}
            args = [segs[1]]
    if table is None:
        raise ApiError(404, "not_found", "no such path")
    if method not in table:
        raise ApiError(405, "method_not_allowed", "method not allowed")
    return table[method], args


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):  # keep test output quiet
        pass

    def _send(self, status, payload=None):
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status, code, message):
        self._send(status, {"error": {"code": code, "message": message}})

    def _read_body(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _json_object(self, raw):
        try:
            text = raw.decode("utf-8")
            data = json.loads(text, parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad_json", "request body is not valid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "request body must be a JSON object")
        return data

    def _handle(self, method):
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query, keep_blank_values=True)
            name, args = route(method, path)
            raw = self._read_body()
            if name == "create_room":
                status, payload = create_room(self._json_object(raw))
            elif name == "create_booking":
                body = self._json_object(raw)
                key = self.headers.get("Idempotency-Key")
                if key is not None:
                    key = key.strip()
                    if key == "":
                        key = None
                status, payload = create_booking(body, key)
            elif name == "list_rooms":
                status, payload = list_rooms()
            elif name == "get_room":
                status, payload = get_room(*args)
            elif name == "list_bookings":
                status, payload = list_bookings(query)
            elif name == "get_booking":
                status, payload = get_booking(*args)
            elif name == "delete_booking":
                status, payload = delete_booking(*args)
            elif name == "availability":
                status, payload = availability(args[0], query)
            else:  # pragma: no cover
                raise ApiError(404, "not_found", "no such path")
            self._send(status, payload)
        except ApiError as e:
            self._error(e.status, e.code, e.message)
        except Exception:  # pragma: no cover
            self._error(500, "internal_error", "internal server error")

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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
