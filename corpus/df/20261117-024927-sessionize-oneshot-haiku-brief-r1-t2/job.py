#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl, writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

SESSION_GAP = timedelta(minutes=30)
EVENT_TYPES = {"view", "click", "purchase"}
AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
CENT = Decimal("0.01")


def parse_ts(value):
    """Return a timezone-aware UTC datetime, or None if not a valid ISO 8601 timestamp with offset."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    """Return a non-negative Decimal, or None if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        value = repr(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not AMOUNT_RE.match(value):
        return None
    try:
        amount = Decimal(value)
    except Exception:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def validate_event(obj):
    """Return (event_dict, None) when valid, or (None, reason) when rejected."""
    if not isinstance(obj, dict):
        return None, "bad_json"

    def missing(v):
        return v is None or (isinstance(v, str) and not v.strip())

    raw_type = obj.get("type")
    needs_amount = isinstance(raw_type, str) and raw_type.strip() == "purchase"
    required = ["event_id", "user", "ts", "type"] + (["amount"] if needs_amount else [])
    for key in required:
        if key not in obj or missing(obj[key]):
            return None, "missing_field"

    event_id = obj["event_id"]
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        return None, "missing_field"
    event_id = str(event_id).strip()

    user = obj["user"]
    if not isinstance(user, str):
        return None, "missing_field"
    user = user.strip().lower()

    etype = raw_type.strip() if isinstance(raw_type, str) else None
    if etype not in EVENT_TYPES:
        return None, "bad_type"

    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"

    amount = None
    if etype == "purchase":
        amount = parse_amount(obj["amount"])
        if amount is None:
            return None, "bad_amount"

    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}, None


def fmt_money(d):
    if d == d.quantize(CENT):
        return f"{d:.2f}"
    return format(d, "f")


def read_lines(path):
    with open(path, "rb") as f:
        data = f.read()
    for raw in data.split(b"\n"):
        yield raw.rstrip(b"\r")


def build_sessions(events):
    """Group unique events into sessions. Returns list of session dicts sorted by start."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user in sorted(by_user):
        evs = sorted(by_user[user], key=lambda e: (e["ts"], e["event_id"]))
        n = 0
        cur = None
        last_ts = None
        for ev in evs:
            if cur is None or ev["ts"] - last_ts > SESSION_GAP:
                n += 1
                cur = {"user": user, "n": n, "start": ev["ts"], "end": ev["ts"],
                       "events": 0, "purchases": 0, "revenue": Decimal("0")}
                sessions.append(cur)
            cur["end"] = ev["ts"]
            cur["events"] += 1
            if ev["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += ev["amount"]
            last_ts = ev["ts"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))
    for s in sessions:
        s["session_id"] = f"{s['user']}-{s['n']}"
    return sessions


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_sessions_csv(path, sessions):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = int(round((s["end"] - s["start"]).total_seconds()))
            w.writerow([s["session_id"], s["user"], iso_z(s["start"]), iso_z(s["end"]), duration,
                        s["events"], s["purchases"], fmt_money(s["revenue"])])


def build_daily(sessions):
    days = {}
    for s in sessions:
        day = s["start"].date().isoformat()
        d = days.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal("0")) + s["revenue"]

    out = []
    for day in sorted(days):
        d = days[day]
        top_user = None
        top_rev = None
        for user in sorted(d["per_user"]):
            rev = d["per_user"][user]
            if rev > 0 and (top_rev is None or rev > top_rev):
                top_user, top_rev = user, rev
        out.append({
            "date": day,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and daily summary.")
    parser.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    seen = set()
    events = []
    rejects = []
    duplicates = 0

    for lineno, raw in enumerate(read_lines(in_path), start=1):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        if not text.strip():
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        ev, reason = validate_event(obj)
        if reason is not None:
            rejects.append({"line": lineno, "reason": reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        events.append(ev)

    sessions = build_sessions(events)
    days = build_daily(sessions)

    os.makedirs(args.out, exist_ok=True)
    write_sessions_csv(os.path.join(args.out, "sessions.csv"), sessions)
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
