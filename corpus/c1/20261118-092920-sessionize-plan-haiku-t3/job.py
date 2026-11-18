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
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SESSION_GAP = timedelta(minutes=30)
ONE_SECOND = timedelta(seconds=1)
VALID_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _required_str(obj, key):
    """Return the string value of a required field, or raise missing_field."""
    if key not in obj or obj[key] is None or not isinstance(obj[key], str):
        raise Reject("missing_field")
    return obj[key]


def _parse_amount(value):
    if "_" in value:
        raise Reject("bad_amount")
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    if amount.as_tuple().exponent < -2:
        raise Reject("bad_amount")
    return amount


def parse_line(raw):
    """Validate one non-blank physical line. Returns an event dict or raises Reject."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Reject("bad_json")
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    event_id = _required_str(obj, "event_id").strip()
    user = _required_str(obj, "user").strip()
    ts_raw = _required_str(obj, "ts")
    type_raw = _required_str(obj, "type")
    if not event_id or not user:
        raise Reject("missing_field")

    try:
        ts = datetime.fromisoformat(ts_raw)
    except (ValueError, TypeError):
        raise Reject("bad_ts")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise Reject("bad_ts")
    ts_utc = ts.astimezone(timezone.utc)

    if type_raw not in VALID_TYPES:
        raise Reject("bad_type")

    amount = None
    if type_raw == "purchase":
        amount_raw = obj.get("amount")
        if not isinstance(amount_raw, str):
            raise Reject("bad_amount")
        amount = _parse_amount(amount_raw)

    return {
        "event_id": event_id,
        "user": user.lower(),
        "ts": ts_utc,
        "type": type_raw,
        "amount": amount,
    }


def read_events(path):
    """Returns (events, rejects, duplicates_dropped). Line numbers are 1-based physical lines."""
    with open(path, "rb") as f:
        data = f.read()
    events = []
    rejects = []
    seen_ids = set()
    duplicates = 0
    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        if not raw.strip():
            continue
        try:
            ev = parse_line(raw)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        if ev["event_id"] in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(ev["event_id"])
        events.append(ev)
    return events, rejects, duplicates


def build_sessions(events):
    """Group by user, cut at gaps > 30 min. Returns list of session dicts sorted by start."""
    by_user = defaultdict(list)
    for ev in events:
        by_user[ev["user"]].append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        n = 0
        prev_ts = None
        for ev in evs:
            if current is None or (ev["ts"] - prev_ts) > SESSION_GAP:
                if current is not None:
                    sessions.append(current)
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
            current["end"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
            prev_ts = ev["ts"]
        if current is not None:
            sessions.append(current)

    sessions.sort(key=lambda s: (s["start"], s["user"], int(s["session_id"].rsplit("-", 1)[1])))
    for s in sessions:
        s["duration_s"] = (s["end"] - s["start"]) // ONE_SECOND
    return sessions


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(d.quantize(CENT))


def build_daily(sessions):
    by_day = {}
    for s in sessions:
        day = s["start"].date().isoformat()
        info = by_day.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                       "per_user": defaultdict(Decimal)})
        info["sessions"] += 1
        info["users"].add(s["user"])
        info["revenue"] += s["revenue"]
        info["per_user"][s["user"]] += s["revenue"]

    days = []
    for day in sorted(by_day):
        info = by_day[day]
        top_user = None
        best = None
        for user in sorted(info["per_user"]):
            rev = info["per_user"][user]
            if best is None or rev > best:
                best = rev
                top_user = user
        if best is None or best <= 0:
            top_user = None
        days.append({
            "date": day,
            "sessions": info["sessions"],
            "users": len(info["users"]),
            "revenue": fmt_money(info["revenue"]),
            "top_user": top_user,
        })
    return days


def write_outputs(out_dir, sessions, rejects, duplicates):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for s in sessions:
            writer.writerow([
                s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                s["duration_s"], s["events"], s["purchases"], fmt_money(s["revenue"]),
            ])

    daily = {
        "days": build_daily(sessions),
        "duplicates_dropped": duplicates,
        "rejected": len(rejects),
    }
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for r in rejects:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory (created if missing)")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        events, rejects, duplicates = read_events(in_path)
        sessions = build_sessions(events)
        write_outputs(args.out_dir, sessions, rejects, duplicates)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
