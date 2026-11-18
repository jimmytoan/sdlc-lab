"""sessionize: turn raw clickstream events into sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, localcontext

GAP_S = 1800
TYPES = ("view", "click", "purchase")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]+)?")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(value.strip())
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise Reject("bad_ts")
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise Reject("bad_ts")


def parse_amount(value):
    if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):
        raise Reject("bad_amount")
    return Decimal(value)


def fmt_money(d):
    if d.as_tuple().exponent > -2:
        with localcontext() as ctx:
            ctx.prec = 100
            d = d.quantize(Decimal("0.01"))
    return format(d, "f")


def fmt_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _missing(v):
    return v is None or v == ""


def parse_line(text):
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    eid, user, ts, typ = (obj.get(k) for k in ("event_id", "user", "ts", "type"))
    if _missing(eid) or _missing(user) or _missing(ts) or _missing(typ):
        raise Reject("missing_field")
    if not isinstance(eid, str) or not isinstance(user, str):
        raise Reject("missing_field")
    t = parse_ts(ts)
    if not isinstance(typ, str) or typ not in TYPES:
        raise Reject("bad_type")
    amount = None
    if typ == "purchase":
        amount = parse_amount(obj.get("amount"))
    return eid, user.lower(), t, typ, amount


def load(path):
    events, rejects, seen, dups = [], [], set(), 0
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        data = f.read()
    for n, raw in enumerate(data.split("\n"), 1):
        text = raw.rstrip("\r")
        if not text.strip():
            continue
        try:
            ev = parse_line(text)
        except Reject as r:
            rejects.append((n, r.reason))
            continue
        if ev[0] in seen:
            dups += 1
            continue
        seen.add(ev[0])
        events.append(ev)
    return events, rejects, dups


def sessionize(events):
    events = sorted(events, key=lambda e: (e[1], e[2], e[0]))
    sessions = []
    cur = None
    counters = {}
    for eid, user, t, typ, amount in events:
        if cur is not None and cur["user"] == user and (t - cur["end"]).total_seconds() <= GAP_S:
            cur["end"] = t
        else:
            counters[user] = counters.get(user, 0) + 1
            cur = {"user": user, "num": counters[user], "start": t, "end": t,
                   "events": 0, "purchases": 0, "revenue": Decimal(0)}
            sessions.append(cur)
        cur["events"] += 1
        if typ == "purchase":
            cur["purchases"] += 1
            with localcontext() as ctx:
                ctx.prec = 100
                cur["revenue"] += amount
    sessions.sort(key=lambda s: (s["start"], s["user"], s["num"]))
    return sessions


def write_sessions(path, sessions):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([f"{s['user']}-{s['num']}", s["user"], fmt_time(s["start"]), fmt_time(s["end"]),
                        int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                        fmt_money(s["revenue"])])


def write_rejects(path, rejects):
    with open(path, "w", encoding="utf-8", newline="") as f:
        for n, r in rejects:
            f.write(json.dumps({"line": n, "reason": r}) + "\n")


def summarize(sessions):
    days = {}
    for s in sessions:
        days.setdefault(s["start"].strftime("%Y-%m-%d"), []).append(s)
    out = []
    for date in sorted(days):
        ss = days[date]
        per_user = {}
        total = Decimal(0)
        with localcontext() as ctx:
            ctx.prec = 100
            for s in ss:
                per_user[s["user"]] = per_user.get(s["user"], Decimal(0)) + s["revenue"]
                total += s["revenue"]
        top = None
        if total > 0:
            top = min(per_user, key=lambda u: (-per_user[u], u))
        out.append({"date": date, "sessions": len(ss), "users": len({s["user"] for s in ss}),
                    "revenue": fmt_money(total), "top_user": top})
    return out


def write_daily(path, days, dups, rejected):
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump({"days": days, "duplicates_dropped": dups, "rejected": rejected}, f, indent=2)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="job.py")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args(argv)
    src = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(src):
        print(f"error: input file not found: {src}", file=sys.stderr)
        return 2
    events, rejects, dups = load(src)
    sessions = sessionize(events)
    os.makedirs(args.out, exist_ok=True)
    write_sessions(os.path.join(args.out, "sessions.csv"), sessions)
    write_rejects(os.path.join(args.out, "rejects.jsonl"), rejects)
    write_daily(os.path.join(args.out, "daily.json"), summarize(sessions), dups, len(rejects))
    return 0


if __name__ == "__main__":
    sys.exit(main())
