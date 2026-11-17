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
ONE_S = timedelta(seconds=1)
NS = 10 ** 9
GAP_NS = 30 * 60 * NS
ALLOWED_TYPES = {"view", "click", "purchase"}

TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def parse_ts(text):
    """Return the instant as integer nanoseconds since the Unix epoch, or None if invalid."""
    m = TS_RE.fullmatch(text)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(x) for x in m.group(1, 2, 3, 4, 5, 6))
    frac = m.group(7) or ""
    off = m.group(8)
    if off in ("Z", "z"):
        off_s = 0
    else:
        sign = 1 if off[0] == "+" else -1
        oh = int(off[1:3])
        om = int(off[4:6])
        if oh > 23 or om > 59:
            return None
        off_s = sign * (oh * 3600 + om * 60)
    try:
        naive = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    secs = (naive - EPOCH) // ONE_S - off_s
    ns_frac = int(frac[:9].ljust(9, "0")) if frac else 0
    return secs * NS + ns_frac


def format_utc(ns):
    dt = EPOCH + timedelta(seconds=ns // NS)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def money(d):
    return f"{d:.2f}"


def check_line(text):
    """Validate one non-empty line. Returns (reason, event_dict) where exactly one is None."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return "bad_json", None
    if not isinstance(obj, dict):
        return "bad_json", None

    for key in ("event_id", "user", "ts", "type"):
        val = obj.get(key)
        if not isinstance(val, str) or val.strip() == "":
            return "missing_field", None

    ts_ns = parse_ts(obj["ts"])
    if ts_ns is None:
        return "bad_ts", None

    if obj["type"] not in ALLOWED_TYPES:
        return "bad_type", None

    amount = None
    if obj["type"] == "purchase":
        raw = obj.get("amount")
        if not isinstance(raw, str) or not AMOUNT_RE.fullmatch(raw):
            return "bad_amount", None
        amount = Decimal(raw)

    return None, {
        "event_id": obj["event_id"],
        "user": obj["user"].strip().lower(),
        "ts": ts_ns,
        "type": obj["type"],
        "amount": amount,
    }


def run(inp, out):
    path = os.path.join(inp, "events.jsonl")
    if not os.path.isfile(path):
        sys.stderr.write(f"error: input file not found: {path}\n")
        return 2

    with open(path, "rb") as f:
        data = f.read()

    rejects = []  # (line_no, reason)
    valid = []    # (line_no, event)
    for line_no, raw in enumerate(data.split(b"\n"), 1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append((line_no, "bad_json"))
            continue
        if line_no == 1 and text.startswith("\ufeff"):
            text = text[1:]
        if text.strip() == "":
            continue
        reason, event = check_line(text)
        if reason is not None:
            rejects.append((line_no, reason))
        else:
            event["line"] = line_no
            valid.append(event)

    # Deduplicate by event_id: keep earliest timestamp; ties keep first in file.
    best = {}
    for ev in valid:
        cur = best.get(ev["event_id"])
        if cur is None or ev["ts"] < cur["ts"]:
            best[ev["event_id"]] = ev
    duplicates_dropped = len(valid) - len(best)
    kept = sorted(best.values(), key=lambda e: (e["ts"], e["line"]))

    # Group per user and split into sessions on gaps > 30 minutes.
    per_user = {}
    for ev in kept:
        per_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in per_user.items():
        n = 0
        cur = None
        prev_ts = None
        for ev in evs:  # already in (ts, line) order
            if cur is None or ev["ts"] - prev_ts > GAP_NS:
                n += 1
                cur = {
                    "session_id": f"{user}-{n}",
                    "user": user,
                    "start": ev["ts"],
                    "end": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0"),
                }
                sessions.append(cur)
            cur["end"] = ev["ts"]
            cur["events"] += 1
            if ev["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += ev["amount"]
            prev_ts = ev["ts"]

    sessions.sort(key=lambda s: (s["start"], s["user"]))

    os.makedirs(out, exist_ok=True)

    # sessions.csv
    with open(os.path.join(out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([
                s["session_id"],
                s["user"],
                format_utc(s["start"]),
                format_utc(s["end"]),
                (s["end"] - s["start"]) // NS,
                s["events"],
                s["purchases"],
                money(s["revenue"]),
            ])

    # daily.json
    days = {}
    for s in sessions:
        date = format_utc(s["start"])[:10]
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal("0")) + s["revenue"]

    day_list = []
    for date in sorted(days):
        d = days[date]
        top_user = None
        if d["revenue"] > 0:
            best_u = None
            best_r = None
            for u, r in d["per_user"].items():
                if best_u is None or r > best_r or (r == best_r and u < best_u):
                    best_u, best_r = u, r
            top_user = best_u
        day_list.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": money(d["revenue"]),
            "top_user": top_user,
        })

    summary = {
        "days": day_list,
        "duplicates_dropped": duplicates_dropped,
        "rejected": len(rejects),
    }
    with open(os.path.join(out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # rejects.jsonl
    with open(os.path.join(out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for line_no, reason in rejects:
            f.write(json.dumps({"line": line_no, "reason": reason}) + "\n")

    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    args = parser.parse_args(argv)
    return run(args.inp, args.out)


if __name__ == "__main__":
    sys.exit(main())
