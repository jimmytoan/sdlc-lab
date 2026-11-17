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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

SESSION_GAP_S = 30 * 60
VALID_TYPES = {"view", "click", "purchase"}
AMOUNT_RE = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fmt_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    if d.as_tuple().exponent >= -2:
        d = d.quantize(Decimal("0.01"))
    return format(d, "f")


def parse_ts(s):
    if not isinstance(s, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(a):
    if isinstance(a, bool):
        raise Reject("bad_amount")
    if isinstance(a, (int, float)):
        a = str(a)
    if not isinstance(a, str):
        raise Reject("bad_amount")
    a = a.strip()
    if not AMOUNT_RE.match(a):
        raise Reject("bad_amount")
    try:
        d = Decimal(a)
    except InvalidOperation:
        raise Reject("bad_amount")
    if not d.is_finite() or d < 0:
        raise Reject("bad_amount")
    return d


def get_text(obj, key):
    v = obj.get(key)
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = str(v)
    if not isinstance(v, str) or not v.strip():
        return None
    return v.strip()


def validate(obj):
    """Return a normalised event dict or raise Reject."""
    event_id = get_text(obj, "event_id")
    user = get_text(obj, "user")
    raw_ts = obj.get("ts")
    raw_type = obj.get("type")
    if event_id is None or user is None or raw_ts is None or raw_type is None:
        raise Reject("missing_field")
    if not isinstance(raw_type, str):
        raise Reject("bad_type")
    etype = raw_type.strip().lower()
    if etype not in VALID_TYPES:
        raise Reject("bad_type")
    ts = parse_ts(raw_ts)
    amount = Decimal(0)
    if etype == "purchase":
        raw_amount = obj.get("amount")
        if raw_amount is None or (isinstance(raw_amount, str) and not raw_amount.strip()):
            raise Reject("missing_field")
        amount = parse_amount(raw_amount)
    return {"event_id": event_id, "user": user.lower(), "ts": ts, "type": etype, "amount": amount}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build clickstream sessions and daily summary.")
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
    seen = set()
    duplicates = 0
    events = []

    for lineno, raw in enumerate(data.split(b"\n"), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        if not isinstance(obj, dict):
            rejects.append({"line": lineno, "reason": "missing_field"})
            continue
        try:
            ev = validate(obj)
        except Reject as r:
            rejects.append({"line": lineno, "reason": r.reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        events.append(ev)

    # Group per user and split into sessions on >30 min inactivity.
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        cur = []
        for ev in evs:
            if cur and (ev["ts"] - cur[-1]["ts"]).total_seconds() > SESSION_GAP_S:
                groups.append(cur)
                cur = []
            cur.append(ev)
        if cur:
            groups.append(cur)
        for n, grp in enumerate(groups, start=1):
            revenue = sum((e["amount"] for e in grp), Decimal(0))
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": grp[0]["ts"],
                "end": grp[-1]["ts"],
                "events": len(grp),
                "purchases": sum(1 for e in grp if e["type"] == "purchase"),
                "revenue": revenue,
            })

    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))

    os.makedirs(args.out, exist_ok=True)

    # sessions.csv
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            dur = int((s["end"] - s["start"]).total_seconds())
            w.writerow([s["session_id"], s["user"], fmt_iso(s["start"]), fmt_iso(s["end"]), dur,
                        s["events"], s["purchases"], fmt_money(s["revenue"])])

    # daily.json
    days = {}
    for s in sessions:
        day = s["start"].strftime("%Y-%m-%d")
        d = days.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal(0)) + s["revenue"]

    day_entries = []
    for day in sorted(days):
        d = days[day]
        positive = [(u, r) for u, r in d["per_user"].items() if r > 0]
        top = min(positive, key=lambda ur: (-ur[1], ur[0]))[0] if positive else None
        day_entries.append({
            "date": day,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })

    daily = {"days": day_entries, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # rejects.jsonl
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
