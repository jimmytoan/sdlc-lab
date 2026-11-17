#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl; writes sessions.csv, daily.json, rejects.jsonl to <OUTPUT_DIR>.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = {"view", "click", "purchase"}
AMOUNT_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]{1,2})?|\.[0-9]{1,2})$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _required_str(obj, key):
    """Return the stripped string value of a required field, or raise missing_field."""
    if key not in obj:
        raise Reject("missing_field")
    val = obj[key]
    if not isinstance(val, str) or val.strip() == "":
        raise Reject("missing_field")
    return val


def parse_ts(raw):
    """Parse an ISO 8601 timestamp with a UTC offset into an aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError, OSError):
        raise Reject("bad_ts")


def parse_event(line_bytes):
    """Validate one non-blank line. Returns a dict event or raises Reject(reason)."""
    try:
        text = line_bytes.decode("utf-8")
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    event_id = _required_str(obj, "event_id")
    user = _required_str(obj, "user").strip().lower()
    ts_raw = _required_str(obj, "ts")
    if "type" not in obj or obj["type"] is None:
        raise Reject("missing_field")

    ts = parse_ts(ts_raw.strip())

    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")

    amount_cents = 0
    if etype == "purchase":
        if "amount" not in obj:
            raise Reject("bad_amount")
        amount = obj["amount"]
        if not isinstance(amount, str) or not AMOUNT_RE.match(amount):
            raise Reject("bad_amount")
        amount_cents = int((Decimal(amount) * 100).to_integral_value())

    return {
        "event_id": event_id,
        "user": user,
        "ts": ts,
        "type": etype,
        "amount_cents": amount_cents,
    }


def fmt_ts(dt):
    s = dt.isoformat()
    if s.endswith("+00:00"):
        s = s[:-6]
    return s + "Z"


def fmt_cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def read_input(path):
    """Return (valid_events_in_file_order_with_index, rejects, total_lines)."""
    rejects = []
    valid = []  # (file_index, event)
    with open(path, "rb") as f:
        data = f.read()
    raw_lines = data.split(b"\n")
    if raw_lines and raw_lines[-1] == b"":
        raw_lines.pop()
    for lineno, raw in enumerate(raw_lines, start=1):
        line = raw.rstrip(b"\r")
        if line.strip() == b"":
            continue
        try:
            ev = parse_event(line)
        except Reject as r:
            rejects.append({"line": lineno, "reason": r.reason})
            continue
        valid.append((lineno, ev))
    return valid, rejects


def dedupe(valid):
    """Keep one copy per event_id: earliest ts, ties by file order. Returns (kept, dropped_count)."""
    best = {}
    for idx, (lineno, ev) in enumerate(valid):
        key = ev["event_id"]
        cur = best.get(key)
        if cur is None or ev["ts"] < cur[1]["ts"]:
            best[key] = (lineno, ev)
    kept = sorted(best.values(), key=lambda p: p[0])
    return [ev for _, ev in kept], len(valid) - len(kept)


def build_sessions(events):
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: e["ts"])
        groups = []
        current = []
        for ev in evs:
            if current and ev["ts"] - current[-1]["ts"] > SESSION_GAP:
                groups.append(current)
                current = []
            current.append(ev)
        if current:
            groups.append(current)
        for n, grp in enumerate(groups, start=1):
            start = grp[0]["ts"]
            end = grp[-1]["ts"]
            purchases = [e for e in grp if e["type"] == "purchase"]
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start,
                "end": end,
                "duration_s": int((end - start).total_seconds() // 1),
                "events": len(grp),
                "purchases": len(purchases),
                "revenue_cents": sum(e["amount_cents"] for e in grp),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    return sessions


def build_daily(sessions, duplicates_dropped, rejected):
    days = {}
    for s in sessions:
        date = s["start"].astimezone(timezone.utc).date().isoformat()
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue_cents": 0, "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue_cents"] += s["revenue_cents"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], 0) + s["revenue_cents"]
    out_days = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["revenue_cents"] > 0:
            # highest revenue; ties go to the user that sorts first
            top = sorted(d["per_user"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out_days.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_cents(d["revenue_cents"]),
            "top_user": top,
        })
    return {"days": out_days, "duplicates_dropped": duplicates_dropped, "rejected": rejected}


def write_outputs(out_dir, sessions, daily, rejects):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        s["duration_s"], s["events"], s["purchases"], fmt_cents(s["revenue_cents"])])
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sessions and daily summary from events.jsonl")
    parser.add_argument("--in", dest="inp", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        valid, rejects = read_input(in_path)
    except OSError as e:
        print(f"error: cannot read input: {e}", file=sys.stderr)
        return 2

    events, dropped = dedupe(valid)
    sessions = build_sessions(events)
    daily = build_daily(sessions, dropped, len(rejects))
    write_outputs(args.out, sessions, daily, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
