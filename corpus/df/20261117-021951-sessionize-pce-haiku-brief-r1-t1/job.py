#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only.

Decisions on points the spec leaves open:
- Negative purchase amounts are rejected as bad_amount; zero is accepted.
  Amounts must be finite decimal strings (no NaN/Infinity, no floats).
- An empty or whitespace-only user is a missing_field reject.
- Non-string values: user/event_id/missing -> missing_field; ts -> bad_ts;
  type -> bad_type; amount -> bad_amount.
- A whitespace-only line is skipped silently (its line number still counts).
- A JSON value that is not an object is bad_json.
- A line that is not valid UTF-8 is bad_json for that line.
- Duplicate event_ids: only valid events take part in dedupe, so the first
  valid occurrence in file order is kept. Invalid copies are never counted
  as duplicates.
- Amount on view/click events is ignored entirely.
- top_user ties are broken by the alphabetically smaller user.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, MAX_EMAX, MAX_PREC, MIN_EMIN, ROUND_HALF_UP, localcontext

GAP = timedelta(minutes=30)
VALID_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")
# Exact arithmetic context: the default 28-digit precision would round cents
# out of large sums and make quantize() fail on very large amounts.
_EXACT = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)
AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$")


class Event:
    __slots__ = ("event_id", "user", "ts", "type", "amount", "line")

    def __init__(self, event_id, user, ts, type_, amount, line):
        self.event_id = event_id
        self.user = user
        self.ts = ts
        self.type = type_
        self.amount = amount
        self.line = line


class Session:
    __slots__ = ("session_id", "user", "start", "end", "events", "purchases", "revenue")

    def __init__(self, session_id, user, start, end, events, purchases, revenue):
        self.session_id = session_id
        self.user = user
        self.start = start
        self.end = end
        self.events = events
        self.purchases = purchases
        self.revenue = revenue


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(s):
    """Parse an ISO 8601 timestamp that must carry an explicit UTC offset.
    Returns a UTC-aware datetime. Raises ValueError otherwise."""
    if not isinstance(s, str):
        raise ValueError("ts not a string")
    dt = datetime.fromisoformat(s)  # raises ValueError if unparseable
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive ts")
    return dt.astimezone(timezone.utc)


def parse_amount(s):
    """Strict decimal parse. Returns a finite, non-negative Decimal.
    Raises ValueError otherwise."""
    if not isinstance(s, str):
        raise ValueError("amount not a string")
    if not AMOUNT_RE.match(s):
        raise ValueError("bad amount syntax")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("bad amount")
    if not d.is_finite():
        raise ValueError("non-finite amount")
    if d < 0:
        raise ValueError("negative amount")
    return d


def _check_str_field(obj, name):
    """Return the stripped string value of a required string field, or raise
    Reject('missing_field') if absent, null, non-string or blank."""
    v = obj.get(name)
    if not isinstance(v, str):
        raise Reject("missing_field")
    v = v.strip() if name == "user" else v
    if v == "" or (name != "user" and v.strip() == ""):
        raise Reject("missing_field")
    return v


def parse_line(lineno, raw):
    """Validate one physical line (bytes or str).
    Returns (Event | None, reason | None). Event is None for skipped blank
    lines; reason is set for rejects."""
    try:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8")
        else:
            text = raw
    except UnicodeDecodeError:
        return None, "bad_json"
    text = text.strip()
    if text == "":
        return None, None
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    try:
        event_id = _check_str_field(obj, "event_id")
        user = _check_str_field(obj, "user").lower()
        if "ts" not in obj or obj["ts"] is None:
            raise Reject("missing_field")
        if "type" not in obj or obj["type"] is None:
            raise Reject("missing_field")
        type_ = obj["type"]
        if not isinstance(type_, str) or type_ not in VALID_TYPES:
            raise Reject("bad_type")
        try:
            ts = parse_ts(obj["ts"])
        except (ValueError, OverflowError, TypeError):
            raise Reject("bad_ts")
        amount = None
        if type_ == "purchase":
            if "amount" not in obj or obj["amount"] is None:
                raise Reject("missing_field")
            try:
                amount = parse_amount(obj["amount"])
            except (ValueError, TypeError):
                raise Reject("bad_amount")
    except Reject as r:
        return None, r.reason
    return Event(event_id, user, ts, type_, amount, lineno), None


def dedupe(events):
    """Keep the first occurrence of each event_id (file order). Returns
    (kept, dropped_count)."""
    seen = set()
    kept = []
    dropped = 0
    for e in events:
        if e.event_id in seen:
            dropped += 1
            continue
        seen.add(e.event_id)
        kept.append(e)
    return kept, dropped


def sessionize(events):
    """Group events per user, split on gaps > 30 minutes, return sessions
    sorted by start (ties by user, then session number)."""
    by_user = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e.ts, e.line))
        groups = []
        cur = [evs[0]]
        for prev, e in zip(evs, evs[1:]):
            if e.ts - prev.ts > GAP:
                groups.append(cur)
                cur = [e]
            else:
                cur.append(e)
        groups.append(cur)
        for n, grp in enumerate(groups, start=1):
            revenue = Decimal("0")
            purchases = 0
            for e in grp:
                if e.type == "purchase":
                    purchases += 1
                    revenue = _EXACT.add(revenue, e.amount)
            sessions.append(Session(
                session_id=f"{user}-{n}",
                user=user,
                start=grp[0].ts,
                end=grp[-1].ts,
                events=len(grp),
                purchases=purchases,
                revenue=revenue,
            ))
    sessions.sort(key=lambda s: (s.start, s.user, int(s.session_id.rsplit("-", 1)[1])))
    return sessions


def fmt_ts(dt):
    # Explicit zero-padded fields: strftime("%Y") is not padded on some
    # platforms (e.g. year 999 would become "999").
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def fmt_money(d):
    # Size the precision to the value so quantize never overflows.
    prec = max(28, d.adjusted() + 12)
    with localcontext() as ctx:
        ctx.prec = prec
        ctx.Emax = MAX_EMAX
        ctx.Emin = MIN_EMIN
        return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def build_daily(sessions):
    days = {}
    for s in sessions:
        date = s.start.date().isoformat()
        day = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                     "per_user": {}})
        day["sessions"] += 1
        day["users"].add(s.user)
        day["revenue"] = _EXACT.add(day["revenue"], s.revenue)
        day["per_user"][s.user] = _EXACT.add(day["per_user"].get(s.user, Decimal("0")), s.revenue)
    out = []
    for date in sorted(days):
        d = days[date]
        top = None
        best = None
        for user, rev in d["per_user"].items():
            if rev <= 0:
                continue
            if best is None or rev > best or (rev == best and user < top):
                best = rev
                top = user
        out.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })
    return out


def write_sessions_csv(path, sessions):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([
                s.session_id, s.user, fmt_ts(s.start), fmt_ts(s.end),
                int((s.end - s.start).total_seconds()), s.events, s.purchases, fmt_money(s.revenue),
            ])


def write_daily_json(path, days, duplicates_dropped, rejected):
    doc = {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejected}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")


def write_rejects_jsonl(path, rejects):
    with open(path, "w", encoding="utf-8") as f:
        for line, reason in rejects:
            f.write(json.dumps({"line": line, "reason": reason}) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build user sessions and a daily summary.")
    ap.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    ap.add_argument("--out", dest="out", required=True, help="output directory")
    args = ap.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2
    try:
        with open(in_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        print(f"error: cannot read input file {in_path}: {exc}", file=sys.stderr)
        return 2

    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    valid = []
    rejects = []
    for lineno, raw in enumerate(lines, start=1):
        ev, reason = parse_line(lineno, raw)
        if reason is not None:
            rejects.append((lineno, reason))
        elif ev is not None:
            valid.append(ev)

    kept, dropped = dedupe(valid)
    sessions = sessionize(kept)
    days = build_daily(sessions)

    os.makedirs(args.out, exist_ok=True)
    write_sessions_csv(os.path.join(args.out, "sessions.csv"), sessions)
    write_daily_json(os.path.join(args.out, "daily.json"), days, dropped, len(rejects))
    write_rejects_jsonl(os.path.join(args.out, "rejects.jsonl"), rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
