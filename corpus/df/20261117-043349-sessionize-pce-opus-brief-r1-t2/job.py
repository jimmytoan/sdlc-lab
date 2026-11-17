"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, Context, localcontext

GAP_SECONDS = 1800
REQUIRED = ("event_id", "user", "ts", "type")
TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")
MONEY_CTX = Context(prec=200, rounding=ROUND_HALF_UP)


@dataclass
class Event:
    event_id: str
    user: str
    ts: datetime
    type: str
    amount: Decimal
    line: int


@dataclass
class Session:
    session_id: str
    user: str
    number: int
    start: datetime
    end: datetime
    events: int
    purchases: int
    revenue: Decimal


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse_amount(value):
    if not isinstance(value, str):
        return None
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    try:
        MONEY_CTX.quantize(d, CENT)
    except InvalidOperation:
        return None
    return d


def parse_line(raw, line_no):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "bad_json"
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return "bad_json"
    if not isinstance(obj, dict):
        return "bad_json"
    for k in REQUIRED:
        if obj.get(k) is None:
            return "missing_field"
    for k in ("event_id", "user"):
        if not isinstance(obj[k], str) or obj[k] == "":
            return "missing_field"
    if obj["type"] == "purchase" and obj.get("amount") is None:
        return "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return "bad_ts"
    typ = obj["type"]
    if not isinstance(typ, str) or typ not in TYPES:
        return "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        amount = parse_amount(obj["amount"])
        if amount is None:
            return "bad_amount"
    return Event(obj["event_id"], obj["user"].lower(), ts, typ, amount, line_no)


def read_events(path):
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    events, rejects, seen = [], [], set()
    dups = 0
    for n, raw in enumerate(lines, 1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if n == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if raw.strip() == b"":
            continue
        res = parse_line(raw, n)
        if isinstance(res, str):
            rejects.append({"line": n, "reason": res})
            continue
        if res.event_id in seen:
            dups += 1
            continue
        seen.add(res.event_id)
        events.append(res)
    return events, rejects, dups


def build_sessions(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e.ts, e.line))
        groups = []
        for e in evs:
            if groups and (e.ts - groups[-1][-1].ts).total_seconds() <= GAP_SECONDS:
                groups[-1].append(e)
            else:
                groups.append([e])
        for i, g in enumerate(groups, 1):
            rev = Decimal(0)
            for e in g:
                if e.type == "purchase":
                    rev = MONEY_CTX.add(rev, e.amount)
            sessions.append(Session(
                f"{user}-{i}", user, i, g[0].ts, g[-1].ts, len(g),
                sum(1 for e in g if e.type == "purchase"), rev))
    sessions.sort(key=lambda s: (s.start, s.user, s.number))
    return sessions


def fmt_money(d):
    q = MONEY_CTX.quantize(d, CENT)
    if q == 0:
        q = Decimal("0.00")
    return format(q, "f")


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_daily(sessions):
    days = {}
    for s in sessions:
        date = s.start.date().isoformat()
        days.setdefault(date, []).append(s)
    out = []
    for date in sorted(days):
        ss = days[date]
        per_user = {}
        total = Decimal(0)
        for s in ss:
            per_user[s.user] = MONEY_CTX.add(per_user.get(s.user, Decimal(0)), s.revenue)
            total = MONEY_CTX.add(total, s.revenue)
        top = None
        if total > 0:
            top = sorted(per_user.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append({"date": date, "sessions": len(ss), "users": len(per_user),
                    "revenue": fmt_money(total), "top_user": top})
    return out


def main(argv):
    parser = argparse.ArgumentParser(prog="job.py", description="Sessionize clickstream events.")
    parser.add_argument("--in", dest="inp", required=True)
    parser.add_argument("--out", dest="out", required=True)
    args = parser.parse_args(argv)

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        events, rejects, dups = read_events(path)
    except OSError as e:
        print(f"error: cannot read input file {path}: {e}", file=sys.stderr)
        return 2

    sessions = build_sessions(events)
    days = build_daily(sessions)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s.session_id, s.user, fmt_ts(s.start), fmt_ts(s.end),
                        int((s.end - s.start).total_seconds()), s.events, s.purchases, fmt_money(s.revenue)])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": days, "duplicates_dropped": dups, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
