import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

GAP = timedelta(minutes=30)
TYPES = ("view", "click", "purchase")


def fmt_money(d):
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_event(line):
    """Returns (event, None) or (None, reason)."""
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        v = obj.get(k)
        if v is None or v == "":
            return None, "missing_field"
    eid, user, ts, typ = obj["event_id"], obj["user"], obj["ts"], obj["type"]
    if not isinstance(user, str) or not user.strip() or isinstance(eid, (dict, list)):
        return None, "missing_field"
    if typ == "purchase" and obj.get("amount") in (None, ""):
        return None, "missing_field"
    dt = None
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.strip())
        except ValueError:
            dt = None
    if dt is None or dt.tzinfo is None or dt.utcoffset() is None:
        return None, "bad_ts"
    dt = dt.astimezone(timezone.utc)
    if typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        a = obj["amount"]
        if isinstance(a, bool) or not isinstance(a, (str, int, float)):
            return None, "bad_amount"
        try:
            amount = Decimal(str(a).strip())
        except InvalidOperation:
            return None, "bad_amount"
        if not amount.is_finite() or amount < 0:
            return None, "bad_amount"
    return {"id": str(eid) if not isinstance(eid, str) else eid, "user": user.strip().lower(),
            "ts": dt, "type": typ, "amount": amount}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2

    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        data = f.read()
    lines = data.replace("\r\n", "\n").split("\n")

    rejects = []
    events = []
    seen = set()
    dups = 0
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        ev, reason = parse_event(line)
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
        evs.sort(key=lambda e: e["ts"])
        groups = []
        for e in evs:
            if groups and e["ts"] - groups[-1][-1]["ts"] <= GAP:
                groups[-1].append(e)
            else:
                groups.append([e])
        for i, g in enumerate(groups, 1):
            purchases = [e for e in g if e["type"] == "purchase"]
            sessions.append({
                "id": f"{user}-{i}", "user": user, "start": g[0]["ts"], "end": g[-1]["ts"],
                "events": len(g), "purchases": len(purchases),
                "revenue": sum((e["amount"] for e in purchases), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["id"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["id"], s["user"], iso(s["start"]), iso(s["end"]),
                        int((s["end"] - s["start"]).total_seconds()), s["events"],
                        s["purchases"], fmt_money(s["revenue"])])

    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].strftime("%Y-%m-%d"), {"n": 0, "rev": {}})
        d["n"] += 1
        d["rev"][s["user"]] = d["rev"].get(s["user"], Decimal(0)) + s["revenue"]
    out_days = []
    for date in sorted(days):
        d = days[date]
        total = sum(d["rev"].values(), Decimal(0))
        top = None
        if total > 0:
            top = sorted(d["rev"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out_days.append({"date": date, "sessions": d["n"], "users": len(d["rev"]),
                         "revenue": fmt_money(total), "top_user": top})
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": out_days, "duplicates_dropped": dups, "rejected": len(rejects)}, f, indent=2)
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
