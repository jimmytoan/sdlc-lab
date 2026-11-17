#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = {"view", "click", "purchase", "logout", "refund"}
REQUIRED = ("event_id", "user", "ts", "type")
CENT = Decimal("0.01")


def fmt_money(d):
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return str(q)


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    if not isinstance(value, str) or "T" not in value.upper():
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = repr(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        d = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def is_missing(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def validate(obj):
    """Return (event, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "bad_json"
    for f in REQUIRED:
        if f not in obj or is_missing(obj[f]):
            return None, "missing_field"
    eid = obj["event_id"]
    if isinstance(eid, (dict, list)):
        return None, "missing_field"
    user = obj["user"]
    if not isinstance(user, str):
        return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    typ = obj["type"]
    if not isinstance(typ, str) or typ not in VALID_TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ in ("purchase", "refund"):
        if "amount" not in obj or is_missing(obj["amount"]):
            return None, "bad_amount"
        amount = parse_amount(obj["amount"])
        if amount is None:
            return None, "bad_amount"
    key = eid if isinstance(eid, str) else json.dumps(eid)
    return {"id": key, "user": user.strip().lower(), "ts": ts, "type": typ, "amount": amount}, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--gap-minutes", dest="gap", nargs="?", default=None, const="")
    args = ap.parse_args(argv)

    gap_minutes = 30
    if args.gap is not None:
        g = args.gap.strip()
        if not (g.isascii() and g.isdigit()) or int(g) <= 0:
            print(f"error: --gap-minutes must be a positive integer, got {args.gap!r}", file=sys.stderr)
            return 64
        gap_minutes = int(g)
    session_gap = timedelta(minutes=gap_minutes)

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

    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    rejects = []
    seen = set()
    duplicates = 0
    events = []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for lineno, bline in enumerate(lines, start=1):
        try:
            line = bline.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        ev, reason = validate(obj)
        if ev is None:
            rejects.append({"line": lineno, "reason": reason})
            continue
        if ev["id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["id"])
        events.append(ev)

    # Group by user, build sessions.
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["type"] == "logout"))
        cur = None
        n = 0
        for e in evs:
            if cur is None or cur["closed"] or e["ts"] - cur["end"] > session_gap:
                n += 1
                cur = {"user": user, "n": n, "start": e["ts"], "end": e["ts"],
                       "events": 0, "purchases": 0, "refunds": 0,
                       "revenue": Decimal(0), "closed": False}
                sessions.append(cur)
            cur["end"] = e["ts"]
            cur["events"] += 1
            if e["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]
            elif e["type"] == "refund":
                cur["refunds"] += 1
                cur["revenue"] -= e["amount"]
            elif e["type"] == "logout":
                cur["closed"] = True

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"])
        for s in sessions:
            dur = int((s["end"] - s["start"]).total_seconds())
            w.writerow([f"{s['user']}-{s['n']}", s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        dur, s["events"], s["purchases"], s["refunds"], fmt_money(s["revenue"])])

    days = {}
    for s in sessions:
        d = s["start"].date().isoformat()
        day = days.setdefault(d, {"sessions": 0, "users": set(), "revenue": Decimal(0), "by_user": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["by_user"][s["user"]] = day["by_user"].get(s["user"], Decimal(0)) + s["revenue"]

    out_days = []
    for d in sorted(days):
        day = days[d]
        top = None
        positive = [(u, r) for u, r in day["by_user"].items() if r > 0]
        if positive:
            top = min(positive, key=lambda ur: (-ur[1], ur[0]))[0]
        out_days.append({"date": d, "sessions": day["sessions"], "users": len(day["users"]),
                         "revenue": fmt_money(day["revenue"]), "top_user": top})

    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": out_days, "duplicates_dropped": duplicates, "rejected": len(rejects),
                   "gap_minutes": gap_minutes}, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
