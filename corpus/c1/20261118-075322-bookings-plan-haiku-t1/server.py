#!/usr/bin/env python3
"""bookings: meeting room booking JSON API (standard library only)."""

import argparse
import base64
import binascii
import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

UTC = dt.timezone.utc
WORK_START = (8, 0)
WORK_END = (18, 0)
MAX_DURATION = dt.timedelta(hours=8)

_TS_RE = re.compile(r"\A([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z\Z")
_DATE_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_INT_RE = re.compile(r"\A[0-9]+\Z")


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

    def __init__(self, code, message=None):
        super().__init__(code)
        self.code = code
        self.status = self.STATUS[code]
        self.message = message or code.replace("_", " ")


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}          # id -> room dict
        self.room_names = set()
        self.room_seq = 0
        self.bookings = {}       # id -> booking dict (internal datetimes)
        self.booking_seq = 0
        self.idem = {}           # key -> (fingerprint, status, body)


# ---------- helpers ----------

def fmt_ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    """Strict YYYY-MM-DDTHH:MM:SSZ. Returns aware datetime or None."""
    if not isinstance(value, str):
        return None
    m = _TS_RE.match(value)
    if not m:
        return None
    try:
        return dt.datetime(*(int(g) for g in m.groups()), tzinfo=UTC)
    except ValueError:
        return None


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def room_out(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_out(b):
    return {
        "id": b["id"],
        "room_id": b["room_id"],
        "start": fmt_ts(b["start"]),
        "end": fmt_ts(b["end"]),
        "title": b["title"],
        "attendees": b["attendees"],
    }


def validation(msg):
    return ApiError("validation_error", msg)


# ---------- rooms ----------

def create_room(store, body, _query=None):
    name = body.get("name")
    capacity = body.get("capacity")
    if not isinstance(name, str) or not (1 <= len(name) <= 50):
        raise validation("name must be a string of 1 to 50 characters")
    if not is_int(capacity) or not (1 <= capacity <= 500):
        raise validation("capacity must be an integer from 1 to 500")
    with store.lock:
        if name in store.room_names:
            raise ApiError("room_exists", "a room with this name already exists")
        store.room_seq += 1
        rid = f"r{store.room_seq}"
        room = {"id": rid, "name": name, "capacity": capacity}
        store.rooms[rid] = room
        store.room_names.add(name)
        return 201, room_out(room)


def list_rooms(store, _body, _query):
    with store.lock:
        return 200, {"rooms": [room_out(r) for r in store.rooms.values()]}


def get_room(store, room_id, _body, _query):
    with store.lock:
        room = store.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        return 200, room_out(room)


def availability(store, room_id, _body, query):
    with store.lock:
        room = store.rooms.get(room_id)
        if room is None:
            raise ApiError("not_found", "room not found")
        date_s = query.get("date")
        if date_s is None or not _DATE_RE.match(date_s):
            raise validation("date must be YYYY-MM-DD")
        try:
            day = dt.date.fromisoformat(date_s)
        except ValueError:
            raise validation("date must be a real calendar date")
        ws = dt.datetime(day.year, day.month, day.day, *WORK_START, tzinfo=UTC)
        we = dt.datetime(day.year, day.month, day.day, *WORK_END, tzinfo=UTC)
        busy = []
        for b in store.bookings.values():
            if b["room_id"] != room_id:
                continue
            if b["start"] < we and b["end"] > ws:
                busy.append((max(b["start"], ws), min(b["end"], we)))
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
        return 200, {"date": date_s, "free": free}


# ---------- bookings ----------

def validate_booking(body):
    room_id = body.get("room_id")
    if not isinstance(room_id, str) or room_id == "":
        raise validation("room_id must be a non-empty string")
    start = parse_ts(body.get("start"))
    if start is None:
        raise validation("start must be a timestamp like 2026-03-02T09:00:00Z")
    end = parse_ts(body.get("end"))
    if end is None:
        raise validation("end must be a timestamp like 2026-03-02T09:00:00Z")
    for d in (start, end):
        if d.minute % 15 != 0 or d.second != 0:
            raise validation("start and end must be on quarter hours")
    if end <= start:
        raise validation("end must be after start")
    if end - start > MAX_DURATION:
        raise validation("booking may be at most 8 hours long")
    title = body.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 200):
        raise validation("title must be a string of 1 to 200 characters")
    attendees = body.get("attendees")
    if not is_int(attendees) or attendees < 1:
        raise validation("attendees must be an integer of at least 1")
    return {"room_id": room_id, "start": start, "end": end, "title": title, "attendees": attendees}


def fingerprint(v):
    return json.dumps({
        "room_id": v["room_id"],
        "start": fmt_ts(v["start"]),
        "end": fmt_ts(v["end"]),
        "title": v["title"],
        "attendees": v["attendees"],
    }, sort_keys=True, separators=(",", ":"))


def create_booking(store, body, idem_key):
    v = validate_booking(body)
    with store.lock:
        fp = None
        if idem_key is not None:
            if idem_key == "" or len(idem_key) > 255:
                raise validation("Idempotency-Key must be 1 to 255 characters")
            fp = fingerprint(v)
            seen = store.idem.get(idem_key)
            if seen is not None:
                if seen[0] == fp:
                    return seen[1], seen[2]
                raise ApiError("idempotency_mismatch", "Idempotency-Key was used for a different request")
        room = store.rooms.get(v["room_id"])
        if room is None:
            raise ApiError("not_found", "room not found")
        if v["attendees"] > room["capacity"]:
            raise ApiError("over_capacity", "more attendees than the room holds")
        for other in store.bookings.values():
            if other["room_id"] == room["id"] and v["start"] < other["end"] and other["start"] < v["end"]:
                raise ApiError("conflict", "room is already booked for this time")
        store.booking_seq += 1
        bid = f"b{store.booking_seq}"
        booking = {"id": bid, "room_id": room["id"], "start": v["start"], "end": v["end"],
                   "title": v["title"], "attendees": v["attendees"]}
        store.bookings[bid] = booking
        out = booking_out(booking)
        if fp is not None:
            store.idem[idem_key] = (fp, 201, out)
        return 201, out


def get_booking(store, bid, _body, _query):
    with store.lock:
        b = store.bookings.get(bid)
        if b is None:
            raise ApiError("not_found", "booking not found")
        return 200, booking_out(b)


def delete_booking(store, bid, _body, _query):
    with store.lock:
        if bid not in store.bookings:
            raise ApiError("not_found", "booking not found")
        del store.bookings[bid]
        return 204, None


def _num_id(bid):
    return int(bid[1:])


def _encode_cursor(b):
    payload = json.dumps({"s": fmt_ts(b["start"]), "i": _num_id(b["id"])}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(text):
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        obj = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise validation("malformed cursor")
    if not isinstance(obj, dict) or set(obj) != {"s", "i"}:
        raise validation("malformed cursor")
    start = parse_ts(obj["s"])
    idn = obj["i"]
    if start is None or not is_int(idn) or idn < 1:
        raise validation("malformed cursor")
    return (start, idn)


def list_bookings(store, _body, query):
    room_id = query.get("room_id")
    frm = query.get("from")
    to = query.get("to")
    f_dt = t_dt = None
    if frm is not None:
        f_dt = parse_ts(frm)
        if f_dt is None:
            raise validation("from must be a timestamp like 2026-03-02T09:00:00Z")
    if to is not None:
        t_dt = parse_ts(to)
        if t_dt is None:
            raise validation("to must be a timestamp like 2026-03-02T09:00:00Z")
    if f_dt is not None and t_dt is not None and f_dt >= t_dt:
        raise validation("from must be before to")

    limit = 20
    if "limit" in query:
        lv = query["limit"]
        if not _INT_RE.match(lv) or not (1 <= int(lv) <= 100):
            raise validation("limit must be an integer from 1 to 100")
        limit = int(lv)

    after = None
    if "cursor" in query:
        after = _decode_cursor(query["cursor"])

    with store.lock:
        items = []
        for b in store.bookings.values():
            if room_id is not None and b["room_id"] != room_id:
                continue
            if t_dt is not None and not (b["start"] < t_dt):
                continue
            if f_dt is not None and not (b["end"] > f_dt):
                continue
            items.append(b)
        items.sort(key=lambda b: (b["start"], _num_id(b["id"])))
        if after is not None:
            items = [b for b in items if (b["start"], _num_id(b["id"])) > after]
        page = items[:limit]
        more = len(items) > limit
        next_cursor = _encode_cursor(page[-1]) if (more and page) else None
        return 200, {"bookings": [booking_out(b) for b in page], "next_cursor": next_cursor}


# ---------- routing ----------

def resolve(method, path):
    seg = path.split("/")
    n = len(seg)
    table = None
    params = ()
    if n == 2 and seg[1] == "rooms":
        table = {"GET": list_rooms, "POST": create_room}
    elif n == 3 and seg[1] == "rooms" and seg[2]:
        table = {"GET": get_room}
        params = (seg[2],)
    elif n == 4 and seg[1] == "rooms" and seg[2] and seg[3] == "availability":
        table = {"GET": availability}
        params = (seg[2],)
    elif n == 2 and seg[1] == "bookings":
        table = {"GET": list_bookings, "POST": create_booking}
    elif n == 3 and seg[1] == "bookings" and seg[2]:
        table = {"GET": get_booking, "DELETE": delete_booking}
        params = (seg[2],)
    if table is None:
        raise ApiError("not_found", "no such route")
    if method not in table:
        raise ApiError("method_not_allowed", "method not allowed on this route")
    return table[method], params


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings"
    sys_version = ""
    store = None  # set on the server instance

    def log_message(self, format, *args):  # keep stderr quiet
        pass

    def _send(self, status, payload=None):
        if payload is None:
            body = b""
        else:
            body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_error(self, err):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}})

    def _read_body(self):
        cl = self.headers.get("Content-Length")
        if cl is None:
            raise ApiError("bad_json", "missing Content-Length")
        if not _INT_RE.match(cl.strip()):
            raise ApiError("bad_json", "invalid Content-Length")
        length = int(cl.strip())
        raw = self.rfile.read(length) if length else b""
        if len(raw) != length:
            raise ApiError("bad_json", "body shorter than Content-Length")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError("bad_json", "body is not valid JSON")
        if not isinstance(obj, dict):
            raise ApiError("bad_json", "body must be a JSON object")
        return obj

    def _dispatch(self, method):
        try:
            parts = urlsplit(self.path)
            handler, params = resolve(method, parts.path)
            qs = parse_qs(parts.query, keep_blank_values=True)
            query = {k: v[0] for k, v in qs.items()}
            store = self.store
            if method == "POST":
                body = self._read_body()
                if handler is create_booking:
                    idem = self.headers.get("Idempotency-Key")
                    status, payload = create_booking(store, body, idem)
                else:
                    status, payload = handler(store, body, query)
            else:
                status, payload = handler(store, *params, None, query)
            self._send(status, payload)
        except ApiError as e:
            self._send_error(e)
        except Exception:
            self._send_error(ApiError("internal_error", "internal error"))

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


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Meeting room bookings API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    Handler.store = Store()
    httpd = Server(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
