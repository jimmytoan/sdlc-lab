#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl, writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SESSION_GAP = timedelta(minutes=30)
CENT = Decimal("0.01")
VALID_TYPES = {"view", "click", "purchase"}
AMOUNT_RE = re.compile(r"^\d+(\.\d+)?$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, OverflowError, TypeError):
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if isinstance(value, bool):
        raise Reject("bad_amount")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise Reject("bad_amount")
    if not AMOUNT_RE.match(text):
        raise Reject("bad_amount")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def validate(obj):
    """Return a normalized event dict or raise Reject."""
    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    typ = obj.get("type")
    amount_raw = obj.get("amount")

    if is_blank(event_id) or isinstance(event_id, (dict, list, bool)):
        raise Reject("missing_field")
    if is_blank(user) or not isinstance(user, str):
        raise Reject("missing_field")
    if is_blank(ts):
        raise Reject("missing_field")
    if is_blank(typ) or not isinstance(typ, str):
        raise Reject("missing_field")

    typ_norm = typ.strip().lower()
    if typ_norm not in VALID_TYPES:
        raise Reject("bad_type")

    if typ_norm == "purchase" and is_blank(amount_raw):
        raise Reject("missing_field")

    dt = parse_ts(ts)

    amount = Decimal("0.00")
    if typ_norm == "purchase":
        amount = parse_amount(amount_raw)

    return {
        "event_id_key": json.dumps(event_id, sort_keys=True),
        "user": user.strip().lower(),
        "dt": dt,
        "is_purchase": typ_norm == "purchase",
        "amount": amount,
    }


def fmt_money(value):
    return str(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    ap = argparse.ArgumentParser(description="sessionize clickstream batch job")
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
    duplicates = 0
    seen_ids = set()
    events = []

    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        text = text.replace("\ufeff", "").strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        if not isinstance(obj, dict):
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        try:
            ev = validate(obj)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        if ev["event_id_key"] in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(ev["event_id_key"])
        events.append(ev)

    # Build sessions per user.
    by_user = defaultdict(list)
    for ev in events:
        by_user[ev["user"]].append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["dt"], e["event_id_key"]))
        cur = None
        n = 0
        for e in evs:
            if cur is None or e["dt"] - cur["end"] > SESSION_GAP:
                n += 1
                cur = {
                    "user": user,
                    "n": n,
                    "start": e["dt"],
                    "end": e["dt"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0.00"),
                }
                sessions.append(cur)
            cur["end"] = e["dt"]
            cur["events"] += 1
            if e["is_purchase"]:
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    os.makedirs(args.out, exist_ok=True)

    # sessions.csv
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            duration = int((s["end"] - s["start"]).total_seconds())
            w.writerow([
                f"{s['user']}-{s['n']}",
                s["user"],
                fmt_ts(s["start"]),
                fmt_ts(s["end"]),
                duration,
                s["events"],
                s["purchases"],
                fmt_money(s["revenue"]),
            ])

    # daily summary, grouped by UTC date of session start
    days = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0.00"), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        if s["revenue"] > 0:
            d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal("0.00")) + s["revenue"]

    day_list = []
    for date in sorted(days):
        d = days[date]
        top_user = None
        if d["revenue"] > 0 and d["per_user"]:
            top_user = max(sorted(d["per_user"].items()), key=lambda kv: kv[1])[0]
        day_list.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })

    daily = {
        "days": day_list,
        "duplicates_dropped": duplicates,
        "rejected": len(rejects),
    }
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    # rejects.jsonl
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
