"""bookings: meeting room booking HTTP JSON API (standard library only)."""
import argparse
import base64
import binascii
import datetime as dt
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

WORK_START = dt.time(8, 0, 0)
WORK_END = dt.time(18, 0, 0)
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^[0-9]+$")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_BOOKING = dt.timedelta(hours=8)

LOCK = threading.Lock()
ROOMS = {}          # id -> room dict
ROOM_ORDER = []     # room ids in creation order
ROOM_NAMES = {}     # casefolded name -> id
BOOKINGS = {}       # id -> booking record
BOOKING_SEQ = [0]
ROOM_SEQ = [0]
IDEM = {}           # idempotency key -> (request body, response body)


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message or code


class Result:
    def __init__(self, status, body=None):
        self.status = status
        self.body = body


# ---------- helpers ----------

def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def strict_eq(a, b):
    """JSON equality where booleans never equal numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(strict_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(strict_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    return a == b


def parse_ts(s):
    """Parse a strict UTC timestamp. Returns datetime or None."""
    if not isinstance(s, str) or not TS_RE.match(s):
        return None
    try:
        return dt.datetime.strptime(s, TS_FMT)
    except ValueError:
        return None


def fmt_ts(d):
    return d.strftime(TS_FMT)


def room_out(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"],
            "buffer_minutes": r["buffer_minutes"]}


def booking_out(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": b["start"],
        "end": b["end"],
        "title": b["title"],
        "attendees": b["attendees"],
    }


def verr(msg):
    return ApiError(422, "validation_error", msg)


# ---------- rooms ----------

def create_room(body):
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str):
        raise verr("name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise verr("name must be 1 to 50 characters")
    if not is_int(cap) or not (1 <= cap <= 500):
        raise verr("capacity must be an integer from 1 to 500")
    buf = body.get("buffer_minutes", 0)
    if not is_int(buf) or not (0 <= buf <= 60):
        raise verr("buffer_minutes must be an integer from 0 to 60")
    key = name.casefold()
    if key in ROOM_NAMES:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    ROOM_SEQ[0] += 1
    rid = f"r{ROOM_SEQ[0]}"
    room = {"id": rid, "name": name, "capacity": cap, "buffer_minutes": buf}
    ROOMS[rid] = room
    ROOM_ORDER.append(rid)
    ROOM_NAMES[key] = rid
    return Result(201, room_out(room))


def list_rooms():
    return Result(200, {"rooms": [room_out(ROOMS[r]) for r in ROOM_ORDER]})


def get_room(rid):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return Result(200, room_out(room))


def availability(rid, query):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    vals = query.get("date")
    if vals is None:
        raise verr("date is required")
    date_s = vals[0]
    if not DATE_RE.match(date_s):
        raise verr("date must be YYYY-MM-DD")
    try:
        day = dt.datetime.strptime(date_s, "%Y-%m-%d").date()
    except ValueError:
        raise verr("date is not a valid calendar date")
    day_start = dt.datetime.combine(day, WORK_START)
    day_end = dt.datetime.combine(day, WORK_END)
    buf = dt.timedelta(minutes=room["buffer_minutes"])
    min_free = dt.timedelta(minutes=15)
    busy = []
    for b in BOOKINGS.values():
        if b["room_id"] != rid:
            continue
        bs = b["start_dt"] - buf
        be = b["end_dt"] + buf
        if bs < day_end and be > day_start:
            busy.append((max(bs, day_start), min(be, day_end)))
    busy.sort()
    free = []
    cur = day_start
    for s, e in busy:
        if s > cur and s - cur >= min_free:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < day_end and day_end - cur >= min_free:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
    return Result(200, {"date": date_s, "free": free})


# ---------- bookings ----------

def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        raise verr("room_id must be a string")
    start_s = body.get("start")
    end_s = body.get("end")
    if not isinstance(start_s, str) or not isinstance(end_s, str):
        raise verr("start and end must be timestamps")
    start = parse_ts(start_s)
    end = parse_ts(end_s)
    if start is None or end is None:
        raise verr("start and end must be timestamps YYYY-MM-DDTHH:MM:SSZ")
    for d in (start, end):
        if d.minute not in (0, 15, 30, 45) or d.second != 0:
            raise verr("start and end must be on quarter-hour boundaries")
    if end <= start:
        raise verr("end must be after start")
    if end - start > MAX_BOOKING:
        raise verr("booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        raise verr("title must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise verr("title must be 1 to 100 characters")
    att = body.get("attendees")
    if not is_int(att) or att < 1:
        raise verr("attendees must be an integer of at least 1")
    return {
        "room_id": room_id,
        "start_s": start_s,
        "end_s": end_s,
        "start_dt": start,
        "end_dt": end,
        "title": title,
        "attendees": att,
    }


def find_conflict(room, start, end, exclude_id=None):
    """True if [start, end) breaks the room's buffer rule against another booking."""
    buf = dt.timedelta(minutes=room["buffer_minutes"])
    for b in BOOKINGS.values():
        if b["room_id"] != room["id"] or b["id"] == exclude_id:
            continue
        if b["start_dt"] < end + buf and start < b["end_dt"] + buf:
            return True
    return False


def create_booking(body, idem_key):
    if idem_key is not None and idem_key in IDEM:
        stored_body, stored_resp = IDEM[idem_key]
        if strict_eq(stored_body, body):
            return Result(201, stored_resp)
        raise ApiError(422, "idempotency_mismatch",
                       "Idempotency-Key was already used with a different body")
    v = validate_booking(body)
    room = ROOMS.get(v["room_id"])
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if v["attendees"] > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    if find_conflict(room, v["start_dt"], v["end_dt"]):
        raise ApiError(409, "conflict", "booking overlaps an existing booking or buffer")
    BOOKING_SEQ[0] += 1
    seq = BOOKING_SEQ[0]
    rec = {
        "id": f"b{seq}",
        "seq": seq,
        "room_id": room["id"],
        "start": v["start_s"],
        "end": v["end_s"],
        "start_dt": v["start_dt"],
        "end_dt": v["end_dt"],
        "title": v["title"],
        "attendees": v["attendees"],
    }
    BOOKINGS[rec["id"]] = rec
    out = booking_out(rec)
    if idem_key is not None:
        IDEM[idem_key] = (body, out)
    return Result(201, out)


def get_booking(bid):
    b = BOOKINGS.get(bid)
    if b is None:
        raise ApiError(404, "not_found", "booking not found")
    return Result(200, booking_out(b))


def patch_booking(bid, body):
    rec = BOOKINGS.get(bid)
    if rec is None:
        raise ApiError(404, "not_found", "booking not found")
    merged = {
        "room_id": rec["room_id"],
        "start": body["start"] if "start" in body else rec["start"],
        "end": body["end"] if "end" in body else rec["end"],
        "title": body["title"] if "title" in body else rec["title"],
        "attendees": body["attendees"] if "attendees" in body else rec["attendees"],
    }
    v = validate_booking(merged)
    room = ROOMS[rec["room_id"]]
    if v["attendees"] > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    if find_conflict(room, v["start_dt"], v["end_dt"], exclude_id=bid):
        raise ApiError(409, "conflict", "booking overlaps an existing booking or buffer")
    rec["start"] = v["start_s"]
    rec["end"] = v["end_s"]
    rec["start_dt"] = v["start_dt"]
    rec["end_dt"] = v["end_dt"]
    rec["title"] = v["title"]
    rec["attendees"] = v["attendees"]
    return Result(200, booking_out(rec))


def delete_booking(bid):
    if bid not in BOOKINGS:
        raise ApiError(404, "not_found", "booking not found")
    del BOOKINGS[bid]
    return Result(204, None)


def encode_cursor(b):
    raw = json.dumps({"s": b["start"], "q": b["seq"]}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(c):
    try:
        raw = base64.urlsafe_b64decode(c.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, binascii.Error):
        raise verr("invalid cursor")
    if not isinstance(obj, dict) or not isinstance(obj.get("s"), str) or not is_int(obj.get("q")):
        raise verr("invalid cursor")
    d = parse_ts(obj["s"])
    if d is None:
        raise verr("invalid cursor")
    return d, obj["q"]


def single(query, name):
    vals = query.get(name)
    return None if vals is None else vals[0]


def list_bookings(query):
    room_id = single(query, "room_id")
    frm_s = single(query, "from")
    to_s = single(query, "to")
    limit_s = single(query, "limit")
    cursor_s = single(query, "cursor")

    frm = to = None
    if frm_s is not None:
        frm = parse_ts(frm_s)
        if frm is None:
            raise verr("from must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    if to_s is not None:
        to = parse_ts(to_s)
        if to is None:
            raise verr("to must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    limit = 20
    if limit_s is not None:
        if not INT_RE.match(limit_s) or not (1 <= int(limit_s) <= 100):
            raise verr("limit must be an integer from 1 to 100")
        limit = int(limit_s)
    after = None
    if cursor_s is not None:
        after = decode_cursor(cursor_s)

    matches = []
    for b in BOOKINGS.values():
        if room_id is not None and b["room_id"] != room_id:
            continue
        if frm is not None and not (b["end_dt"] > frm):
            continue
        if to is not None and not (b["start_dt"] < to):
            continue
        matches.append(b)
    total = len(matches)
    items = [b for b in matches
             if after is None or (b["start_dt"], b["seq"]) > after]
    items.sort(key=lambda b: (b["start_dt"], b["seq"]))
    page = items[:limit]
    next_cursor = None
    if len(items) > limit:
        next_cursor = encode_cursor(page[-1])
    return Result(200, {"bookings": [booking_out(b) for b in page],
                        "next_cursor": next_cursor, "total": total})


# ---------- routing ----------

def route(method, path, query, body_fn, headers):
    """Returns Result. Raises ApiError."""
    parts = path.split("/")
    # parts[0] == "" for absolute paths
    if len(parts) == 2 and parts[1] == "rooms":
        allowed = {"GET": lambda: list_rooms(), "POST": lambda: create_room(body_fn())}
    elif len(parts) == 3 and parts[1] == "rooms" and parts[2]:
        rid = parts[2]
        allowed = {"GET": lambda: get_room(rid)}
    elif len(parts) == 4 and parts[1] == "rooms" and parts[2] and parts[3] == "availability":
        rid = parts[2]
        allowed = {"GET": lambda: availability(rid, query)}
    elif len(parts) == 2 and parts[1] == "bookings":
        key = headers.get("Idempotency-Key")
        if key is not None and key.strip() == "":
            key = None
        allowed = {
            "GET": lambda: list_bookings(query),
            "POST": lambda: create_booking(body_fn(), key),
        }
    elif len(parts) == 3 and parts[1] == "bookings" and parts[2]:
        bid = parts[2]
        allowed = {
            "GET": lambda: get_booking(bid),
            "DELETE": lambda: delete_booking(bid),
            "PATCH": lambda: patch_booking(bid, body_fn()),
        }
    else:
        raise ApiError(404, "not_found", "no such path")
    if method not in allowed:
        raise ApiError(405, "method_not_allowed", "method not allowed")
    return allowed[method]()


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _send(self, status, body=None, head=False):
        data = b""
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        if status != 204:
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data and not head:
            self.wfile.write(data)

    def _error(self, e, head=False):
        self._send(e.status, {"error": {"code": e.code, "message": e.message}}, head)

    def _handle(self, method):
        head = method == "HEAD"
        length = self.headers.get("Content-Length")
        raw = b""
        if length:
            try:
                n = int(length)
            except ValueError:
                n = 0
            if n > 0:
                raw = self.rfile.read(n)

        def body_fn():
            try:
                text = raw.decode("utf-8")
                obj = json.loads(text)
            except (UnicodeError, ValueError):
                raise ApiError(400, "bad_json", "request body is not valid JSON")
            if not isinstance(obj, dict):
                raise ApiError(400, "bad_json", "request body must be a JSON object")
            return obj

        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            with LOCK:
                if method == "HEAD":
                    # HEAD is not supported on any route
                    res = route("HEAD", url.path, query, body_fn, self.headers)
                else:
                    res = route(method, url.path, query, body_fn, self.headers)
            self._send(res.status, res.body, head)
        except ApiError as e:
            self._error(e, head)
        except Exception:  # pragma: no cover - safety net
            self._send(500, {"error": {"code": "internal_error", "message": "internal error"}}, head)

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

    def do_HEAD(self):
        self._handle("HEAD")

    def do_OPTIONS(self):
        self._handle("OPTIONS")


def main(argv=None):
    p = argparse.ArgumentParser(description="bookings API server")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
