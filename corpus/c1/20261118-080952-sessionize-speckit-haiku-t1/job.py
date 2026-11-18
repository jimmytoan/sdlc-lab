"""sessionize: clickstream sessions nightly batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR> (created if missing; results replaced).

Exit status:
    0  run completed (including when some lines were rejected or the file is empty)
    2  <INPUT_DIR>/events.jsonl is missing (or not a regular file), or invalid arguments
    1  unexpected I/O failure while reading or writing
Standard library only.
"""
import argparse
import codecs
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

INPUT_FILE_NAME = "events.jsonl"
SESSIONS_FILE = "sessions.csv"
DAILY_FILE = "daily.json"
REJECTS_FILE = "rejects.jsonl"

SESSION_GAP = timedelta(seconds=1800)
VALID_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")
UTC = timezone.utc
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
SESSION_COLUMNS = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


class Reject(Exception):
    """Raised by parse_event with a reject reason code."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    """Parse an ISO 8601 timestamp with a UTC offset into a UTC datetime (whole seconds).

    Raises ValueError if the value is not a string, cannot be parsed, or has no offset.
    """
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    text = value
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp has no UTC offset")
    try:
        return dt.astimezone(UTC).replace(microsecond=0)
    except OverflowError as exc:
        raise ValueError("timestamp out of range") from exc


def format_utc(dt):
    """Format a UTC datetime as YYYY-MM-DDTHH:MM:SSZ."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_revenue(amount):
    """Format a Decimal with exactly two decimal places, e.g. 19.99 or 0.00."""
    with localcontext() as ctx:
        ctx.prec = 1000
        return format(amount.quantize(CENT), "f")


def _present_str(value):
    return isinstance(value, str) and value.strip() != ""


def parse_event(text):
    """Validate one non-blank event line.

    Returns (user_key, utc_datetime, event_id, type, amount) where amount is a
    Decimal (Decimal(0) for view/click). Raises Reject(reason) on failure.
    Validation order: bad_json, missing_field, bad_type, bad_ts, bad_amount.
    """
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    etype = obj.get("type")

    if not _present_str(event_id) or not _present_str(user):
        raise Reject("missing_field")
    if ts is None or (isinstance(ts, str) and ts.strip() == "") or etype is None or (
            isinstance(etype, str) and etype.strip() == ""):
        raise Reject("missing_field")

    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")

    try:
        when = parse_ts(ts)
    except (ValueError, TypeError):
        raise Reject("bad_ts")

    amount = Decimal(0)
    if etype == "purchase":
        raw = obj.get("amount")
        if not isinstance(raw, str) or AMOUNT_RE.fullmatch(raw) is None:
            raise Reject("bad_amount")
        try:
            with localcontext() as ctx:
                ctx.prec = 1000
                amount = Decimal(raw)
                if amount <= 0:
                    raise Reject("bad_amount")
                amount.quantize(CENT)  # must be representable in two decimals
        except InvalidOperation:
            raise Reject("bad_amount")

    return user.strip().lower(), when, event_id, etype, amount


def read_events(path):
    """Single pass over events.jsonl.

    Returns (events, rejects, duplicates_dropped) where events is a list of
    valid, de-duplicated tuples (user_key, when, event_id, type, amount) in
    file order and rejects is a list of {"line", "reason"} dicts.
    """
    events = []
    rejects = []
    seen = set()
    duplicates = 0
    with open(path, "rb") as handle:
        for lineno, raw in enumerate(handle, 1):
            if lineno == 1 and raw.startswith(codecs.BOM_UTF8):
                raw = raw[len(codecs.BOM_UTF8):]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                rejects.append({"line": lineno, "reason": "bad_json"})
                continue
            if text.strip() == "":
                continue
            try:
                record = parse_event(text)
            except Reject as rej:
                rejects.append({"line": lineno, "reason": rej.reason})
                continue
            event_id = record[2]
            if event_id in seen:
                duplicates += 1
                continue
            seen.add(event_id)
            events.append(record)
    return events, rejects, duplicates


def build_sessions(events):
    """Group valid events into sessions.

    Returns a list of session dicts sorted by (start, session_id).
    """
    ordered = sorted(events, key=lambda e: (e[0], e[1], e[2]))
    sessions = []
    current = None
    count_for_user = 0
    last_user = None
    last_time = None
    for user, when, _event_id, etype, amount in ordered:
        if current is None or user != last_user or (when - last_time) > SESSION_GAP:
            if user != last_user:
                count_for_user = 0
            count_for_user += 1
            current = {
                "session_id": f"{user}-{count_for_user}",
                "user": user,
                "start": when,
                "end": when,
                "events": 0,
                "purchases": 0,
                "revenue": Decimal(0),
            }
            sessions.append(current)
        current["end"] = when
        current["events"] += 1
        if etype == "purchase":
            current["purchases"] += 1
            current["revenue"] += amount
        last_user = user
        last_time = when

    sessions.sort(key=lambda s: (s["start"], s["session_id"]))
    return sessions


def session_rows(sessions):
    """Turn session dicts into CSV-ready rows."""
    rows = []
    for s in sessions:
        rows.append({
            "session_id": s["session_id"],
            "user": s["user"],
            "start": format_utc(s["start"]),
            "end": format_utc(s["end"]),
            "duration_s": int((s["end"] - s["start"]).total_seconds()),
            "events": s["events"],
            "purchases": s["purchases"],
            "revenue": format_revenue(s["revenue"]),
        })
    return rows


def build_daily_document(sessions, duplicates_dropped, rejected_count):
    """Build the daily.json document from the sorted sessions."""
    by_date = {}
    for s in sessions:
        day = s["start"].date().isoformat()
        entry = by_date.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        entry["sessions"] += 1
        entry["users"].add(s["user"])
        entry["revenue"] += s["revenue"]
        entry["per_user"][s["user"]] = entry["per_user"].get(s["user"], Decimal(0)) + s["revenue"]

    days = []
    for day in sorted(by_date):
        entry = by_date[day]
        if entry["revenue"] > 0:
            top_user = min(entry["per_user"].items(), key=lambda item: (-item[1], item[0]))[0]
        else:
            top_user = None
        days.append({
            "date": day,
            "sessions": entry["sessions"],
            "users": len(entry["users"]),
            "revenue": format_revenue(entry["revenue"]),
            "top_user": top_user,
        })

    return {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejected_count}


def _write_text(path, writer_fn):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer_fn(handle)


def write_outputs(out_dir, rows, daily_doc, rejects):
    """Write the three result files, replacing earlier ones atomically per file."""
    os.makedirs(out_dir, exist_ok=True)

    def write_sessions(handle):
        writer = csv.DictWriter(handle, fieldnames=SESSION_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    def write_daily(handle):
        json.dump(daily_doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    def write_rejects(handle):
        for rec in rejects:
            handle.write(json.dumps({"line": rec["line"], "reason": rec["reason"]}) + "\n")

    outputs = [
        (SESSIONS_FILE, write_sessions),
        (DAILY_FILE, write_daily),
        (REJECTS_FILE, write_rejects),
    ]
    temps = []
    try:
        for name, fn in outputs:
            tmp = os.path.join(out_dir, f".{name}.{os.getpid()}.tmp")
            temps.append(tmp)
            _write_text(tmp, fn)
        for (name, _fn), tmp in zip(outputs, temps):
            os.replace(tmp, os.path.join(out_dir, name))
    finally:
        for tmp in temps:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def build_parser():
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary from events.jsonl.")
    parser.add_argument("--in", dest="in_dir", required=True, help="folder containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="folder for results (created if missing)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    in_path = os.path.join(args.in_dir, INPUT_FILE_NAME)
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        with localcontext() as ctx:
            ctx.prec = 1000
            events, rejects, duplicates = read_events(in_path)
            sessions = build_sessions(events)
            rows = session_rows(sessions)
            daily_doc = build_daily_document(sessions, duplicates, len(rejects))
        write_outputs(args.out_dir, rows, daily_doc, rejects)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
