#!/usr/bin/env python3
"""bookings: meeting room booking JSON API (standard library only).

Usage: python server.py --port N   (listens on 127.0.0.1:N)
"""
import argparse
import base64
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

UTC = timezone.utc
TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_RE = re.compile(r"[0-9]+")
MAX_BOOKING = timedelta(hours=8)
WORK_START = 8
WORK_END = 18
QUARTERS = (0, 15, 30, 45)


class ApiError(Exception):
    def __init__(self, status, code, message=None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")


def verr(message="invalid request"):
    return ApiError(422, "validation_error", message)


def fmt_ts(dt):
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def parse_ts(v):
    """Strict YYYY-MM-DDTHH:MM:SSZ. Returns aware UTC datetime or None."""
    if not isinstance(v, str) or not TS_RE.fullmatch(v):
        return None
    try:
        return datetime(int(v[0:4]), int(v[5:7]), int(v[8:10]),
                        int(v[11:13]), int(v[14:16]), int(v[17:19]), tzinfo=UTC)
    except ValueError:
        return None


def is_int(v):
    return type(v) is int


def epoch(dt):
    return int(dt.timestamp())


def encode_cursor(start_dt, seq):
    raw = json.dumps({"s": epoch(start_dt), "q": seq}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        pad = "=" * (-len(c) % 4)
        obj = json.loads(base64.urlsafe_b64decode((c + pad).encode()).decode("utf-8"))
    except Exception:
        raise verr("invalid cursor")
    if (not isinstance(obj, dict) or set(obj) != {"s", "q"}
            or not is_int(obj["s"]) or not is_int(obj["q"])):
        raise verr("invalid cursor")
    return obj["s"], obj["q"]


def json_equal(a, b):
    """Equality of parsed JSON values: numbers compare by value, booleans never equal numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    return a is None and b is None


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.rooms = {}          # id -> room dict (insertion ordered)
        self.room_seq = 0
        self.room_names = set()  # casefolded trimmed names
        self.bookings = {}       # id -> internal booking record
        self.booking_seq = 0
        self.idem = {}           # key -> (parsed body, response dict)


def room_out(r):
    return {"id": r["id"], "name": r["name"], "capacity": r["capacity"]}


def booking_out(b):
    return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
            "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bookings"
    sys_version = ""

    def log_message(self, format, *args):  # keep quiet
        pass

    # ----- HTTP plumbing -------------------------------------------------
    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in te:
                data = bytearray()
                while True:
                    line = self.rfile.readline(65537)
                    if not line:
                        break
                    size = int(line.split(b";")[0].strip().decode("ascii"), 16)
                    if size == 0:
                        while True:
                            trailer = self.rfile.readline(65537)
                            if trailer in (b"\r\n", b"\n", b""):
                                break
                        break
                    data += self.rfile.read(size)
                    self.rfile.readline(65537)
                return bytes(data)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return b""
            return self.rfile.read(n)
        except (ValueError, OSError):
            self.close_connection = True
            return b""

    def _send(self, status, payload=None):
        if status == 204 or payload is None:
            self.send_response(status)
            self.end_headers()
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_obj(self, status, code, message):
        self._send(status, {"error": {"code": code, "message": message}})

    def _dispatch(self, method):
        body = self._read_body()  # always drain the request body first
        try:
            parts = urlsplit(self.path)
            path = parts.path
            query = parse_qs(parts.query, keep_blank_values=True)
            match = self._match(path)
            if match is None:
                raise ApiError(404, "not_found", "no such path")
            handlers, params = match
            name = handlers.get(method)
            if name is None:
                raise ApiError(405, "method_not_allowed", f"{method} not allowed here")
            status, payload = getattr(self, name)(params, query, body)
            self._send(status, payload)
        except ApiError as e:
            self._send_error_obj(e.status, e.code, e.message)
        except Exception as e:  # pragma: no cover - safety net
            self._send_error_obj(500, "internal_error", str(e))

    @staticmethod
    def _match(path):
        segs = path.split("/")
        if len(segs) < 2 or segs[0] != "":
            return None
        if segs == ["", "rooms"]:
            return {"GET": "list_rooms", "POST": "create_room"}, {}
        if segs == ["", "bookings"]:
            return {"GET": "list_bookings", "POST": "create_booking"}, {}
        if len(segs) == 3 and segs[1] == "rooms" and segs[2]:
            return {"GET": "get_room"}, {"id": unquote(segs[2])}
        if len(segs) == 4 and segs[1] == "rooms" and segs[2] and segs[3] == "availability":
            return {"GET": "availability"}, {"id": unquote(segs[2])}
        if len(segs) == 3 and segs[1] == "bookings" and segs[2]:
            return {"GET": "get_booking", "DELETE": "delete_booking"}, {"id": unquote(segs[2])}
        return None

    @staticmethod
    def _json_object(body):
        try:
            obj = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            raise ApiError(400, "bad_json", "body is not valid JSON")
        if not isinstance(obj, dict):
            raise ApiError(400, "bad_json", "body must be a JSON object")
        return obj

    @property
    def store(self):
        return self.server.store

    # ----- HTTP methods --------------------------------------------------
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

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler answers unknown methods with 501 HTML. Route
        # them through the normal dispatcher so they get the JSON 404/405 errors.
        if code == 501 and getattr(self, "command", None):
            self._dispatch(self.command)
            return
        super().send_error(code, message, explain)

    # ----- rooms ---------------------------------------------------------
    def list_rooms(self, params, query, body):
        with self.store.lock:
            return 200, {"rooms": [room_out(r) for r in self.store.rooms.values()]}

    def create_room(self, params, query, body):
        obj = self._json_object(body)
        if "name" not in obj or "capacity" not in obj:
            raise verr("name and capacity are required")
        name = obj["name"]
        if not isinstance(name, str):
            raise verr("name must be a string")
        name = name.strip()
        if not 1 <= len(name) <= 50:
            raise verr("name must be 1 to 50 characters")
        cap = obj["capacity"]
        if not is_int(cap) or not 1 <= cap <= 500:
            raise verr("capacity must be an integer from 1 to 500")
        st = self.store
        with st.lock:
            key = name.casefold()
            if key in st.room_names:
                raise ApiError(409, "room_exists", "a room with this name already exists")
            st.room_seq += 1
            rid = f"r{st.room_seq}"
            room = {"id": rid, "name": name, "capacity": cap}
            st.rooms[rid] = room
            st.room_names.add(key)
            return 201, room_out(room)

    def get_room(self, params, query, body):
        with self.store.lock:
            room = self.store.rooms.get(params["id"])
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            return 200, room_out(room)

    def availability(self, params, query, body):
        st = self.store
        with st.lock:
            rid = params["id"]
            if rid not in st.rooms:
                raise ApiError(404, "not_found", "room not found")
            dv = query.get("date", [None])[0]
            if dv is None or not DATE_RE.fullmatch(dv):
                raise verr("date must be YYYY-MM-DD")
            try:
                d = date(int(dv[0:4]), int(dv[5:7]), int(dv[8:10]))
            except ValueError:
                raise verr("date is not a real calendar date")
            ws = datetime(d.year, d.month, d.day, WORK_START, tzinfo=UTC)
            we = datetime(d.year, d.month, d.day, WORK_END, tzinfo=UTC)
            spans = []
            for b in st.bookings.values():
                if b["room_id"] != rid:
                    continue
                s = max(b["start"], ws)
                e = min(b["end"], we)
                if s < e:
                    spans.append((s, e))
            spans.sort()
            free = []
            cur = ws
            for s, e in spans:
                if s > cur:
                    free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
                if e > cur:
                    cur = e
            if cur < we:
                free.append({"start": fmt_ts(cur), "end": fmt_ts(we)})
            return 200, {"date": dv, "free": free}

    # ----- bookings ------------------------------------------------------
    def _validate_booking(self, obj):
        for field in ("room_id", "start", "end", "title", "attendees"):
            if field not in obj:
                raise verr(f"{field} is required")
        room_id = obj["room_id"]
        if not isinstance(room_id, str):
            raise verr("room_id must be a string")
        start = parse_ts(obj["start"])
        if start is None:
            raise verr("start must be YYYY-MM-DDTHH:MM:SSZ")
        end = parse_ts(obj["end"])
        if end is None:
            raise verr("end must be YYYY-MM-DDTHH:MM:SSZ")
        for dt in (start, end):
            if dt.minute not in QUARTERS or dt.second != 0:
                raise verr("times must be on quarter hours with zero seconds")
        if end <= start:
            raise verr("end must be after start")
        if end - start > MAX_BOOKING:
            raise verr("booking may be at most 8 hours")
        title = obj["title"]
        if not isinstance(title, str):
            raise verr("title must be a string")
        title = title.strip()
        if not 1 <= len(title) <= 100:
            raise verr("title must be 1 to 100 characters")
        attendees = obj["attendees"]
        if not is_int(attendees) or attendees < 1:
            raise verr("attendees must be an integer of at least 1")
        return room_id, start, end, title, attendees

    def create_booking(self, params, query, body):
        obj = self._json_object(body)
        key = self.headers.get("Idempotency-Key")
        if key == "":  # empty header value counts as no key
            key = None
        st = self.store
        with st.lock:
            if key is not None and key in st.idem:
                stored_body, stored_resp = st.idem[key]
                try:
                    same = json_equal(stored_body, obj)
                except RecursionError:
                    same = False
                if same:
                    return 201, dict(stored_resp)
                raise ApiError(422, "idempotency_mismatch",
                               "Idempotency-Key was used with a different body")
            room_id, start, end, title, attendees = self._validate_booking(obj)
            room = st.rooms.get(room_id)
            if room is None:
                raise ApiError(404, "not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError(422, "over_capacity", "attendees exceed room capacity")
            for other in st.bookings.values():
                if (other["room_id"] == room_id and other["start"] < end
                        and start < other["end"]):
                    raise ApiError(409, "conflict", "booking overlaps an existing booking")
            st.booking_seq += 1
            seq = st.booking_seq
            rec = {"id": f"b{seq}", "seq": seq, "room_id": room_id, "start": start,
                   "end": end, "title": title, "attendees": attendees}
            st.bookings[rec["id"]] = rec
            resp = booking_out(rec)
            if key is not None:
                st.idem[key] = (obj, dict(resp))
            return 201, resp

    def get_booking(self, params, query, body):
        with self.store.lock:
            rec = self.store.bookings.get(params["id"])
            if rec is None:
                raise ApiError(404, "not_found", "booking not found")
            return 200, booking_out(rec)

    def delete_booking(self, params, query, body):
        with self.store.lock:
            if params["id"] not in self.store.bookings:
                raise ApiError(404, "not_found", "booking not found")
            del self.store.bookings[params["id"]]
            return 204, None

    def list_bookings(self, params, query, body):
        def first(name):
            vals = query.get(name)
            return vals[0] if vals else None

        room_id = first("room_id")
        frm = None
        if "from" in query:
            frm = parse_ts(first("from"))
            if frm is None:
                raise verr("from must be YYYY-MM-DDTHH:MM:SSZ")
        to = None
        if "to" in query:
            to = parse_ts(first("to"))
            if to is None:
                raise verr("to must be YYYY-MM-DDTHH:MM:SSZ")
        limit = 20
        if "limit" in query:
            lv = first("limit")
            if not INT_RE.fullmatch(lv or ""):
                raise verr("limit must be an integer from 1 to 100")
            limit = int(lv)
            if not 1 <= limit <= 100:
                raise verr("limit must be an integer from 1 to 100")
        cursor = None
        if "cursor" in query:
            cursor = decode_cursor(first("cursor") or "")

        st = self.store
        with st.lock:
            items = []
            for b in st.bookings.values():
                if room_id is not None and b["room_id"] != room_id:
                    continue
                if frm is not None and not b["end"] > frm:
                    continue
                if to is not None and not b["start"] < to:
                    continue
                items.append(b)
            items.sort(key=lambda b: (epoch(b["start"]), b["seq"]))
            if cursor is not None:
                cs, cq = cursor
                items = [b for b in items if (epoch(b["start"]), b["seq"]) > (cs, cq)]
            page = items[:limit]
            more = len(items) > limit
            next_cursor = encode_cursor(page[-1]["start"], page[-1]["seq"]) if more else None
            return 200, {"bookings": [booking_out(b) for b in page], "next_cursor": next_cursor}


def main():
    parser = argparse.ArgumentParser(description="bookings meeting room API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.store = Store()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
