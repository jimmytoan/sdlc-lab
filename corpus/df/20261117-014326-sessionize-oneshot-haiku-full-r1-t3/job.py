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
from datetime import datetime, timedelta
from fractions import Fraction

EPOCH = datetime(1970, 1, 1)
ALLOWED_TYPES = {"view", "click", "purchase"}
SESSION_GAP = 1800  # seconds; a gap of exactly 30 minutes stays in the same session

TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def parse_ts(s):
    """Return the instant as an exact Fraction of UTC seconds since epoch, or None if invalid."""
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    y, mo, d, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    tz = m.group(8)
    try:
        local = datetime(y, mo, d, hh, mi, ss)
    except ValueError:
        return None
    if tz in ("Z", "z"):
        offset = 0
    else:
        sign = 1 if tz[0] == "+" else -1
        oh = int(tz[1:3])
        om = int(tz[4:6])
        if oh > 23 or om > 59:
            return None
        offset = sign * (oh * 3600 + om * 60)
    try:
        utc = local - timedelta(seconds=offset)
    except OverflowError:
        return None
    whole = (utc - EPOCH) // timedelta(seconds=1)
    fraction = Fraction(int(frac), 10 ** len(frac)) if frac else Fraction(0)
    return whole + fraction


def utc_datetime(key):
    """Datetime (UTC, naive) of the whole-second part of an instant key."""
    return EPOCH + timedelta(seconds=math.floor(key))


def fmt_ts(key):
    dt = utc_datetime(key)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def amount_to_cents(a):
    intpart, _, frac = a.partition(".")
    return int(intpart) * 100 + int((frac + "00")[:2])


def cents_to_str(c):
    return f"{c // 100}.{c % 100:02d}"


def process_lines(data):
    """Validate every line. Returns (valid_events, rejects)."""
    valid = []
    rejects = []

    def reject(line_no, reason):
        rejects.append({"line": line_no, "reason": reason})

    for line_no, raw in enumerate(data.split(b"\n"), 1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            reject(line_no, "bad_json")
            continue
        if line_no == 1 and text.startswith("\ufeff"):
            text = text[1:]
        if text.strip() == "":
            continue
        try:
            obj = json.loads(text)
        except Exception:
            reject(line_no, "bad_json")
            continue
        if not isinstance(obj, dict):
            reject(line_no, "bad_json")
            continue

        eid = obj.get("event_id")
        user = obj.get("user")
        ts = obj.get("ts")
        typ = obj.get("type")
        if any((not isinstance(v, str)) or v.strip() == "" for v in (eid, user, ts, typ)):
            reject(line_no, "missing_field")
            continue

        key = parse_ts(ts)
        if key is None:
            reject(line_no, "bad_ts")
            continue

        if typ not in ALLOWED_TYPES:
            reject(line_no, "bad_type")
            continue

        cents = 0
        if typ == "purchase":
            amount = obj.get("amount")
            if not isinstance(amount, str) or not AMOUNT_RE.fullmatch(amount):
                reject(line_no, "bad_amount")
                continue
            cents = amount_to_cents(amount)

        valid.append({
            "event_id": eid,
            "user": user.strip().lower(),
            "key": key,
            "type": typ,
            "cents": cents,
            "line": line_no,
        })
    return valid, rejects


def dedupe(valid):
    """Keep the earliest event per event_id (ties: first in file). Returns (kept, dropped_count)."""
    best = {}
    for ev in valid:  # file order
        cur = best.get(ev["event_id"])
        if cur is None or ev["key"] < cur["key"]:
            best[ev["event_id"]] = ev
    kept = sorted(best.values(), key=lambda e: (e["key"], e["line"]))
    return kept, len(valid) - len(kept)


def build_sessions(kept):
    per_user = {}
    for ev in kept:  # already sorted by (key, line)
        per_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in per_user.items():
        n = 0
        cur = None
        for ev in evs:
            if cur is None or ev["key"] - cur["end"] > SESSION_GAP:
                n += 1
                cur = {
                    "session_id": f"{user}-{n}",
                    "user": user,
                    "start": ev["key"],
                    "end": ev["key"],
                    "events": 0,
                    "purchases": 0,
                    "cents": 0,
                }
                sessions.append(cur)
            cur["end"] = ev["key"]
            cur["events"] += 1
            if ev["type"] == "purchase":
                cur["purchases"] += 1
                cur["cents"] += ev["cents"]
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    return sessions


def build_daily(sessions, duplicates_dropped, rejected):
    days = {}
    for s in sessions:
        day = utc_datetime(s["start"]).date().isoformat()
        d = days.setdefault(day, {"sessions": 0, "users": set(), "cents": 0, "user_cents": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["cents"] += s["cents"]
        d["user_cents"][s["user"]] = d["user_cents"].get(s["user"], 0) + s["cents"]

    out_days = []
    for day in sorted(days):
        d = days[day]
        top = None
        if d["cents"] != 0:
            for u in sorted(d["user_cents"]):
                if top is None or d["user_cents"][u] > d["user_cents"][top]:
                    top = u
        out_days.append({
            "date": day,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": cents_to_str(d["cents"]),
            "top_user": top,
        })
    return {
        "days": out_days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="sessionize clickstream batch job")
    parser.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        sys.stderr.write(f"error: input file not found: {in_path}\n")
        return 2

    with open(in_path, "rb") as f:
        data = f.read()

    valid, rejects = process_lines(data)
    kept, dropped = dedupe(valid)
    sessions = build_sessions(kept)
    daily = build_daily(sessions, dropped, len(rejects))

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([
                s["session_id"],
                s["user"],
                fmt_ts(s["start"]),
                fmt_ts(s["end"]),
                math.floor(s["end"] - s["start"]),
                s["events"],
                s["purchases"],
                cents_to_str(s["cents"]),
            ])

    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
