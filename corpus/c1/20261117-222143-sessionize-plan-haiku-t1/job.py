#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation, localcontext

SESSION_GAP = timedelta(minutes=30)
EVENT_TYPES = ("view", "click", "purchase")
CENT = Decimal("0.01")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _is_missing(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _parse_event_id(value):
    if isinstance(value, bool):
        raise Reject("missing_field")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    raise Reject("missing_field")


def _parse_ts(value):
    if not isinstance(value, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(value.strip())
    except (ValueError, TypeError):
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError, OSError):
        raise Reject("bad_ts")


def _parse_amount(value):
    if isinstance(value, bool):
        raise Reject("bad_amount")
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise Reject("bad_amount")
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            amt = Decimal(text)
            if not amt.is_finite() or amt < 0:
                raise Reject("bad_amount")
            # Must be representable at cent precision; otherwise treat as overflow.
            amt.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, ArithmeticError):
        raise Reject("bad_amount")
    return amt


def parse_line(raw_text):
    """Return an event dict or raise Reject. raw_text is a decoded, non-blank line."""
    try:
        obj = json.loads(raw_text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    for field in ("event_id", "user", "ts", "type"):
        if field not in obj or _is_missing(obj[field]):
            raise Reject("missing_field")

    event_id = _parse_event_id(obj["event_id"])
    if event_id == "":
        raise Reject("missing_field")

    user = obj["user"]
    if not isinstance(user, str):
        raise Reject("missing_field")
    user = user.strip().lower()
    if user == "":
        raise Reject("missing_field")

    ts = _parse_ts(obj["ts"])

    etype = obj["type"]
    if not isinstance(etype, str) or etype.strip() not in EVENT_TYPES:
        raise Reject("bad_type")
    etype = etype.strip()

    amount = None
    if etype == "purchase":
        if "amount" not in obj or obj["amount"] is None:
            raise Reject("missing_field")
        amount = _parse_amount(obj["amount"])

    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}


def read_events(path):
    """Return (unique_events_in_file_order, rejects, duplicates_dropped)."""
    with open(path, "rb") as f:
        data = f.read()

    rejects = []
    seen = set()
    events = []
    duplicates = 0

    lines = data.split(b"\n")
    for idx, raw in enumerate(lines):
        line_no = idx + 1
        if idx == 0 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if raw.strip() == b"":
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        try:
            ev = parse_line(text)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        events.append(ev)
    return events, rejects, duplicates


def build_sessions(events):
    """Return list of session dicts sorted by start, user, session number."""
    by_user = {}
    for order, ev in enumerate(events):
        by_user.setdefault(ev["user"], []).append((ev["ts"], order, ev))

    sessions = []
    for user, items in by_user.items():
        items.sort(key=lambda t: (t[0], t[1]))
        groups = []
        current = []
        prev_ts = None
        for ts, _, ev in items:
            if current and (ts - prev_ts) > SESSION_GAP:
                groups.append(current)
                current = []
            current.append(ev)
            prev_ts = ts
        if current:
            groups.append(current)

        for n, group in enumerate(groups, start=1):
            start = group[0]["ts"]
            end = group[-1]["ts"]
            revenue = Decimal("0")
            purchases = 0
            for ev in group:
                if ev["type"] == "purchase":
                    purchases += 1
                    revenue += ev["amount"]
            duration = (end - start) // timedelta(seconds=1)
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start_dt": start,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_s": int(duration),
                "events": len(group),
                "purchases": purchases,
                "revenue": revenue,
                "seq": n,
            })

    sessions.sort(key=lambda s: (s["start_dt"], s["user"], s["seq"]))
    return sessions


def money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def build_daily(sessions, duplicates, rejected):
    by_day = {}
    for s in sessions:
        day = s["start_dt"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(s)

    days = []
    for day in sorted(by_day):
        group = by_day[day]
        total = Decimal("0")
        per_user = {}
        users = set()
        for s in group:
            total += s["revenue"]
            users.add(s["user"])
            per_user[s["user"]] = per_user.get(s["user"], Decimal("0")) + s["revenue"]
        top_user = None
        best = None
        for user in sorted(per_user):
            rev = per_user[user]
            if rev <= 0:
                continue
            if best is None or rev > best:
                best = rev
                top_user = user
        days.append({
            "date": day,
            "sessions": len(group),
            "users": len(users),
            "revenue": money(total),
            "top_user": top_user,
        })
    return {"days": days, "duplicates_dropped": duplicates, "rejected": rejected}


def _atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_outputs(out_dir, sessions, daily, rejects):
    os.makedirs(out_dir, exist_ok=True)

    import io
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for s in sessions:
        writer.writerow([s["session_id"], s["user"], s["start"], s["end"], s["duration_s"],
                         s["events"], s["purchases"], money(s["revenue"])])
    _atomic_write(os.path.join(out_dir, "sessions.csv"), buf.getvalue())

    _atomic_write(os.path.join(out_dir, "daily.json"), json.dumps(daily, indent=2, ensure_ascii=False) + "\n")

    rej_text = "".join(json.dumps(r) + "\n" for r in rejects)
    _atomic_write(os.path.join(out_dir, "rejects.jsonl"), rej_text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary from events.jsonl.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        events, rejects, duplicates = read_events(in_path)
    except OSError as exc:
        print(f"error: cannot read input file {in_path}: {exc}", file=sys.stderr)
        return 2

    sessions = build_sessions(events)
    daily = build_daily(sessions, duplicates, len(rejects))

    try:
        write_outputs(args.out_dir, sessions, daily, rejects)
    except OSError as exc:
        print(f"error: cannot write outputs to {args.out_dir}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
