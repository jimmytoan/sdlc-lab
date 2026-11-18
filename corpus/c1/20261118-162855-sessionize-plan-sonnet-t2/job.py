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


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(raw):
    """Return (event, None) or (None, reason). None event with None reason = skip."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "bad_json"
    if not text.strip():
        return None, None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        v = obj.get(k)
        if v is None or v == "" or (k != "ts" and not isinstance(v, str)):
            return None, "missing_field"
    user = obj["user"].strip().lower()
    if not user:
        return None, "missing_field"
    ts = obj["ts"]
    if not isinstance(ts, str):
        return None, "bad_ts"
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None, "bad_ts"
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None, "bad_ts"
    dt = dt.astimezone(timezone.utc)
    typ = obj["type"]
    if typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str):
            return None, "bad_amount"
        try:
            amount = Decimal(a.strip())
        except (InvalidOperation, ValueError):
            return None, "bad_amount"
        if not amount.is_finite() or amount < 0:
            return None, "bad_amount"
    return {"id": obj["event_id"], "user": user, "ts": dt, "type": typ, "amount": amount}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args()
    path = os.path.join(args.inp, "events.jsonl")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        print("error: cannot read %s: %s" % (path, e), file=sys.stderr)
        return 2
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    events, rejects, seen, dups = [], [], set(), 0
    for n, raw in enumerate(lines, 1):
        ev, reason = validate(raw.rstrip(b"\r"))
        if ev is None:
            if reason:
                rejects.append({"line": n, "reason": reason})
            continue
        if ev["id"] in seen:
            dups += 1
            continue
        seen.add(ev["id"])
        ev["line"] = n
        events.append(ev)

    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    gap = timedelta(minutes=30)
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        groups = []
        for e in evs:
            if groups and e["ts"] - groups[-1][-1]["ts"] <= gap:
                groups[-1].append(e)
            else:
                groups.append([e])
        for i, g in enumerate(groups, 1):
            pur = [e for e in g if e["type"] == "purchase"]
            sessions.append({
                "user": user, "n": i, "id": "%s-%d" % (user, i),
                "start": g[0]["ts"], "end": g[-1]["ts"],
                "duration": int((g[-1]["ts"] - g[0]["ts"]).total_seconds() // 1),
                "events": len(g), "purchases": len(pur),
                "revenue": sum((e["amount"] for e in pur), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].strftime("%Y-%m-%d"), [])
        d.append(s)
    day_out = []
    for date in sorted(days):
        ss = days[date]
        per = {}
        for s in ss:
            per[s["user"]] = per.get(s["user"], Decimal(0)) + s["revenue"]
        total = sum(per.values(), Decimal(0))
        top = None
        if total > 0:
            top = min(per, key=lambda u: (-per[u], u))
        day_out.append({"date": date, "sessions": len(ss), "users": len(per),
                        "revenue": money(total), "top_user": top})

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["id"], s["user"], fmt(s["start"]), fmt(s["end"]), s["duration"],
                        s["events"], s["purchases"], money(s["revenue"])])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_out, "duplicates_dropped": dups, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
