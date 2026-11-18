"""sessionize: clickstream sessions nightly batch job.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only (Python 3.13).
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

INPUT_FILE = "events.jsonl"
SESSIONS_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
VALID_TYPES = ("view", "click", "purchase")
REQUIRED_FIELDS = ("event_id", "user", "ts", "type")
SESSION_GAP_S = 1800  # "no more than 30 minutes" of inactivity, inclusive
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="job.py", description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="directory that contains events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory (created if missing)")
    return parser.parse_args(argv)


def parse_ts(text):
    """`ts` ISO 8601 with UTC offset. Returns an aware UTC datetime, or None if invalid or naive."""
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_amount_cents(value):
    """`amount_cents`: present only for `purchase`; non-negative; at most 2 decimals.

    Returns an int number of cents, or None if the value is not a valid decimal string.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def validate_line(raw_bytes, line_no):
    """Return (event, None) for a valid line, or (None, reason) for a reject."""
    try:
        obj = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"

    values = {}
    for field in REQUIRED_FIELDS:
        value = obj.get(field)
        # event_id non-empty after strip; user strip then lower-case, non-empty
        if not isinstance(value, str) or not value.strip():
            return None, "missing_field"
        values[field] = value.strip()

    if values["type"] not in VALID_TYPES:
        return None, "bad_type"

    ts = parse_ts(values["ts"])
    if ts is None:
        return None, "bad_ts"

    amount_cents = None
    if values["type"] == "purchase":
        if "amount" not in obj or obj["amount"] is None or (isinstance(obj["amount"], str) and not obj["amount"].strip()):
            return None, "missing_field"
        amount_cents = parse_amount_cents(obj["amount"])
        if amount_cents is None:
            return None, "bad_amount"
    # view and click: any amount field is ignored

    event = {
        "event_id": values["event_id"],
        "user": values["user"].lower(),
        "ts": ts,
        "type": values["type"],
        "amount_cents": amount_cents,
        "line": line_no,
    }
    return event, None


def read_events(path):
    """Stream events.jsonl. Returns (kept_events, rejects, duplicates_dropped).

    Blank lines are skipped and are not rejects. The first valid occurrence of an
    event_id is kept; later valid occurrences are counted as duplicates.
    """
    events = []
    rejects = []
    seen = set()
    duplicates = 0
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            if line_no == 1 and raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]  # tolerate a UTF-8 byte order mark
            if not raw.strip():
                continue
            event, reason = validate_line(raw, line_no)
            if reason is not None:
                rejects.append({"line": line_no, "reason": reason})
                continue
            if event["event_id"] in seen:
                duplicates += 1
                continue
            seen.add(event["event_id"])
            events.append(event)
    return events, rejects, duplicates


def fmt_ts(dt):
    """Output time format: UTC, whole seconds, `YYYY-MM-DDTHH:MM:SSZ`."""
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def build_sessions(events):
    """Group events into sessions. Returns a list of session dicts sorted by (start, user, n)."""
    by_user = defaultdict(list)
    for ev in sorted(events, key=lambda e: (e["ts"], e["event_id"])):
        by_user[ev["user"]].append(ev)

    sessions = []
    for user, user_events in by_user.items():
        groups = []
        current = [user_events[0]]
        for prev, ev in zip(user_events, user_events[1:]):
            if (ev["ts"] - prev["ts"]).total_seconds() > SESSION_GAP_S:
                groups.append(current)
                current = [ev]
            else:
                current.append(ev)
        groups.append(current)

        for n, group in enumerate(groups, start=1):
            start = group[0]["ts"]
            end = group[-1]["ts"]
            # Durations are measured on the whole-second values written to the file,
            # so a consumer computing end - start gets the same number.
            start_s = start.replace(microsecond=0)
            end_s = end.replace(microsecond=0)
            purchases = [e for e in group if e["type"] == "purchase"]
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start_s,
                "end": end_s,
                "duration_s": int((end_s - start_s).total_seconds()),
                "events": len(group),
                "purchases": len(purchases),
                "revenue_cents": sum(e["amount_cents"] for e in purchases),
                "n": n,
            })

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))
    return sessions


def write_sessions(out_dir, sessions):
    path = os.path.join(out_dir, "sessions.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(SESSIONS_HEADER)
        for s in sessions:
            writer.writerow([
                s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]), s["duration_s"],
                s["events"], s["purchases"], fmt_money(s["revenue_cents"]),
            ])


def build_daily(sessions):
    """One entry per UTC start date with sessions. Sessions and revenue belong to the start date."""
    by_date = defaultdict(list)
    for s in sessions:
        by_date[s["start"].strftime("%Y-%m-%d")].append(s)

    days = []
    for date in sorted(by_date):
        day_sessions = by_date[date]
        revenue_by_user = defaultdict(int)
        for s in day_sessions:
            revenue_by_user[s["user"]] += s["revenue_cents"]
        total_cents = sum(revenue_by_user.values())
        if total_cents > 0:
            # highest revenue wins; ties go to the alphabetically first user
            top_user = min(revenue_by_user, key=lambda u: (-revenue_by_user[u], u))
        else:
            top_user = None  # no revenue that day
        days.append({
            "date": date,
            "sessions": len(day_sessions),
            "users": len({s["user"] for s in day_sessions}),
            "revenue": fmt_money(total_cents),
            "top_user": top_user,
        })
    return days


def write_daily(out_dir, days, duplicates_dropped, rejected):
    payload = {
        "days": days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
    }
    path = os.path.join(out_dir, "daily.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def write_rejects(out_dir, rejects):
    path = os.path.join(out_dir, "rejects.jsonl")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n")


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    input_path = os.path.join(args.in_dir, INPUT_FILE)
    # Check before creating anything, so a failed run leaves no output behind.
    if not os.path.isfile(input_path):
        print(f"sessionize: input file not found: {input_path}", file=sys.stderr)
        return 2

    events, rejects, duplicates = read_events(input_path)
    sessions = build_sessions(events)
    days = build_daily(sessions)

    os.makedirs(args.out_dir, exist_ok=True)
    write_sessions(args.out_dir, sessions)
    write_rejects(args.out_dir, rejects)
    write_daily(args.out_dir, days, duplicates, len(rejects))
    return 0


if __name__ == "__main__":
    sys.exit(main())
