#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = {"view", "click", "purchase"}
REQUIRED_FIELDS = ("event_id", "user", "ts", "type")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


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
    return amount


def validate(obj):
    """Return a normalised event dict or raise Reject."""
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for field in REQUIRED_FIELDS:
        if field not in obj or obj[field] is None:
            raise Reject("missing_field")
    event_id = str(obj["event_id"]).strip()
    user = str(obj["user"]).strip().lower()
    if event_id == "" or user == "":
        raise Reject("missing_field")
    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")
    ts = parse_ts(obj["ts"])
    amount = None
    if etype == "purchase":
        if "amount" not in obj or obj["amount"] is None:
            raise Reject("missing_field")
        amount = parse_amount(obj["amount"])
    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}


def read_events(path):
    """Return (valid events in file order, rejects list, duplicates_dropped)."""
    events, rejects = [], []
    seen = set()
    duplicates = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, start=1):
            if raw.strip() == "":
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                rejects.append({"line": lineno, "reason": "bad_json"})
                continue
            try:
                ev = validate(obj)
            except Reject as r:
                rejects.append({"line": lineno, "reason": r.reason})
                continue
            if ev["event_id"] in seen:
                duplicates += 1
                continue
            seen.add(ev["event_id"])
            events.append(ev)
    return events, rejects, duplicates


def build_sessions(events):
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        for ev in evs:
            if current is None or ev["ts"] - current["end"] > SESSION_GAP:
                current = {"user": user, "start": ev["ts"], "end": ev["ts"],
                           "events": 0, "purchases": 0, "revenue": Decimal(0)}
                sessions.append(current)
            current["end"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
    # numbering per user, in start order
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    counter = {}
    for s in sessions:
        counter[s["user"]] = counter.get(s["user"], 0) + 1
        s["session_id"] = f"{s['user']}-{counter[s['user']]}"
    return sessions


def fmt_money(d):
    if d.as_tuple().exponent >= -2:
        return f"{d.quantize(Decimal('0.01')):f}"
    return f"{d:f}"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_outputs(out_dir, sessions, rejects, duplicates):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], iso(s["start"]), iso(s["end"]),
                        int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                        fmt_money(s["revenue"])])

    days = {}
    for s in sessions:
        day = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal(0), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal(0)) + s["revenue"]
    day_list = []
    for day in sorted(days):
        d = days[day]
        top = None
        best = None
        for user, rev in d["by_user"].items():
            if rev <= 0:
                continue
            if best is None or rev > best or (rev == best and user < top):
                best, top = rev, user
        day_list.append({"date": day, "sessions": d["sessions"], "users": len(d["users"]),
                         "revenue": fmt_money(d["revenue"]), "top_user": top})
    summary = {"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary.")
    parser.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    args = parser.parse_args(argv)

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        events, rejects, duplicates = read_events(path)
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 2
    sessions = build_sessions(events)
    try:
        write_outputs(args.out, sessions, rejects, duplicates)
    except OSError as e:
        print(f"error: cannot write output: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
