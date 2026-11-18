"""sessionize: nightly batch job turning raw web events into sessions."""
import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, Context, ROUND_HALF_UP

REASONS = ("bad_json", "missing_field", "bad_ts", "bad_type", "bad_amount")
GAP_SECONDS = 1800
CTX = Context(prec=60)
TYPES = ("view", "click", "purchase")
TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|z|[+-]\d{2}:\d{2})$",
    re.ASCII)
AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$", re.ASCII)


@dataclass(frozen=True)
class Event:
    line: int
    event_id: str
    user: str
    ts: datetime
    type: str
    amount: Decimal


@dataclass(frozen=True)
class Reject:
    line: int
    reason: str


@dataclass
class Session:
    session_id: str
    user: str
    start: datetime
    end: datetime
    events: int
    purchases: int
    revenue: Decimal


def read_lines(path):
    with open(path, "rb") as f:
        data = f.read()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    for i, p in enumerate(parts, 1):
        if p.endswith(b"\r"):
            p = p[:-1]
        yield i, p


def parse_ts(s):
    m = TS_RE.match(s)
    if not m:
        raise ValueError("bad ts")
    d, t, frac, off = m.groups()
    if off in ("Z", "z"):
        off = "+00:00"
    frac = (frac or "")[:6]
    text = d + "T" + t + ("." + frac if frac else "") + off
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            raise ValueError("naive")
        return dt.astimezone(timezone.utc)
    except OverflowError as e:
        raise ValueError(str(e))


def parse_amount(s):
    if not isinstance(s, str) or not AMOUNT_RE.match(s):
        raise ValueError("bad amount")
    return Decimal(s)


def parse_event(raw, line=0):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Reject(line, "bad_json")
    if text.strip() == "":
        return None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return Reject(line, "bad_json")
    if not isinstance(obj, dict):
        return Reject(line, "bad_json")
    for k in ("event_id", "user", "ts"):
        v = obj.get(k)
        if not isinstance(v, str) or v == "":
            return Reject(line, "missing_field")
    if obj.get("type") is None:
        return Reject(line, "missing_field")
    try:
        ts = parse_ts(obj["ts"])
    except (ValueError, OverflowError):
        return Reject(line, "bad_ts")
    typ = obj["type"]
    if not isinstance(typ, str) or typ not in TYPES:
        return Reject(line, "bad_type")
    amount = Decimal(0)
    if typ == "purchase":
        a = obj.get("amount")
        if a is None:
            return Reject(line, "missing_field")
        try:
            amount = parse_amount(a)
        except ValueError:
            return Reject(line, "bad_amount")
    return Event(line, obj["event_id"], obj["user"].lower(), ts, typ, amount)


def dedupe(events):
    seen = set()
    out = []
    dups = 0
    for e in events:
        if e.event_id in seen:
            dups += 1
        else:
            seen.add(e.event_id)
            out.append(e)
    return out, dups


def sessionize(events):
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
        for n, g in enumerate(groups, 1):
            rev = Decimal(0)
            purchases = 0
            for e in g:
                if e.type == "purchase":
                    purchases += 1
                    rev = CTX.add(rev, e.amount)
            sessions.append(Session(f"{user}-{n}", user, g[0].ts, g[-1].ts,
                                    len(g), purchases, rev))
    sessions.sort(key=lambda s: (s.start, s.user))
    return sessions


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(CTX.quantize(d, Decimal("0.01")) if False else d.quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP, context=CTX))


def _replace_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def write_sessions_csv(path, sessions):
    import io
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["session_id", "user", "start", "end", "duration_s", "events",
                "purchases", "revenue"])
    for s in sessions:
        st = s.start.replace(microsecond=0)
        en = s.end.replace(microsecond=0)
        w.writerow([s.session_id, s.user, fmt_utc(st), fmt_utc(en),
                    int((en - st).total_seconds()), s.events, s.purchases,
                    fmt_money(s.revenue)])
    _replace_write(path, buf.getvalue())


def build_daily(sessions, dup_count, reject_count):
    days = {}
    for s in sessions:
        days.setdefault(s.start.date(), []).append(s)
    out = []
    for d in sorted(days):
        ss = days[d]
        total = Decimal(0)
        per_user = {}
        for s in ss:
            total = CTX.add(total, s.revenue)
            per_user[s.user] = CTX.add(per_user.get(s.user, Decimal(0)), s.revenue)
        top = None
        if fmt_money(total) != "0.00" and per_user:
            top = min(per_user, key=lambda u: (-per_user[u], u))
        out.append({"date": d.isoformat(), "sessions": len(ss),
                    "users": len({s.user for s in ss}),
                    "revenue": fmt_money(total), "top_user": top})
    return {"days": out, "duplicates_dropped": dup_count, "rejected": reject_count}


def write_daily_json(path, daily):
    _replace_write(path, json.dumps(daily, ensure_ascii=False) + "\n")


def write_rejects(path, rejects):
    text = "".join(json.dumps({"line": r.line, "reason": r.reason}) + "\n"
                   for r in rejects)
    _replace_write(path, text)


def parse_args(argv):
    p = argparse.ArgumentParser(prog="job.py")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out", dest="out", required=True)
    try:
        return p.parse_args(argv)
    except SystemExit:
        raise SystemExit(2)


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 2
    src = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(src):
        print(f"error: input file not found: {src}", file=sys.stderr)
        return 2
    try:
        events, rejects = [], []
        for n, raw in read_lines(src):
            r = parse_event(raw, n)
            if r is None:
                continue
            if isinstance(r, Reject):
                rejects.append(r)
            else:
                events.append(r)
        unique, dups = dedupe(events)
        sessions = sessionize(unique)
        daily = build_daily(sessions, dups, len(rejects))
        os.makedirs(args.out, exist_ok=True)
        write_sessions_csv(os.path.join(args.out, "sessions.csv"), sessions)
        write_daily_json(os.path.join(args.out, "daily.json"), daily)
        write_rejects(os.path.join(args.out, "rejects.jsonl"), rejects)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
