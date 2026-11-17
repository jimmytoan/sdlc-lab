#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

GAP = timedelta(minutes=30)
TYPES = {"view", "click", "purchase"}
CENT = Decimal("0.01")


def money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if len(s) < 11 or s[10] not in "Tt ":
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse_amount(value):
    if not isinstance(value, str):
        return None
    try:
        d = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def present(v):
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def validate(obj):
    """Return (event, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "bad_json"
    for f in ("event_id", "user", "ts", "type"):
        if f not in obj or not present(obj[f]):
            return None, "missing_field"
    eid = obj["event_id"]
    if isinstance(eid, bool) or not isinstance(eid, (str, int)):
        return None, "missing_field"
    user = obj["user"]
    if not isinstance(user, str):
        return None, "missing_field"
    if obj["type"] == "purchase" and ("amount" not in obj or not present(obj["amount"])):
        return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    typ = obj["type"]
    if not isinstance(typ, str) or typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        amount = parse_amount(obj["amount"])
        if amount is None:
            return None, "bad_amount"
    return {
        "event_id": str(eid),
        "user": user.strip().lower(),
        "ts": ts,
        "type": typ,
        "amount": amount,
    }, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args(argv)

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        print(f"error: cannot read input file {path}: {e}", file=sys.stderr)
        return 2

    rejects = []
    seen = set()
    duplicates = 0
    events = []

    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for i, bline in enumerate(lines, start=1):
        if i == 1 and bline.startswith(b"\xef\xbb\xbf"):
            bline = bline[3:]
        if bline.endswith(b"\r"):
            bline = bline[:-1]
        if not bline.strip():
            continue
        try:
            text = bline.decode("utf-8")
            obj = json.loads(text)
        except (UnicodeDecodeError, ValueError, RecursionError):
            rejects.append({"line": i, "reason": "bad_json"})
            continue
        ev, reason = validate(obj)
        if reason:
            rejects.append({"line": i, "reason": reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        events.append(ev)

    # group per user
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        cur = None
        n = 0
        for ev in evs:
            if cur is None or ev["ts"] - cur["end"] > GAP:
                n += 1
                cur = {"user": user, "n": n, "start": ev["ts"], "end": ev["ts"],
                       "events": 0, "purchases": 0, "revenue": Decimal(0)}
                sessions.append(cur)
            cur["end"] = ev["ts"]
            cur["events"] += 1
            if ev["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += ev["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            dur = int((s["end"] - s["start"]).total_seconds())
            w.writerow([f"{s['user']}-{s['n']}", s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        dur, s["events"], s["purchases"], money(s["revenue"])])

    days = {}
    for s in sessions:
        d = s["start"].date().isoformat()
        day = days.setdefault(d, {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["per_user"][s["user"]] = day["per_user"].get(s["user"], Decimal(0)) + s["revenue"]

    day_list = []
    for d in sorted(days):
        day = days[d]
        top = None
        best = Decimal(0)
        for u in sorted(day["per_user"]):
            r = day["per_user"][u]
            if r > best:
                best, top = r, u
        day_list.append({"date": d, "sessions": day["sessions"], "users": len(day["users"]),
                         "revenue": money(day["revenue"]), "top_user": top})

    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects)},
                  f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
