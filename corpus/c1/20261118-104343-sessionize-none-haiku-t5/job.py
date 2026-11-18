#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl; writes sessions.csv, daily.json, rejects.jsonl to <OUTPUT_DIR>.
"""
import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

GAP = timedelta(minutes=30)
VALID_TYPES = {"view", "click", "purchase"}
CENT = Decimal("0.01")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fmt_money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        raise Reject("missing_field")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt


def parse_amount(value):
    if isinstance(value, bool):
        raise Reject("bad_amount")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise Reject("bad_amount")
    try:
        d = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not d.is_finite() or d < 0:
        raise Reject("bad_amount")
    return d


def parse_line(raw):
    """Return a validated event dict, or raise Reject(reason)."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    for field in ("event_id", "user", "ts", "type"):
        if field not in obj or obj[field] is None:
            raise Reject("missing_field")

    event_id = obj["event_id"]
    if isinstance(event_id, (int, float)) and not isinstance(event_id, bool):
        event_id = str(event_id)
    if not isinstance(event_id, str) or not event_id.strip():
        raise Reject("missing_field")
    event_id = event_id.strip()

    user = obj["user"]
    if not isinstance(user, str) or not user.strip():
        raise Reject("missing_field")
    user = user.strip().lower()

    ts = parse_ts(obj["ts"])

    etype = obj["type"]
    if not isinstance(etype, str):
        raise Reject("bad_type")
    etype = etype.strip().lower()
    if etype not in VALID_TYPES:
        raise Reject("bad_type")

    amount = Decimal("0")
    if etype == "purchase":
        if "amount" not in obj or obj["amount"] is None:
            raise Reject("missing_field")
        amount = parse_amount(obj["amount"])

    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}


def build_sessions(events):
    """events: list of dicts (already deduplicated). Returns list of session dicts."""
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = None
        prev_ts = None
        for e in evs:
            if current is None or e["ts"] - prev_ts > GAP:
                current = []
                groups.append(current)
            current.append(e)
            prev_ts = e["ts"]
        for n, grp in enumerate(groups, start=1):
            start = grp[0]["ts"]
            end = grp[-1]["ts"]
            purchases = [g for g in grp if g["type"] == "purchase"]
            revenue = sum((g["amount"] for g in purchases), Decimal("0"))
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start,
                "end": end,
                "duration_s": int((end - start).total_seconds()),
                "events": len(grp),
                "purchases": len(purchases),
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def build_daily(sessions, duplicates, rejected):
    days = {}
    for s in sessions:
        date = s["start"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                   "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    out_days = []
    for date in sorted(days):
        d = days[date]
        top = None
        best = Decimal("0")
        for user in sorted(d["by_user"]):
            amt = d["by_user"][user]
            if amt > best:
                best = amt
                top = user
        out_days.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })
    return {"days": out_days, "duplicates_dropped": duplicates, "rejected": rejected}


def run(in_dir, out_dir):
    src = os.path.join(in_dir, "events.jsonl")
    with open(src, "rb") as f:
        raw_lines = f.read().split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()

    rejects = []
    seen = set()
    events = []
    duplicates = 0
    for lineno, raw in enumerate(raw_lines, start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        text = text.strip()
        if not text:
            continue
        try:
            ev = parse_line(text)
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
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        s["duration_s"], s["events"], s["purchases"], fmt_money(s["revenue"])])
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description="Build clickstream sessions and daily summary.")
    p.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    p.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = p.parse_args(argv)

    src = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(src):
        print(f"error: input file not found: {src}", file=sys.stderr)
        return 2
    try:
        run(args.in_dir, args.out_dir)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
