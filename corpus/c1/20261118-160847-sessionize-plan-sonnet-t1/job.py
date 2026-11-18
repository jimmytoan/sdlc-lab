import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")


def money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def nonempty(v):
    return isinstance(v, str) and v.strip() != ""


def parse_ts(s):
    try:
        dt = datetime.fromisoformat(s.strip())
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    return dt.replace(microsecond=0)


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(line):
    """Return (event, None) or (None, reason)."""
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        if not nonempty(obj.get(k)):
            return None, "missing_field"
    if obj["type"] == "purchase" and obj.get("amount") is None:
        return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    if obj["type"] not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if obj["type"] == "purchase":
        a = obj["amount"]
        if not isinstance(a, str):
            return None, "bad_amount"
        try:
            amount = Decimal(a.strip())
        except InvalidOperation:
            return None, "bad_amount"
        if not amount.is_finite() or amount < 0:
            return None, "bad_amount"
    return {"id": obj["event_id"], "user": obj["user"].strip().lower(),
            "ts": ts, "type": obj["type"], "amount": amount}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2

    events, rejects, seen, dups = [], [], set(), 0
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        data = f.read()
    lines = data.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for n, line in enumerate(lines, 1):
        if line.strip() == "":
            continue
        ev, reason = validate(line)
        if reason:
            rejects.append({"line": n, "reason": reason})
            continue
        if ev["id"] in seen:
            dups += 1
            continue
        seen.add(ev["id"])
        events.append(ev)

    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: e["ts"])  # stable
        groups = []
        for e in evs:
            if groups and (e["ts"] - groups[-1][-1]["ts"]).total_seconds() <= 1800:
                groups[-1].append(e)
            else:
                groups.append([e])
        for i, g in enumerate(groups, 1):
            pur = [e for e in g if e["type"] == "purchase"]
            sessions.append({
                "num": i, "user": user, "start": g[0]["ts"], "end": g[-1]["ts"],
                "events": len(g), "purchases": len(pur),
                "revenue": sum((e["amount"] for e in pur), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["num"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([f"{s['user']}-{s['num']}", s["user"], fmt(s["start"]), fmt(s["end"]),
                        int((s["end"] - s["start"]).total_seconds()), s["events"],
                        s["purchases"], money(s["revenue"])])

    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].strftime("%Y-%m-%d"), {"n": 0, "users": {}})
        d["n"] += 1
        d["users"][s["user"]] = d["users"].get(s["user"], Decimal(0)) + s["revenue"]
    out_days = []
    for date in sorted(days):
        d = days[date]
        total = sum(d["users"].values(), Decimal(0))
        top = None
        if total > 0:
            top = sorted(d["users"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out_days.append({"date": date, "sessions": d["n"], "users": len(d["users"]),
                         "revenue": money(total), "top_user": top})
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": out_days, "duplicates_dropped": dups, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
