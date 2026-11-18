#!/usr/bin/env python3
"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl (one JSON event per line, UTF-8) and writes
sessions.csv, daily.json and rejects.jsonl into <OUTPUT_DIR>.

Exit codes: 0 success, 2 events.jsonl missing, 1 other failure.
Standard library only.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

INPUT_NAME = "events.jsonl"
SESSIONS_NAME = "sessions.csv"
DAILY_NAME = "daily.json"
REJECTS_NAME = "rejects.jsonl"

SESSION_GAP = timedelta(minutes=30)
ONE_SECOND = timedelta(seconds=1)
EVENT_TYPES = ("view", "click", "purchase")
SESSION_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
UTF8_BOM = b"\xef\xbb\xbf"

# Strict shape of an ISO 8601 timestamp with a mandatory UTC offset.
TS_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
# Plain digits, optional decimal point, at most two decimal places.
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")


class Event:
    __slots__ = ("ts", "line", "user", "type", "cents")

    def __init__(self, ts, line, user, etype, cents):
        self.ts = ts          # timezone-aware UTC datetime, full precision
        self.line = line      # 1-based physical line number
        self.user = user      # lower-cased user name
        self.type = etype
        self.cents = cents    # purchase amount in integer cents (0 otherwise)


class Session:
    __slots__ = ("user", "number", "first", "last", "events", "purchases", "cents")

    def __init__(self, user, number, ts):
        self.user = user
        self.number = number
        self.first = ts
        self.last = ts
        self.events = 0
        self.purchases = 0
        self.cents = 0

    @property
    def session_id(self):
        return f"{self.user}-{self.number}"


def parse_timestamp(text):
    """Return the timestamp as an aware UTC datetime, or None if it is not acceptable."""
    if not TS_RE.fullmatch(text):
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse_amount(value):
    """Return the amount in integer cents, or None if it is not acceptable."""
    if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):
        return None
    whole, _, frac = value.partition(".")
    try:
        return int(whole) * 100 + int(frac.ljust(2, "0"))
    except ValueError:  # e.g. absurdly long digit strings
        return None


def format_cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def format_utc(dt):
    """YYYY-MM-DDTHH:MM:SSZ, fractional seconds truncated."""
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def check_line(text, line_no):
    """Validate one non-blank line.

    Returns ((obj, ts, cents), None) when accepted, or (None, reason) when rejected.
    Checks run in the fixed order bad_json, missing_field, bad_ts, bad_type, bad_amount.
    """
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "missing_field"

    for key in ("event_id", "user", "ts", "type"):
        value = obj.get(key)
        if not isinstance(value, str) or value == "":
            return None, "missing_field"

    etype = obj["type"]
    amount_raw = obj.get("amount")
    if etype == "purchase" and (amount_raw is None or amount_raw == ""):
        return None, "missing_field"

    ts = parse_timestamp(obj["ts"])
    if ts is None:
        return None, "bad_ts"

    if etype not in EVENT_TYPES:
        return None, "bad_type"

    cents = 0
    if etype == "purchase":
        cents = parse_amount(amount_raw)
        if cents is None:
            return None, "bad_amount"

    return (obj, ts, cents), None


def read_events(path):
    """Read and validate events.jsonl.

    Returns (events, rejects, duplicates_dropped). rejects is a list of (line, reason).
    """
    events = []
    rejects = []
    seen_ids = set()
    duplicates = 0

    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, 1):
            if line_no == 1 and raw.startswith(UTF8_BOM):
                raw = raw[len(UTF8_BOM):]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                rejects.append((line_no, "bad_json"))
                continue
            if text.strip() == "":
                continue

            checked, reason = check_line(text, line_no)
            if reason is not None:
                rejects.append((line_no, reason))
                continue

            obj, ts, cents = checked
            event_id = obj["event_id"]
            if event_id in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(event_id)
            events.append(Event(ts, line_no, obj["user"].lower(), obj["type"], cents))

    return events, rejects, duplicates


def build_sessions(events):
    """Split each user's events into sessions on gaps longer than 30 minutes."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev.user, []).append(ev)

    sessions = []
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.ts, e.line))
        current = None
        number = 0
        for ev in user_events:
            if current is None or ev.ts - current.last > SESSION_GAP:
                number += 1
                current = Session(user, number, ev.ts)
                sessions.append(current)
            current.last = ev.ts
            current.events += 1
            if ev.type == "purchase":
                current.purchases += 1
                current.cents += ev.cents

    sessions.sort(key=lambda s: (s.first, s.user, s.number))
    return sessions


def build_days(sessions):
    """One entry per UTC date on which at least one session starts."""
    days = {}
    for s in sessions:
        day = s.first.date().isoformat()
        entry = days.get(day)
        if entry is None:
            entry = {"sessions": 0, "users": set(), "cents": 0, "per_user": {}}
            days[day] = entry
        entry["sessions"] += 1
        entry["users"].add(s.user)
        entry["cents"] += s.cents
        entry["per_user"][s.user] = entry["per_user"].get(s.user, 0) + s.cents

    result = []
    for day in sorted(days):
        entry = days[day]
        per_user = entry["per_user"]
        top = min(per_user, key=lambda u: (-per_user[u], u))  # ties: alphabetically first
        result.append({
            "date": day,
            "sessions": entry["sessions"],
            "users": len(entry["users"]),
            "revenue": format_cents(entry["cents"]),
            "top_user": top if per_user[top] > 0 else None,
        })
    return result


def render_outputs(sessions, days, duplicates, rejects):
    """Build the three output files as bytes so that nothing is written if rendering fails."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SESSION_HEADER)
    for s in sessions:
        writer.writerow([
            s.session_id,
            s.user,
            format_utc(s.first),
            format_utc(s.last),
            (s.last - s.first) // ONE_SECOND,
            s.events,
            s.purchases,
            format_cents(s.cents),
        ])
    sessions_bytes = buf.getvalue().encode("utf-8")

    daily = {"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    daily_bytes = (json.dumps(daily, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    rejects_bytes = "".join(
        json.dumps({"line": line, "reason": reason}) + "\n" for line, reason in rejects
    ).encode("utf-8")

    return {
        SESSIONS_NAME: sessions_bytes,
        DAILY_NAME: daily_bytes,
        REJECTS_NAME: rejects_bytes,
    }


def write_outputs(out_dir, files):
    """Write each file via a temporary file in out_dir, then rename it into place."""
    os.makedirs(out_dir, exist_ok=True)
    temps = []
    try:
        for name, data in files.items():
            tmp = os.path.join(out_dir, f".{name}.{os.getpid()}.tmp")
            temps.append(tmp)
            with open(tmp, "wb") as f:
                f.write(data)
        for name, tmp in zip(files, temps):
            os.replace(tmp, os.path.join(out_dir, name))
    except BaseException:
        for tmp in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Build user sessions and a daily summary from events.jsonl.",
    )
    parser.add_argument("--in", dest="input_dir", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="output_dir", required=True, metavar="OUTPUT_DIR",
                        help="folder to write sessions.csv, daily.json and rejects.jsonl to")
    args = parser.parse_args(argv)

    source = os.path.join(args.input_dir, INPUT_NAME)
    if not os.path.isfile(source):
        print(f"error: input file not found: {source}", file=sys.stderr)
        return 2

    try:
        events, rejects, duplicates = read_events(source)
        sessions = build_sessions(events)
        days = build_days(sessions)
        files = render_outputs(sessions, days, duplicates, rejects)
        write_outputs(args.output_dir, files)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
