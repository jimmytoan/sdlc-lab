"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

UTC = timezone.utc
GAP = timedelta(minutes=30)
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
TYPES = ("view", "click", "purchase")


@dataclass
class Event:
    line: int
    event_id: str
    user: str
    ts: datetime
    type: str
    amount: Decimal


@dataclass
class Session:
    user: str
    n: int
    start: datetime
    end: datetime
    events: int
    purchases: int
    revenue: Decimal


def fmt_ts(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(d.quantize(Decimal("0.01"), ROUND_HALF_UP))


def validate(raw_text, line_no):
    """Return an Event or a reason string."""
    try:
        obj = json.loads(raw_text)
    except (ValueError, RecursionError):
        return "bad_json"
    if not isinstance(obj, dict):
        return "bad_json"

    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            return "missing_field"
    event_id = obj["event_id"]
    if not isinstance(event_id, str) or event_id == "":
        return "missing_field"
    user = obj["user"]
    if not isinstance(user, str) or user.lower() == "":
        return "missing_field"
    etype = obj["type"]
    if etype == "purchase" and obj.get("amount") is None:
        return "missing_field"

    ts_raw = obj["ts"]
    if not isinstance(ts_raw, str):
        return "bad_ts"
    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        return "bad_ts"
    if ts.tzinfo is None or ts.utcoffset() is None:
        return "bad_ts"
    try:
        ts = ts.astimezone(UTC)
    except (OverflowError, ValueError):
        return "bad_ts"

    if not isinstance(etype, str) or etype not in TYPES:
        return "bad_type"

    amount = Decimal(0)
    if etype == "purchase":
        raw_amt = obj["amount"]
        if not isinstance(raw_amt, str) or not AMOUNT_RE.fullmatch(raw_amt):
            return "bad_amount"
        amount = Decimal(raw_amt)

    return Event(line_no, event_id, user.lower(), ts, etype, amount)


def parse_lines(path):
    with open(path, "rb") as f:
        data = f.read()
    events, rejects = [], []
    seen = set()
    duplicates = 0
    raw_lines = data.split(b"\n")
    for idx, raw in enumerate(raw_lines, start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if idx == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": idx, "reason": "bad_json"})
            continue
        if text.strip() == "":
            continue
        result = validate(text, idx)
        if isinstance(result, str):
            rejects.append({"line": idx, "reason": result})
            continue
        if result.event_id in seen:
            duplicates += 1
            continue
        seen.add(result.event_id)
        events.append(result)
    return events, rejects, duplicates


def build_sessions(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e.ts, e.line))
        n = 0
        cur = None
        prev_ts = None
        for e in evs:
            if cur is None or e.ts - prev_ts > GAP:
                n += 1
                cur = Session(user, n, e.ts, e.ts, 0, 0, Decimal(0))
                sessions.append(cur)
            cur.end = e.ts
            cur.events += 1
            if e.type == "purchase":
                cur.purchases += 1
                cur.revenue += e.amount
            prev_ts = e.ts
    sessions.sort(key=lambda s: (s.start, s.user, s.n))
    return sessions


def write_sessions_csv(sessions, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s",
                    "events", "purchases", "revenue"])
        for s in sorted(sessions, key=lambda s: (s.start, s.user, s.n)):
            w.writerow([f"{s.user}-{s.n}", s.user, fmt_ts(s.start), fmt_ts(s.end),
                        int((s.end - s.start).total_seconds()), s.events,
                        s.purchases, fmt_money(s.revenue)])


def build_daily(sessions, duplicates, rejected):
    days = {}
    for s in sessions:
        date = s.start.astimezone(UTC).date().isoformat()
        d = days.setdefault(date, {"sessions": 0, "users": {}, "revenue": Decimal(0)})
        d["sessions"] += 1
        d["users"][s.user] = d["users"].get(s.user, Decimal(0)) + s.revenue
        d["revenue"] += s.revenue
    out = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["revenue"] != 0:
            top = min(d["users"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out.append({"date": date, "sessions": d["sessions"], "users": len(d["users"]),
                    "revenue": fmt_money(d["revenue"]), "top_user": top})
    return {"days": out, "duplicates_dropped": duplicates, "rejected": rejected}


def main():
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="in_dir", required=True)
    ap.add_argument("--out", dest="out_dir", required=True)
    args = ap.parse_args()

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2
    try:
        events, rejects, duplicates = parse_lines(in_path)
    except OSError as exc:
        print(f"error: cannot read input file {in_path}: {exc}", file=sys.stderr)
        return 2

    sessions = build_sessions(events)
    daily = build_daily(sessions, duplicates, len(rejects))

    os.makedirs(args.out_dir, exist_ok=True)
    write_sessions_csv(sessions, os.path.join(args.out_dir, "sessions.csv"))
    with open(os.path.join(args.out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(os.path.join(args.out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
