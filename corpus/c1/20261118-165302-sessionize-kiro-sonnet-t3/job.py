"""sessionize: turn raw clickstream events into per-user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext

getcontext().prec = 1_000_000

GAP_US = 1_800_000_000
HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII)
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]+)?", re.ASCII)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class Event:
    line: int
    event_id: str
    user: str
    ts: int  # UTC microseconds since epoch
    type: str
    amount: Decimal


@dataclass
class Reject:
    line: int
    reason: str


@dataclass
class Session:
    user: str
    number: int
    start: int
    end: int
    events: int
    purchases: int
    revenue: Decimal


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"error: {message}\n")
        raise SystemExit(2)


def read_lines(path):
    with open(path, "rb") as f:
        data = f.read()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    out = []
    for i, p in enumerate(parts, 1):
        if p.endswith(b"\r"):
            p = p[:-1]
        out.append((i, p))
    return out


def parse_ts(s):
    if not isinstance(s, str):
        return None
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    try:
        y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
        frac = m.group(7)
        us = int((frac[1:] + "000000")[:6]) if frac else 0
        z = m.group(8)
        if z == "Z":
            off = timedelta(0)
        else:
            oh, om = int(z[1:3]), int(z[4:6])
            if oh > 23 or om > 59:
                return None
            off = timedelta(hours=oh, minutes=om)
            if z[0] == "-":
                off = -off
        dt = datetime(y, mo, d, h, mi, se, us, tzinfo=timezone(off))
        utc = dt.astimezone(timezone.utc)
        delta = utc - EPOCH
    except (ValueError, OverflowError):
        return None
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def parse_amount(v):
    if not isinstance(v, str) or not AMOUNT_RE.fullmatch(v):
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def fmt_ts(micros):
    secs = micros // 1_000_000
    return (EPOCH + timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _bad_const(name):
    raise ValueError(name)


def parse_event(lineno, raw):
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_bad_const)
        if not isinstance(obj, dict):
            return Reject(lineno, "bad_json")
        json.dumps(obj, ensure_ascii=False).encode("utf-8")  # lone surrogates
    except (ValueError, RecursionError, OverflowError):
        return Reject(lineno, "bad_json")
    vals = {}
    for k in ("event_id", "user", "ts", "type"):
        v = obj.get(k)
        if not isinstance(v, str) or not v.strip():
            return Reject(lineno, "missing_field")
        vals[k] = v
    if vals["type"] == "purchase" and obj.get("amount") is None:
        return Reject(lineno, "missing_field")
    ts = parse_ts(vals["ts"])
    if ts is None:
        return Reject(lineno, "bad_ts")
    if vals["type"] not in ("view", "click", "purchase"):
        return Reject(lineno, "bad_type")
    amount = Decimal(0)
    if vals["type"] == "purchase":
        amount = parse_amount(obj["amount"])
        if amount is None:
            return Reject(lineno, "bad_amount")
    return Event(lineno, vals["event_id"], vals["user"].strip().lower(), ts, vals["type"], amount)


def dedupe(events):
    seen = set()
    kept = []
    dropped = 0
    for e in events:
        if e.event_id in seen:
            dropped += 1
        else:
            seen.add(e.event_id)
            kept.append(e)
    return kept, dropped


def sessionize(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)
    sessions = []
    for user in sorted(by_user):
        evs = sorted(by_user[user], key=lambda e: (e.ts, e.line))
        cur = None
        n = 0
        prev = None
        for e in evs:
            if cur is None or e.ts - prev > GAP_US:
                n += 1
                cur = Session(user, n, e.ts, e.ts, 0, 0, Decimal(0))
                sessions.append(cur)
            cur.end = e.ts
            cur.events += 1
            if e.type == "purchase":
                cur.purchases += 1
                cur.revenue += e.amount
            prev = e.ts
    return sessions


def build_rows(sessions):
    rows = []
    for s in sessions:
        rows.append({
            "key": (fmt_ts(s.start), s.user, s.number),
            "session_id": f"{s.user}-{s.number}",
            "user": s.user,
            "start": fmt_ts(s.start),
            "end": fmt_ts(s.end),
            "duration_s": s.end // 1_000_000 - s.start // 1_000_000,
            "events": s.events,
            "purchases": s.purchases,
            "revenue": fmt_money(s.revenue),
        })
    rows.sort(key=lambda r: r["key"])
    return rows


def summarize_days(rows):
    days = {}
    for r in rows:
        days.setdefault(r["start"][:10], []).append(r)
    out = []
    for date in sorted(days):
        rs = days[date]
        per_user = {}
        total = Decimal(0)
        for r in rs:
            rev = Decimal(r["revenue"])
            per_user[r["user"]] = per_user.get(r["user"], Decimal(0)) + rev
            total += rev
        top = None
        if total > 0:
            best = max(per_user.values())
            top = min(u for u in per_user if per_user[u] == best)
        out.append({"date": date, "sessions": len(rs), "users": len(per_user),
                    "revenue": format(total.quantize(Decimal("0.01")), "f"), "top_user": top})
    return out


def write_outputs(out_dir, rows, days, dups, rejects):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sessions.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r[c] for c in HEADER])
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8", newline="") as f:
        json.dump({"days": days, "duplicates_dropped": dups, "rejected": len(rejects)},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8", newline="") as f:
        for r in rejects:
            f.write(json.dumps({"line": r.line, "reason": r.reason}) + "\n")


def main(argv=None):
    p = _Parser(prog="job.py", allow_abbrev=False)
    p.add_argument("--in", dest="in_dir", required=True)
    p.add_argument("--out", dest="out_dir", required=True)
    args = p.parse_args(argv)
    path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        lines = read_lines(path)
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 2
    events, rejects = [], []
    for no, raw in lines:
        if not raw.strip():
            continue
        r = parse_event(no, raw)
        (rejects if isinstance(r, Reject) else events).append(r)
    events, dups = dedupe(events)
    rows = build_rows(sessionize(events))
    days = summarize_days(rows)
    write_outputs(args.out_dir, rows, days, dups, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
