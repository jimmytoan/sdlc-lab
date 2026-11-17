"""bookings: meeting room booking API (standard library only)."""
import argparse
import base64
import json
import re
import threading
from datetime import datetime, timedelta, date as date_cls
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

LOCK = threading.RLock()

ROOMS = []           # list of room dicts in creation order
ROOMS_BY_ID = {}
BOOKINGS = {}        # id -> record {"seq", "data", "start_dt", "end_dt"}
COUNTERS = {"room": 0, "booking": 0}
IDEMPOTENCY = {}     # key -> (body, response dict)

TS_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
INT_RE = re.compile(r"[0-9]+")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def parse_ts(v):
    if not isinstance(v, str):
        return None
    m = TS_RE.fullmatch(v)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def fmt_ts(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def is_int(v):
    return type(v) is int


def deep_eq(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(deep_eq(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(deep_eq(x, y) for x, y in zip(a, b))
    if type(a) is not type(b):
        return False
    return a == b


def _reject_constant(c):
    raise ValueError("invalid constant " + c)


def parse_json_body(raw):
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text, parse_constant=_reject_constant)
    except Exception:
        raise ApiError(400, "bad_json", "request body is not valid JSON")
    if not isinstance(obj, dict):
        raise ApiError(400, "bad_json", "request body must be a JSON object")
    return obj


def verr(msg):
    return ApiError(422, "validation_error", msg)


# ---------------- handlers ----------------

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
    low = name.casefold()
    for r in ROOMS:
        if r["name"].casefold() == low:
            raise ApiError(409, "room_exists", "a room with this name already exists")
    COUNTERS["room"] += 1
    room = {"id": f"r{COUNTERS['room']}", "name": name, "capacity": cap}
    ROOMS.append(room)
    ROOMS_BY_ID[room["id"]] = room
    return 201, dict(room)


def get_room(rid):
    room = ROOMS_BY_ID.get(rid)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    return 200, dict(room)


def create_booking(body):
    room_id = body.get("room_id")
    start = body.get("start")
    end = body.get("end")
    title = body.get("title")
    attendees = body.get("attendees")
    if not isinstance(room_id, str):
        raise verr("room_id must be a string")
    sdt = parse_ts(start)
    edt = parse_ts(end)
    if sdt is None:
        raise verr("start must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    if edt is None:
        raise verr("end must be a timestamp YYYY-MM-DDTHH:MM:SSZ")
    for dt, n in ((sdt, "start"), (edt, "end")):
        if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
            raise verr(f"{n} must be aligned to a quarter hour")
    if edt <= sdt:
        raise verr("end must be after start")
    if edt - sdt > timedelta(hours=8):
        raise verr("booking may be at most 8 hours long")
    if not isinstance(title, str):
        raise verr("title must be a string")
    title = title.strip()
    if not (1 <= len(title) <= 100):
        raise verr("title must be 1 to 100 characters")
    if not is_int(attendees) or attendees < 1:
        raise verr("attendees must be an integer of at least 1")
    room = ROOMS_BY_ID.get(room_id)
    if room is None:
        raise ApiError(404, "not_found", "room not found")
    if attendees > room["capacity"]:
        raise ApiError(422, "over_capacity", "attendees exceed room capacity")
    for rec in BOOKINGS.values():
        if rec["data"]["room_id"] != room_id:
            continue
        if sdt < rec["end_dt"] and rec["start_dt"] < edt:
            raise ApiError(409, "conflict", "booking overlaps an existing booking")
    COUNTERS["booking"] += 1
    seq = COUNTERS["booking"]
    data = {"id": f"b{seq}", "room_id": room_id, "start": fmt_ts(sdt), "end": fmt_ts(edt),
            "title": title, "attendees": attendees}
    BOOKINGS[data["id"]] = {"seq": seq, "data": data, "start_dt": sdt, "end_dt": edt}
    return 201, dict(data)


def post_booking(body, key):
    if key:
        if key in IDEMPOTENCY:
            stored_body, stored_resp = IDEMPOTENCY[key]
            if deep_eq(stored_body, body):
                return 201, dict(stored_resp)
            raise ApiError(422, "idempotency_mismatch",
                           "idempotency key was used with a different request body")
    status, resp = create_booking(body)
    if key:
        IDEMPOTENCY[key] = (body, dict(resp))
    return status, resp


def get_booking(bid):
    rec = BOOKINGS.get(bid)
    if rec is None:
        raise ApiError(404, "not_found", "booking not found")
    return 200, dict(rec["data"])


def delete_booking(bid):
    if bid not in BOOKINGS:
        raise ApiError(404, "not_found", "booking not found")
    del BOOKINGS[bid]
    return 204, None


def encode_cursor(start, seq):
    raw = json.dumps([start, seq]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(c):
    try:
        if not c or not re.fullmatch(r"[A-Za-z0-9_-]+", c):
            raise ValueError
        pad = "=" * (-len(c) % 4)
        obj = json.loads(base64.urlsafe_b64decode(c + pad).decode("utf-8"))
        if not (isinstance(obj, list) and len(obj) == 2):
            raise ValueError
        s, n = obj
        dt = parse_ts(s)
        if dt is None or not is_int(n) or n < 1:
            raise ValueError
        return (dt, n)
    except Exception:
        raise verr("invalid cursor")


def list_bookings(query):
    qs = parse_qs(query, keep_blank_values=True)

    def first(name):
        v = qs.get(name)
        return v[0] if v else None

    room_id = first("room_id")
    f = first("from")
    t = first("to")
    limit_s = first("limit")
    cursor = first("cursor")
    fdt = tdt = None
    if f is not None:
        fdt = parse_ts(f)
        if fdt is None:
            raise verr("invalid from")
    if t is not None:
        tdt = parse_ts(t)
        if tdt is None:
            raise verr("invalid to")
    limit = 20
    if limit_s is not None:
        if not INT_RE.fullmatch(limit_s):
            raise verr("invalid limit")
        limit = int(limit_s)
        if not (1 <= limit <= 100):
            raise verr("limit must be from 1 to 100")
    after = None
    if cursor is not None:
        after = decode_cursor(cursor)

    items = []
    for rec in BOOKINGS.values():
        d = rec["data"]
        if room_id is not None and d["room_id"] != room_id:
            continue
        if fdt is not None and not (rec["end_dt"] > fdt):
            continue
        if tdt is not None and not (rec["start_dt"] < tdt):
            continue
        k = (rec["start_dt"], rec["seq"])
        if after is not None and not (k > after):
            continue
        items.append((k, d))
    items.sort(key=lambda x: x[0])
    page = items[:limit]
    next_cursor = None
    if len(items) > limit and page:
        k = page[-1][0]
        next_cursor = encode_cursor(fmt_ts(k[0]), k[1])
    return 200, {"bookings": [dict(d) for _, d in page], "next_cursor": next_cursor}


def availability(rid, query):
    if rid not in ROOMS_BY_ID:
        raise ApiError(404, "not_found", "room not found")
    qs = parse_qs(query, keep_blank_values=True)
    v = qs.get("date")
    ds = v[0] if v else None
    if ds is None:
        raise verr("date is required")
    m = DATE_RE.fullmatch(ds)
    if not m:
        raise verr("date must be YYYY-MM-DD")
    try:
        d = date_cls(*(int(g) for g in m.groups()))
    except ValueError:
        raise verr("date must be a real calendar date")
    day_start = datetime(d.year, d.month, d.day, 8, 0, 0)
    day_end = datetime(d.year, d.month, d.day, 18, 0, 0)
    intervals = []
    for rec in BOOKINGS.values():
        if rec["data"]["room_id"] != rid:
            continue
        s = max(rec["start_dt"], day_start)
        e = min(rec["end_dt"], day_end)
        if s < e:
            intervals.append((s, e))
    intervals.sort()
    free = []
    cur = day_start
    for s, e in intervals:
        if s > cur:
            free.append({"start": fmt_ts(cur), "end": fmt_ts(s)})
        if e > cur:
            cur = e
    if cur < day_end:
        free.append({"start": fmt_ts(cur), "end": fmt_ts(day_end)})
    return 200, {"date": f"{d.year:04d}-{d.month:02d}-{d.day:02d}", "free": free}


# ---------------- HTTP plumbing ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"

    def log_message(self, format, *args):
        pass

    def _send(self, status, obj, extra_headers=None):
        if obj is None:
            data = b""
        else:
            data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _read_body(self):
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b""
        try:
            n = int(cl)
        except ValueError:
            return None
        if n < 0:
            return None
        if n == 0:
            return b""
        return self.rfile.read(n)

    def _route(self, path):
        parts = path.split("/")
        if len(parts) < 2 or parts[0] != "":
            return None
        p = parts[1:]
        if p == ["rooms"]:
            return {"GET": ("list_rooms",), "POST": ("create_room",)}
        if len(p) == 2 and p[0] == "rooms" and p[1]:
            return {"GET": ("get_room", p[1])}
        if len(p) == 3 and p[0] == "rooms" and p[1] and p[2] == "availability":
            return {"GET": ("availability", p[1])}
        if p == ["bookings"]:
            return {"GET": ("list_bookings",), "POST": ("create_booking",)}
        if len(p) == 2 and p[0] == "bookings" and p[1]:
            return {"GET": ("get_booking", p[1]), "DELETE": ("delete_booking", p[1])}
        return None

    def _handle(self):
        self.close_connection = True
        try:
            split = urlsplit(self.path)
            path, query = split.path, split.query
            raw = self._read_body()
            routes = self._route(path)
            if routes is None:
                raise ApiError(404, "not_found", "path not found")
            if self.command not in routes:
                raise ApiError(405, "method_not_allowed", "method not allowed")
            action = routes[self.command]
            name, args = action[0], action[1:]
            with LOCK:
                if name == "list_rooms":
                    status, obj = 200, {"rooms": [dict(r) for r in ROOMS]}
                elif name == "create_room":
                    if raw is None:
                        raise ApiError(400, "bad_json", "invalid body")
                    status, obj = create_room(parse_json_body(raw))
                elif name == "get_room":
                    status, obj = get_room(*args)
                elif name == "availability":
                    status, obj = availability(args[0], query)
                elif name == "list_bookings":
                    status, obj = list_bookings(query)
                elif name == "create_booking":
                    if raw is None:
                        raise ApiError(400, "bad_json", "invalid body")
                    body = parse_json_body(raw)
                    key = self.headers.get("Idempotency-Key")
                    status, obj = post_booking(body, key if key else None)
                elif name == "get_booking":
                    status, obj = get_booking(*args)
                elif name == "delete_booking":
                    status, obj = delete_booking(*args)
                else:
                    raise ApiError(404, "not_found", "path not found")
            self._send(status, obj)
        except ApiError as e:
            extra = None
            if e.status == 405:
                try:
                    routes = self._route(urlsplit(self.path).path) or {}
                    extra = {"Allow": ", ".join(sorted(routes))}
                except Exception:
                    extra = None
            self._send(e.status, {"error": {"code": e.code, "message": e.message}}, extra)
        except Exception as e:  # pragma: no cover
            try:
                self._send(500, {"error": {"code": "internal_error", "message": str(e)}})
            except Exception:
                pass

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
