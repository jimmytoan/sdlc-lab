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
TWO_PLACES = Decimal("0.01")


class BadEvent(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _required(obj, name):
    value = obj.get(name)
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise BadEvent("missing_field")
    return value


def parse_ts(text):
    if not isinstance(text, str):
        raise BadEvent("bad_ts")
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise BadEvent("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise BadEvent("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if isinstance(value, bool):
        raise BadEvent("bad_amount")
    if isinstance(value, (int, float)):
        text = repr(value) if isinstance(value, float) else str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise BadEvent("bad_amount")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise BadEvent("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise BadEvent("bad_amount")
    return amount


def parse_line(line):
    """Return a validated event dict, or raise BadEvent(reason)."""
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        raise BadEvent("bad_json")
    if not isinstance(obj, dict):
        raise BadEvent("bad_json")

    event_id = _required(obj, "event_id")
    user = _required(obj, "user")
    ts_raw = _required(obj, "ts")
    etype = _required(obj, "type")

    if not isinstance(user, str):
        raise BadEvent("missing_field")
    user = user.strip().lower()
    if not user:
        raise BadEvent("missing_field")

    ts = parse_ts(ts_raw)

    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise BadEvent("bad_type")

    amount = None
    if etype == "purchase":
        amount = parse_amount(_required(obj, "amount"))

    return {
        "event_id": str(event_id),
        "user": user,
        "ts": ts,
        "type": etype,
        "amount": amount,
    }


def load_events(path):
    """Return (valid_events_in_file_order, rejects, duplicates_dropped)."""
    events = []
    rejects = []
    seen_ids = set()
    duplicates = 0
    with open(path, "rb") as f:
        for lineno, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                rejects.append({"line": lineno, "reason": "bad_json"})
                continue
            try:
                ev = parse_line(text)
            except BadEvent as exc:
                rejects.append({"line": lineno, "reason": exc.reason})
                continue
            if ev["event_id"] in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(ev["event_id"])
            events.append(ev)
    return events, rejects, duplicates


def build_sessions(events):
    """Group events into sessions. Returns list of session dicts sorted by start."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: e["ts"])  # stable: ties keep file order
        current = None
        n = 0
        for ev in evs:
            if current is None or ev["ts"] - current["end"] > SESSION_GAP:
                n += 1
                current = {
                    "session_id": f"{user}-{n}",
                    "user": user,
                    "start": ev["ts"],
                    "end": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0"),
                }
                sessions.append(current)
            current["end"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(amount):
    return str(amount.quantize(TWO_PLACES))


def write_sessions(path, sessions):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = int((s["end"] - s["start"]).total_seconds())
            w.writerow([
                s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                duration, s["events"], s["purchases"], fmt_money(s["revenue"]),
            ])


def build_daily(sessions):
    days = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    out = []
    for date in sorted(days):
        d = days[date]
        top_user = None
        if d["revenue"] > 0:
            # highest revenue; ties broken by user name (alphabetical)
            top_user = min(d["by_user"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary.")
    parser.add_argument("--in", dest="input_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="output_dir", required=True, help="output directory")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.input_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        events, rejects, duplicates = load_events(in_path)
    except OSError as exc:
        print(f"error: cannot read {in_path}: {exc}", file=sys.stderr)
        return 2

    sessions = build_sessions(events)
    days = build_daily(sessions)

    os.makedirs(args.output_dir, exist_ok=True)
    write_sessions(os.path.join(args.output_dir, "sessions.csv"), sessions)

    summary = {
        "days": days,
        "duplicates_dropped": duplicates,
        "rejected": len(rejects),
    }
    with open(os.path.join(args.output_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.output_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
