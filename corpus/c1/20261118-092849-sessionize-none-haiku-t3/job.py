#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl, writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

INACTIVITY_LIMIT_S = 30 * 60
VALID_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")


def fail(msg, code=2):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def fmt_money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def fmt_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    """Return an aware UTC datetime, or None if the timestamp is invalid or has no UTC offset."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s[-1] in "zZ":
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    """Return a finite, non-negative Decimal, or None if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def validate(obj):
    """Return (event_dict, None) for a valid event or (None, reason) for a reject."""
    if not isinstance(obj, dict):
        return None, "bad_json"
    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            return None, "missing_field"
    event_id = str(obj["event_id"]).strip()
    user = obj["user"]
    if not isinstance(user, str) or not user.strip():
        return None, "missing_field"
    if not event_id:
        return None, "missing_field"
    etype = obj["type"]
    if not isinstance(etype, str):
        return None, "bad_type"
    etype = etype.strip().lower()
    if etype not in VALID_TYPES:
        return None, "bad_type"
    if etype == "purchase" and obj.get("amount") is None:
        return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    amount = Decimal("0")
    if etype == "purchase":
        amount = parse_amount(obj["amount"])
        if amount is None:
            return None, "bad_amount"
    return {
        "event_id": event_id,
        "user": user.strip().lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,
    }, None


def read_events(path):
    """Yield (line_no, raw_line) for every non-blank line."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, start=1):
            if raw.strip():
                yield line_no, raw


def build_sessions(events):
    """events: list of valid event dicts (already de-duplicated)."""
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        n = 0
        for e in evs:
            if current is not None:
                gap = (e["ts"] - current["end"]).total_seconds()
                if gap <= INACTIVITY_LIMIT_S:
                    current["end"] = e["ts"]
                    current["events"] += 1
                    if e["type"] == "purchase":
                        current["purchases"] += 1
                        current["revenue"] += e["amount"]
                    continue
                sessions.append(current)
            n += 1
            current = {
                "session_id": f"{user}-{n}",
                "user": user,
                "start": e["ts"],
                "end": e["ts"],
                "events": 1,
                "purchases": 1 if e["type"] == "purchase" else 0,
                "revenue": e["amount"] if e["type"] == "purchase" else Decimal("0"),
            }
        if current is not None:
            sessions.append(current)
    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def build_daily(sessions):
    days = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        day = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                     "user_revenue": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["user_revenue"][s["user"]] = day["user_revenue"].get(s["user"], Decimal("0")) + s["revenue"]
    out = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["revenue"] > 0:
            best = max(d["user_revenue"].values())
            top = sorted(u for u, r in d["user_revenue"].items() if r == best)[0]
        out.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary.")
    parser.add_argument("--in", dest="inp", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        fail(f"error: input file not found: {in_path}")

    try:
        os.makedirs(args.out, exist_ok=True)
    except OSError as exc:
        fail(f"error: cannot create output directory {args.out}: {exc}")

    rejects = []
    seen_ids = set()
    duplicates = 0
    events = []
    try:
        for line_no, raw in read_events(in_path):
            try:
                obj = json.loads(raw)
            except ValueError:
                rejects.append({"line": line_no, "reason": "bad_json"})
                continue
            event, reason = validate(obj)
            if reason is not None:
                rejects.append({"line": line_no, "reason": reason})
                continue
            if event["event_id"] in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(event["event_id"])
            events.append(event)
    except OSError as exc:
        fail(f"error: cannot read {in_path}: {exc}")

    sessions = build_sessions(events)
    days = build_daily(sessions)

    try:
        with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
            for s in sessions:
                w.writerow([
                    s["session_id"], s["user"], fmt_utc(s["start"]), fmt_utc(s["end"]),
                    int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                    fmt_money(s["revenue"]),
                ])

        summary = {"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
        with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")

        with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
            for r in rejects:
                f.write(json.dumps(r) + "\n")
    except OSError as exc:
        fail(f"error: cannot write output: {exc}", code=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
