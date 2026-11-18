#!/usr/bin/env python3
"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only (Python 3.13).

Exit codes: 0 success; 2 input file missing or usage error; 1 other failure.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

INPUT_FILE_NAME = "events.jsonl"
SESSIONS_FILE = "sessions.csv"
DAILY_FILE = "daily.json"
REJECTS_FILE = "rejects.jsonl"
SESSION_GAP = timedelta(minutes=30)
VALID_TYPES = ("view", "click", "purchase")
SESSION_COLUMNS = ["session_id", "user", "start", "end", "duration_s",
                   "events", "purchases", "revenue"]


class InputMissing(Exception):
    pass


class Reject(Exception):
    """Raised inside validation; carries the reject reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- reading

def read_lines(path):
    """Yield (line_no, raw_bytes) for every physical line, 1-based."""
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            yield line_no, raw


def _missing(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def validate_line(line_no, raw):
    """Return ('event', dict) or ('reject', reason) for one non-blank line."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "reject", "bad_json"
    try:
        obj = json.loads(text)
    except Exception:
        return "reject", "bad_json"
    if not isinstance(obj, dict):
        return "reject", "missing_field"

    for key in ("event_id", "user", "ts", "type"):
        if _missing(obj.get(key)):
            return "reject", "missing_field"
    if not isinstance(obj["event_id"], str) or not isinstance(obj["user"], str):
        return "reject", "missing_field"

    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        return "reject", "bad_type"

    ts_raw = obj["ts"]
    if not isinstance(ts_raw, str):
        return "reject", "bad_ts"
    try:
        ts = datetime.fromisoformat(ts_raw)
    except (ValueError, TypeError):
        return "reject", "bad_ts"
    if ts.tzinfo is None or ts.utcoffset() is None:
        return "reject", "bad_ts"
    ts_utc = ts.astimezone(timezone.utc)

    amount = None
    if etype == "purchase":
        amt_raw = obj.get("amount")
        if _missing(amt_raw):
            return "reject", "missing_field"
        if not isinstance(amt_raw, str):
            return "reject", "bad_amount"
        try:
            amount = Decimal(amt_raw)
        except (InvalidOperation, ValueError):
            return "reject", "bad_amount"
        if not amount.is_finite() or amount <= 0:
            return "reject", "bad_amount"

    event = {
        "event_id": obj["event_id"].strip(),
        "user": obj["user"].strip().lower(),
        "ts": ts_utc,
        "type": etype,
        "amount": amount,
        "line_no": line_no,
    }
    return "event", event


# ---------------------------------------------------------------- processing

def process(events_path):
    """Read and validate the input. Returns (kept_events, rejects, duplicates)."""
    rejects = []
    candidates = []
    for line_no, raw in read_lines(events_path):
        if raw.strip() == b"":
            continue
        kind, value = validate_line(line_no, raw)
        if kind == "event":
            candidates.append(value)
        else:
            rejects.append({"line": line_no, "reason": value})

    seen = set()
    kept = []
    duplicates = 0
    for ev in candidates:  # file order
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        kept.append(ev)

    kept.sort(key=lambda e: (e["ts"], e["line_no"]))
    return kept, rejects, duplicates


def sessionize(events):
    """Group sorted events into sessions. Returns a list of session dicts."""
    by_user = defaultdict(list)
    for ev in events:
        by_user[ev["user"]].append(ev)

    sessions = []
    for user, evs in by_user.items():
        current = None
        n = 0
        prev_ts = None
        for ev in evs:  # already in time order
            if current is None or ev["ts"] - prev_ts > SESSION_GAP:
                n += 1
                current = {
                    "session_id": f"{user}-{n}",
                    "user": user,
                    "start": ev["ts"],
                    "end": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0"),
                }
                sessions.append(current)
            current["end"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
            prev_ts = ev["ts"]

    for s in sessions:
        # Whole-second values as they appear in the output, so that
        # duration_s is reproducible from the written columns.
        start_s = s["start"].replace(microsecond=0)
        end_s = s["end"].replace(microsecond=0)
        s["duration_s"] = int((end_s - start_s).total_seconds())

    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def money(value):
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def fmt_utc(dt):
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def daily_summary(sessions):
    by_day = defaultdict(list)
    for s in sessions:
        by_day[s["start"].date().isoformat()].append(s)

    days = []
    for date in sorted(by_day):
        day_sessions = by_day[date]
        revenue_by_user = defaultdict(lambda: Decimal("0"))
        for s in day_sessions:
            revenue_by_user[s["user"]] += s["revenue"]
        total = sum((s["revenue"] for s in day_sessions), Decimal("0"))
        top_user = None
        if total != 0:
            # highest revenue; ties broken by alphabetically first user name
            top_user = min(revenue_by_user.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        days.append({
            "date": date,
            "sessions": len(day_sessions),
            "users": len({s["user"] for s in day_sessions}),
            "revenue": money(total),
            "top_user": top_user,
        })
    return days


# ---------------------------------------------------------------- output

def write_outputs(out_dir, sessions, days, rejects, duplicates):
    os.makedirs(out_dir, exist_ok=True)
    tmp_files = {}
    try:
        # Write everything to temporary files first ...
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-", suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(SESSION_COLUMNS)
            for s in sessions:
                writer.writerow([
                    s["session_id"], s["user"], fmt_utc(s["start"]), fmt_utc(s["end"]),
                    s["duration_s"], s["events"], s["purchases"], money(s["revenue"]),
                ])
        tmp_files[SESSIONS_FILE] = tmp

        daily = {
            "days": days,
            "duplicates_dropped": duplicates,
            "rejected": len(rejects),
        }
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(daily, f, indent=2)
            f.write("\n")
        tmp_files[DAILY_FILE] = tmp

        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".tmp-", suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            for r in rejects:
                f.write(json.dumps(r) + "\n")
        tmp_files[REJECTS_FILE] = tmp

        # ... and only then move them into place.
        for name in (SESSIONS_FILE, DAILY_FILE, REJECTS_FILE):
            os.replace(tmp_files[name], os.path.join(out_dir, name))
    except BaseException:
        for tmp in tmp_files.values():
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------- main

def run(in_dir, out_dir):
    events_path = os.path.join(in_dir, INPUT_FILE_NAME)
    if not os.path.isfile(events_path):
        raise InputMissing(events_path)

    kept, rejects, duplicates = process(events_path)
    sessions = sessionize(kept)
    days = daily_summary(sessions)
    write_outputs(out_dir, sessions, days, rejects, duplicates)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory")
    args = parser.parse_args(argv)

    try:
        run(args.in_dir, args.out_dir)
    except InputMissing as exc:
        print(f"error: input file not found: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # unexpected failure
        print(f"error: unexpected failure: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
