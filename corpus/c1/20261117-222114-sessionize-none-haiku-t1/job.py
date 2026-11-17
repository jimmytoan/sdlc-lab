#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = {"view", "click", "purchase"}
CENT = Decimal("0.01")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        raise Reject("missing_field")
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
    if isinstance(value, bool) or value is None:
        raise Reject("bad_amount")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise Reject("bad_amount")
        text = repr(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise Reject("bad_amount")
    try:
        amt = Decimal(text)
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not amt.is_finite() or amt < 0:
        raise Reject("bad_amount")
    return amt


def parse_event(obj):
    """Validate one decoded JSON object. Returns a dict or raises Reject."""
    if not isinstance(obj, dict):
        raise Reject("missing_field")
    for key in ("event_id", "user", "ts", "type"):
        if key not in obj or obj[key] is None:
            raise Reject("missing_field")
    event_id = obj["event_id"]
    if isinstance(event_id, (dict, list)):
        raise Reject("missing_field")
    event_id = str(event_id).strip()
    user = obj["user"]
    if not isinstance(user, str) or not user.strip():
        raise Reject("missing_field")
    user = user.strip().lower()
    if not event_id:
        raise Reject("missing_field")
    etype = obj["type"]
    if not isinstance(etype, str) or not etype.strip():
        raise Reject("missing_field")
    etype = etype.strip().lower()
    ts = parse_ts(obj["ts"])
    if etype not in VALID_TYPES:
        raise Reject("bad_type")
    amount = Decimal("0")
    if etype == "purchase":
        if "amount" not in obj or obj["amount"] is None:
            raise Reject("missing_field")
        amount = parse_amount(obj["amount"])
    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(amt):
    return str(amt.quantize(CENT, rounding=ROUND_HALF_UP))


def build_sessions(events):
    """events: list of dicts with user, ts, type, amount. Returns list of session dicts."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = None
        for ev in evs:
            if current is None or ev["ts"] - current[-1]["ts"] > SESSION_GAP:
                current = []
                groups.append(current)
            current.append(ev)
        for idx, group in enumerate(groups, start=1):
            start = group[0]["ts"]
            end = group[-1]["ts"]
            purchases = [e for e in group if e["type"] == "purchase"]
            revenue = sum((e["amount"] for e in purchases), Decimal("0"))
            sessions.append({
                "session_id": f"{user}-{idx}",
                "user": user,
                "start_dt": start,
                "start": fmt_ts(start),
                "end": fmt_ts(end),
                "duration_s": int((end - start).total_seconds()),
                "events": len(group),
                "purchases": len(purchases),
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start_dt"], s["user"], s["session_id"]))
    return sessions


def build_daily(sessions, duplicates, rejected):
    days = {}
    for s in sessions:
        date = s["start_dt"].strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                   "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal("0")) + s["revenue"]
    out_days = []
    for date in sorted(days):
        d = days[date]
        top_user = None
        best = None
        for user, rev in d["per_user"].items():
            if rev <= 0:
                continue
            if best is None or rev > best or (rev == best and user < top_user):
                best = rev
                top_user = user
        out_days.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })
    return {"days": out_days, "duplicates_dropped": duplicates, "rejected": rejected}


def run(in_dir, out_dir):
    src = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(src):
        sys.stderr.write(f"error: input file not found: {src}\n")
        return 2

    seen = set()
    events = []
    rejects = []
    duplicates = 0

    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, RecursionError):
                rejects.append({"line": lineno, "reason": "bad_json"})
                continue
            try:
                ev = parse_event(obj)
            except Reject as r:
                rejects.append({"line": lineno, "reason": r.reason})
                continue
            if ev["event_id"] in seen:
                duplicates += 1
                continue
            seen.add(ev["event_id"])
            events.append(ev)

    sessions = build_sessions(events)
    daily = build_daily(sessions, duplicates, len(rejects))

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], s["start"], s["end"], s["duration_s"],
                        s["events"], s["purchases"], fmt_money(s["revenue"])])
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory")
    args = parser.parse_args(argv)
    return run(args.in_dir, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
