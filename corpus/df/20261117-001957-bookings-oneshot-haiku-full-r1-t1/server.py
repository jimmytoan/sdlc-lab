"""bookings: meeting room booking API (standard library only)."""
import argparse
import base64
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")

LOCK = threading.Lock()
ROOMS = {}          # "r1" -> {"id", "name", "capacity"}
ROOM_ORDER = []     # room ids in creation order
ROOM_NAMES = set()  # casefolded trimmed names
ROOM_SEQ = [0]
BOOKINGS = {}       # "b1" -> record (with datetimes and seq)
BOOKING_SEQ = [0]
IDEM = {}           # idempotency key -> (canonical body, 201 payload)


class ApiError(Exception):
    def __init__(self, status, code, msg=""):
        super().__init__(msg)
        self.status = status
        self.code = code
        self.msg = msg or code


def err_body(code, msg):
    return {"error": {"code": code, "message": msg}}


def parse_ts(s):
    if not isinstance(s, str):
        return None
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    try:
        return dt.datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(s):
    if not isinstance(s, str) or not DATE_RE.fullmatch(s):
        return None
    try:
        return dt.date(*(int(x) for x in s.split("-")))
    except ValueError:
        return None


def parse_obj(raw):
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "bad_json", "body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError(400, "bad_json", "body must be a JSON object")
    return obj


def validation(msg):
    return ApiError(422, "validation_error", msg)


# ---------- rooms ----------

def list_rooms():
    return 200, {"rooms": [dict(ROOMS[i]) for i in ROOM_ORDER]}


def create_room(raw):
    obj = parse_obj(raw)
    name = obj.get("name")
    cap = obj.get("capacity")
    if not isinstance(name, str):
        raise validation("name must be a string")
    name = name.strip()
    if not (1 <= len(name) <= 50):
        raise validation("name must be 1 to 50 characters")
    if type(cap) is not int or not (1 <= cap <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    key = name.casefold()
    if key in ROOM_NAMES:
        raise ApiError(409, "room_exists", "a room with this name already exists")
    ROOM_SEQ[0] += 1
    rid = f"r{ROOM_SEQ[0]}"
    room = {"id": rid, "name": name, "capacity": cap}
    ROOMS[rid] = room
    ROOM_ORDER.append(rid)
    ROOM_NAMES.add(key)
    return 201, dict(room)


def get_room(rid):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "no such room")
    return 200, dict(room)


# ---------- bookings ----------

def booking_out(rec):
    return {
        "id": rec["id"],
        "room_id": rec["room_id"],
        "start": fmt_ts(rec["start"]),
        "end": fmt_ts(rec["end"]),
        "title": rec["title"],
        "attendees": rec["attendees"],
    }


def create_booking(raw, headers):
    obj = parse_obj(raw)
    key = headers.get("Idempotency-Key")
    if key is not None and key == "":
        key = None
    canon = json.dumps(obj, sort_keys=True)
    if key is not None and key in IDEM:
        stored_canon, stored_payload = IDEM[key]
        if stored_canon == canon:
            return 201, dict(stored_payload)
        raise ApiError(422, "idempotency_mismatch", "key was used with a different body")

    room_id = obj.get("room_id")
    start_s = obj.get("start")
    end_s = obj.get("end")
    title = obj.get("title")
    attendees = obj.get("attendees")
    if not isinstance(room_id, str):
        raise validation("room_id must be a string")
    if not isinstance(title, str):
        raise validation("title must be a string")
    if type(attendees) is not int or attendees < 1:
        raise validation("attendees must be an integer of at least 1")
    start = parse_ts(start_s)
    end = parse_ts(end_s)
    if start is None or end is None:
        raise validation("start and end must be timestamps YYYY-MM-DDTHH:MM:SSZ")
    for d in (start, end):
        if d.minute % 15 != 0 or d.second != 0:
            raise validation("start and end must be on quarter hours with zero seconds")
    if end <= start:
        raise validation("end must be after start")
    if end - start > dt.timedelta(hours=8):
        raise validation("booking may be at most 8 hours long")
    t = title.strip()
    if not (1 <= len(t) <= 100):
        raise validation("title must be 1 to 100 characters")

    room = ROOMS.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "no such room")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    for rec in BOOKINGS.values():
        if rec["room_id"] == room_id and rec["start"] < end and start < rec["end"]:
            raise ApiError(409, "conflict", "booking overlaps an existing booking")

    BOOKING_SEQ[0] += 1
    seq = BOOKING_SEQ[0]
    rec = {
        "id": f"b{seq}",
        "seq": seq,
        "room_id": room_id,
        "start": start,
        "end": end,
        "title": t,
        "attendees": attendees,
    }
    BOOKINGS[rec["id"]] = rec
    payload = booking_out(rec)
    if key is not None:
        IDEM[key] = (canon, dict(payload))
    return 201, payload


def encode_cursor(rec):
    raw = json.dumps([fmt_ts(rec["start"]), rec["seq"]]).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(s):
    try:
        raw = base64.urlsafe_b64decode(s.encode("ascii"))
        arr = json.loads(raw.decode("utf-8"))
        if isinstance(arr, list) and len(arr) == 2 and isinstance(arr[0], str) and type(arr[1]) is int:
            st = parse_ts(arr[0])
            if st is not None:
                return (st, arr[1])
    except Exception:
        pass
    return None


def list_bookings(query):
    def first(k):
        v = query.get(k)
        return v[0] if v else ""

    from_dt = None
    to_dt = None
    if "from" in query:
        from_dt = parse_ts(first("from"))
        if from_dt is None:
            raise validation("from must be a timestamp")
    if "to" in query:
        to_dt = parse_ts(first("to"))
        if to_dt is None:
            raise validation("to must be a timestamp")
    limit = 20
    if "limit" in query:
        v = first("limit")
        if not INT_RE.fullmatch(v):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(v)
        if not (1 <= limit <= 100):
            raise validation("limit must be an integer from 1 to 100")
    cur = None
    if "cursor" in query:
        cur = decode_cursor(first("cursor"))
        if cur is None:
            raise validation("invalid cursor")
    room_filter = first("room_id") if "room_id" in query else None

    items = []
    for rec in sorted(BOOKINGS.values(), key=lambda r: (r["start"], r["seq"])):
        if room_filter is not None and rec["room_id"] != room_filter:
            continue
        if from_dt is not None and not (rec["end"] > from_dt):
            continue
        if to_dt is not None and not (rec["start"] < to_dt):
            continue
        if cur is not None and not ((rec["start"], rec["seq"]) > cur):
            continue
        items.append(rec)
    page = items[:limit]
    nxt = encode_cursor(page[-1]) if (len(items) > limit and page) else None
    return 200, {"bookings": [booking_out(r) for r in page], "next_cursor": nxt}


def get_booking(bid):
    rec = BOOKINGS.get(bid)
    if rec is None:
        raise ApiError(404, "not_found", "no such booking")
    return 200, booking_out(rec)


def delete_booking(bid):
    if bid not in BOOKINGS:
        raise ApiError(404, "not_found", "no such booking")
    del BOOKINGS[bid]
    return 204, None


def availability(rid, query):
    room = ROOMS.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "no such room")
    date_v = query.get("date", [None])[0]
    d = parse_date(date_v)
    if d is None:
        raise validation("date must be a real calendar date YYYY-MM-DD")
    ws = dt.datetime(d.year, d.month, d.day, 8, 0, 0)
    we = dt.datetime(d.year, d.month, d.day, 18, 0, 0)
    busy = sorted(
        ((r["start"], r["end"]) for r in BOOKINGS.values()
         if r["room_id"] == rid and r["start"] < we and ws < r["end"]),
        key=lambda x: x[0],
    )
    free = []
    cur = ws
    for s, e in busy:
        s = max(s, ws)
        e = min(e, we)
        if s > cur:
            free.append((cur, s))
        if e > cur:
            cur = e
    if cur < we:
        free.append((cur, we))
    return 200, {
        "date": date_v,
        "free": [{"start": fmt_ts(a), "end": fmt_ts(b)} for a, b in free],
    }


# ---------- routing ----------

def route(method, target, raw, headers):
    parts = urlsplit(target)
    query = parse_qs(parts.query, keep_blank_values=True)
    segs = [unquote(x) for x in parts.path.split("/")]
    n = len(segs)
    if n < 2 or segs[0] != "":
        raise ApiError(404, "not_found", "no such path")
    head = segs[1]
    table = None
    if head == "rooms":
        if n == 2:
            table = {"GET": lambda: list_rooms(), "POST": lambda: create_room(raw)}
        elif n == 3 and segs[2]:
            rid = segs[2]
            table = {"GET": lambda: get_room(rid)}
        elif n == 4 and segs[2] and segs[3] == "availability":
            rid = segs[2]
            table = {"GET": lambda: availability(rid, query)}
    elif head == "bookings":
        if n == 2:
            table = {
                "GET": lambda: list_bookings(query),
                "POST": lambda: create_booking(raw, headers),
            }
        elif n == 3 and segs[2]:
            bid = segs[2]
            table = {
                "GET": lambda: get_booking(bid),
                "DELETE": lambda: delete_booking(bid),
            }
    if table is None:
        raise ApiError(404, "not_found", "no such path")
    fn = table.get(method)
    if fn is None:
        raise ApiError(405, "method_not_allowed", "method not allowed for this path")
    with LOCK:
        return fn()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _handle(self, method):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            status, payload = route(method, self.path, raw, self.headers)
        except ApiError as e:
            status, payload = e.status, err_body(e.code, e.msg)
        except Exception:
            status, payload = 500, err_body("internal_error", "internal error")
        self._send(status, payload)

    def _send(self, status, payload):
        if payload is None:
            data = b""
        else:
            data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)

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
