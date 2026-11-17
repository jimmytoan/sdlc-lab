#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

DEFAULT_GAP_MINUTES = 30
TYPES = {"view", "click", "purchase", "logout", "refund"}
AMOUNT_TYPES = {"purchase", "refund"}
CENT = Decimal("0.01")
EXIT_USAGE = 64


def money(d):
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return str(q)


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _missing(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def validate(obj):
    """Return (event dict, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "bad_json"
    for f in ("event_id", "user", "ts", "type"):
        if f not in obj or _missing(obj[f]):
            return None, "missing_field"
    eid = obj["event_id"]
    if isinstance(eid, bool) or not isinstance(eid, (str, int)):
        return None, "missing_field"
    user = obj["user"]
    if not isinstance(user, str):
        return None, "missing_field"
    typ = obj["type"]
    if typ in AMOUNT_TYPES and ("amount" not in obj or _missing(obj["amount"])):
        return None, "missing_field"
    ts = obj["ts"]
    if not isinstance(ts, str):
        return None, "bad_ts"
    try:
        dt = datetime.fromisoformat(ts.strip())
    except (ValueError, TypeError):
        return None, "bad_ts"
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None, "bad_ts"
    try:
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None, "bad_ts"
    if not isinstance(typ, str) or typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ in AMOUNT_TYPES:
        a = obj["amount"]
        if not isinstance(a, str):
            return None, "bad_amount"
        try:
            amount = Decimal(a.strip())
        except (InvalidOperation, ValueError):
            return None, "bad_amount"
        if not amount.is_finite() or amount < 0:
            return None, "bad_amount"
    return {
        "event_id": str(eid),
        "user": user.strip().lower(),
        "ts": dt,
        "type": typ,
        "amount": amount,
    }, None


def parse_gap(value):
    if value is None:
        return DEFAULT_GAP_MINUTES
    s = value.strip()
    if not re.fullmatch(r"[0-9]+", s):
        return None
    n = int(s)
    if n <= 0:
        return None
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--gap-minutes", dest="gap", default=None)
    args = ap.parse_args(argv)

    gap_minutes = parse_gap(args.gap)
    if gap_minutes is None:
        print(f"error: --gap-minutes must be a positive integer, got {args.gap!r}", file=sys.stderr)
        return EXIT_USAGE
    gap = timedelta(minutes=gap_minutes)

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
    events = []
    seen = set()
    duplicates = 0
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for i, bline in enumerate(lines, start=1):
        if i == 1 and bline.startswith(b"\xef\xbb\xbf"):
            bline = bline[3:]
        if bline.endswith(b"\r"):
            bline = bline[:-1]
        if bline.strip() == b"":
            continue
        try:
            text = bline.decode("utf-8")
            obj = json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
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

    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        cur = None
        closed = False
        n = 0
        for ev in evs:
            if cur is None or closed or ev["ts"] - cur["end"] > gap:
                n += 1
                cur = {"session_id": f"{user}-{n}", "user": user, "start": ev["ts"], "end": ev["ts"],
                       "events": 0, "purchases": 0, "refunds": 0, "revenue": Decimal(0), "n": n}
                sessions.append(cur)
                closed = False
            cur["end"] = ev["ts"]
            cur["events"] += 1
            if ev["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += ev["amount"]
            elif ev["type"] == "refund":
                cur["refunds"] += 1
                cur["revenue"] -= ev["amount"]
            elif ev["type"] == "logout":
                closed = True

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds",
                    "revenue"])
        for s in sessions:
            dur = int((s["end"] - s["start"]).total_seconds())
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]), dur,
                        s["events"], s["purchases"], s["refunds"], money(s["revenue"])])

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
        best = Decimal(0)
        for u in sorted(day["by_user"]):
            r = day["by_user"][u]
            if r > best:
                best = r
                top = u
        out_days.append({"date": d, "sessions": day["sessions"], "users": len(day["users"]),
                         "revenue": money(day["revenue"]), "top_user": top})

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
