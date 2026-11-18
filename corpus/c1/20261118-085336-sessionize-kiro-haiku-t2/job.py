#!/usr/bin/env python3
"""sessionize: nightly clickstream sessions batch job.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only, no network access.
"""
import argparse
import csv
import io
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta
from fractions import Fraction

INPUT_NAME = "events.jsonl"
SESSION_GAP_S = 1800
EVENT_TYPES = ("view", "click", "purchase")
OUTPUT_NAMES = ("sessions.csv", "daily.json", "rejects.jsonl")

SKIP = object()  # marker for whitespace-only lines

# ts: YYYY-MM-DDTHH:MM:SS[.fraction](Z|+HH:MM|-HH:MM); ASCII digits only.
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")

EPOCH_DT = datetime(1970, 1, 1)

# Huge numeric literals (in ignored fields or as long amounts) must not fail the run.
sys.set_int_max_str_digits(0)


def _days_from_civil(y, m, d):
    """Days since 1970-01-01 in the proleptic Gregorian calendar (integer math)."""
    if m <= 2:
        y -= 1
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


# Range of UTC instants representable as years 0001..9999 (design decision:
# anything outside is rejected as bad_ts because YYYY cannot represent it).
MIN_UTC = _days_from_civil(1, 1, 1) * 86400
MAX_UTC = _days_from_civil(10000, 1, 1) * 86400


def _days_in_month(y, m):
    if m == 2:
        leap = (y % 4 == 0 and y % 100 != 0) or y % 400 == 0
        return 29 if leap else 28
    return 30 if m in (4, 6, 9, 11) else 31


def _no_constant(name):
    raise ValueError("non-standard JSON constant: " + name)


# ---------------------------------------------------------------- validation

def _parse_ts(s):
    """Return the UTC instant (int or Fraction of epoch seconds), or None if invalid."""
    m = TS_RE.fullmatch(s)
    if m is None:
        return None
    y, mo, d, hh, mi, ss = (int(g) for g in m.groups()[:6])
    frac, off = m.group(7), m.group(8)
    if not 1 <= mo <= 12:
        return None
    if not 1 <= d <= _days_in_month(y, mo):
        return None
    if hh > 23 or mi > 59 or ss > 59:
        return None
    if off == "Z":
        off_s = 0
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        off_s = (oh * 3600 + om * 60) * (1 if off[0] == "+" else -1)
    local = _days_from_civil(y, mo, d) * 86400 + hh * 3600 + mi * 60 + ss
    utc = local - off_s
    if frac:
        digits = frac[1:]
        utc = Fraction(utc) + Fraction(int(digits), 10 ** len(digits))
    if utc < MIN_UTC or utc >= MAX_UTC:
        return None
    return utc


def _parse_amount_cents(s):
    """Return integer cents for a valid positive amount string, or None."""
    if AMOUNT_RE.fullmatch(s) is None:
        return None
    ip, _, fp = s.partition(".")
    cents = int(ip) * 100
    if fp:
        cents += int((fp + "0")[:2])
    if cents == 0:
        return None
    return cents


def validate(line_no, obj):
    """Return an event tuple, or a reject reason string. Checks in R3.7 order."""
    # missing_field
    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            return "missing_field"
    etype = obj["type"]
    if etype == "purchase" and obj.get("amount") is None:
        return "missing_field"
    for key in ("event_id", "user"):
        v = obj[key]
        if not isinstance(v, str) or v.strip() == "":
            return "missing_field"
    # bad_type
    if not (isinstance(etype, str) and etype in EVENT_TYPES):
        return "bad_type"
    # bad_ts
    ts_raw = obj["ts"]
    if not isinstance(ts_raw, str):
        return "bad_ts"
    ts = _parse_ts(ts_raw)
    if ts is None:
        return "bad_ts"
    # bad_amount (only purchases; amount is never read for other types)
    cents = None
    if etype == "purchase":
        amt = obj["amount"]
        if not isinstance(amt, str):
            return "bad_amount"
        cents = _parse_amount_cents(amt)
        if cents is None:
            return "bad_amount"
    user = obj["user"].strip().lower()
    return (line_no, obj["event_id"], user, ts, etype, cents)


def parse_line(line_no, raw):
    """Return SKIP, a reject reason string, or an event tuple."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "bad_json"
    if text.strip() == "":
        return SKIP
    try:
        obj = json.loads(text, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return "bad_json"
    if not isinstance(obj, dict):
        return "bad_json"
    # Strings that cannot be encoded as UTF-8 (lone surrogate escapes) cannot
    # be written to the outputs; treat such a line as not valid UTF-8 text.
    for key in ("event_id", "user"):
        v = obj.get(key)
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                return "bad_json"
    return validate(line_no, obj)


# ------------------------------------------------------------------ reading

def read_lines(path):
    """Yield (line_no, raw_bytes) for each physical line. LF or CRLF terminators."""
    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            if raw.endswith(b"\n"):
                raw = raw[:-1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
            yield n, raw


# ------------------------------------------------------------ sessionizing

class Session:
    __slots__ = ("user", "no", "start", "end", "events", "purchases", "revenue")

    def __init__(self, user, no, ts):
        self.user = user
        self.no = no
        self.start = ts
        self.end = ts
        self.events = 0
        self.purchases = 0
        self.revenue = 0  # cents


def build_sessions(events):
    """events: accepted, de-duplicated event tuples (line, event_id, user, ts, type, cents)."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev[2], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e[3], e[0]))
        current = None
        count = 0
        prev_ts = None
        for _line, _eid, _user, ts, etype, cents in evs:
            if current is None or ts - prev_ts > SESSION_GAP_S:
                count += 1
                current = Session(user, count, ts)
                sessions.append(current)
            current.end = ts
            current.events += 1
            if etype == "purchase":
                current.purchases += 1
                current.revenue += cents
            prev_ts = ts
    return sessions


def _utc_datetime(seconds_int):
    return EPOCH_DT + timedelta(seconds=seconds_int)


def _fmt_ts(seconds_int):
    dt = _utc_datetime(seconds_int)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _fmt_date(seconds_int):
    dt = _utc_datetime(seconds_int)
    return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)


def _fmt_money(cents):
    return "%d.%02d" % (cents // 100, cents % 100)


def summarise(sessions):
    """Return day rows sorted by date: dicts with date, sessions, users, revenue, top_user."""
    per_day = {}
    for s in sessions:
        key = _fmt_date(s.start // 1)
        day = per_day.get(key)
        if day is None:
            day = {"sessions": 0, "users": set(), "revenue": 0, "by_user": {}}
            per_day[key] = day
        day["sessions"] += 1
        day["users"].add(s.user)
        day["revenue"] += s.revenue
        day["by_user"][s.user] = day["by_user"].get(s.user, 0) + s.revenue
    rows = []
    for key in sorted(per_day):
        day = per_day[key]
        top = None
        if day["revenue"] > 0:
            best = -1
            for user in sorted(day["by_user"]):  # ascending: ties keep the first name
                rev = day["by_user"][user]
                if rev > best:
                    best = rev
                    top = user
        rows.append({
            "date": key,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": _fmt_money(day["revenue"]),
            "top_user": top,
        })
    return rows


# ---------------------------------------------------------------- rendering

def render_sessions_csv(sessions):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["session_id", "user", "start", "end", "duration_s",
                     "events", "purchases", "revenue"])
    ordered = sorted(sessions, key=lambda s: (s.start // 1, s.user, s.no))
    for s in ordered:
        start_s = s.start // 1
        writer.writerow([
            "%s-%d" % (s.user, s.no),
            s.user,
            _fmt_ts(start_s),
            _fmt_ts(s.end // 1),
            (s.end - s.start) // 1,
            s.events,
            s.purchases,
            _fmt_money(s.revenue),
        ])
    return buf.getvalue().encode("utf-8")


def render_daily_json(days, duplicates_dropped, rejected):
    doc = {
        "days": days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def render_rejects(rejects):
    lines = ['{"line": %d, "reason": %s}\n' % (n, json.dumps(reason))
             for n, reason in rejects]
    return "".join(lines).encode("utf-8")


# ------------------------------------------------------------------ writing

def _create_temp(out_dir):
    """Create a uniquely named temp file (O_CREAT | O_EXCL). Return (path, fd)."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    while True:
        path = os.path.join(out_dir, ".sessionize-%d-%s.tmp" % (os.getpid(), secrets.token_hex(8)))
        try:
            fd = os.open(path, flags, 0o666)
        except FileExistsError:
            continue
        return path, fd


def _remove_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_outputs(out_dir, files):
    """files: list of (final_name, bytes). Return None on success, else an error message."""
    staged = []  # temp paths, in the same order as files
    try:
        for _name, data in files:
            path, fd = _create_temp(out_dir)
            staged.append(path)  # registered before writing so cleanup covers partial writes
            try:
                f = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
    except OSError as e:
        for path in staged:
            _remove_quietly(path)
        return "cannot write output in %s: %s" % (out_dir, e)

    for i, (name, _data) in enumerate(files):
        try:
            os.replace(staged[i], os.path.join(out_dir, name))
        except OSError as e:
            for path in staged[i:]:
                _remove_quietly(path)
            return "cannot replace output %s: %s" % (name, e)
    return None


# --------------------------------------------------------------------- run

def run(in_dir, out_dir):
    src = os.path.join(in_dir, INPUT_NAME)
    if not os.path.isfile(src):
        print("missing input: %s is not a regular file" % src, file=sys.stderr)
        return 2

    rejects = []
    events = []
    seen_ids = set()
    duplicates = 0
    try:
        for line_no, raw in read_lines(src):
            res = parse_line(line_no, raw)
            if res is SKIP:
                continue
            if isinstance(res, str):
                rejects.append((line_no, res))
                continue
            if res[1] in seen_ids:
                duplicates += 1
                continue
            seen_ids.add(res[1])
            events.append(res)
    except OSError as e:
        print("cannot read input %s: %s" % (src, e), file=sys.stderr)
        return 1

    sessions = build_sessions(events)
    days = summarise(sessions)
    files = [
        ("sessions.csv", render_sessions_csv(sessions)),
        ("daily.json", render_daily_json(days, duplicates, len(rejects))),
        ("rejects.jsonl", render_rejects(rejects)),
    ]

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print("cannot create output directory %s: %s" % (out_dir, e), file=sys.stderr)
        return 1

    err = write_outputs(out_dir, files)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Turn raw clickstream events into sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="directory for sessions.csv, daily.json and rejects.jsonl")
    args = parser.parse_args(argv)
    try:
        return run(args.in_dir, args.out_dir)
    except Exception as e:  # unexpected failure: no output has been committed yet
        print("unexpected error: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
