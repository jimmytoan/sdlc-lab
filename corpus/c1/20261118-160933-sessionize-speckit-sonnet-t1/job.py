"""sessionize: turn raw web events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

TYPES = ("view", "click", "purchase")
GAP = 1800


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def parse_line(text, lineno):
    """Return (event, None) or (None, reason). event = (ts, line, id, user, type, amount)."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    eid, user, ts, typ = (obj.get(k) for k in ("event_id", "user", "ts", "type"))
    if (not isinstance(eid, str) or not eid or not isinstance(user, str) or not user
            or ts is None or typ is None):
        return None, "missing_field"
    if not isinstance(ts, str):
        return None, "bad_ts"
    try:
        dt = datetime.fromisoformat(ts.strip())
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None, "bad_ts"
        dt = dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None, "bad_ts"
    if not isinstance(typ, str) or typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str):
            return None, "bad_amount"
        try:
            amount = Decimal(a.strip())
        except InvalidOperation:
            return None, "bad_amount"
        if not amount.is_finite() or amount < 0:
            return None, "bad_amount"
    return (dt, lineno, eid, user.lower(), typ, amount), None


def sessionize(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e[3], []).append(e)
    rows = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e[0], e[1]))
        groups = []
        cur = [evs[0]]
        for e in evs[1:]:
            if (e[0] - cur[-1][0]).total_seconds() > GAP:
                groups.append(cur)
                cur = [e]
            else:
                cur.append(e)
        groups.append(cur)
        for n, g in enumerate(groups, 1):
            start, end = g[0][0], g[-1][0]
            start_s, end_s = start.replace(microsecond=0), end.replace(microsecond=0)
            purchases = [e for e in g if e[4] == "purchase"]
            rows.append({
                "n": n, "user": user, "start": start_s, "end": end_s,
                "sort": start,
                "duration_s": int((end_s - start_s).total_seconds()),
                "events": len(g), "purchases": len(purchases),
                "revenue": sum((e[5] for e in purchases), Decimal(0)),
            })
    rows.sort(key=lambda r: (r["sort"], r["user"], r["n"]))
    return rows


def summarize(sessions):
    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].strftime("%Y-%m-%d"),
                            {"sessions": 0, "users": {}})
        d["sessions"] += 1
        d["users"][s["user"]] = d["users"].get(s["user"], Decimal(0)) + s["revenue"]
    out = []
    for date in sorted(days):
        d = days[date]
        total = sum(d["users"].values(), Decimal(0))
        top = None
        if total > 0:
            top = sorted(d["users"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append({"date": date, "sessions": d["sessions"], "users": len(d["users"]),
                    "revenue": fmt_money(total), "top_user": top})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="job.py")
    ap.add_argument("--in", dest="in_dir", required=True)
    ap.add_argument("--out", dest="out_dir", required=True)
    args = ap.parse_args(argv)
    path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    events, rejects, seen, dups = [], [], set(), 0
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        data = f.read()
    for i, raw in enumerate(data.split("\n"), 1):
        text = raw.rstrip("\r")
        if not text.strip():
            continue
        ev, reason = parse_line(text, i)
        if reason:
            rejects.append({"line": i, "reason": reason})
            continue
        if ev[2] in seen:
            dups += 1
            continue
        seen.add(ev[2])
        events.append(ev)
    sessions = sessionize(events) if events else []
    daily = {"days": summarize(sessions), "duplicates_dropped": dups, "rejected": len(rejects)}
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "sessions.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([f"{s['user']}-{s['n']}", s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        s["duration_s"], s["events"], s["purchases"], fmt_money(s["revenue"])])
    with open(os.path.join(args.out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
