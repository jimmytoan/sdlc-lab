#!/usr/bin/env python3
"""sessionize: turn raw web clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only.
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import sys
import tempfile
from collections import namedtuple

GAP_NS = 1800 * 10**9
NS_PER_SEC = 10**9
NS_PER_DAY = 86400 * 10**9
SEC_PER_DAY = 86400
EPOCH_ORDINAL = 719163  # date(1970, 1, 1).toordinal()

INPUT_NAME = "events.jsonl"
OUTPUT_NAMES = ("sessions.csv", "daily.json", "rejects.jsonl")
VALID_TYPES = ("view", "click", "purchase")
SESSION_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]

Reject = namedtuple("Reject", "line reason")
Event = namedtuple("Event", "line event_id user instant_ns type cents")
Session = namedtuple("Session", "user n start_ns end_ns events purchases cents")


class BadLine(Exception):
    """Raised by the line validator; carries the reject reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- input reading

def read_input_bytes(path):
    """Read the whole input file as bytes. Raises OSError if it cannot be read."""
    with open(path, "rb") as f:
        return f.read()


def iter_lines(data):
    """Yield (line_no, raw_bytes) for every physical line.

    Splits on LF only, strips one trailing CR per line, treats a final line
    without a terminator as a line, and drops a UTF-8 BOM on line 1 only.
    """
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()  # the newline terminated the last line; no extra line
    for line_no, raw in enumerate(parts, 1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if line_no == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        yield line_no, raw


# ---------------------------------------------------------------- validation

def _no_raise_constant(name):
    raise ValueError("non-standard JSON constant: " + name)


def _has_surrogates(s):
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def parse_ts(value):
    """Return the UTC instant in integer nanoseconds. Raises BadLine('bad_ts')."""
    if not isinstance(value, str):
        raise BadLine("bad_ts")
    m = _TS_RE.fullmatch(value)
    if m is None:
        raise BadLine("bad_ts")
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac, off = m.group(7), m.group(8)
    if year == 0:
        raise BadLine("bad_ts")
    try:
        ordinal = datetime.date(year, month, day).toordinal()
    except ValueError:
        raise BadLine("bad_ts")
    if hour > 23 or minute > 59 or second > 59:
        raise BadLine("bad_ts")
    if off == "Z":
        sign, off_sec = 1, 0
    else:
        sign = -1 if off[0] == "-" else 1
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            raise BadLine("bad_ts")
        off_sec = oh * 3600 + om * 60
    frac_ns = int(frac[1:].ljust(9, "0")) if frac else 0
    days = ordinal - EPOCH_ORDINAL
    sec = days * SEC_PER_DAY + hour * 3600 + minute * 60 + second - sign * off_sec
    return sec * NS_PER_SEC + frac_ns


def parse_amount(value):
    """Return integer cents for a purchase amount. Raises BadLine('bad_amount')."""
    if not isinstance(value, str) or _AMOUNT_RE.fullmatch(value) is None:
        raise BadLine("bad_amount")
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + int(frac.ljust(2, "0") or "0")


def normalize_user(user):
    return user.strip().lower()


def parse_line(line_no, raw):
    """Validate one non-blank line.

    Returns an Event, or raises BadLine with the first failing reason
    in the order bad_json, missing_field, bad_ts, bad_type, bad_amount.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BadLine("bad_json")
    try:
        obj = json.loads(text, parse_constant=_no_raise_constant)
    except (ValueError, RecursionError):
        raise BadLine("bad_json")
    if not isinstance(obj, dict):
        raise BadLine("bad_json")
    for key in ("event_id", "user"):
        if isinstance(obj.get(key), str) and _has_surrogates(obj[key]):
            raise BadLine("bad_json")

    # missing_field checks
    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            raise BadLine("missing_field")
    event_id, user = obj["event_id"], obj["user"]
    if not isinstance(event_id, str) or not isinstance(user, str):
        raise BadLine("missing_field")
    if event_id == "":
        raise BadLine("missing_field")
    if user.strip() == "":
        raise BadLine("missing_field")
    etype = obj["type"]
    if etype == "purchase" and obj.get("amount") is None:
        raise BadLine("missing_field")

    instant_ns = parse_ts(obj["ts"])

    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise BadLine("bad_type")

    cents = 0
    if etype == "purchase":
        cents = parse_amount(obj["amount"])

    return Event(line_no, event_id, normalize_user(user), instant_ns, etype, cents)


def validate_lines(data):
    """Return (valid_events, rejects) for the whole input, in file order."""
    events, rejects = [], []
    for line_no, raw in iter_lines(data):
        if raw.decode("utf-8", errors="replace").strip() == "":
            continue
        try:
            events.append(parse_line(line_no, raw))
        except BadLine as bad:
            rejects.append(Reject(line_no, bad.reason))
    return events, rejects


# ---------------------------------------------------------------- pipeline

def dedupe(events):
    """Keep the first occurrence of each event_id. Returns (kept, duplicates_dropped)."""
    seen = set()
    kept = []
    dropped = 0
    for ev in events:
        if ev.event_id in seen:
            dropped += 1
            continue
        seen.add(ev.event_id)
        kept.append(ev)
    return kept, dropped


def sessionize(events):
    """Group events by user and split on gaps greater than 30 minutes."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev.user, []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e.instant_ns, e.line))
        n = 0
        current = None
        prev_ns = None
        for ev in evs:
            if current is None or ev.instant_ns - prev_ns > GAP_NS:
                if current is not None:
                    sessions.append(current)
                n += 1
                current = {"user": user, "n": n, "start_ns": ev.instant_ns,
                           "end_ns": ev.instant_ns, "events": 0, "purchases": 0, "cents": 0}
            current["end_ns"] = ev.instant_ns
            current["events"] += 1
            if ev.type == "purchase":
                current["purchases"] += 1
                current["cents"] += ev.cents
            prev_ns = ev.instant_ns
        if current is not None:
            sessions.append(current)
    result = [Session(s["user"], s["n"], s["start_ns"], s["end_ns"], s["events"], s["purchases"], s["cents"])
              for s in sessions]
    result.sort(key=lambda s: (s.start_ns, s.user, s.n))
    return result


def civil_from_days(z):
    """Convert days since 1970-01-01 to (year, month, day). Works for any integer."""
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    if m <= 2:
        y += 1
    return y, m, d


def format_instant(ns):
    """Format an instant as YYYY-MM-DDTHH:MM:SSZ, truncating fractional seconds."""
    sec = ns // NS_PER_SEC
    days, rem = divmod(sec, SEC_PER_DAY)
    y, m, d = civil_from_days(days)
    hh, rem = divmod(rem, 3600)
    mm, ss = divmod(rem, 60)
    return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mm:02d}:{ss:02d}Z"


def format_date(ns):
    days = ns // NS_PER_DAY
    y, m, d = civil_from_days(days)
    return f"{y:04d}-{m:02d}-{d:02d}"


def format_cents(c):
    return f"{c // 100}.{c % 100:02d}"


def build_sessions_csv(sessions):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SESSION_HEADER)
    for s in sessions:
        start = format_instant(s.start_ns)
        end = format_instant(s.end_ns)
        start_s = s.start_ns // NS_PER_SEC
        end_s = s.end_ns // NS_PER_SEC
        duration = end_s - start_s  # from the written (truncated) values
        writer.writerow([f"{s.user}-{s.n}", s.user, start, end, duration,
                         s.events, s.purchases, format_cents(s.cents)])
    return buf.getvalue().encode("utf-8")


def build_daily(sessions, duplicates_dropped, rejected_count):
    by_date = {}
    for s in sessions:
        date = format_date(s.start_ns)
        entry = by_date.setdefault(date, {"sessions": 0, "cents": 0, "users": {}})
        entry["sessions"] += 1
        entry["cents"] += s.cents
        entry["users"][s.user] = entry["users"].get(s.user, 0) + s.cents
    days = []
    for date in sorted(by_date):
        e = by_date[date]
        positive = [(u, c) for u, c in e["users"].items() if c > 0]
        top = min(positive, key=lambda uc: (-uc[1], uc[0]))[0] if positive else None
        days.append({
            "date": date,
            "sessions": e["sessions"],
            "users": len(e["users"]),
            "revenue": format_cents(e["cents"]),
            "top_user": top,
        })
    obj = {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejected_count}
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_rejects(rejects):
    lines = [json.dumps({"line": r.line, "reason": r.reason}) + "\n" for r in rejects]
    return "".join(lines).encode("utf-8")


def run_pipeline(data):
    """Run all stages in memory. Returns (csv_bytes, daily_bytes, rejects_bytes)."""
    valid, rejects = validate_lines(data)
    kept, duplicates = dedupe(valid)
    sessions = sessionize(kept)
    csv_bytes = build_sessions_csv(sessions)
    daily_bytes = build_daily(sessions, duplicates, len(rejects))
    rejects_bytes = build_rejects(rejects)
    return csv_bytes, daily_bytes, rejects_bytes


# ---------------------------------------------------------------- output

def write_outputs(out_dir, csv_bytes, daily_bytes, rejects_bytes):
    """Write the three files atomically. Returns an exit code (0 or 1)."""
    contents = {
        "sessions.csv": csv_bytes,
        "daily.json": daily_bytes,
        "rejects.jsonl": rejects_bytes,
    }
    temps = []
    try:
        os.makedirs(out_dir, exist_ok=True)
        for name in OUTPUT_NAMES:
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".sessionize-", suffix=".tmp")
            temps.append(tmp)
            with os.fdopen(fd, "wb") as f:
                f.write(contents[name])
                f.flush()
                os.fsync(f.fileno())
        for name, tmp in zip(OUTPUT_NAMES, temps):
            os.replace(tmp, os.path.join(out_dir, name))
        temps = []
    except OSError as exc:
        for tmp in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        print(f"error: cannot write output to {out_dir}: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------- CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Turn clickstream events into sessions, a daily summary and rejects.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="folder to write sessions.csv, daily.json and rejects.jsonl")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)  # exits 2 with usage on stderr when args are missing

    input_path = os.path.join(args.in_dir, INPUT_NAME)
    if not os.path.isfile(input_path):
        print(f"error: input file not found: {input_path}", file=sys.stderr)
        return 2
    try:
        data = read_input_bytes(input_path)
    except OSError as exc:
        print(f"error: cannot read input file {input_path}: {exc}", file=sys.stderr)
        return 2

    csv_bytes, daily_bytes, rejects_bytes = run_pipeline(data)
    return write_outputs(args.out_dir, csv_bytes, daily_bytes, rejects_bytes)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
