#!/usr/bin/env python3
"""sessionize: turn raw web events (events.jsonl) into sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DEFAULT_GAP_MINUTES = 30
ALLOWED_TYPES = ("view", "click", "purchase", "logout", "refund")
AMOUNT_TYPES = ("purchase", "refund")

# YYYY-MM-DDTHH:MM:SS[.frac](Z|+HH:MM|-HH:MM)
TS_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})$"
)
AMOUNT_RE = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")
GAP_RE = re.compile(r"^[0-9]+$")


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
    if off in ("Z", "z"):
        offset_s = 0
    else:
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            raise Reject("bad_ts")
        offset_s = sign * (oh * 3600 + om * 60)
    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        raise Reject("bad_ts")
    whole = Fraction((dt - EPOCH) // timedelta(seconds=1))
    frac = Fraction(int(frac_digits), 10 ** len(frac_digits)) if frac_digits else Fraction(0)
    return whole + frac - offset_s


def fmt_utc(instant):
    """Format an exact instant as YYYY-MM-DDTHH:MM:SSZ (fraction cut off)."""
    secs = int(instant // 1)  # instant is non-negative for realistic data; floor
    dt = EPOCH + timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(line_no, raw):
    """Validate one physical line. Returns event dict or raises Reject."""
    def bad_json(_):
        raise ValueError("non-finite constant")

    try:
        obj = json.loads(raw, parse_constant=bad_json)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    fields = {}
    for name in ("event_id", "user", "ts", "type"):
        v = obj.get(name)
        if not isinstance(v, str) or v.strip() == "":
            raise Reject("missing_field")
        fields[name] = v

    instant = parse_ts(fields["ts"])

    etype = fields["type"]
    if etype not in ALLOWED_TYPES:
        raise Reject("bad_type")

    amount = None
    if etype in AMOUNT_TYPES:
        a = obj.get("amount")
        if not isinstance(a, str) or not AMOUNT_RE.match(a):
            raise Reject("bad_amount")
        amount = Decimal(a)

    return {
        "line": line_no,
        "event_id": fields["event_id"],
        "user": fields["user"].strip().lower(),
        "instant": instant,
        "type": etype,
        "amount": amount,
    }


def money(d):
    if d == 0:
        d = Decimal("0")  # no negative zero
    return f"{d:.2f}"


def run(in_dir, out_dir, gap_minutes=DEFAULT_GAP_MINUTES):
    gap = Fraction(gap_minutes * 60)
    src = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(src):
        sys.stderr.write(f"error: input file not found: {src}\n")
        return 2

    rejects = []
    valid = []
    with open(src, "rb") as f:
        data = f.read()
    for idx, raw_bytes in enumerate(data.split(b"\n"), start=1):
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": idx, "reason": "bad_json"})
            continue
        if text.strip() == "":
            continue
        try:
            valid.append(validate(idx, text))
        except Reject as r:
            rejects.append({"line": idx, "reason": r.reason})

    # Deduplicate by exact event_id: keep earliest timestamp, then earliest line.
    best = {}
    for ev in valid:
        key = ev["event_id"]
        cur = best.get(key)
        if cur is None or (ev["instant"], ev["line"]) < (cur["instant"], cur["line"]):
            best[key] = ev
    kept = sorted(best.values(), key=lambda e: (e["instant"], e["line"]))
    duplicates_dropped = len(valid) - len(kept)

    # Sessionize per user.
    by_user = {}
    for ev in kept:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["instant"], e["line"]))
        current = None
        n = 0
        prev_instant = None
        closed = False
        for ev in evs:
            if current is None or closed or ev["instant"] - prev_instant > gap:
                if current is not None:
                    sessions.append(current)
                n += 1
                current = {
                    "user": user,
                    "session_id": f"{user}-{n}",
                    "first": ev["instant"],
                    "last": ev["instant"],
                    "events": 0,
                    "purchases": 0,
                    "refunds": 0,
                    "revenue": Decimal("0"),
                }
                closed = False
            current["last"] = ev["instant"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
            elif ev["type"] == "refund":
                current["refunds"] += 1
                current["revenue"] -= ev["amount"]
            elif ev["type"] == "logout":
                closed = True
            prev_instant = ev["instant"]
        if current is not None:
            sessions.append(current)

    sessions.sort(key=lambda s: (s["first"], s["user"]))

    os.makedirs(out_dir, exist_ok=True)

    # sessions.csv
    rows = []
    for s in sessions:
        duration = int((s["last"] - s["first"]) // 1)
        rows.append([
            s["session_id"], s["user"], fmt_utc(s["first"]), fmt_utc(s["last"]),
            str(duration), str(s["events"]), str(s["purchases"]), str(s["refunds"]), money(s["revenue"]),
        ])
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"])
        w.writerows(rows)

    # daily.json
    by_day = {}
    for s in sessions:
        date = fmt_utc(s["first"])[:10]
        d = by_day.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal("0")) + s["revenue"]

    days = []
    for date in sorted(by_day):
        d = by_day[date]
        top_user = None
        best_rev = max(d["per_user"].values())
        if best_rev > 0:
            top_user = min(u for u, r in d["per_user"].items() if r == best_rev)
        days.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": money(d["revenue"]),
            "top_user": top_user,
        })

    daily = {
        "days": days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": len(rejects),
        "gap_minutes": gap_minutes,
    }
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    # rejects.jsonl
    rejects.sort(key=lambda r: r["line"])
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="sessionize clickstream batch job")
    p.add_argument("--in", dest="in_dir", required=True)
    p.add_argument("--out", dest="out_dir", required=True)
    p.add_argument("--gap-minutes", dest="gap_minutes", default=str(DEFAULT_GAP_MINUTES))
    args = p.parse_args(argv)
    if not GAP_RE.match(args.gap_minutes) or int(args.gap_minutes) <= 0:
        sys.stderr.write(f"error: --gap-minutes must be a positive integer, got {args.gap_minutes!r}\n")
        return 64
    return run(args.in_dir, args.out_dir, int(args.gap_minutes))


if __name__ == "__main__":
    sys.exit(main())
