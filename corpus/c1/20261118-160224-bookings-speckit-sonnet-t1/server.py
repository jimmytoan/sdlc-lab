import argparse
import base64
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

# ---- state ----
LOCK = threading.Lock()
ROOMS = {}
BOOKINGS = {}
IDEM = {}
COUNTERS = {"r": 0, "b": 0}

STATUS = {
    "not_found": 404, "method_not_allowed": 405, "bad_json": 400,
    "validation_error": 422, "over_capacity": 422, "room_exists": 409,
    "conflict": 409, "idempotency_mismatch": 422,
}


class ApiError(Exception):
    def __init__(self, code, message=""):
        self.status = STATUS[code]
        self.code = code
        self.message = message or code


# ---- helpers ----
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_ts(v, field, quarter=True):
    if not isinstance(v, str) or not TS_RE.match(v):
        raise ApiError("validation_error", f"{field} must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        d = datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid timestamp")
    if quarter and (d.second != 0 or d.minute % 15 != 0):
        raise ApiError("validation_error", f"{field} must be on a quarter hour")
    return d


def fmt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def parse_body(raw):
    if not raw or not raw.strip():
        raise ApiError("bad_json", "empty body")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError("bad_json", "invalid JSON")
    if not isinstance(data, dict):
        raise ApiError("validation_error", "body must be a JSON object")
    return data


# ---- handlers ----
def create_room(data):
    name, cap = data.get("name"), data.get("capacity")
    if not isinstance(name, str) or not name.strip() or len(name) > 50:
        raise ApiError("validation_error", "name must be 1-50 characters")
    if not is_int(cap) or not 1 <= cap <= 500:
        raise ApiError("validation_error", "capacity must be an integer 1-500")
    with LOCK:
        if any(r["name"] == name for r in ROOMS.values()):
            raise ApiError("room_exists", "room name already exists")
        COUNTERS["r"] += 1
        room = {"id": f"r{COUNTERS['r']}", "name": name, "capacity": cap}
        ROOMS[room["id"]] = room
        return 201, dict(room)


def get_room(rid):
    with LOCK:
        if rid not in ROOMS:
            raise ApiError("not_found", "room not found")
        return 200, dict(ROOMS[rid])


def create_booking(data, key):
    canon = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with LOCK:
        if key is not None and key in IDEM:
            h, st, body = IDEM[key]
            if h != canon:
                raise ApiError("idempotency_mismatch", "Idempotency-Key reused with a different request")
            return st, dict(body)
        rid = data.get("room_id")
        if not isinstance(rid, str) or not rid:
            raise ApiError("validation_error", "room_id must be a string")
        start = parse_ts(data.get("start"), "start")
        end = parse_ts(data.get("end"), "end")
        if end <= start:
            raise ApiError("validation_error", "end must be after start")
        if end - start > timedelta(hours=8):
            raise ApiError("validation_error", "booking longer than 8 hours")
        title = data.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ApiError("validation_error", "title must be 1-200 characters")
        att = data.get("attendees")
        if not is_int(att) or att < 1:
            raise ApiError("validation_error", "attendees must be an integer >= 1")
        room = ROOMS.get(rid)
        if room is None:
            raise ApiError("not_found", "room not found")
        if att > room["capacity"]:
            raise ApiError("over_capacity", "too many attendees for this room")
        for b in BOOKINGS.values():
            if b["room_id"] == rid and start < b["_end"] and b["_start"] < end:
                raise ApiError("conflict", "room already booked in that period")
        COUNTERS["b"] += 1
        n = COUNTERS["b"]
        b = {"id": f"b{n}", "room_id": rid, "start": fmt(start), "end": fmt(end),
             "title": title, "attendees": att, "_start": start, "_end": end, "_seq": n}
        BOOKINGS[b["id"]] = b
        out = pub(b)
        if key is not None:
            IDEM[key] = (canon, 201, out)
        return 201, dict(out)


def pub(b):
    return {k: v for k, v in b.items() if not k.startswith("_")}


def get_booking(bid):
    with LOCK:
        if bid not in BOOKINGS:
            raise ApiError("not_found", "booking not found")
        return 200, pub(BOOKINGS[bid])


def delete_booking(bid):
    with LOCK:
        if bid not in BOOKINGS:
            raise ApiError("not_found", "booking not found")
        del BOOKINGS[bid]
        return 204, None


def qget(q, name):
    v = q.get(name)
    return v[-1] if v else None


def list_bookings(q):
    room_id = qget(q, "room_id")
    frm = qget(q, "from")
    to = qget(q, "to")
    frm = parse_ts(frm, "from", quarter=False) if frm is not None else None
    to = parse_ts(to, "to", quarter=False) if to is not None else None
    if frm is not None and to is not None and frm >= to:
        raise ApiError("validation_error", "from must be before to")
    limit = 20
    lv = qget(q, "limit")
    if lv is not None:
        if not re.fullmatch(r"[0-9]+", lv) or not 1 <= int(lv) <= 100:
            raise ApiError("validation_error", "limit must be an integer 1-100")
        limit = int(lv)
    after = None
    cv = qget(q, "cursor")
    if cv is not None:
        try:
            s, seq = base64.urlsafe_b64decode(cv.encode("ascii")).decode("ascii").split("|")
            after = (parse_ts(s, "cursor", quarter=False), int(seq))
        except Exception:
            raise ApiError("validation_error", "invalid cursor")
    with LOCK:
        items = sorted(BOOKINGS.values(), key=lambda b: (b["_start"], b["_seq"]))
        res = []
        for b in items:
            if room_id is not None and b["room_id"] != room_id:
                continue
            if frm is not None and not b["_end"] > frm:
                continue
            if to is not None and not b["_start"] < to:
                continue
            if after is not None and (b["_start"], b["_seq"]) <= after:
                continue
            res.append(b)
        page = res[:limit]
        nxt = None
        if len(res) > limit:
            last = page[-1]
            nxt = base64.urlsafe_b64encode(f"{fmt(last['_start'])}|{last['_seq']}".encode()).decode()
        return 200, {"bookings": [pub(b) for b in page], "next_cursor": nxt}


def availability(rid, q):
    d = qget(q, "date")
    if d is None or not DATE_RE.match(d):
        raise ApiError("validation_error", "date must be YYYY-MM-DD")
    try:
        day = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ApiError("validation_error", "invalid date")
    ws, we = day + timedelta(hours=8), day + timedelta(hours=18)
    with LOCK:
        if rid not in ROOMS:
            raise ApiError("not_found", "room not found")
        busy = sorted((b["_start"], b["_end"]) for b in BOOKINGS.values()
                      if b["room_id"] == rid and b["_start"] < we and b["_end"] > ws)
    free = []
    cur = ws
    for s, e in busy:
        if s > cur:
            free.append({"start": fmt(cur), "end": fmt(min(s, we))})
        cur = max(cur, e)
        if cur >= we:
            break
    if cur < we:
        free.append({"start": fmt(cur), "end": fmt(we)})
    return 200, {"date": d, "free": free}


# ---- router ----
def route(method, path, q, headers, read_body):
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != "" or parts[1] not in ("rooms", "bookings") or "" in parts[1:]:
        raise ApiError("not_found", "no such path")
    seg = parts[1:]
    n = len(seg)
    if seg[0] == "rooms":
        if n == 1:
            allowed = {"GET", "POST"}
        elif n == 2:
            allowed = {"GET"}
        elif n == 3 and seg[2] == "availability":
            allowed = {"GET"}
        else:
            raise ApiError("not_found", "no such path")
    else:
        if n == 1:
            allowed = {"GET", "POST"}
        elif n == 2:
            allowed = {"GET", "DELETE"}
        else:
            raise ApiError("not_found", "no such path")
    if method not in allowed:
        raise ApiError("method_not_allowed", "method not allowed")
    if seg[0] == "rooms":
        if n == 1:
            if method == "POST":
                return create_room(parse_body(read_body()))
            with LOCK:
                return 200, {"rooms": [dict(r) for r in ROOMS.values()]}
        if n == 2:
            return get_room(seg[1])
        return availability(seg[1], q)
    if n == 1:
        if method == "POST":
            return create_booking(parse_body(read_body()), headers.get("Idempotency-Key"))
        return list_bookings(q)
    if method == "GET":
        return get_booking(seg[1])
    return delete_booking(seg[1])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _send(self, status, body):
        if status == 204:
            self.send_response(204)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        try:
            u = urlsplit(self.path)
            q = parse_qs(u.query, keep_blank_values=True)
            status, body = route(self.command, u.path, q, self.headers, self._read_body)
        except ApiError as e:
            status, body = e.status, {"error": {"code": e.code, "message": e.message}}
        except Exception:
            status, body = 500, {"error": {"code": "internal_error", "message": "internal error"}}
        self._send(status, body)

    do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = do_HEAD = do_OPTIONS = _handle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
