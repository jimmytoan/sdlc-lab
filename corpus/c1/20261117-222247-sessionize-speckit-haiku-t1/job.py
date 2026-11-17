#!/usr/bin/env python3
"""sessionize: turn raw web events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")
DECIMAL_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    text = value.strip()
    if not text:
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if not isinstance(value, str):
        raise Reject("bad_amount")
    text = value.strip()
    if not DECIMAL_RE.match(text):
        raise Reject("bad_amount")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    return amount


def parse_event(raw):
    """Validate one decoded JSON object. Returns a dict or raises Reject."""
    if not isinstance(raw, dict):
        raise Reject("bad_json")
    for field in ("event_id", "user", "ts", "type"):
        v = raw.get(field)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise Reject("missing_field")
    event_type = raw["type"]
    if not isinstance(event_type, str):
        raise Reject("bad_type")
    event_type = event_type.strip()
    ts = parse_ts(raw["ts"])
    if event_type not in VALID_TYPES:
        raise Reject("bad_type")
    amount = Decimal("0")
    if event_type == "purchase":
        if raw.get("amount") is None:
            raise Reject("missing_field")
        amount = parse_amount(raw["amount"])
    user = str(raw["user"]).strip().lower()
    return {
        "event_id": str(raw["event_id"]),
        "user": user,
        "ts": ts,
        "type": event_type,
        "amount": amount,
    }


def read_events(path):
    """Returns (valid_events_in_file_order, rejects, duplicates_dropped)."""
    events = []
    rejects = []
    seen_ids = set()
    duplicates = 0
    with open(path, "rb") as f:
        for line_no, raw_line in enumerate(f, start=1):
            text = raw_line.decode("utf-8-sig", errors="replace").strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except ValueError:
                rejects.append({"line": line_no, "reason": "bad_json"})
                continue
            try:
                event = parse_event(raw)
            except Reject as rej:
                rejects.append({"line": line_no, "reason": rej.reason})
                continue
            if event["event_id"] in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(event["event_id"])
            events.append(event)
    return events, rejects, duplicates


def build_sessions(events):
    """Group events into sessions. Returns list of session dicts sorted by start."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, user_events in by_user.items():
        user_events.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        for ev in user_events:
            if current is None or ev["ts"] - current["end"] > SESSION_GAP:
                current = {
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

    sessions.sort(key=lambda s: (s["start"], s["user"]))
    counters = {}
    for s in sessions:
        counters[s["user"]] = counters.get(s["user"], 0) + 1
        s["session_id"] = f"{s['user']}-{counters[s['user']]}"
    return sessions


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(amount):
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


def write_sessions_csv(path, sessions):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["session_id", "user", "start", "end", "duration_s",
                         "events", "purchases", "revenue"])
        for s in sessions:
            duration = int((s["end"] - s["start"]).total_seconds())
            writer.writerow([
                s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                duration, s["events"], s["purchases"], fmt_money(s["revenue"]),
            ])


def build_daily(sessions, rejected, duplicates):
    per_day = {}
    for s in sessions:
        day = s["start"].strftime("%Y-%m-%d")
        d = per_day.setdefault(day, {"sessions": 0, "users": set(),
                                     "revenue": Decimal("0"), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    days = []
    for day in sorted(per_day):
        d = per_day[day]
        top_user = None
        if d["revenue"] > 0:
            # most revenue; ties broken by user name for determinism
            top_user = min(d["by_user"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        days.append({
            "date": day,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })
    return {"days": days, "duplicates_dropped": duplicates, "rejected": rejected}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sessions and daily summary from events.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        events, rejects, duplicates = read_events(in_path)
    except OSError as exc:
        print(f"error: cannot read {in_path}: {exc}", file=sys.stderr)
        return 2

    sessions = build_sessions(events)
    daily = build_daily(sessions, len(rejects), duplicates)

    try:
        os.makedirs(args.out_dir, exist_ok=True)
        write_sessions_csv(os.path.join(args.out_dir, "sessions.csv"), sessions)
        with open(os.path.join(args.out_dir, "daily.json"), "w", encoding="utf-8") as f:
            json.dump(daily, f, indent=2)
            f.write("\n")
        with open(os.path.join(args.out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
            for rej in rejects:
                f.write(json.dumps(rej) + "\n")
    except OSError as exc:
        print(f"error: cannot write output: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
