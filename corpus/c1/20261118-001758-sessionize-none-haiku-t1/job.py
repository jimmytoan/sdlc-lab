#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl; writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fmt_money(d):
    return str(d.quantize(CENT))


def parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if isinstance(value, bool):
        raise Reject("bad_amount")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise Reject("bad_amount")
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    # Money is kept to cents; more precision would silently round.
    if amount != amount.quantize(CENT):
        raise Reject("bad_amount")
    return amount


def is_missing(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def validate(obj):
    """Return a normalised event dict or raise Reject."""
    if not isinstance(obj, dict):
        raise Reject("missing_field")
    for field in ("event_id", "user", "ts", "type"):
        if field not in obj or is_missing(obj[field]):
            raise Reject("missing_field")
    if obj["type"] not in VALID_TYPES:
        raise Reject("bad_type")
    ts = parse_ts(obj["ts"])
    amount = Decimal("0")
    if obj["type"] == "purchase":
        if "amount" not in obj or is_missing(obj["amount"]):
            raise Reject("missing_field")
        amount = parse_amount(obj["amount"])
    user = str(obj["user"]).strip().lower()
    return {
        "event_id": str(obj["event_id"]),
        "user": user,
        "ts": ts,
        "type": obj["type"],
        "amount": amount,
    }


def read_events(path):
    """Yield (line_no, event_or_None, reason_or_None) for each non-blank line."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                yield line_no, None, "bad_json"
                continue
            try:
                yield line_no, validate(obj), None
            except Reject as r:
                yield line_no, None, r.reason


def build_sessions(events):
    """events: list of normalised events (already de-duplicated)."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        counter = 0
        for ev in evs:
            if current is None or ev["ts"] - current["end"] > SESSION_GAP:
                counter += 1
                current = {
                    "session_id": f"{user}-{counter}",
                    "user": user,
                    "start": ev["ts"],
                    "end": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0"),
                }
                sessions.append(current)
            current["end"] = max(current["end"], ev["ts"])
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_outputs(out_dir, sessions, duplicates, rejects):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = int((s["end"] - s["start"]).total_seconds())
            w.writerow([s["session_id"], s["user"], iso_z(s["start"]), iso_z(s["end"]), duration,
                        s["events"], s["purchases"], fmt_money(s["revenue"])])

    # Daily summary: a session belongs to the UTC date of its start.
    days = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        if s["revenue"] > 0:
            d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    day_entries = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["by_user"]:
            # highest revenue; ties broken by user name
            top = min(d["by_user"], key=lambda u: (-d["by_user"][u], u))
        day_entries.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })

    summary = {"days": day_entries, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for rej in rejects:
            f.write(json.dumps(rej) + "\n")


def run(in_dir, out_dir):
    path = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2

    rejects = []
    seen = set()
    unique_events = []
    duplicates = 0
    for line_no, ev, reason in read_events(path):
        if reason is not None:
            rejects.append({"line": line_no, "reason": reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        unique_events.append(ev)

    sessions = build_sessions(unique_events)
    write_outputs(out_dir, sessions, duplicates, rejects)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory")
    args = parser.parse_args(argv)
    try:
        return run(args.in_dir, args.out_dir)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
