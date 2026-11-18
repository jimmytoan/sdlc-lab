"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only.

Exit codes: 0 success, 2 missing input file (or usage error), 1 unexpected failure.
"""
import argparse
import csv
import json
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

INPUT_NAME = "events.jsonl"
SESSIONS_NAME = "sessions.csv"
DAILY_NAME = "daily.json"
REJECTS_NAME = "rejects.jsonl"

SESSION_HEADER = ("session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue")
VALID_TYPES = frozenset({"view", "click", "purchase"})
GAP = timedelta(minutes=30)
# Matched with fullmatch, so a trailing newline cannot slip through.
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
REJECT_REASONS = ("bad_json", "missing_field", "bad_type", "bad_ts", "bad_amount")


def parse_event(raw_text):
    """Validate one non-blank line. Returns (event, None) or (None, reason).

    Checks run in this order and the first failure wins.
    """
    try:
        obj = json.loads(raw_text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"

    for key in ("event_id", "user", "ts", "type"):
        if not isinstance(obj.get(key), str):
            return None, "missing_field"
    event_id = obj["event_id"].strip()
    user = obj["user"].strip()
    if not event_id or not user:
        return None, "missing_field"

    etype = obj["type"]
    if etype == "purchase" and obj.get("amount") is None:
        return None, "missing_field"
    if etype not in VALID_TYPES:
        return None, "bad_type"

    try:
        # Lone surrogates from JSON escapes cannot be written as UTF-8 output.
        event_id.encode("utf-8")
        user.encode("utf-8")
    except UnicodeEncodeError:
        return None, "bad_json"

    try:
        ts = datetime.fromisoformat(obj["ts"])
    except ValueError:
        return None, "bad_ts"
    if ts.tzinfo is None or ts.utcoffset() is None:
        return None, "bad_ts"
    try:
        ts = ts.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None, "bad_ts"

    cents = 0
    if etype == "purchase":
        amount = obj["amount"]
        if not isinstance(amount, str) or not AMOUNT_RE.fullmatch(amount):
            return None, "bad_amount"
        whole, _, frac = amount.partition(".")
        cents = int(whole) * 100 + int((frac + "00")[:2])

    event = {
        "event_id": event_id,
        "user": user.lower(),
        "ts": ts,
        "type": etype,
        "cents": cents,
    }
    return event, None


def read_events(path):
    """Return (valid events in file order, reject rows in line order)."""
    data = path.read_bytes()
    events = []
    rejects = []
    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        if raw.strip() == b"":
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        if line_no == 1 and text.startswith("\ufeff"):
            text = text[1:]
        event, reason = parse_event(text)
        if reason is not None:
            rejects.append({"line": line_no, "reason": reason})
        else:
            events.append(event)
    return events, rejects


def drop_duplicates(events):
    """Keep the first valid occurrence of each event_id. Returns (kept, dropped_count)."""
    seen = set()
    kept = []
    dropped = 0
    for ev in events:
        if ev["event_id"] in seen:
            dropped += 1
            continue
        seen.add(ev["event_id"])
        kept.append(ev)
    return kept, dropped


def build_sessions(events):
    """Group events per user and split on gaps larger than GAP."""
    by_user = defaultdict(list)
    for ev in events:
        by_user[ev["user"]].append(ev)

    sessions = []
    for user in sorted(by_user):
        ordered = sorted(by_user[user], key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = []
        for ev in ordered:
            if current and ev["ts"] - current[-1]["ts"] > GAP:
                groups.append(current)
                current = []
            current.append(ev)
        if current:
            groups.append(current)

        for n, group in enumerate(groups, start=1):
            start = group[0]["ts"]
            end = group[-1]["ts"]
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start,
                "end": end,
                "duration_s": (end - start) // timedelta(seconds=1),
                "events": len(group),
                "purchases": sum(1 for e in group if e["type"] == "purchase"),
                "cents": sum(e["cents"] for e in group),
            })

    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def fmt_utc(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def fmt_money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def build_daily(sessions, dups, rejected_count):
    """One entry per UTC start date of sessions."""
    days = {}
    for s in sessions:
        date = s["start"].date().isoformat()
        day = days.setdefault(date, {"sessions": 0, "users": set(), "cents": 0,
                                     "user_cents": defaultdict(int)})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["cents"] += s["cents"]
        day["user_cents"][s["user"]] += s["cents"]

    day_entries = []
    for date in sorted(days):
        day = days[date]
        earners = [(u, c) for u, c in day["user_cents"].items() if c > 0]
        top_user = min(earners, key=lambda uc: (-uc[1], uc[0]))[0] if earners else None
        day_entries.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": fmt_money(day["cents"]),
            "top_user": top_user,
        })
    return {"days": day_entries, "duplicates_dropped": dups, "rejected": rejected_count}


def write_outputs(out_dir, sessions, daily_doc, rejects):
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / SESSIONS_NAME, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(SESSION_HEADER)
        for s in sessions:
            writer.writerow([
                s["session_id"],
                s["user"],
                fmt_utc(s["start"]),
                fmt_utc(s["end"]),
                s["duration_s"],
                s["events"],
                s["purchases"],
                fmt_money(s["cents"]),
            ])

    with open(out_dir / DAILY_NAME, "w", encoding="utf-8") as f:
        json.dump(daily_doc, f, indent=2)
        f.write("\n")

    with open(out_dir / REJECTS_NAME, "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")


def run(in_dir, out_dir):
    input_path = in_dir / INPUT_NAME
    if not input_path.is_file():
        print(f"error: events file not found: {input_path}", file=sys.stderr)
        return 2

    events, rejects = read_events(input_path)
    unique, dups = drop_duplicates(events)
    sessions = build_sessions(unique)
    daily_doc = build_daily(sessions, dups, len(rejects))
    write_outputs(out_dir, sessions, daily_doc, rejects)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="output directory (created if missing)")
    args = parser.parse_args(argv)
    try:
        return run(Path(args.in_dir), Path(args.out_dir))
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
