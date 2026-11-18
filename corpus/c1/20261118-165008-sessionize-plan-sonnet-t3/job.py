import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

AMOUNT_RE = re.compile(r"^[+]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
TYPES = ("view", "click", "purchase")
GAP = 1800
CENT = Decimal("0.01")


def money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_line(obj):
    """Return (event, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        v = obj.get(k)
        if not isinstance(v, str) or v == "":
            return None, "missing_field"
    user = obj["user"].strip().lower()
    if not user:
        return None, "missing_field"
    try:
        dt = datetime.fromisoformat(obj["ts"])
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None, "bad_ts"
        dt = dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None, "bad_ts"
    if obj["type"] not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if obj["type"] == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or not AMOUNT_RE.match(a):
            return None, "bad_amount"
        try:
            amount = Decimal(a)
        except Exception:
            return None, "bad_amount"
        if not amount.is_finite():
            return None, "bad_amount"
    return {"id": obj["event_id"], "user": user, "ts": dt,
            "purchase": obj["type"] == "purchase", "amount": amount}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args, _ = ap.parse_known_args()
    path = os.path.join(args.inp, "events.jsonl")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 2
    text = raw.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    rejects = []
    seen = set()
    dups = 0
    events = []
    for n, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            rejects.append({"line": n, "reason": "bad_json"})
            continue
        ev, reason = parse_line(obj)
        if reason:
            rejects.append({"line": n, "reason": reason})
            continue
        if ev["id"] in seen:
            dups += 1
            continue
        seen.add(ev["id"])
        ev["seq"] = len(events)
        events.append(ev)

    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["seq"]))
        groups = []
        for e in evs:
            if groups and (e["ts"] - groups[-1][-1]["ts"]).total_seconds() <= GAP:
                groups[-1].append(e)
            else:
                groups.append([e])
        for i, g in enumerate(groups, 1):
            sessions.append({
                "id": f"{user}-{i}", "user": user, "n": i,
                "start": g[0]["ts"], "end": g[-1]["ts"],
                "duration": int((g[-1]["ts"] - g[0]["ts"]).total_seconds()),
                "events": len(g),
                "purchases": sum(1 for e in g if e["purchase"]),
                "revenue": sum((e["amount"] for e in g), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].strftime("%Y-%m-%d"), {"sessions": 0, "rev": {}, "total": Decimal(0)})
        d["sessions"] += 1
        d["rev"][s["user"]] = d["rev"].get(s["user"], Decimal(0)) + s["revenue"]
        d["total"] += s["revenue"]
    day_list = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["total"] > 0:
            top = sorted(d["rev"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        day_list.append({"date": date, "sessions": d["sessions"], "users": len(d["rev"]),
                         "revenue": money(d["total"]), "top_user": top})

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["id"], s["user"], fmt(s["start"]), fmt(s["end"]), s["duration"],
                        s["events"], s["purchases"], money(s["revenue"])])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_list, "duplicates_dropped": dups, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
