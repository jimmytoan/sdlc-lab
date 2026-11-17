#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in INPUT_DIR --out OUTPUT_DIR

Reads INPUT_DIR/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to OUTPUT_DIR. Standard library only.

Decisions where the spec is silent:
- Validation happens before deduplication: an invalid line is a reject and is
  never counted as a duplicate.
- Of several valid copies sharing an event_id, the first one in file order is kept.
  event_id is compared by its JSON value, so the number 1 and the string "1"
  are different ids.
- Amounts must be JSON strings matching a plain decimal: optional sign, digits,
  optional fraction, optional exponent ("19.99", "20", "-5.00", "0.1234", "1e3",
  "+5", "1."). Anything else (underscores, whitespace, NaN, Infinity, empty) is
  bad_amount. Values are kept exactly. Revenue is rounded to cents only when
  written (ROUND_HALF_UP). Amounts with an adjusted exponent above 100 are
  bad_amount, so every output value stays representable.
  A present amount is validated on every event type, but only purchases use it.
- Timestamps must carry a UTC offset. Naive timestamps are bad_ts, and so are
  timestamps that fall outside datetime's range once converted to UTC.
- A leading UTF-8 byte order mark on the file is ignored.
- User names are stripped and lower-cased. An empty name is missing_field.
- Sessions are assigned to the UTC calendar date of their start. A session's
  revenue and sessions count go to that date, and top_user is computed from
  the same per-session revenue.
- top_user is the user with the highest revenue that day (ties: alphabetically
  smallest). It is null when the day's revenue is exactly zero.
- Blank (whitespace-only) lines are skipped but still counted in line numbers.
  Lines that are not valid UTF-8 are bad_json.
"""
import argparse
import csv
import decimal
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

decimal.getcontext().prec = 1000  # exact sums for any amount we accept
GAP = timedelta(minutes=30)
TYPES = {"view", "click", "purchase"}
CENT = Decimal("0.01")
UTC = timezone.utc
MAX_ADJUSTED = 100

AMOUNT_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


class RejectLine(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str):
        raise RejectLine("bad_ts")
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise RejectLine("bad_ts")
        return dt.astimezone(UTC)
    except (ValueError, OverflowError):
        # OverflowError: the UTC conversion falls outside datetime's range
        raise RejectLine("bad_ts")


def parse_amount(value):
    if not isinstance(value, str) or not AMOUNT_RE.fullmatch(value):
        raise RejectLine("bad_amount")
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError):
        raise RejectLine("bad_amount")
    if not d.is_finite():
        raise RejectLine("bad_amount")
    if d != 0 and d.adjusted() > MAX_ADJUSTED:
        raise RejectLine("bad_amount")
    return d + Decimal("0")  # normalise -0 to 0


def parse_line(lineno, raw):
    """Return (event, None) for a valid line or (None, reject) otherwise."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, {"line": lineno, "reason": "bad_json"}
    try:
        if not isinstance(obj, dict):
            raise RejectLine("missing_field")
        for key in ("event_id", "user", "ts", "type"):
            if obj.get(key) is None:
                raise RejectLine("missing_field")
        event_id = obj["event_id"]
        if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
            raise RejectLine("missing_field")
        if event_id == "":
            raise RejectLine("missing_field")
        # key keeps JSON types apart: 1 (number) and "1" (string) differ
        event_key = json.dumps(event_id)
        user = obj["user"]
        if not isinstance(user, str) or user.strip() == "":
            raise RejectLine("missing_field")
        user = user.strip().lower()
        ts = parse_ts(obj["ts"])
        etype = obj["type"]
        if not isinstance(etype, str) or etype not in TYPES:
            raise RejectLine("bad_type")
        amount = None
        if etype == "purchase" and obj.get("amount") is None:
            raise RejectLine("missing_field")
        if obj.get("amount") is not None:
            # validated for every event type: a present amount must be a valid decimal
            parsed = parse_amount(obj["amount"])
            if etype == "purchase":
                amount = parsed
    except RejectLine as rj:
        return None, {"line": lineno, "reason": rj.reason}
    return {"event_id": event_key, "user": user, "ts": ts, "type": etype,
            "amount": amount, "line": lineno}, None


def load_events(path):
    events, rejects = [], []
    with open(path, "rb") as f:
        for lineno, raw in enumerate(f, start=1):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                rejects.append({"line": lineno, "reason": "bad_json"})
                continue
            if lineno == 1:
                text = text.lstrip("\ufeff")
            if text.strip() == "":
                continue
            ev, rej = parse_line(lineno, text)
            if rej is not None:
                rejects.append(rej)
            else:
                events.append(ev)
    return events, rejects


def dedupe(events):
    """Keep the first valid event (file order) for each event_id."""
    seen = set()
    unique = []
    dropped = 0
    for ev in events:
        if ev["event_id"] in seen:
            dropped += 1
            continue
        seen.add(ev["event_id"])
        unique.append(ev)
    return unique, dropped


def sessionize(events):
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = None
        for ev in evs:
            if current is None or ev["ts"] - current[-1]["ts"] > GAP:
                current = []
                groups.append(current)
            current.append(ev)
        for n, group in enumerate(groups, start=1):
            revenue = sum((e["amount"] for e in group if e["type"] == "purchase"), Decimal("0"))
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": group[0]["ts"],
                "end": group[-1]["ts"],
                "events": len(group),
                "purchases": sum(1 for e in group if e["type"] == "purchase"),
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start"], s["session_id"]))
    return sessions


def fmt_ts(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def fmt_money(d):
    q = d.quantize(CENT, rounding=ROUND_HALF_UP) + Decimal("0")
    return f"{q:.2f}"


def build_daily(sessions, rejects_count, duplicates_dropped):
    per_day = {}
    for s in sessions:
        day = s["start"].date().isoformat()
        info = per_day.setdefault(day, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                        "user_revenue": {}})
        info["sessions"] += 1
        info["users"].add(s["user"])
        info["revenue"] += s["revenue"]
        info["user_revenue"][s["user"]] = info["user_revenue"].get(s["user"], Decimal("0")) + s["revenue"]
    days = []
    for day in sorted(per_day):
        info = per_day[day]
        top_user = None
        if info["revenue"] != 0:
            # highest revenue; ties -> alphabetically smallest user
            top_user = min(info["user_revenue"], key=lambda u: (-info["user_revenue"][u], u))
        days.append({
            "date": day,
            "sessions": info["sessions"],
            "users": len(info["users"]),
            "revenue": fmt_money(info["revenue"]),
            "top_user": top_user,
        })
    return {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejects_count}


def write_outputs(out_dir, sessions, daily, rejects):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "sessions.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([
                s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                fmt_money(s["revenue"]),
            ])
    with open(out / "daily.json", "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(out / "rejects.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sessionize clickstream events.")
    parser.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, help="output directory")
    args = parser.parse_args(argv)

    events_path = Path(args.inp) / "events.jsonl"
    if not events_path.is_file():
        print(f"error: input file not found: {events_path}", file=sys.stderr)
        return 2

    try:
        events, rejects = load_events(events_path)
    except OSError as exc:
        print(f"error: cannot read {events_path}: {exc}", file=sys.stderr)
        return 2

    unique, dropped = dedupe(events)
    sessions = sessionize(unique)
    daily = build_daily(sessions, len(rejects), dropped)
    write_outputs(args.out, sessions, daily, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
