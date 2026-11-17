#!/usr/bin/env python3
"""sessionize: turn raw web events (events.jsonl) into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SESSION_GAP = 30 * 60  # seconds; a gap of exactly 30 minutes stays in the session
ALLOWED_TYPES = ("view", "click", "purchase")

TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?"
    r"(?:([Zz])|([+-])([0-9]{2}):([0-9]{2}))"
)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(text):
    """Return exact UTC time as a Fraction of seconds since the epoch. Raises Reject('bad_ts')."""
    m = TS_RE.fullmatch(text)
    if not m:
        raise Reject("bad_ts")
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    frac_digits = m.group(7) or ""
    if m.group(8):  # Z
        offset = timedelta(0)
    else:
        oh, om = int(m.group(10)), int(m.group(11))
        if oh > 23 or om > 59:
            raise Reject("bad_ts")
        offset = timedelta(hours=oh, minutes=om)
        if m.group(9) == "-":
            offset = -offset
    try:
        dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone(offset))
    except ValueError:
        raise Reject("bad_ts")
    delta = dt - EPOCH
    whole = delta.days * 86400 + delta.seconds
    frac = Fraction(int(frac_digits), 10 ** len(frac_digits)) if frac_digits else Fraction(0)
    return Fraction(whole) + frac


def fmt_utc(total):
    """Format a Fraction-of-seconds epoch time as YYYY-MM-DDTHH:MM:SSZ (fraction cut off)."""
    secs = math.floor(total)
    return (EPOCH + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate(obj):
    """Validate a parsed JSON object. Returns a normalized event dict or raises Reject."""
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for key in ("event_id", "user", "ts", "type"):
        if not is_nonempty_str(obj.get(key)):
            raise Reject("missing_field")
    ts = parse_ts(obj["ts"])
    etype = obj["type"]
    if etype not in ALLOWED_TYPES:
        raise Reject("bad_type")
    amount = None
    if etype == "purchase":
        raw = obj.get("amount")
        if not isinstance(raw, str) or not AMOUNT_RE.fullmatch(raw):
            raise Reject("bad_amount")
        amount = Decimal(raw)
    return {
        "event_id": obj["event_id"],
        "user": obj["user"].strip().lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,
    }


def read_events(path):
    """Yield (line_no, raw_bytes_decoded_or_None) for each non-blank line."""
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    for idx, raw in enumerate(lines, start=1):
        if idx == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if raw.strip() == b"":
            continue
        yield idx, raw


def process(in_dir, out_dir):
    path = os.path.join(in_dir, "events.jsonl")
    rejects = []
    valid = []  # (line_no, event)
    for line_no, raw in read_events(path):
        try:
            text = raw.decode("utf-8")
            try:
                obj = json.loads(text)
            except ValueError:
                raise Reject("bad_json")
            ev = validate(obj)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        valid.append((line_no, ev))

    # Deduplicate by exact event_id: keep earliest ts, ties -> first in file.
    best = {}
    for line_no, ev in valid:
        key = ev["event_id"]
        cur = best.get(key)
        if cur is None or (ev["ts"], line_no) < (cur[1]["ts"], cur[0]):
            best[key] = (line_no, ev)
    duplicates_dropped = len(valid) - len(best)
    kept = sorted(best.values(), key=lambda p: (p[1]["ts"], p[0]))

    # Group per user (already time-ordered).
    per_user = {}
    for line_no, ev in kept:
        per_user.setdefault(ev["user"], []).append(ev)

    sessions = []  # dicts
    for user, evs in per_user.items():
        current = None
        n = 0
        prev_ts = None
        for ev in evs:
            if current is None or ev["ts"] - prev_ts > SESSION_GAP:
                n += 1
                current = {
                    "session_id": f"{user}-{n}",
                    "user": user,
                    "first": ev["ts"],
                    "last": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0"),
                }
                sessions.append(current)
            current["last"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
            prev_ts = ev["ts"]

    sessions.sort(key=lambda s: (s["first"], s["user"]))

    os.makedirs(out_dir, exist_ok=True)

    # sessions.csv
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = math.floor(s["last"] - s["first"])
            w.writerow([
                s["session_id"], s["user"], fmt_utc(s["first"]), fmt_utc(s["last"]), duration,
                s["events"], s["purchases"], f"{s['revenue'].quantize(Decimal('0.01'))}",
            ])

    # daily.json
    by_day = {}
    for s in sessions:
        date = fmt_utc(s["first"])[:10]
        by_day.setdefault(date, []).append(s)
    days = []
    for date in sorted(by_day):
        ss = by_day[date]
        total = Decimal("0")
        per_user_rev = {}
        for s in ss:
            total += s["revenue"]
            per_user_rev[s["user"]] = per_user_rev.get(s["user"], Decimal("0")) + s["revenue"]
        top_user = None
        if total != 0:
            top_user = min(per_user_rev, key=lambda u: (-per_user_rev[u], u))
        days.append({
            "date": date,
            "sessions": len(ss),
            "users": len({s["user"] for s in ss}),
            "revenue": f"{total.quantize(Decimal('0.01'))}",
            "top_user": top_user,
        })
    summary = {
        "days": days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": len(rejects),
    }
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # rejects.jsonl
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="sessionize: clickstream sessions batch job")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)
    path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    process(args.in_dir, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
