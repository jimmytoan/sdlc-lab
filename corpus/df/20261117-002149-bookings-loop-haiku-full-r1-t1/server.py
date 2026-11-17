"""bookings: meeting room booking API (standard library only)."""
import argparse
import base64
import binascii
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

WORK_START = dt.time(8, 0)
WORK_END = dt.time(18, 0)
MAX_BOOKING = dt.timedelta(hours=8)

_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INT_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message or code


def parse_ts(value):
    """Return aware-naive UTC datetime for a strict timestamp string, or None."""
    if not isinstance(value, str) or not _TS_RE.fullmatch(value):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> dict
        self.room_order = []     # ids in creation order
        self.room_names = set()  # lower-cased names
        self.room_seq = 0
        self.bookings = {}       # id -> dict (with internal fields)
        self.booking_seq = 0
        self.idem = {}           # key -> (canonical body, response dict)


STORE = Store()


def public_room(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def public_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["_start"]),
        "end": fmt_ts(b["_end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------- rooms ----------

def create_room(body):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str):
        raise ApiError(422, "validation_error", "name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise ApiError(422, "validation_error", "name must be 1 to 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise ApiError(422, "validation_error", "capacity must be an integer from 1 to 500")
    with STORE.lock:
        if name.lower() in STORE.room_names:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        STORE.room_seq += 1
        rid = f"r{STORE.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        STORE.rooms[rid] = room
        STORE.room_order.append(rid)
        STORE.room_names.add(name.lower())
        return 201, public_room(room)


def list_rooms():
    with STORE.lock:
        return 200, {"rooms": [public_room(STORE.rooms[r]) for r in STORE.room_order]}


def get_room(rid):
    with STORE.lock:
        room = STORE.rooms.get(rid)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, public_room(room)


def availability(rid, query):
    with STORE.lock:
        room = STORE.rooms.get(rid)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        raw = query.get("date")
        if raw is None or not _DATE_RE.fullmatch(raw):
            raise ApiError(422, "validation_error", "date must be YYYY-MM-DD")
        try:
            day = dt.date.fromisoformat(raw)
        except ValueError:
            raise ApiError(422, "validation_error", "date must be a real calendar date")
        ws = dt.datetime.combine(day, WORK_START)
        we = dt.datetime.combine(day, WORK_END)
        busy = []
        for b in STORE.bookings.values():
            if b["room_id"] != rid:
                continue
            s = max(b["_start"], ws)
            e = min(b["_end"], we)
            if e > s:
                busy.append((s, e))
        busy.sort()
        free = []
        cur = ws
        for s, e in busy:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            if e > cur:
                cur = e
        if cur < we:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
        return 200, {"date": raw, "free": free}


# ---------- bookings ----------

def validate_booking(body):
    room_id = body.get("room_id")
    start_raw = body.get("start")
    end_raw = body.get("end")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str):
        raise ApiError(422, "validation_error", "room_id must be a string")
    start = parse_ts(start_raw)
    end = parse_ts(end_raw)
    if start is None or end is None:
        raise ApiError(422, "validation_error", "start and end must be timestamps YYYY-MM-DDTHH:MM:SSZ")
    for t in (start, end):
        if t.minute not in (0, 15, 30, 45) or t.second != 0:
            raise ApiError(422, "validation_error", "start and end must be on quarter hours")
    if end <= start:
        raise ApiError(422, "validation_error", "end must be after start")
    if end - start > MAX_BOOKING:
        raise ApiError(422, "validation_error", "booking may be at most 8 hours")
    if not isinstance(title, str):
        raise ApiError(422, "validation_error", "title must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise ApiError(422, "validation_error", "title must be 1 to 100 characters")
    if not is_int(attendees) or attendees < 1:
        raise ApiError(422, "validation_error", "attendees must be an integer of at least 1")
    return room_id, start, end, title, attendees


def create_booking(body, key):
    with STORE.lock:
        canon = canonical(body)
        if key is not None and key in STORE.idem:
            stored_canon, stored_resp = STORE.idem[key]
            if stored_canon == canon:
                return 201, stored_resp
            raise ApiError(422, "idempotency_mismatch", "Idempotency-Key reused with a different body")

        room_id, start, end, title, attendees = validate_booking(body)
        room = STORE.rooms.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "attendees exceed room capacity")
        for b in STORE.bookings.values():
            if b["room_id"] == room_id and b["_start"] < end and start < b["_end"]:
                raise ApiError(409, "conflict", "room is already booked for this time")

        STORE.booking_seq += 1
        bid = f"b{STORE.booking_seq}"
        rec = {"id": bid, "room_id": room_id, "title": title, "attendees": attendees,
               "_start": start, "_end": end, "_seq": STORE.booking_seq}
        STORE.bookings[bid] = rec
        resp = public_booking(rec)
        if key is not None:
            STORE.idem[key] = (canon, resp)
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


def encode_cursor(start, seq):
    raw = f"{fmt_ts(start)}|{seq}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + pad).encode()).decode("utf-8")
        ts_part, seq_part = raw.split("|")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ApiError(422, "validation_error", "invalid cursor")
    start = parse_ts(ts_part)
    if start is None or not _INT_RE.fullmatch(seq_part):
        raise ApiError(422, "validation_error", "invalid cursor")
    return start, int(seq_part)


def list_bookings(query):
    def first(name):
        vals = query.get(name)
        return vals[0] if vals else None

    room_id = first("room_id")
    from_raw, to_raw = first("from"), first("to")
    limit_raw, cursor_raw = first("limit"), first("cursor")

    frm = to = None
    if from_raw is not None:
        frm = parse_ts(from_raw)
        if frm is None:
            raise ApiError(422, "validation_error", "from must be a timestamp")
    if to_raw is not None:
        to = parse_ts(to_raw)
        if to is None:
            raise ApiError(422, "validation_error", "to must be a timestamp")
    limit = 20
    if limit_raw is not None:
        if not _INT_RE.fullmatch(limit_raw) or not (1 <= int(limit_raw) <= 100):
            raise ApiError(422, "validation_error", "limit must be an integer from 1 to 100")
        limit = int(limit_raw)
    after = None
    if cursor_raw is not None:
        after = decode_cursor(cursor_raw)

    with STORE.lock:
        items = []
        for b in STORE.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not (b["_end"] > frm):
                continue
            if to is not None and not (b["_start"] < to):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["_start"], b["_seq"]))
        if after is not None:
            items = [b for b in items if (b["_start"], b["_seq"]) > after]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(last["_start"], last["_seq"])
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


# ---------- routing ----------

ROUTES = {
    "/rooms": {"GET": "list_rooms", "POST": "create_room"},
    "/rooms/{id}": {"GET": "get_room"},
    "/rooms/{id}/availability": {"GET": "availability"},
    "/bookings": {"GET": "list_bookings", "POST": "create_booking"},
    "/bookings/{id}": {"GET": "get_booking", "DELETE": "delete_booking"},
}


def match_route(path):
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "":
        return None, None
    segs = parts[1:]
    if segs == ["rooms"]:
        return "/rooms", {}
    if segs == ["bookings"]:
        return "/bookings", {}
    if len(segs) == 2 and segs[0] == "rooms" and segs[1]:
        return "/rooms/{id}", {"id": segs[1]}
    if len(segs) == 3 and segs[0] == "rooms" and segs[1] and segs[2] == "availability":
        return "/rooms/{id}/availability", {"id": segs[1]}
    if len(segs) == 2 and segs[0] == "bookings" and segs[1]:
        return "/bookings/{id}", {"id": segs[1]}
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _send(self, status, payload=None, extra_headers=None):
        self.send_response(status)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        if payload is None:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        self._send(status, {"error": {"code": code, "message": message}})

    def _read_body(self):
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _dispatch(self, method):
        try:
            split = urlsplit(self.path)
            pattern, params = match_route(split.path)
            if pattern is None:
                self._error(404, "not_found", "no such path")
                return
            handlers = ROUTES[pattern]
            if method not in handlers:
                self._error(405, "method_not_allowed", "method not allowed",)
                return
            name = handlers[method]
            query = parse_qs(split.query, keep_blank_values=True)
            body_raw = self._read_body()

            if name == "create_room" or name == "create_booking":
                try:
                    body = json.loads(body_raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ApiError(400, "bad_json", "request body is not valid JSON")
                if not isinstance(body, dict):
                    raise ApiError(400, "bad_json", "request body must be a JSON object")
                if name == "create_room":
                    status, payload = create_room(body)
                else:
                    key = self.headers.get("Idempotency-Key")
                    if key is not None and key == "":
                        key = None
                    status, payload = create_booking(body, key)
                self._send(status, payload)
                return

            if name == "list_rooms":
                status, payload = list_rooms()
            elif name == "get_room":
                status, payload = get_room(params["id"])
            elif name == "availability":
                status, payload = availability(params["id"], {k: v[0] for k, v in query.items()})
            elif name == "list_bookings":
                status, payload = list_bookings(query)
            elif name == "get_booking":
                status, payload = get_booking(params["id"])
            elif name == "delete_booking":
                status, payload = delete_booking(params["id"])
            else:
                raise ApiError(404, "not_found", "no such path")
            self._send(status, payload)
        except ApiError as e:
            self._error(e.status, e.code, e.message)
        except Exception:  # pragma: no cover - safety net
            self._error(500, "internal_error", "internal server error")

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


def main():
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
