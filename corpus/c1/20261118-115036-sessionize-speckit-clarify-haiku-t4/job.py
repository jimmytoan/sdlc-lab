"""sessionize: turn raw web events into user sessions and a daily summary.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes to <OUTPUT_DIR>:
    sessions.csv   one row per user session, sorted by start time
    daily.json     one entry per UTC day with sessions, plus run counts
    rejects.jsonl  one line per rejected input line

Exit codes: 0 on success (rejected lines do not fail the run), 2 when
events.jsonl is missing. Standard library only.
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext

ALLOWED_TYPES = {"view", "click", "purchase"}
REQUIRED_FIELDS = ("event_id", "user", "ts", "type")
AMOUNT_PATTERN = r"[0-9]+(\.[0-9]{1,2})?"
AMOUNT_RE = re.compile(AMOUNT_PATTERN)
SESSION_GAP = timedelta(minutes=30)

REASON_BAD_JSON = "bad_json"
REASON_MISSING_FIELD = "missing_field"
REASON_BAD_TS = "bad_ts"
REASON_BAD_TYPE = "bad_type"
REASON_BAD_AMOUNT = "bad_amount"

INPUT_FILE_NAME = "events.jsonl"
SESSIONS_FILE_NAME = "sessions.csv"
DAILY_FILE_NAME = "daily.json"
REJECTS_FILE_NAME = "rejects.jsonl"
SESSION_COLUMNS = ("session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Event:
    line_no: int
    event_id: str
    user: str
    ts: datetime
    type: str
    amount: Decimal | None


@dataclass(frozen=True)
class Rejection:
    line: int
    reason: str


@dataclass(frozen=True)
class Session:
    user: str
    number: int
    start: datetime
    end: datetime
    duration_s: int
    events: int
    purchases: int
    revenue: Decimal

    @property
    def session_id(self):
        return f"{self.user}-{self.number}"


def parse_ts(value):
    """Return the timestamp converted to aware UTC, or None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def validate_line(line_no, raw):
    """Return an Event for a valid line, or a Rejection giving the first failed check."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return Rejection(line_no, REASON_BAD_JSON)
    if not isinstance(obj, dict):
        return Rejection(line_no, REASON_BAD_JSON)

    for field in REQUIRED_FIELDS:
        if _is_blank(obj.get(field)):
            return Rejection(line_no, REASON_MISSING_FIELD)
    if not isinstance(obj["event_id"], str) or not isinstance(obj["user"], str):
        return Rejection(line_no, REASON_MISSING_FIELD)

    ts = parse_ts(obj["ts"])
    if ts is None:
        return Rejection(line_no, REASON_BAD_TS)

    event_type = obj["type"]
    if not isinstance(event_type, str) or event_type not in ALLOWED_TYPES:
        return Rejection(line_no, REASON_BAD_TYPE)

    amount = None
    if event_type == "purchase":
        raw_amount = obj.get("amount")
        if not isinstance(raw_amount, str) or not AMOUNT_RE.fullmatch(raw_amount):
            return Rejection(line_no, REASON_BAD_AMOUNT)
        amount = Decimal(raw_amount)

    return Event(
        line_no=line_no,
        event_id=obj["event_id"],
        user=obj["user"].strip().lower(),
        ts=ts,
        type=event_type,
        amount=amount,
    )


def read_lines(path):
    """Yield (1-based line number, raw bytes without the newline) for each physical line."""
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        return
    parts = data.split(b"\n")
    if parts[-1] == b"":
        parts.pop()  # the final newline does not start a new line
    for line_no, raw in enumerate(parts, start=1):
        if line_no == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]  # tolerate a UTF-8 byte order mark
        yield line_no, raw


def dedupe_events(events):
    """Keep one Event per event_id (earliest ts, then first line). Return (kept, duplicates_dropped)."""
    best = {}
    for event in events:
        current = best.get(event.event_id)
        if current is None or (event.ts, event.line_no) < (current.ts, current.line_no):
            best[event.event_id] = event
    return list(best.values()), len(events) - len(best)


def build_sessions(events):
    """Group events into per-user sessions split by gaps longer than SESSION_GAP."""
    by_user = {}
    for event in events:
        by_user.setdefault(event.user, []).append(event)

    sessions = []
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e.ts, e.event_id))
        groups = []
        previous_ts = None
        for event in user_events:
            if previous_ts is None or event.ts - previous_ts > SESSION_GAP:
                groups.append([])
            groups[-1].append(event)
            previous_ts = event.ts
        for number, group in enumerate(groups, start=1):
            purchases = [e for e in group if e.type == "purchase"]
            revenue = sum((e.amount for e in purchases), Decimal("0")).quantize(CENT)
            start, end = group[0].ts, group[-1].ts
            sessions.append(Session(
                user=user,
                number=number,
                start=start,
                end=end,
                duration_s=(end - start) // timedelta(seconds=1),
                events=len(group),
                purchases=len(purchases),
                revenue=revenue,
            ))
    sessions.sort(key=lambda s: (s.start, s.user, s.number))
    return sessions


def build_daily(sessions):
    """One entry per UTC start date with sessions, in ascending date order."""
    days = {}
    for session in sessions:
        day = days.setdefault(session.start.date().isoformat(), {"sessions": 0, "users": set(), "user_revenue": {}})
        day["sessions"] += 1
        day["users"].add(session.user)
        day["user_revenue"][session.user] = day["user_revenue"].get(session.user, Decimal("0")) + session.revenue

    entries = []
    for date in sorted(days):
        day = days[date]
        revenue = sum(day["user_revenue"].values(), Decimal("0")).quantize(CENT)
        top_user = None
        best = max(day["user_revenue"].values())
        if best > 0:
            top_user = min(user for user, amount in day["user_revenue"].items() if amount == best)
        entries.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": format(revenue, ".2f"),
            "top_user": top_user,
        })
    return entries


def write_outputs(out_dir, sessions, days, duplicates_dropped, rejects):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, SESSIONS_FILE_NAME), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(SESSION_COLUMNS)
        for s in sessions:
            writer.writerow([
                s.session_id, s.user, format_utc(s.start), format_utc(s.end),
                s.duration_s, s.events, s.purchases, format(s.revenue, ".2f"),
            ])

    summary = {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": len(rejects)}
    with open(os.path.join(out_dir, DAILY_FILE_NAME), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(out_dir, REJECTS_FILE_NAME), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps({"line": r.line, "reason": r.reason}) + "\n")


def format_utc(dt):
    """Format a UTC datetime as YYYY-MM-DDTHH:MM:SSZ (whole seconds)."""
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary from events.jsonl.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="folder to write sessions.csv, daily.json and rejects.jsonl to")
    args = parser.parse_args(argv)

    input_path = os.path.join(args.in_dir, INPUT_FILE_NAME)
    if not os.path.isfile(input_path):
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        return 2

    getcontext().prec = 60  # exact decimal sums for any realistic revenue

    events, rejects = [], []
    for line_no, raw in read_lines(input_path):
        result = validate_line(line_no, raw)
        if isinstance(result, Event):
            events.append(result)
        else:
            rejects.append(result)

    kept, duplicates_dropped = dedupe_events(events)
    sessions = build_sessions(kept)
    days = build_daily(sessions)
    write_outputs(args.out_dir, sessions, days, duplicates_dropped, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
