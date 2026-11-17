#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

DEFAULT_GAP_MINUTES = 30
TYPES = {"view", "click", "purchase", "logout", "refund"}
AMOUNT_TYPES = {"purchase", "refund"}
AMOUNT_RE = re.compile(r"^\+?(\d+(\.\d*)?|\.\d+)$")
REQUIRED = ("event_id", "user", "ts", "type")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if not isinstance(value, str):
        raise Reject("bad_amount")
    s = value.strip()
    if not AMOUNT_RE.match(s):
        raise Reject("bad_amount")
    try:
        amt = Decimal(s)
    except InvalidOperation:
        raise Reject("bad_amount")
    if not amt.is_finite() or amt < 0:
        raise Reject("bad_amount")
    return amt


def parse_event(line):
    """Return a validated event dict, or raise Reject(reason)."""
    try:
        obj = json.loads(line)
    except ValueError:
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for field in REQUIRED:
        if field not in obj or obj[field] is None:
            raise Reject("missing_field")
    event_id = obj["event_id"]
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        raise Reject("missing_field")
    event_id = str(event_id).strip()
    user = obj["user"]
    if not isinstance(user, str) or not user.strip():
        raise Reject("missing_field")
    user = user.strip().lower()
    if not event_id:
        raise Reject("missing_field")
    ts_raw = obj["ts"]
    if isinstance(ts_raw, str) and not ts_raw.strip():
        raise Reject("missing_field")
    ts = parse_ts(ts_raw)
    etype = obj["type"]
    if not isinstance(etype, str) or etype not in TYPES:
        raise Reject("bad_type")
    amount = None
    if etype in AMOUNT_TYPES:
        if "amount" not in obj or obj["amount"] is None:
            raise Reject("missing_field")
        amount = parse_amount(obj["amount"])
    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    exp = d.as_tuple().exponent
    if exp >= -2:
        return f"{d:.2f}"
    return format(d, "f")


def parse_gap(value):
    """Return the gap in minutes if value is a positive integer string, else None."""
    s = value.strip()
    if not re.fullmatch(r"[0-9]+", s):
        return None
    n = int(s)
    return n if n > 0 else None


def build_sessions(events, gap):
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: e["ts"])
        current = None
        for ev in evs:
            if (current is None or current["closed"] or ev["ts"] - current["end"] > gap):
                current = {"user": user, "start": ev["ts"], "end": ev["ts"], "events": 0,
                           "purchases": 0, "refunds": 0, "revenue": Decimal("0"), "closed": False}
                sessions.append(current)
            current["end"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
            elif ev["type"] == "refund":
                current["refunds"] += 1
                current["revenue"] -= ev["amount"]
            elif ev["type"] == "logout":
                current["closed"] = True
    # assign per-user running numbers in start order
    counter = {}
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    for s in sessions:
        counter[s["user"]] = counter.get(s["user"], 0) + 1
        s["session_id"] = f"{s['user']}-{counter[s['user']]}"
    return sessions


def main(argv=None):
    parser = argparse.ArgumentParser(description="sessionize clickstream batch job")
    parser.add_argument("--in", dest="inp", required=True, help="input directory")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    parser.add_argument("--gap-minutes", dest="gap_minutes", default=None,
                        help="session inactivity gap in minutes (positive integer, default 30)")
    args = parser.parse_args(argv)

    gap_minutes = DEFAULT_GAP_MINUTES
    if args.gap_minutes is not None:
        gap_minutes = parse_gap(args.gap_minutes)
        if gap_minutes is None:
            print(f"error: --gap-minutes must be a positive integer, got {args.gap_minutes!r}",
                  file=sys.stderr)
            return 64
    gap = timedelta(minutes=gap_minutes)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    seen = set()
    events = []
    rejects = []
    duplicates = 0
    with open(in_path, encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                ev = parse_event(line)
            except Reject as r:
                rejects.append({"line": lineno, "reason": r.reason})
                continue
            if ev["event_id"] in seen:
                duplicates += 1
                continue
            seen.add(ev["event_id"])
            events.append(ev)

    sessions = build_sessions(events, gap)

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases",
                    "refunds", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                        s["refunds"], fmt_money(s["revenue"])])

    # daily summary, keyed by UTC date of session start
    days = {}
    for s in sessions:
        day = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    day_entries = []
    for day in sorted(days):
        d = days[day]
        top = None
        if d["by_user"]:
            best = max(d["by_user"].values())
            if best > 0:
                top = sorted(u for u, v in d["by_user"].items() if v == best)[0]
        day_entries.append({"date": day, "sessions": d["sessions"], "users": len(d["users"]),
                            "revenue": fmt_money(d["revenue"]), "top_user": top})

    daily = {"gap_minutes": gap_minutes, "days": day_entries, "duplicates_dropped": duplicates,
             "rejected": len(rejects)}
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
