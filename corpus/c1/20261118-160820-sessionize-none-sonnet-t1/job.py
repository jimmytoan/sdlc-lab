import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

GAP = timedelta(minutes=30)
CENT = Decimal("0.01")


def money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def parse_event(raw):
    """Return (event, None) or (None, reason)."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        v = obj.get(k)
        if v is None or v == "" or isinstance(v, bool):
            return None, "missing_field"
    eid, user, ts, typ = obj["event_id"], obj["user"], obj["ts"], obj["type"]
    if not isinstance(user, str) or not user.strip() or not isinstance(eid, (str, int)):
        return None, "missing_field"
    if typ == "purchase" and obj.get("amount") in (None, ""):
        return None, "missing_field"
    if not isinstance(ts, str):
        return None, "bad_ts"
    try:
        t = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None, "bad_ts"
    if t.tzinfo is None or t.utcoffset() is None:
        return None, "bad_ts"
    t = t.astimezone(timezone.utc)
    if typ not in ("view", "click", "purchase"):
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
    return {"id": str(eid), "user": user.strip().lower(), "ts": t, "type": typ, "amount": amount}, None


def fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()
    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    with open(path, "rb") as f:
        data = f.read()

    events, rejects, seen, dups = [], [], set(), 0
    for n, bl in enumerate(data.split(b"\n"), 1):
        line = bl.decode("utf-8", errors="replace").strip("\r")
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
        evs.sort(key=lambda e: (e["ts"], e["id"]))
        groups = []
        for e in evs:
            if groups and e["ts"] - groups[-1][-1]["ts"] <= GAP:
                groups[-1].append(e)
            else:
                groups.append([e])
        for i, g in enumerate(groups, 1):
            p = [e for e in g if e["type"] == "purchase"]
            start, end = g[0]["ts"], g[-1]["ts"]
            sessions.append({
                "id": f"{user}-{i}", "user": user, "start": start, "end": end,
                "dur": int((end - start).total_seconds()), "events": len(g),
                "purchases": len(p), "revenue": sum((e["amount"] for e in p), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["id"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["id"], s["user"], fmt(s["start"]), fmt(s["end"]), s["dur"],
                        s["events"], s["purchases"], money(s["revenue"])])

    days = {}
    for s in sessions:
        days.setdefault(s["start"].strftime("%Y-%m-%d"), []).append(s)
    out_days = []
    for d in sorted(days):
        ss = days[d]
        rev = {}
        for s in ss:
            rev[s["user"]] = rev.get(s["user"], Decimal(0)) + s["revenue"]
        total = sum(rev.values(), Decimal(0))
        top = None
        if total > 0:
            top = sorted(rev.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out_days.append({"date": d, "sessions": len(ss), "users": len(rev),
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
