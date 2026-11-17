#!/usr/bin/env python3
"""sessionize: turn raw web events (events.jsonl) into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from decimal import Decimal, InvalidOperation

GAP_SECONDS = 30 * 60
ALLOWED_TYPES = ("view", "click", "purchase")

TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$"
)
AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    """Return the instant as an exact Fraction of seconds since the Unix epoch (UTC)."""
    m = TS_RE.match(value)
    if not m:
        raise Reject("bad_ts")
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac_digits = m.group(7) or ""
    off = m.group(8)
    try:
        base = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        raise Reject("bad_ts")
    if off == "Z":
        offset_s = 0
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            raise Reject("bad_ts")
        offset_s = (oh * 3600 + om * 60) * (1 if off[0] == "+" else -1)
    epoch = int((base - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds())
    frac = Fraction(int(frac_digits), 10 ** len(frac_digits)) if frac_digits else Fraction(0)
    return epoch - offset_s + frac


def fmt_utc(instant):
    secs = int(instant // 1)  # floor; instants are non-negative in practice
    dt = datetime(1970, 1, 1) + timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date(instant):
    secs = int(instant // 1)
    return (datetime(1970, 1, 1) + timedelta(seconds=secs)).strftime("%Y-%m-%d")


def cents_str(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def validate(obj):
    """Validate one parsed JSON object. Returns a normalised event dict or raises Reject."""
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for key in ("event_id", "user", "ts", "type"):
        v = obj.get(key)
        if not isinstance(v, str) or v.strip() == "":
            raise Reject("missing_field")
    event_id = obj["event_id"]
    user = obj["user"].strip().lower()
    instant = parse_ts(obj["ts"])
    etype = obj["type"]
    if etype not in ALLOWED_TYPES:
        raise Reject("bad_type")
    cents = 0
    if etype == "purchase":
        amount = obj.get("amount")
        if not isinstance(amount, str) or not AMOUNT_RE.match(amount):
            raise Reject("bad_amount")
        try:
            d = Decimal(amount)
        except InvalidOperation:
            raise Reject("bad_amount")
        cents = int(d * 100)
    return {"event_id": event_id, "user": user, "instant": instant, "type": etype, "cents": cents}


def read_events(path):
    """Yield (line_no, raw_bytes) for non-blank lines, numbering every physical line."""
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for idx, raw in enumerate(lines, start=1):
        if raw.strip() == b"":
            continue
        yield idx, raw


def run(in_dir, out_dir):
    src = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(src):
        sys.stderr.write(f"error: input file not found: {src}\n")
        return 2

    rejects = []
    valid = []  # (line_no, event)
    for line_no, raw in read_events(src):
        try:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise Reject("bad_json")
            try:
                obj = json.loads(text)
            except (ValueError, RecursionError):
                raise Reject("bad_json")
            ev = validate(obj)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        valid.append((line_no, ev))

    # Deduplicate by event_id: keep earliest timestamp, ties -> first in file.
    best = {}
    for line_no, ev in valid:
        key = ev["event_id"]
        cur = best.get(key)
        if cur is None or (ev["instant"], line_no) < (cur[1]["instant"], cur[0]):
            best[key] = (line_no, ev)
    duplicates_dropped = len(valid) - len(best)
    kept = sorted(best.values(), key=lambda p: (p[1]["instant"], p[0]))

    # Group per user and split into sessions.
    per_user = {}
    for line_no, ev in kept:
        per_user.setdefault(ev["user"], []).append(ev)

    sessions = []  # dicts
    for user, evs in per_user.items():
        evs.sort(key=lambda e: e["instant"])  # stable: file order preserved on ties
        current = None
        n = 0
        prev = None
        for ev in evs:
            if prev is None or ev["instant"] - prev["instant"] > GAP_SECONDS:
                n += 1
                current = {
                    "user": user,
                    "n": n,
                    "first": ev["instant"],
                    "last": ev["instant"],
                    "events": 0,
                    "purchases": 0,
                    "cents": 0,
                }
                sessions.append(current)
            current["last"] = ev["instant"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["cents"] += ev["cents"]
            prev = ev

    sessions.sort(key=lambda s: (s["first"], s["user"]))
    for s in sessions:
        s["session_id"] = f"{s['user']}-{s['n']}"
        s["start"] = fmt_utc(s["first"])
        s["end"] = fmt_utc(s["last"])
        s["duration_s"] = int((s["last"] - s["first"]) // 1)
        s["date"] = utc_date(s["first"])

    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], s["start"], s["end"], s["duration_s"],
                        s["events"], s["purchases"], cents_str(s["cents"])])

    # Daily summary.
    days = {}
    for s in sessions:
        d = days.setdefault(s["date"], {"sessions": 0, "users": set(), "cents": 0, "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["cents"] += s["cents"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], 0) + s["cents"]

    day_list = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["cents"] > 0:
            best_user = None
            for u in sorted(d["per_user"]):
                if best_user is None or d["per_user"][u] > d["per_user"][best_user]:
                    best_user = u
            top = best_user
        day_list.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": cents_str(d["cents"]),
            "top_user": top,
        })

    daily = {
        "days": day_list,
        "duplicates_dropped": duplicates_dropped,
        "rejected": len(rejects),
    }
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="sessionize clickstream batch job")
    p.add_argument("--in", dest="in_dir", required=True)
    p.add_argument("--out", dest="out_dir", required=True)
    a = p.parse_args(argv)
    return run(a.in_dir, a.out_dir)


if __name__ == "__main__":
    sys.exit(main())
