"""Meeting room booking JSON API (standard library only).

Run: python server.py --port N   (listens on 127.0.0.1)
"""
import argparse
import base64
import binascii
import copy
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

UTC = timezone.utc
WORK_START_HOUR = 8
WORK_END_HOUR = 18
MAX_DURATION = timedelta(hours=8)
MIN_FREE_GAP = timedelta(minutes=15)

TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
INT_STR_RE = re.compile(r"[0-9]+")
BOOKING_ID_RE = re.compile(r"b([0-9]+)")


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
    }

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.status = self.STATUS[code]
        self.message = message


# ---------------------------------------------------------------- helpers

def fmt_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value, field):
    """Strict YYYY-MM-DDTHH:MM:SSZ. Returns aware UTC datetime or raises validation_error."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise ApiError("validation_error", f"{field} must be a timestamp like 2026-03-02T09:30:00Z")
    try:
        return datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]),
                        int(value[11:13]), int(value[14:16]), int(value[17:19]), tzinfo=UTC)
    except ValueError:
        raise ApiError("validation_error", f"{field} is not a valid date and time")


def parse_quarter_ts(value, field):
    dt = parse_ts(value, field)
    if dt.minute not in (0, 15, 30, 45) or dt.second != 0:
        raise ApiError("validation_error", f"{field} must fall on a quarter hour")
    return dt


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def require_string(body, field):
    v = body.get(field)
    if v is None and field not in body:
        raise ApiError("validation_error", f"{field} is required")
    if not isinstance(v, str):
        raise ApiError("validation_error", f"{field} must be a string")
    return v


def require_int(body, field, lo, hi=None):
    if field not in body:
        raise ApiError("validation_error", f"{field} is required")
    v = body[field]
    if not is_int(v):
        raise ApiError("validation_error", f"{field} must be an integer")
    if v < lo or (hi is not None and v > hi):
        if hi is None:
            raise ApiError("validation_error", f"{field} must be at least {lo}")
        raise ApiError("validation_error", f"{field} must be between {lo} and {hi}")
    return v


def encode_cursor(start_dt, booking_num):
    raw = json.dumps([fmt_ts(start_dt), booking_num], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token):
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        raise ApiError("validation_error", "cursor is malformed")
    if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], str)
            and is_int(data[1]) and data[1] >= 0):
        raise ApiError("validation_error", "cursor is malformed")
    try:
        start_dt = parse_ts(data[0], "cursor")
    except ApiError:
        raise ApiError("validation_error", "cursor is malformed")
    return start_dt, data[1]


def query_value(query, name):
    vals = query.get(name)
    if not vals:
        return None
    return vals[0]


# ---------------------------------------------------------------- state

def validate_booking_fields(body):
    """Shared POST/PATCH rules for start, end, title, attendees. Returns (start, end, title, attendees)."""
    for f in ("start", "end"):
        if f not in body:
            raise ApiError("validation_error", f"{f} is required")
    start_raw, end_raw = body["start"], body["end"]
    if "title" not in body:
        raise ApiError("validation_error", "title is required")
    title = body["title"]
    if not isinstance(title, str):
        raise ApiError("validation_error", "title must be a string")
    attendees = require_int(body, "attendees", 1)
    start = parse_quarter_ts(start_raw, "start")
    end = parse_quarter_ts(end_raw, "end")
    if not end > start:
        raise ApiError("validation_error", "end must be after start")
    if end - start > MAX_DURATION:
        raise ApiError("validation_error", "booking may be at most 8 hours long")
    return start, end, title, attendees


def conflicts_with_buffer(buf, start, end, other):
    """True if [start, end) plus buffer overlaps other booking plus buffer."""
    buf_d = timedelta(minutes=buf)
    return start < other["end"] + buf_d and other["start"] < end + buf_d


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}            # id -> room dict
        self.room_order = []       # ids in creation order
        self.room_names = set()
        self.bookings = {}         # id -> booking dict (internal datetimes)
        self.room_bookings = {}    # room_id -> set of booking ids
        self.next_room = 1
        self.next_booking = 1
        self.idempotency = {}      # key -> {"fingerprint", "status", "body"}

    # --- rooms
    def create_room(self, body):
        name = require_string(body, "name")
        if not (1 <= len(name) <= 50):
            raise ApiError("validation_error", "name must be 1 to 50 characters")
        capacity = require_int(body, "capacity", 1, 500)
        if "buffer_minutes" in body:
            buffer_minutes = require_int(body, "buffer_minutes", 0, 60)
        else:
            buffer_minutes = 0
        with self.lock:
            if name in self.room_names:
                raise ApiError("room_exists", "a room with this name already exists")
            rid = f"r{self.next_room}"
            self.next_room += 1
            room = {"id": rid, "name": name, "capacity": capacity,
                    "buffer_minutes": buffer_minutes}
            self.rooms[rid] = room
            self.room_order.append(rid)
            self.room_names.add(name)
            self.room_bookings[rid] = set()
            return copy.deepcopy(room)

    def list_rooms(self):
        with self.lock:
            return {"rooms": [copy.deepcopy(self.rooms[r]) for r in self.room_order]}

    def get_room(self, rid):
        with self.lock:
            room = self.rooms.get(rid)
            if room is None:
                raise ApiError("not_found", "room not found")
            return copy.deepcopy(room)

    def availability(self, rid, date_str):
        if not DATE_RE.fullmatch(date_str or ""):
            raise ApiError("validation_error", "date must be YYYY-MM-DD")
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            raise ApiError("validation_error", "date is not a valid calendar date")
        day_start = datetime(d.year, d.month, d.day, WORK_START_HOUR, tzinfo=UTC)
        day_end = datetime(d.year, d.month, d.day, WORK_END_HOUR, tzinfo=UTC)
        with self.lock:
            if rid not in self.rooms:
                raise ApiError("not_found", "room not found")
            buf = timedelta(minutes=self.rooms[rid]["buffer_minutes"])
            busy = []
            for bid in self.room_bookings[rid]:
                b = self.bookings[bid]
                exp_start = b["start"] - buf
                exp_end = b["end"] + buf
                if exp_start < day_end and exp_end > day_start:
                    busy.append((max(exp_start, day_start), min(exp_end, day_end)))
        busy.sort()
        free = []
        cursor = day_start
        for s, e in busy:
            if s - cursor >= MIN_FREE_GAP:
                free.append({"start": fmt_ts(cursor), "end": fmt_ts(s)})
            if e > cursor:
                cursor = e
        if day_end - cursor >= MIN_FREE_GAP:
            free.append({"start": fmt_ts(cursor), "end": fmt_ts(day_end)})
        return {"date": date_str, "free": free}

    # --- bookings
    def _serialize(self, b):
        return {"id": b["id"], "room_id": b["room_id"], "start": fmt_ts(b["start"]),
                "end": fmt_ts(b["end"]), "title": b["title"], "attendees": b["attendees"]}

    def create_booking(self, body, idem_key):
        room_id = body.get("room_id")
        if "room_id" not in body:
            raise ApiError("validation_error", "room_id is required")
        if not isinstance(room_id, str):
            raise ApiError("validation_error", "room_id must be a string")
        start, end, title, attendees = validate_booking_fields(body)

        normalized = {"room_id": room_id, "start": fmt_ts(start), "end": fmt_ts(end),
                      "title": title, "attendees": attendees}
        fingerprint = json.dumps(normalized, sort_keys=True, ensure_ascii=False)

        with self.lock:
            if idem_key is not None and idem_key in self.idempotency:
                rec = self.idempotency[idem_key]
                if rec["fingerprint"] != fingerprint:
                    raise ApiError("idempotency_mismatch",
                                   "Idempotency-Key was already used for a different request")
                return rec["status"], copy.deepcopy(rec["body"])

            room = self.rooms.get(room_id)
            if room is None:
                raise ApiError("not_found", "room not found")
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            buf = room["buffer_minutes"]
            for bid in self.room_bookings[room_id]:
                other = self.bookings[bid]
                if conflicts_with_buffer(buf, start, end, other):
                    raise ApiError("conflict", "booking overlaps an existing booking or buffer")

            bid = f"b{self.next_booking}"
            self.next_booking += 1
            rec_b = {"id": bid, "room_id": room_id, "start": start, "end": end,
                     "title": title, "attendees": attendees}
            self.bookings[bid] = rec_b
            self.room_bookings[room_id].add(bid)
            out = self._serialize(rec_b)
            if idem_key is not None:
                self.idempotency[idem_key] = {"fingerprint": fingerprint, "status": 201,
                                              "body": copy.deepcopy(out)}
            return 201, out

    def update_booking(self, bid, body):
        with self.lock:
            b = self.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            candidate = {
                "start": body["start"] if "start" in body else fmt_ts(b["start"]),
                "end": body["end"] if "end" in body else fmt_ts(b["end"]),
                "title": body["title"] if "title" in body else b["title"],
                "attendees": body["attendees"] if "attendees" in body else b["attendees"],
            }
            start, end, title, attendees = validate_booking_fields(candidate)
            room = self.rooms[b["room_id"]]
            if attendees > room["capacity"]:
                raise ApiError("over_capacity", "more attendees than the room holds")
            buf = room["buffer_minutes"]
            for other_id in self.room_bookings[b["room_id"]]:
                if other_id == bid:
                    continue
                if conflicts_with_buffer(buf, start, end, self.bookings[other_id]):
                    raise ApiError("conflict", "booking overlaps an existing booking or buffer")
            # all checks passed: apply
            b["start"], b["end"] = start, end
            b["title"], b["attendees"] = title, attendees
            return self._serialize(b)

    def get_booking(self, bid):
        with self.lock:
            b = self.bookings.get(bid)
            if b is None:
                raise ApiError("not_found", "booking not found")
            return self._serialize(b)

    def delete_booking(self, bid):
        with self.lock:
            b = self.bookings.pop(bid, None)
            if b is None:
                raise ApiError("not_found", "booking not found")
            self.room_bookings[b["room_id"]].discard(bid)

    def list_bookings(self, query):
        room_filter = query_value(query, "room_id")
        from_raw = query_value(query, "from")
        to_raw = query_value(query, "to")
        limit_raw = query_value(query, "limit")
        cursor_raw = query_value(query, "cursor")

        from_dt = parse_ts(from_raw, "from") if from_raw is not None else None
        to_dt = parse_ts(to_raw, "to") if to_raw is not None else None
        if from_dt is not None and to_dt is not None and not from_dt < to_dt:
            raise ApiError("validation_error", "from must be before to")
        if limit_raw is None:
            limit = 20
        else:
            if not INT_STR_RE.fullmatch(limit_raw):
                raise ApiError("validation_error", "limit must be an integer from 1 to 100")
            limit = int(limit_raw)
            if not 1 <= limit <= 100:
                raise ApiError("validation_error", "limit must be an integer from 1 to 100")
        after = None
        if cursor_raw is not None:
            after = decode_cursor(cursor_raw)

        with self.lock:
            if room_filter is not None:
                ids = list(self.room_bookings.get(room_filter, ()))
            else:
                ids = list(self.bookings.keys())
            matching = []
            for bid in ids:
                b = self.bookings[bid]
                if from_dt is not None and not b["end"] > from_dt:
                    continue
                if to_dt is not None and not b["start"] < to_dt:
                    continue
                matching.append((b["start"], int(bid[1:]), b))
            matching.sort(key=lambda t: (t[0], t[1]))
            total = len(matching)
            items = matching
            if after is not None:
                items = [t for t in matching if (t[0], t[1]) > (after[0], after[1])]
            page = items[:limit]
            has_more = len(items) > limit
            out = [self._serialize(t[2]) for t in page]
            next_cursor = None
            if has_more and page:
                last = page[-1]
                next_cursor = encode_cursor(last[0], last[1])
            return {"bookings": out, "next_cursor": next_cursor, "total": total}


STATE = State()


# ---------------------------------------------------------------- routing

def parse_json_object(raw):
    if not raw:
        raise ApiError("bad_json", "request body must be JSON")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise ApiError("validation_error", "request body must be a JSON object")
    return data


def _post_rooms(ctx):
    body = parse_json_object(ctx["raw"])
    return 201, STATE.create_room(body), {}


def _get_rooms(ctx):
    return 200, STATE.list_rooms(), {}


def _get_room(ctx, rid):
    return 200, STATE.get_room(rid), {}


def _get_availability(ctx, rid):
    date_str = query_value(ctx["query"], "date")
    return 200, STATE.availability(rid, date_str), {}


def _post_booking(ctx):
    body = parse_json_object(ctx["raw"])
    key = ctx["idem_key"]
    status, out = STATE.create_booking(body, key)
    return status, out, {}


def _get_bookings(ctx):
    return 200, STATE.list_bookings(ctx["query"]), {}


def _get_booking(ctx, bid):
    return 200, STATE.get_booking(bid), {}


def parse_patch_body(raw):
    if not raw:
        raise ApiError("bad_json", "request body must be JSON")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ApiError("bad_json", "request body is not valid JSON")
    if not isinstance(data, dict):
        raise ApiError("bad_json", "request body must be a JSON object")
    return data


def _patch_booking(ctx, bid):
    body = parse_patch_body(ctx["raw"])
    return 200, STATE.update_booking(bid, body), {}


def _delete_booking(ctx, bid):
    STATE.delete_booking(bid)
    return 204, None, {}


# (pattern, {method: handler}); pattern items are literals or None (= path parameter)
ROUTES = [
    (("rooms",), {"GET": _get_rooms, "POST": _post_rooms}),
    (("rooms", None), {"GET": _get_room}),
    (("rooms", None, "availability"), {"GET": _get_availability}),
    (("bookings",), {"GET": _get_bookings, "POST": _post_booking}),
    (("bookings", None), {"GET": _get_booking, "DELETE": _delete_booking, "PATCH": _patch_booking}),
]


def match_route(path):
    """Returns (handlers dict, params list) or None if no pattern matches the path."""
    segs = [s for s in path.split("/") if s != ""]
    for pattern, handlers in ROUTES:
        if len(pattern) != len(segs):
            continue
        params = []
        ok = True
        for p, s in zip(pattern, segs):
            if p is None:
                params.append(s)
            elif p != s:
                ok = False
                break
        if ok:
            return handlers, params
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "bookings/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # keep stdout/stderr quiet
        pass

    def _send(self, status, payload=None, extra_headers=None):
        extra_headers = extra_headers or {}
        self.send_response(status)
        for k, v in extra_headers.items():
            self.send_header(k, v)
        if status == 204 or payload is None:
            if status != 204:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":  # HEAD responses carry headers only
            self.wfile.write(data)

    def _send_error(self, err, extra_headers=None):
        self._send(err.status, {"error": {"code": err.code, "message": err.message}}, extra_headers)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _dispatch(self, method):
        raw = self._read_body()  # always consume the body, even on error paths
        try:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, keep_blank_values=True)
            matched = match_route(parts.path)
            if matched is None:
                raise ApiError("not_found", "no such endpoint")
            handlers, params = matched
            if method not in handlers:
                allow = ", ".join(sorted(handlers.keys()))
                raise _MethodNotAllowed(allow)
            key = self.headers.get("Idempotency-Key")
            if key is not None and key.strip() == "":
                key = None
            ctx = {"raw": raw, "query": query, "idem_key": key}
            status, payload, extra = handlers[method](ctx, *params)
            self._send(status, payload, extra)
        except _MethodNotAllowed as e:
            self._send_error(ApiError("method_not_allowed", "method not allowed"),
                             {"Allow": e.allow})
        except ApiError as e:
            self._send_error(e)
        except Exception:  # never let a handler bug drop the connection
            self._send(500, {"error": {"code": "internal_error",
                                       "message": "unexpected server error"}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")


class _MethodNotAllowed(Exception):
    def __init__(self, allow):
        super().__init__("method not allowed")
        self.allow = allow


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meeting room booking API")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
