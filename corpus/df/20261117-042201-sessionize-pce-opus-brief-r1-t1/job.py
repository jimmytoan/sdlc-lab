"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

TYPES = ("view", "click", "purchase")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
GAP = timedelta(minutes=30)
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]

Event = namedtuple("Event", "event_id user ts type amount")


class Session:
    __slots__ = ("user", "n", "start", "end", "events", "purchases", "revenue")

    def __init__(self, user, n, start):
        self.user = user
        self.n = n
        self.start = start
        self.end = start
        self.events = 0
        self.purchases = 0
        self.revenue = Decimal("0")

    @property
    def session_id(self):
        return f"{self.user}-{self.n}"


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_ts(s):
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def validate(text):
    try:
        obj = json.loads(text)
    except Exception:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        if obj.get(k) is None:
            return None, "missing_field"
    for k in ("event_id", "user"):
        if not isinstance(obj[k], str) or obj[k] == "":
            return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    typ = obj["type"]
    if not isinstance(typ, str) or typ not in TYPES:
        return None, "bad_type"
    amount = Decimal("0")
    if typ == "purchase":
        raw = obj.get("amount")
        if raw is None:
            return None, "missing_field"
        if not isinstance(raw, str) or not AMOUNT_RE.fullmatch(raw):
            return None, "bad_amount"
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return None, "bad_amount"
    return Event(obj["event_id"], obj["user"].lower(), ts, typ, amount), None


def read_events(path):
    events, rejects, dups = [], [], 0
    seen = set()
    with open(path, "rb") as f:
        for n, raw in enumerate(f, start=1):
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if n == 1 and raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                rejects.append({"line": n, "reason": "bad_json"})
                continue
            if text.strip() == "":
                continue
            ev, reason = validate(text)
            if ev is None:
                rejects.append({"line": n, "reason": reason})
                continue
            if ev.event_id in seen:
                dups += 1
                continue
            seen.add(ev.event_id)
            events.append(ev)
    return events, rejects, dups


def build_sessions(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e.ts, e.event_id))
        cur = None
        prev = None
        n = 0
        for e in evs:
            if cur is None or e.ts - prev > GAP:
                n += 1
                cur = Session(user, n, e.ts)
                sessions.append(cur)
            cur.end = e.ts
            cur.events += 1
            if e.type == "purchase":
                cur.purchases += 1
                cur.revenue += e.amount
            prev = e.ts
    sessions.sort(key=lambda s: (s.start, s.user, s.n))
    return sessions


def build_daily(sessions):
    days = {}
    for s in sessions:
        days.setdefault(s.start.date(), []).append(s)
    out = []
    for date in sorted(days):
        ss = days[date]
        per_user = {}
        for s in ss:
            per_user[s.user] = per_user.get(s.user, Decimal("0")) + s.revenue
        total = sum((s.revenue for s in ss), Decimal("0"))
        top = None
        if total > 0:
            top = min((u for u in per_user), key=lambda u: (-per_user[u], u))
        out.append({
            "date": date.isoformat(),
            "sessions": len(ss),
            "users": len(per_user),
            "revenue": fmt_money(total),
            "top_user": top,
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args(argv)

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
    daily = build_daily(sessions)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for s in sessions:
            w.writerow([
                s.session_id, s.user, fmt_ts(s.start), fmt_ts(s.end),
                int((s.end - s.start).total_seconds()), s.events, s.purchases,
                fmt_money(s.revenue),
            ])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": daily, "duplicates_dropped": dups, "rejected": len(rejects)},
                  f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
