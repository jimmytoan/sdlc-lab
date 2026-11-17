#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

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
from decimal import Decimal

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = ("view", "click", "purchase")
# Non-negative decimal with at most two fraction digits, e.g. "19.99", "20", "0.5", ".5".
AMOUNT_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]{0,2})?|\.[0-9]{1,2})$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _required_str(obj, key):
    """Return the stripped-or-raw string value for a required key, or raise missing_field."""
    if key not in obj:
        raise Reject("missing_field")
    value = obj[key]
    if not isinstance(value, str) or value.strip() == "":
        raise Reject("missing_field")
    return value


def parse_ts(text):
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise Reject("bad_ts")


def parse_event(raw_line):
    """Validate one non-blank line. Returns a dict event or raises Reject."""
    try:
        obj = json.loads(raw_line)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    event_id = _required_str(obj, "event_id")
    user_raw = _required_str(obj, "user")
    ts_raw = _required_str(obj, "ts")
    if "type" not in obj:
        raise Reject("missing_field")
    etype = obj["type"]
    if not isinstance(etype, str) or etype.strip() == "":
        raise Reject("missing_field")

    ts = parse_ts(ts_raw)
    if etype not in VALID_TYPES:
        raise Reject("bad_type")

    amount = None
    if etype == "purchase":
        if "amount" not in obj:
            raise Reject("bad_amount")
        a = obj["amount"]
        if not isinstance(a, str) or not AMOUNT_RE.match(a):
            raise Reject("bad_amount")
        amount = Decimal(a)

    return {
        "event_id": event_id,
        "user": user_raw.strip().lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,
    }


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def money(d):
    return f"{d.quantize(Decimal('0.01')):f}"


def read_events(path):
    """Yield (line_no, raw_text) for each non-blank line."""
    with open(path, "rb") as f:
        data = f.read()
    for line_no, chunk in enumerate(data.split(b"\n"), start=1):
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        if chunk.strip() == b"":
            continue
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            yield line_no, None
            continue
        yield line_no, text


def build(in_path):
    rejects = []
    valid = []  # (seq, event)
    for line_no, text in read_events(in_path):
        if text is None:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        try:
            ev = parse_event(text)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        valid.append((line_no, ev))

    # Deduplicate globally by event_id. Keep the earliest timestamp; ties keep the first in file.
    best = {}
    for seq, (line_no, ev) in enumerate(valid):
        key = ev["event_id"]
        cur = best.get(key)
        if cur is None or ev["ts"] < cur[1]["ts"]:
            best[key] = (seq, ev)
    duplicates_dropped = len(valid) - len(best)
    kept = sorted(best.values(), key=lambda pair: pair[0])
    events = [ev for _, ev in kept]

    # Group per user and split into sessions on gaps > 30 minutes.
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: e["ts"])  # stable: file order for equal timestamps
        current = None
        for ev in evs:
            if current is None or ev["ts"] - current["end"] > SESSION_GAP:
                current = {"user": user, "start": ev["ts"], "end": ev["ts"],
                           "events": 0, "purchases": 0, "revenue": Decimal("0")}
                sessions.append(current)
            current["end"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"]))
    per_user_count = {}
    for s in sessions:
        n = per_user_count.get(s["user"], 0) + 1
        per_user_count[s["user"]] = n
        s["session_id"] = f"{s['user']}-{n}"

    return sessions, rejects, duplicates_dropped


def write_outputs(out_dir, sessions, rejects, duplicates_dropped):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = int((s["end"] - s["start"]).total_seconds())
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        duration, s["events"], s["purchases"], money(s["revenue"])])

    # Daily summary: a session belongs to the UTC date of its start.
    days = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        if s["revenue"] > 0:
            d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    day_entries = []
    for date in sorted(days):
        d = days[date]
        top_user = None
        if d["by_user"]:
            best_amount = max(d["by_user"].values())
            top_user = sorted(u for u, v in d["by_user"].items() if v == best_amount)[0]
        day_entries.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": money(d["revenue"]),
            "top_user": top_user,
        })

    daily = {"days": day_entries, "duplicates_dropped": duplicates_dropped, "rejected": len(rejects)}
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary from events.jsonl")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        sessions, rejects, dropped = build(in_path)
        write_outputs(args.out_dir, sessions, rejects, dropped)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
