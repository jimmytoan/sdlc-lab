#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json, rejects.jsonl
into <OUTPUT_DIR>.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VALID_TYPES = ("view", "click", "purchase", "logout", "refund")
MONEY_TYPES = ("purchase", "refund")
DEFAULT_GAP_MINUTES = 30
EXIT_BAD_ARG = 64
CENT = Decimal("0.01")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _present(obj, key):
    val = obj.get(key)
    if val is None:
        return None
    if isinstance(val, str):
        val = val.strip()
        return val if val else None
    return val


def parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    text = value.strip()
    if text.endswith(("Z", "z")):
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
        text = repr(value) if isinstance(value, float) else str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise Reject("bad_amount")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    return amount


def validate(obj):
    """Return a normalised event dict or raise Reject."""
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    event_id = _present(obj, "event_id")
    user = _present(obj, "user")
    ts = _present(obj, "ts")
    etype = _present(obj, "type")
    if event_id is None or user is None or ts is None or etype is None:
        raise Reject("missing_field")
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int, float)):
        raise Reject("missing_field")
    event_id = str(event_id)
    if not isinstance(user, str):
        raise Reject("missing_field")
    user = user.lower()
    when = parse_ts(ts)
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")
    amount = Decimal("0")
    if etype in MONEY_TYPES:
        raw_amount = obj.get("amount")
        if raw_amount is None:
            raise Reject("missing_field")
        amount = parse_amount(raw_amount)
    return {"event_id": event_id, "user": user, "ts": when, "type": etype, "amount": amount}


def read_events(path):
    """Yield (line_no, event_or_None, reject_reason_or_None) for each non-blank line."""
    with open(path, "rb") as f:
        raw_lines = f.read().split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    for idx, raw in enumerate(raw_lines, start=1):
        if raw.strip() == b"":
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            yield idx, None, "bad_json"
            continue
        try:
            yield idx, validate(obj), None
        except Reject as rej:
            yield idx, None, rej.reason


def fmt_money(amount):
    out = str(amount.quantize(CENT, rounding=ROUND_HALF_UP))
    if out.startswith("-") and Decimal(out) == 0:
        out = out[1:]
    return out


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_sessions(events, gap):
    """events: list of unique valid events. Returns list of session dicts."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        closed = False
        for ev in evs:
            if current is None or closed or ev["ts"] - current["end"] > gap:
                current = {"user": user, "start": ev["ts"], "end": ev["ts"],
                           "events": 0, "purchases": 0, "refunds": 0, "revenue": Decimal("0")}
                sessions.append(current)
            closed = False
            current["end"] = max(current["end"], ev["ts"])
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
            elif ev["type"] == "refund":
                current["refunds"] += 1
                current["revenue"] -= ev["amount"]
            elif ev["type"] == "logout":
                closed = True
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    counters = {}
    for s in sessions:
        counters[s["user"]] = counters.get(s["user"], 0) + 1
        s["session_id"] = f"{s['user']}-{counters[s['user']]}"
    return sessions


def run(in_dir, out_dir, gap_minutes=DEFAULT_GAP_MINUTES):
    events_path = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(events_path):
        print(f"error: input file not found: {events_path}", file=sys.stderr)
        return 2
    gap = timedelta(minutes=gap_minutes)

    rejects = []
    seen = set()
    unique = []
    duplicates = 0
    for line_no, ev, reason in read_events(events_path):
        if reason is not None:
            rejects.append({"line": line_no, "reason": reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        unique.append(ev)

    sessions = build_sessions(unique, gap)

    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"])
        for s in sessions:
            writer.writerow([
                s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                s["refunds"], fmt_money(s["revenue"]),
            ])

    days = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    day_list = []
    for date in sorted(days):
        d = days[date]
        top_user = None
        best = None
        for user in sorted(d["by_user"]):
            total = d["by_user"][user]
            if total > 0 and (best is None or total > best):
                best = total
                top_user = user
        day_list.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })

    daily = {"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects), "gap_minutes": gap_minutes}
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


def parse_gap(value):
    """Return a positive int number of minutes, or None if invalid."""
    text = value.strip()
    if not text.isascii() or not text.isdigit():
        return None
    n = int(text)
    return n if n > 0 else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True)
    parser.add_argument("--out", dest="out_dir", required=True)
    parser.add_argument("--gap-minutes", dest="gap_minutes", default=str(DEFAULT_GAP_MINUTES))
    args = parser.parse_args(argv)
    gap_minutes = parse_gap(args.gap_minutes)
    if gap_minutes is None:
        print(f"error: --gap-minutes must be a positive integer, got {args.gap_minutes!r}", file=sys.stderr)
        return EXIT_BAD_ARG
    return run(args.in_dir, args.out_dir, gap_minutes)


if __name__ == "__main__":
    sys.exit(main())
