"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl (one JSON event per line) and writes to
<OUTPUT_DIR>:
    sessions.csv   one row per user session, sorted by start time
    daily.json     one entry per UTC day with sessions, plus run totals
    rejects.jsonl  one record per line that could not be used

Exit codes: 0 on success, 2 when the events file does not exist.
Standard library only.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

EVENTS_FILE = "events.jsonl"
SESSION_GAP = timedelta(minutes=30)  # a gap strictly greater than this splits sessions
VALID_TYPES = ("view", "click", "purchase")
# Data model rule: String matching ^\d+(\.\d{1,2})?$ (ASCII digits only).
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
SESSION_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


class Reject(Exception):
    """Raised by validation; carries the single reject reason for the line."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Event:
    __slots__ = ("event_id", "user", "ts", "type", "cents")

    def __init__(self, event_id, user, ts, etype, cents):
        self.event_id = event_id  # case-sensitive identity for deduplication
        self.user = user  # lower-cased
        self.ts = ts  # aware UTC datetime
        self.type = etype
        self.cents = cents  # purchase amount in integer cents; 0 otherwise


def fmt_utc(dt):
    """Format as YYYY-MM-DDTHH:MM:SSZ (UTC, whole seconds)."""
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def fmt_cents(cents):
    """Exactly two decimal places, e.g. 2499 -> '24.99'."""
    return f"{cents // 100}.{cents % 100:02d}"


def parse_amount(value):
    """Return the amount in integer cents, or None when it is not valid."""
    if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):
        return None
    whole, _, frac = value.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def is_empty(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def validate(obj):
    """Validate one decoded JSON value. Returns an Event or raises Reject.

    Check order (research Decision 4): object shape and required fields,
    then type, then timestamp, then amount.
    """
    if not isinstance(obj, dict):
        raise Reject("missing_field")
    for key in ("event_id", "user", "ts", "type"):
        if is_empty(obj.get(key)):
            raise Reject("missing_field")

    raw_id = obj["event_id"]
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        raise Reject("missing_field")
    event_id = str(raw_id)

    raw_user = obj["user"]
    if not isinstance(raw_user, str):
        raise Reject("missing_field")
    user = raw_user.strip().lower()
    if not user:
        raise Reject("missing_field")

    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")

    # ts: ISO 8601 with explicit UTC offset, stored as aware UTC datetime.
    raw_ts = obj["ts"]
    if not isinstance(raw_ts, str):
        raise Reject("bad_ts")
    try:
        ts = datetime.fromisoformat(raw_ts)
    except ValueError:
        raise Reject("bad_ts")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise Reject("bad_ts")
    try:
        ts = ts.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise Reject("bad_ts")

    # amount: required for purchases (missing or invalid -> bad_amount).
    # Views and clicks ignore any amount field.
    cents = 0
    if etype == "purchase":
        cents = parse_amount(obj.get("amount"))
        if cents is None:
            raise Reject("bad_amount")

    return Event(event_id, user, ts, etype, cents)


def read_events(path):
    """Stream the events file. Returns (events, rejects, duplicates_dropped).

    rejects is a list of (line_number, reason) in input order. Blank lines
    are skipped but still counted in line numbers.
    """
    events, rejects = [], []
    seen = set()
    duplicates = 0
    # Binary mode so that line numbers follow physical lines and bad UTF-8
    # becomes a bad_json reject instead of aborting the run.
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                rejects.append((line_no, "bad_json"))
                continue
            try:
                event = validate(obj)
            except Reject as r:
                rejects.append((line_no, r.reason))
                continue
            # Deduplicate only after validation: the first valid occurrence wins.
            if event.event_id in seen:
                duplicates += 1
                continue
            seen.add(event.event_id)
            events.append(event)
    return events, rejects, duplicates


def build_sessions(events):
    """Group by user, sort by time and split on gaps longer than 30 minutes.

    Returns session dicts sorted by (start, user, n).
    """
    by_user = defaultdict(list)
    for e in events:
        by_user[e.user].append(e)

    sessions = []
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.ts, e.event_id))
        current = None
        n = 0
        for e in user_events:
            if current is None or e.ts - current["end"] > SESSION_GAP:
                if current is not None:
                    sessions.append(current)
                n += 1
                current = {"user": user, "n": n, "start": e.ts, "end": e.ts,
                           "events": 0, "purchases": 0, "revenue": 0}
            current["end"] = e.ts  # sorted, so this is the latest event so far
            current["events"] += 1
            if e.type == "purchase":
                current["purchases"] += 1
                current["revenue"] += e.cents
        if current is not None:
            sessions.append(current)

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))
    return sessions


def build_daily(sessions):
    """One entry per UTC start date that has sessions, ascending by date."""
    days = {}
    for s in sessions:
        dt = s["start"]
        date = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        day = days.get(date)
        if day is None:
            day = days[date] = {"sessions": 0, "users": set(), "revenue": 0,
                                "by_user": defaultdict(int)}
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["by_user"][s["user"]] += s["revenue"]

    entries = []
    for date in sorted(days):
        day = days[date]
        top_user = None
        if day["revenue"] > 0:
            best = max(day["by_user"].values())
            # Ties go to the alphabetically first user name.
            top_user = min(u for u, rev in day["by_user"].items() if rev == best)
        entries.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": fmt_cents(day["revenue"]),
            "top_user": top_user,
        })
    return entries


def write_outputs(out_dir, sessions, days, duplicates, rejects):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(SESSION_HEADER)
        for s in sessions:
            w.writerow([
                f"{s['user']}-{s['n']}",
                s["user"],
                fmt_utc(s["start"]),
                fmt_utc(s["end"]),
                int((s["end"] - s["start"]).total_seconds()),
                s["events"],
                s["purchases"],
                fmt_cents(s["revenue"]),
            ])

    summary = {"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for line_no, reason in rejects:
            f.write(json.dumps({"line": line_no, "reason": reason}) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Turn raw web events into sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="folder to write sessions.csv, daily.json and rejects.jsonl to")
    args = parser.parse_args(argv)

    path = os.path.join(args.in_dir, EVENTS_FILE)
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2

    try:
        events, reject_pairs, duplicates = read_events(path)
    except OSError as exc:
        print(f"error: cannot read input file {path}: {exc}", file=sys.stderr)
        return 1

    sessions = build_sessions(events)
    days = build_daily(sessions)
    write_outputs(args.out_dir, sessions, days, duplicates, reject_pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
