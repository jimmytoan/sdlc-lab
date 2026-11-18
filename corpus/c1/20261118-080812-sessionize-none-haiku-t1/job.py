#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl; writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

GAP = timedelta(minutes=30)
TYPES = {"view", "click", "purchase"}


def fail(msg, code=2):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(code)


def parse_ts(value):
    """Return an aware UTC datetime, or None if invalid (no UTC offset => invalid)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    """Return a non-negative finite Decimal, or None if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def fmt_money(amount):
    """Format a Decimal with at least two decimal places, never rounding away digits."""
    text = format(amount, "f")
    if "." not in text:
        return text + ".00"
    whole, frac = text.split(".", 1)
    if len(frac) < 2:
        frac = frac + "0" * (2 - len(frac))
    return whole + "." + frac


def iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(lineno, raw):
    """Return (event, None) for a valid event or (None, reason) for a reject."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"

    for key in ("event_id", "user", "ts", "type"):
        if key not in obj or obj[key] is None:
            return None, "missing_field"

    event_id = obj["event_id"]
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        return None, "missing_field"
    event_id = str(event_id).strip()
    if not event_id:
        return None, "missing_field"

    user = obj["user"]
    if not isinstance(user, str) or not user.strip():
        return None, "missing_field"
    user = user.strip().lower()

    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"

    etype = obj["type"]
    if not isinstance(etype, str) or etype not in TYPES:
        return None, "bad_type"

    amount = Decimal("0")
    if etype == "purchase":
        if "amount" not in obj or obj["amount"] is None:
            return None, "missing_field"
        parsed = parse_amount(obj["amount"])
        if parsed is None:
            return None, "bad_amount"
        amount = parsed

    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}, None


def build_sessions(events):
    """events: unique valid events. Returns list of session dicts sorted by start."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = None
        for ev in evs:
            if current is None or ev["ts"] - current[-1]["ts"] > GAP:
                current = []
                groups.append(current)
            current.append(ev)
        for n, group in enumerate(groups, start=1):
            start = group[0]["ts"]
            end = group[-1]["ts"]
            purchases = [e for e in group if e["type"] == "purchase"]
            revenue = sum((e["amount"] for e in purchases), Decimal("0"))
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start,
                "end": end,
                "duration_s": int((end - start).total_seconds()),
                "events": len(group),
                "purchases": len(purchases),
                "revenue": revenue,
            })

    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def build_daily(sessions, duplicates, rejected):
    per_day = {}
    for s in sessions:
        day = s["start"].strftime("%Y-%m-%d")
        d = per_day.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                     "user_revenue": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["user_revenue"][s["user"]] = d["user_revenue"].get(s["user"], Decimal("0")) + s["revenue"]

    days = []
    for day in sorted(per_day):
        d = per_day[day]
        top_user = None
        best = None
        for user in sorted(d["user_revenue"]):
            rev = d["user_revenue"][user]
            if rev > 0 and (best is None or rev > best):
                best = rev
                top_user = user
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
    parser.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        fail(f"error: input file not found: {in_path}")

    rejects = []
    valid = []
    try:
        with open(in_path, encoding="utf-8", errors="replace") as f:
            for lineno, raw in enumerate(f, start=1):
                if not raw.strip():
                    continue
                ev, reason = validate(lineno, raw)
                if reason is not None:
                    rejects.append({"line": lineno, "reason": reason})
                else:
                    valid.append(ev)
    except OSError as exc:
        fail(f"error: cannot read {in_path}: {exc}")

    seen = set()
    unique = []
    duplicates = 0
    for ev in valid:
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        unique.append(ev)

    sessions = build_sessions(unique)
    daily = build_daily(sessions, duplicates, len(rejects))

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            writer.writerow([s["session_id"], s["user"], iso_utc(s["start"]), iso_utc(s["end"]),
                             s["duration_s"], s["events"], s["purchases"], fmt_money(s["revenue"])])

    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
