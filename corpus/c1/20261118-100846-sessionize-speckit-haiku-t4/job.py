"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

EVENTS_FILE = "events.jsonl"
SESSIONS_FILE = "sessions.csv"
DAILY_FILE = "daily.json"
REJECTS_FILE = "rejects.jsonl"

SESSION_GAP = timedelta(minutes=30)
EVENT_TYPES = ("view", "click", "purchase")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def validate_line(raw):
    """Validate one raw (bytes) input line.

    Returns (event, None) for a valid line, where event is a tuple
    (event_id, user, ts_utc, type, amount), or (None, reason) for a reject.
    Checks run in a fixed order; the first failure is reported.
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "missing_field"

    fields = {}
    for key in ("event_id", "user", "ts", "type"):
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, "missing_field"
        fields[key] = value.strip()

    event_type = fields["type"]
    if event_type not in EVENT_TYPES:
        return None, "bad_type"

    try:
        ts = datetime.fromisoformat(fields["ts"])
        if ts.tzinfo is None or ts.utcoffset() is None:
            return None, "bad_ts"
        ts = ts.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError):
        return None, "bad_ts"

    amount = None
    if event_type == "purchase":
        raw_amount = obj.get("amount")
        if not isinstance(raw_amount, str) or not AMOUNT_RE.fullmatch(raw_amount):
            return None, "bad_amount"
        amount = Decimal(raw_amount)

    return (fields["event_id"], fields["user"].lower(), ts, event_type, amount), None


class Session:
    __slots__ = ("user", "number", "start", "end", "events", "purchases", "revenue")

    def __init__(self, user, number, ts):
        self.user = user
        self.number = number
        self.start = ts
        self.end = ts
        self.events = 0
        self.purchases = 0
        self.revenue = Decimal("0")

    @property
    def session_id(self):
        return f"{self.user}-{self.number}"


def read_events(path):
    """Stream the events file. Returns (events, rejects, duplicates_dropped).

    events: list of (line_no, event_tuple) for valid, de-duplicated events.
    """
    events = []
    rejects = []
    duplicates = 0
    seen_ids = set()
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            event, reason = validate_line(raw)
            if reason is not None:
                rejects.append({"line": line_no, "reason": reason})
                continue
            event_id = event[0]
            if event_id in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(event_id)
            events.append((line_no, event))
    return events, rejects, duplicates


def build_sessions(events):
    """Group events per user into sessions; returns sessions sorted by start."""
    by_user = {}
    for line_no, (_, user, ts, event_type, amount) in events:
        by_user.setdefault(user, []).append((ts, line_no, event_type, amount))

    sessions = []
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e[0], e[1]))
        current = None
        number = 0
        for ts, _, event_type, amount in user_events:
            if current is None or ts - current.end > SESSION_GAP:
                number += 1
                current = Session(user, number, ts)
                sessions.append(current)
            current.end = ts
            current.events += 1
            if event_type == "purchase":
                current.purchases += 1
                current.revenue += amount

    sessions.sort(key=lambda s: (s.start, s.user, s.number))
    return sessions


def build_daily(sessions):
    """One entry per UTC start date that has sessions, in date order."""
    days = {}
    for s in sessions:
        date = s.start.strftime("%Y-%m-%d")
        day = days.get(date)
        if day is None:
            day = {"sessions": 0, "users": set(), "revenue": Decimal("0"), "per_user": {}}
            days[date] = day
        day["sessions"] += 1
        day["users"].add(s.user)
        day["revenue"] += s.revenue
        day["per_user"][s.user] = day["per_user"].get(s.user, Decimal("0")) + s.revenue

    result = []
    for date in sorted(days):
        day = days[date]
        paying = [(user, rev) for user, rev in day["per_user"].items() if rev > 0]
        top_user = min(paying, key=lambda ur: (-ur[1], ur[0]))[0] if paying else None
        result.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": f"{day['revenue']:.2f}",
            "top_user": top_user,
        })
    return result


def write_outputs(out_dir, sessions, days, duplicates, rejects):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, SESSIONS_FILE), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            writer.writerow([
                s.session_id,
                s.user,
                s.start.strftime(TS_FORMAT),
                s.end.strftime(TS_FORMAT),
                (s.end - s.start) // timedelta(seconds=1),
                s.events,
                s.purchases,
                f"{s.revenue:.2f}",
            ])

    daily = {"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(out_dir, DAILY_FILE), "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(daily, indent=2, ensure_ascii=False) + "\n")

    with open(os.path.join(out_dir, REJECTS_FILE), "w", encoding="utf-8", newline="\n") as f:
        for reject in rejects:
            f.write(json.dumps(reject, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary from events.jsonl.")
    parser.add_argument("--in", dest="input_dir", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="output_dir", required=True, metavar="OUTPUT_DIR",
                        help="folder for sessions.csv, daily.json and rejects.jsonl")
    args = parser.parse_args(argv)

    events_path = os.path.join(args.input_dir, EVENTS_FILE)
    if not os.path.isfile(events_path):
        print(f"error: input file not found: {events_path}", file=sys.stderr)
        return 2

    events, rejects, duplicates = read_events(events_path)
    sessions = build_sessions(events)
    days = build_daily(sessions)
    write_outputs(args.output_dir, sessions, days, duplicates, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
