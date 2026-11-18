#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only.
"""
import argparse
import csv
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = ("view", "click", "purchase")
# Generous precision so that normalisation and sums never round amounts.
DEC_PREC = 200


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _is_blank_str(v):
    return isinstance(v, str) and v.strip() == ""


def _parse_amount(raw):
    """Return a Decimal amount or raise Reject('bad_amount')."""
    if not isinstance(raw, str):
        raise Reject("bad_amount")
    text = raw.strip()
    if text == "":
        raise Reject("bad_amount")
    with localcontext() as ctx:
        ctx.prec = DEC_PREC
        try:
            d = Decimal(text)
        except (InvalidOperation, ValueError):
            raise Reject("bad_amount")
        if not d.is_finite() or d < 0:
            raise Reject("bad_amount")
        norm = d.normalize()
        if norm.as_tuple().exponent < -2:
            raise Reject("bad_amount")
    return d


def _parse_ts(raw):
    """Return an aware UTC datetime or raise Reject('bad_ts')."""
    if not isinstance(raw, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(raw.strip())
    except (ValueError, TypeError):
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def validate_event(obj):
    """Validate a decoded JSON object. Returns a normalised event dict or raises Reject."""
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    # Required-field presence (step 3 of the plan).
    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    etype = obj.get("type")

    if not isinstance(event_id, str) or event_id.strip() == "":
        raise Reject("missing_field")
    if not isinstance(user, str) or user.strip() == "":
        raise Reject("missing_field")
    if ts is None or _is_blank_str(ts):
        raise Reject("missing_field")
    if etype is None or _is_blank_str(etype):
        raise Reject("missing_field")

    # type (step 4)
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")

    # ts (step 5)
    when = _parse_ts(ts)

    # amount (step 6)
    amount = None
    if etype == "purchase":
        amount = _parse_amount(obj.get("amount"))

    return {
        "event_id": event_id.strip(),
        "user": user.strip().lower(),
        "ts": when,
        "type": etype,
        "amount": amount,
    }


def read_events(path):
    """Yield (line_no, event_or_None, reason_or_None) for each non-blank line."""
    with open(path, "rb") as f:
        data = f.read()
    for idx, raw in enumerate(data.split(b"\n"), start=1):
        if raw.strip() == b"":
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            yield idx, None, "bad_json"
            continue
        if text.strip() == "":
            continue
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            yield idx, None, "bad_json"
            continue
        try:
            ev = validate_event(obj)
        except Reject as r:
            yield idx, None, r.reason
            continue
        yield idx, ev, None


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    with localcontext() as ctx:
        ctx.prec = DEC_PREC
        return f"{d.quantize(Decimal('0.01')):f}"


def build_sessions(events):
    """Group events by user and split on gaps. Returns list of session dicts."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = [evs[0]]
        for prev, cur in zip(evs, evs[1:]):
            if cur["ts"] - prev["ts"] > SESSION_GAP:
                groups.append(current)
                current = [cur]
            else:
                current.append(cur)
        groups.append(current)

        for n, grp in enumerate(groups, start=1):
            start = grp[0]["ts"]
            end = grp[-1]["ts"]
            purchases = [e for e in grp if e["type"] == "purchase"]
            with localcontext() as ctx:
                ctx.prec = DEC_PREC
                revenue = sum((e["amount"] for e in purchases), Decimal(0))
            sessions.append({
                "n": n,
                "session_id": f"{user}-{n}",
                "user": user,
                "start_dt": start,
                "end_dt": end,
                "start": fmt_ts(start),
                "end": fmt_ts(end),
                "duration_s": int(round((end - start).total_seconds())),
                "events": len(grp),
                "purchases": len(purchases),
                "revenue_dec": revenue,
                "revenue": fmt_money(revenue),
            })

    sessions.sort(key=lambda s: (s["start_dt"], s["user"], s["n"]))
    return sessions


def build_daily(sessions):
    by_day = {}
    for s in sessions:
        by_day.setdefault(s["start_dt"].strftime("%Y-%m-%d"), []).append(s)

    days = []
    for date in sorted(by_day):
        group = by_day[date]
        with localcontext() as ctx:
            ctx.prec = DEC_PREC
            total = sum((s["revenue_dec"] for s in group), Decimal(0))
            per_user = {}
            for s in group:
                per_user[s["user"]] = per_user.get(s["user"], Decimal(0)) + s["revenue_dec"]
        top_user = None
        if total > 0:
            # highest revenue; ties broken by lexicographically smallest user
            top_user = min(per_user, key=lambda u: (-per_user[u], u))
        days.append({
            "date": date,
            "sessions": len(group),
            "users": len({s["user"] for s in group}),
            "revenue": fmt_money(total),
            "top_user": top_user,
        })
    return days


def _atomic_write(out_dir, name, writer):
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer(f)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        os.replace(tmp, os.path.join(out_dir, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(in_dir, out_dir):
    src = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(src):
        sys.stderr.write(f"error: input file not found: {src}\n")
        return 2

    valid = []
    rejects = []
    seen_ids = set()
    duplicates = 0
    for line_no, ev, reason in read_events(src):
        if reason is not None:
            rejects.append({"line": line_no, "reason": reason})
            continue
        if ev["event_id"] in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(ev["event_id"])
        valid.append(ev)

    sessions = build_sessions(valid)
    days = build_daily(sessions)

    # Sessions sorted by start; ties by user then session number (already sorted).
    session_rows = [
        {k: s[k] for k in ("session_id", "user", "start", "end", "duration_s",
                           "events", "purchases", "revenue")}
        for s in sessions
    ]
    daily = {
        "days": days,
        "duplicates_dropped": duplicates,
        "rejected": len(rejects),
    }

    os.makedirs(out_dir, exist_ok=True)

    header = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]

    def write_csv(f):
        w = csv.writer(f)
        w.writerow(header)
        for row in session_rows:
            w.writerow([row[h] for h in header])

    def write_daily(f):
        json.dump(daily, f, indent=2)
        f.write("\n")

    def write_rejects(f):
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    _atomic_write(out_dir, "sessions.csv", write_csv)
    _atomic_write(out_dir, "daily.json", write_daily)
    _atomic_write(out_dir, "rejects.jsonl", write_rejects)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sessionize clickstream events.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)
    return run(args.in_dir, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
