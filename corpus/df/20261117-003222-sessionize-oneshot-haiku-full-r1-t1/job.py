#!/usr/bin/env python3
"""sessionize: turn raw web events (events.jsonl) into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal

EPOCH = datetime(1970, 1, 1)
NS = 10 ** 9
GAP_NS = 30 * 60 * NS
ALLOWED_TYPES = {"view", "click", "purchase"}

# ISO 8601 date and time with seconds and a UTC offset (optional fraction).
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:?[0-9]{2})",
    re.ASCII,
)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?", re.ASCII)


def parse_ts(s):
    """Return the instant as integer nanoseconds since the Unix epoch (UTC), or None if invalid."""
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    try:
        base = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    frac = m.group(7) or ""
    ns = int((frac + "000000000")[:9])
    off = m.group(8)
    if off in ("Z", "z"):
        offset_s = 0
    else:
        digits = off[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:])
        if oh > 23 or om > 59:
            return None
        offset_s = (oh * 3600 + om * 60) * (1 if off[0] == "+" else -1)
    local_s = (base - EPOCH) // timedelta(seconds=1)
    return (local_s - offset_s) * NS + ns


def fmt_ts(ns):
    """Format integer nanoseconds as YYYY-MM-DDTHH:MM:SSZ (fraction cut off)."""
    sec = ns // NS
    return (EPOCH + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(line_no, raw_bytes, first_line):
    """Return (record, None) for a valid event, or (None, reason) for a rejected line."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, "bad_json"
    if first_line and text.startswith("\ufeff"):
        text = text[1:]
    if not text.strip():
        return "skip", None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"

    vals = {}
    for key in ("event_id", "user", "ts", "type"):
        v = obj.get(key)
        if not isinstance(v, str) or not v.strip():
            return None, "missing_field"
        vals[key] = v

    ts_ns = parse_ts(vals["ts"])
    if ts_ns is None:
        return None, "bad_ts"

    etype = vals["type"]
    if etype not in ALLOWED_TYPES:
        return None, "bad_type"

    amount = Decimal(0)
    if etype == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or not AMOUNT_RE.fullmatch(a):
            return None, "bad_amount"
        amount = Decimal(a)

    rec = {
        "event_id": vals["event_id"],
        "user": vals["user"].strip().lower(),
        "ts": ts_ns,
        "line": line_no,
        "type": etype,
        "amount": amount,
    }
    return rec, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="sessionize clickstream events")
    ap.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    ap.add_argument("--out", dest="out", required=True, help="output directory")
    args = ap.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    with open(in_path, "rb") as f:
        data = f.read()

    rejects = []
    valid = []
    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        rec, reason = validate(line_no, raw, line_no == 1)
        if reason is not None:
            rejects.append({"line": line_no, "reason": reason})
        elif rec == "skip":
            continue
        else:
            valid.append(rec)

    # Deduplicate by exact event_id: keep earliest timestamp, ties -> first in file.
    best = {}
    for rec in valid:
        cur = best.get(rec["event_id"])
        if cur is None or (rec["ts"], rec["line"]) < (cur["ts"], cur["line"]):
            best[rec["event_id"]] = rec
    duplicates_dropped = len(valid) - len(best)
    kept = sorted(best.values(), key=lambda r: (r["ts"], r["line"]))

    # Sessionize per user.
    per_user = {}
    for rec in kept:
        per_user.setdefault(rec["user"], []).append(rec)

    sessions = []
    for user, evs in per_user.items():
        n = 0
        cur = None
        for e in evs:
            if cur is None or e["ts"] - cur["end"] > GAP_NS:
                n += 1
                cur = {"user": user, "sid": f"{user}-{n}", "start": e["ts"], "end": e["ts"],
                       "events": 0, "purchases": 0, "revenue": Decimal(0)}
                sessions.append(cur)
            cur["end"] = e["ts"]
            cur["events"] += 1
            if e["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]
    sessions.sort(key=lambda s: (s["start"], s["user"]))

    # Write sessions.csv
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([
                s["sid"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                (s["end"] - s["start"]) // NS, s["events"], s["purchases"],
                f"{s['revenue']:.2f}",
            ])

    # Daily summary.
    days = {}
    for s in sessions:
        date = fmt_ts(s["start"])[:10]
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal(0)) + s["revenue"]

    day_list = []
    for date in sorted(days):
        d = days[date]
        if d["revenue"] == 0:
            top = None
        else:
            top = min(d["per_user"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        day_list.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": f"{d['revenue']:.2f}",
            "top_user": top,
        })

    daily = {"days": day_list, "duplicates_dropped": duplicates_dropped, "rejected": len(rejects)}
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Rejects.
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
