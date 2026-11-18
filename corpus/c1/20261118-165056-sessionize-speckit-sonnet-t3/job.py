"""sessionize: turn raw clickstream events into sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

GAP = timedelta(minutes=30)
TYPES = ("view", "click", "purchase")
TWO = Decimal("0.01")


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(d.quantize(TWO))


def read_lines(path):
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def parse_line(text):
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        if not _nonempty_str(obj.get(k)):
            return None, "missing_field"
    try:
        ts = datetime.fromisoformat(obj["ts"])
        if ts.tzinfo is None or ts.utcoffset() is None:
            return None, "bad_ts"
        ts = ts.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None, "bad_ts"
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
        except InvalidOperation:
            return None, "bad_amount"
        if not amount.is_finite() or amount < 0:
            return None, "bad_amount"
    return {"event_id": obj["event_id"], "user": obj["user"].lower(), "ts": ts,
            "type": typ, "amount": amount}, None


def sessionize(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        cur = None
        n = 0
        for e in evs:
            if cur is None or e["ts"] - cur["end"] > GAP:
                n += 1
                cur = {"user": user, "n": n, "start": e["ts"], "end": e["ts"],
                       "events": 0, "purchases": 0, "revenue": Decimal(0)}
                sessions.append(cur)
            cur["end"] = e["ts"]
            cur["events"] += 1
            if e["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]
        
    for s in sessions:
        s["duration_s"] = int((s["end"] - s["start"]).total_seconds())
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))
    return sessions


def write_sessions(path, sessions):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events",
                    "purchases", "revenue"])
        for s in sessions:
            w.writerow([f"{s['user']}-{s['n']}", s["user"], fmt_ts(s["start"]),
                        fmt_ts(s["end"]), s["duration_s"], s["events"], s["purchases"],
                        fmt_money(s["revenue"])])


def summarize(sessions, duplicates_dropped, rejected):
    days = {}
    for s in sessions:
        days.setdefault(s["start"].strftime("%Y-%m-%d"), []).append(s)
    out = []
    for date in sorted(days):
        ss = days[date]
        per_user = {}
        total = Decimal(0)
        for s in ss:
            per_user[s["user"]] = per_user.get(s["user"], Decimal(0)) + s["revenue"]
            total += s["revenue"]
        top = None
        if total > 0:
            top = sorted(per_user.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append({"date": date, "sessions": len(ss), "users": len(per_user),
                    "revenue": fmt_money(total), "top_user": top})
    return {"days": out, "duplicates_dropped": duplicates_dropped, "rejected": rejected}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="job.py")
    ap.add_argument("--in", dest="in_dir", required=True)
    ap.add_argument("--out", dest="out_dir", required=True)
    args = ap.parse_args(argv)

    src = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(src):
        print(f"job.py: input file not found: {src}", file=sys.stderr)
        return 2

    events, rejects, seen, dups = [], [], set(), 0
    for i, text in enumerate(read_lines(src), 1):
        ev, reason = parse_line(text)
        if reason:
            rejects.append({"line": i, "reason": reason})
        elif ev["event_id"] in seen:
            dups += 1
        else:
            seen.add(ev["event_id"])
            events.append(ev)

    sessions = sessionize(events)
    os.makedirs(args.out_dir, exist_ok=True)
    write_sessions(os.path.join(args.out_dir, "sessions.csv"), sessions)
    with open(os.path.join(args.out_dir, "rejects.jsonl"), "w", encoding="utf-8", newline="") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(args.out_dir, "daily.json"), "w", encoding="utf-8", newline="") as f:
        json.dump(summarize(sessions, dups, len(rejects)), f, indent=2)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
