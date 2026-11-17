"""bookings: meeting room booking HTTP JSON API (standard library only)."""
import argparse
import base64
import copy
import json
import re
import threading
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

LOCK = threading.Lock()
ROOMS = {}          # id -> public room dict
ROOM_ORDER = []     # ids in creation order
ROOM_NAMES = {}     # casefolded name -> id
BOOKINGS = {}       # id -> internal booking dict (deleted ones removed)
IDEM = {}           # idempotency key -> (request body, response body)
COUNTERS = {"room": 0, "booking": 0}


class ApiError(Exception):
    def __init__(self, status, code, message="error"):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def vfail(msg="invalid request"):
    raise ApiError(422, "validation_error", msg)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_ts(v):
    if not isinstance(v, str) or not TS_RE.fullmatch(v):
        return None
    try:
        return datetime.strptime(v, TS_FMT)
    except ValueError:
        return None


def fmt_ts(dt):
    return dt.strftime(TS_FMT)


def parse_date(v):
    if not isinstance(v, str) or not DATE_RE.fullmatch(v):
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return None


def json_body(raw):
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError(400, "bad_json", "request body must be a JSON object")
    return obj


def encode_cursor(start_str, seq):
    raw = json.dumps([start_str, seq], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(c):
    try:
        padded = c + "=" * (-len(c) % 4)
        obj = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if (isinstance(obj, list) and len(obj) == 2 and isinstance(obj[0], str)
                and is_int(obj[1])):
            return obj[0], obj[1]
    except Exception:
        pass
    vfail("invalid cursor")


def public_booking(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


# ---------- rooms ----------

def h_create_room(args, q, headers, raw):
    body = json_body(raw)
    name = body.get("name")
    cap = body.get("capacity")
    if not isinstance(name, str):
        vfail("name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        vfail("name must be 1 to 50 characters")
    if not is_int(cap) or not (1 <= cap <= 500):
        vfail("capacity must be an integer from 1 to 500")
    with LOCK:
        key = name.casefold()
        if key in ROOM_NAMES:
            raise ApiError(409, "room_exists", "a room with this name already exists")
        COUNTERS["room"] += 1
        rid = "r%d" % COUNTERS["room"]
        room = {"id": rid, "name": name, "capacity": cap}
        ROOMS[rid] = room
        ROOM_ORDER.append(rid)
        ROOM_NAMES[key] = rid
        return 201, dict(room)


def h_list_rooms(args, q, headers, raw):
    with LOCK:
        return 200, {"rooms": [dict(ROOMS[r]) for r in ROOM_ORDER]}


def h_get_room(args, q, headers, raw):
    with LOCK:
        room = ROOMS.get(args["id"])
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        return 200, dict(room)


# ---------- bookings ----------

def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str):
        vfail("room_id must be a string")
    start = parse_ts(body.get("start"))
    end = parse_ts(body.get("end"))
    if start is None or end is None:
        vfail("start and end must be valid timestamps")
    for dt in (start, end):
        if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
            vfail("timestamps must be on quarter hours with zero seconds")
    if not end > start:
        vfail("end must be after start")
    if end - start > timedelta(hours=8):
        vfail("booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str):
        vfail("title must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        vfail("title must be 1 to 100 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        vfail("attendees must be an integer of at least 1")
    return room_id, start, end, title, attendees


def h_create_booking(args, q, headers, raw):
    body = json_body(raw)
    key = headers.get("Idempotency-Key") or None
    with LOCK:
        if key is not None and key in IDEM:
            stored_body, resp = IDEM[key]
            if stored_body == body:
                return 201, dict(resp)
            raise ApiError(422, "idempotency_mismatch",
                           "idempotency key was used with a different request body")
        room_id, start, end, title, attendees = validate_booking(body)
        room = ROOMS.get(room_id)
        if room is None:
            raise ApiError(404, "not_found", "room not found")
        if attendees > room["capacity"]:
            raise ApiError(422, "over_capacity", "attendees exceed room capacity")
        for other in BOOKINGS.values():
            if other["room_id"] == room_id and other["start"] < end and start < other["end"]:
                raise ApiError(409, "conflict", "booking overlaps an existing booking")
        COUNTERS["booking"] += 1
        seq = COUNTERS["booking"]
        b = {"seq": seq, "id": "b%d" % seq, "room_id": room_id, "start": start,
             "end": end, "title": title, "attendees": attendees}
        BOOKINGS[b["id"]] = b
        pub = public_booking(b)
        if key is not None:
            IDEM[key] = (copy.deepcopy(body), copy.deepcopy(pub))
        return 201, pub


def h_get_booking(args, q, headers, raw):
    with LOCK:
        b = BOOKINGS.get(args["id"])
        if b is None:
            raise ApiError(404, "not_found", "booking not found")
        return 200, public_booking(b)


def h_delete_booking(args, q, headers, raw):
    with LOCK:
        if BOOKINGS.pop(args["id"], None) is None:
            raise ApiError(404, "not_found", "booking not found")
        return 204, None


def h_list_bookings(args, q, headers, raw):
    room_id = q.get("room_id")
    frm = None
    to = None
    if "from" in q:
        frm = parse_ts(q["from"])
        if frm is None:
            vfail("from must be a valid timestamp")
    if "to" in q:
        to = parse_ts(q["to"])
        if to is None:
            vfail("to must be a valid timestamp")
    limit = 20
    if "limit" in q:
        v = q["limit"]
        if not INT_RE.fullmatch(v) or not (1 <= int(v) <= 100):
            vfail("limit must be an integer from 1 to 100")
        limit = int(v)
    cursor = None
    if "cursor" in q:
        cursor = decode_cursor(q["cursor"])

    with LOCK:
        items = []
        for b in BOOKINGS.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not b["end"] > frm:
                continue
            if to is not None and not b["start"] < to:
                continue
            items.append(b)
        items.sort(key=lambda b: (fmt_ts(b["start"]), b["seq"]))
        if cursor is not None:
            items = [b for b in items if (fmt_ts(b["start"]), b["seq"]) > cursor]
        page = items[:limit]
        next_cursor = None
        if len(items) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(fmt_ts(last["start"]), last["seq"])
        return 200, {"bookings": [public_booking(b) for b in page], "next_cursor": next_cursor}


# ---------- availability ----------

def h_availability(args, q, headers, raw):
    with LOCK:
        if args["id"] not in ROOMS:
            raise ApiError(404, "not_found", "room not found")
        d = parse_date(q.get("date"))
        if d is None:
            vfail("date must be a valid YYYY-MM-DD date")
        ws = datetime.combine(d, time(8, 0, 0))
        we = datetime.combine(d, time(18, 0, 0))
        spans = []
        for b in BOOKINGS.values():
            if b["room_id"] == args["id"] and b["start"] < we and b["end"] > ws:
                spans.append((max(b["start"], ws), min(b["end"], we)))
        spans.sort(key=lambda s: s[0])
        free = []
        cur = ws
        for s, e in spans:
            if s > cur:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
            if e > cur:
                cur = e
        if cur < we:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
        return 200, {"date": d.isoformat(), "free": free}


# ---------- routing ----------

def match(segs):
    n = len(segs)
    if n == 1 and segs[0] == "rooms":
        return {"GET": h_list_rooms, "POST": h_create_room}, {}
    if n == 2 and segs[0] == "rooms":
        return {"GET": h_get_room}, {"id": segs[1]}
    if n == 3 and segs[0] == "rooms" and segs[2] == "availability":
        return {"GET": h_availability}, {"id": segs[1]}
    if n == 1 and segs[0] == "bookings":
        return {"GET": h_list_bookings, "POST": h_create_booking}, {}
    if n == 2 and segs[0] == "bookings":
        return {"GET": h_get_booking, "DELETE": h_delete_booking}, {"id": segs[1]}
    return None


def dispatch(method, raw_path, query, headers, raw):
    path = unquote(raw_path).rstrip("/") or "/"
    segs = path.split("/")[1:]
    found = match(segs)
    if found is None:
        raise ApiError(404, "not_found", "no such path")
    table, args = found
    handler = table.get(method)
    if handler is None:
        raise ApiError(405, "method_not_allowed", "method not allowed for this path")
    q = {}
    for k, v in parse_qsl(query, keep_blank_values=True):
        q.setdefault(k, v)
    return handler(args, q, headers, raw)


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, format, *args):
        pass

    def _send(self, status, body, method):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        if status != 204:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD" and data:
            self.wfile.write(data)

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        parts = urlsplit(self.path)
        try:
            status, body = dispatch(method, parts.path, parts.query, self.headers, raw)
        except ApiError as e:
            status = e.status
            body = {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status = 500
            body = {"error": {"code": "internal_error", "message": "internal server error"}}
        self._send(status, body, method)

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

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def do_HEAD(self):
        self._handle("HEAD")


def main():
    parser = argparse.ArgumentParser(description="bookings API server")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
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
