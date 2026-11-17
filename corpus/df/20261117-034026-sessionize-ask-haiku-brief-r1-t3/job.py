#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl, writes sessions.csv, daily.json, rejects.jsonl to <OUTPUT_DIR>.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

GAP = timedelta(minutes=30)
VALID_TYPES = {"view", "click", "purchase"}
# Non-negative decimal with at most two fraction digits. Zero is allowed.
AMOUNT_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]{0,2})?|\.[0-9]{1,2})$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fmt_utc(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def fmt_money(d):
    return f"{d.quantize(Decimal('0.01'))}"


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
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise Reject("bad_ts")


def parse_amount(value):
    if not isinstance(value, str) or not AMOUNT_RE.match(value):
        raise Reject("bad_amount")
    return Decimal(value)


def parse_line(raw, lineno):
    """Return a validated event dict, or raise Reject."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("missing_field")

    def get_str(key):
        v = obj.get(key)
        if v is None:
            raise Reject("missing_field")
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise Reject("missing_field")
        s = str(v).strip()
        if s == "":
            raise Reject("missing_field")
        return s

    event_id = get_str("event_id")
    user = get_str("user").lower()
    if "ts" not in obj or obj["ts"] is None:
        raise Reject("missing_field")
    if "type" not in obj or obj["type"] is None:
        raise Reject("missing_field")

    ts = parse_ts(obj["ts"])

    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")

    amount = None
    if etype == "purchase":
        if obj.get("amount") is None:
            raise Reject("bad_amount")
        amount = parse_amount(obj["amount"])

    return {"event_id": event_id, "user": user, "ts": ts, "type": etype,
            "amount": amount, "line": lineno}


def read_events(path):
    """Yield (lineno, raw_text) for each non-blank line; raw bytes decoded per line."""
    with open(path, "rb") as f:
        for lineno, chunk in enumerate(f, 1):
            try:
                text = chunk.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and text.strip() == "":
                continue
            yield lineno, text


def run(inp, out):
    path = os.path.join(inp, "events.jsonl")
    if not os.path.isfile(path):
        sys.stderr.write(f"error: input file not found: {path}\n")
        return 2

    rejects = []
    valid = []
    for lineno, text in read_events(path):
        if text is None:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        try:
            valid.append(parse_line(text.strip("\r\n"), lineno))
        except Reject as r:
            rejects.append({"line": lineno, "reason": r.reason})

    # Deduplicate by event_id: keep earliest timestamp, ties by file position.
    valid.sort(key=lambda e: (e["ts"], e["line"]))
    seen = set()
    unique = []
    duplicates = 0
    for e in valid:
        if e["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(e["event_id"])
        unique.append(e)

    # Group by user, sort by (ts, file position), split on gaps > 30 min.
    by_user = {}
    for e in unique:
        by_user.setdefault(e["user"], []).append(e)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        current = None
        number = 0
        for e in evs:
            if current is None or e["ts"] - current["end"] > GAP:
                number += 1
                current = {"user": user, "number": number, "start": e["ts"], "end": e["ts"],
                           "events": 0, "purchases": 0, "revenue": Decimal("0")}
                sessions.append(current)
            current["end"] = e["ts"]
            current["events"] += 1
            if e["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += e["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["number"]))

    os.makedirs(out, exist_ok=True)

    with open(os.path.join(out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = int((s["end"] - s["start"]).total_seconds())
            w.writerow([f"{s['user']}-{s['number']}", s["user"], fmt_utc(s["start"]), fmt_utc(s["end"]),
                        duration, s["events"], s["purchases"], fmt_money(s["revenue"])])

    # Daily summary, grouped by UTC date of session start.
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
        if d["revenue"] > 0:
            best = None
            for user, rev in d["by_user"].items():
                if rev <= 0:
                    continue
                # Highest revenue wins; ties go to the user sorting first.
                if best is None or rev > best[1] or (rev == best[1] and user < best[0]):
                    best = (user, rev)
            top = best[0] if best else None
        day_entries.append({"date": day, "sessions": d["sessions"], "users": len(d["users"]),
                            "revenue": fmt_money(d["revenue"]), "top_user": top})

    summary = {"days": day_entries, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    with open(os.path.join(out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary from events.")
    parser.add_argument("--in", dest="inp", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)
    return run(args.inp, args.out)


if __name__ == "__main__":
    sys.exit(main())
