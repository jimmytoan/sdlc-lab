#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only. See .kiro/specs/sessionize/.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Python 3.11+ limits int<->str conversion; amounts and numbers must not hit it.
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

SESSION_GAP = timedelta(seconds=1800)
EVENT_TYPES = ("view", "click", "purchase")
SKIP = object()  # sentinel: whitespace-only line, no reject written
OUTPUT_NAMES = ("sessions.csv", "daily.json", "rejects.jsonl")

# R4.1 to R4.4, R4.10: ASCII digits only, uppercase T, optional 1-6 digit fraction,
# offset Z or +HH:MM / -HH:MM. Anything else (no offset, +0700, space, lowercase t,
# 7-digit fraction, date only) does not match.
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?"
    r"(Z|([+-])([0-9]{2}):([0-9]{2}))",
    re.ASCII,
)
# R4.14: digits, optional point and one or two digits, nothing else.
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?", re.ASCII)


class Event:
    """A valid event. Money is kept as integer cents so sums are exact."""

    __slots__ = ("line", "event_id", "user", "ts", "type", "cents")

    def __init__(self, line, event_id, user, ts, type_, cents):
        self.line = line
        self.event_id = event_id
        self.user = user
        self.ts = ts
        self.type = type_
        self.cents = cents  # 0 unless purchase


class Session:
    __slots__ = ("user", "number", "start", "end", "events", "purchases", "cents")

    def __init__(self, user, number, start, end, events, purchases, cents):
        self.user = user
        self.number = number
        self.start = start
        self.end = end
        self.events = events
        self.purchases = purchases
        self.cents = cents

    @property
    def session_id(self):
        return f"{self.user}-{self.number}"


# ---------------------------------------------------------------- R2: lines

def read_lines(data):
    """Yield (line_no, raw_bytes_without_terminator) for each physical line (R2.1-2.3)."""
    if not data:
        return
    parts = data.split(b"\n")
    if data.endswith(b"\n"):
        parts.pop()  # the empty string after the final terminator is not a line
    for idx, raw in enumerate(parts, start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]  # R2.2
        yield idx, raw


def _reject_constant(name):
    raise ValueError(f"non-finite JSON constant: {name}")


def _has_unencodable_str(obj):
    """Lone surrogates from JSON escapes cannot be written as UTF-8."""
    for key in ("event_id", "user", "ts", "type", "amount"):
        value = obj.get(key)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return True
    return False


def parse_line(raw):
    """Return SKIP, a bad_json reason string, or a dict (the event object)."""
    try:
        text = raw.decode("utf-8")  # R2.5
    except UnicodeDecodeError:
        return "bad_json"
    if text.strip() == "":  # R2.4
        return SKIP
    try:
        obj = json.loads(text, parse_constant=_reject_constant)  # R2.6, R2.7
    except (ValueError, RecursionError):
        return "bad_json"
    if not isinstance(obj, dict):  # R2.8
        return "bad_json"
    if _has_unencodable_str(obj):
        return "bad_json"
    return obj  # R2.9: other members are simply never read


# ---------------------------------------------------------- R3, R4: fields

def parse_ts(text):
    """Return an aware UTC datetime, or None if the ts string is not accepted."""
    m = TS_RE.fullmatch(text)
    if m is None:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7)
    micro = int(frac.ljust(6, "0")) if frac else 0
    if m.group(8) == "Z":
        tz = timezone.utc
    else:
        off_h, off_m = int(m.group(10)), int(m.group(11))
        if off_h > 23 or off_m > 59:  # R4.4
            return None
        offset = timedelta(hours=off_h, minutes=off_m)
        if m.group(9) == "-":
            offset = -offset
        tz = timezone(offset)
    try:
        # datetime() rejects month 13, Feb 30, hour 24, minute 60, second 60 (R4.7)
        value = datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
        return value.astimezone(timezone.utc)  # R4.9
    except (ValueError, OverflowError):
        return None


def parse_amount(text):
    """Return the amount in integer cents, or None if it does not match R4.14."""
    m = AMOUNT_RE.fullmatch(text)
    if m is None:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    return int(whole) * 100 + int(frac.ljust(2, "0"))


def validate(obj, line):
    """Return an Event, or the reject reason string (R3.10 precedence)."""
    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    type_ = obj.get("type")
    amount = obj.get("amount")

    # 1. missing_field, checked across all members first
    if not isinstance(event_id, str) or event_id == "":  # R3.1
        return "missing_field"
    if not isinstance(user, str) or user.strip() == "":  # R3.3
        return "missing_field"
    if ts is None:  # R3.5
        return "missing_field"
    if type_ is None:  # R3.7
        return "missing_field"
    if type_ == "purchase" and amount is None:  # R4.11, R4.12
        return "missing_field"

    # 2. bad_type (case-sensitive exact match, R3.8, R3.9)
    if not isinstance(type_, str) or type_ not in EVENT_TYPES:
        return "bad_type"

    # 3. bad_ts
    if not isinstance(ts, str):  # R3.6
        return "bad_ts"
    ts_utc = parse_ts(ts)
    if ts_utc is None:
        return "bad_ts"

    # 4. bad_amount: only purchases carry an amount (R3.11)
    cents = 0
    if type_ == "purchase":
        if not isinstance(amount, str):  # R4.13
            return "bad_amount"
        cents = parse_amount(amount)
        if cents is None:  # R4.15 to R4.18
            return "bad_amount"

    return Event(line, event_id, user.strip().lower(), ts_utc, type_, cents)  # R3.4


# ------------------------------------------------- R5, R6: pipeline stages

def process(data):
    """Run lines -> parse -> validate -> de-duplicate.

    Returns (kept_events, rejects, duplicates_dropped); rejects is a list of
    (line, reason) in ascending line order.
    """
    rejects = []
    valid = []
    for line_no, raw in read_lines(data):
        parsed = parse_line(raw)
        if parsed is SKIP:
            continue
        if isinstance(parsed, str):
            rejects.append((line_no, parsed))
            continue
        result = validate(parsed, line_no)
        if isinstance(result, str):
            rejects.append((line_no, result))
        else:
            valid.append(result)

    seen = set()
    kept = []
    duplicates = 0
    for ev in valid:  # file order; first occurrence wins (R6.2, R6.7)
        if ev.event_id in seen:
            duplicates += 1  # R6.4
        else:
            seen.add(ev.event_id)
            kept.append(ev)
    return kept, rejects, duplicates


def sessionize(kept):
    """Group by user, split on gaps over 30 minutes, number per user (R7)."""
    by_user = {}
    for ev in kept:
        by_user.setdefault(ev.user, []).append(ev)

    sessions = []
    for user in sorted(by_user):
        evs = sorted(by_user[user], key=lambda e: (e.ts, e.line))
        groups = [[evs[0]]]
        for prev, cur in zip(evs, evs[1:]):
            if cur.ts - prev.ts > SESSION_GAP:  # R7.2, R7.3
                groups.append([cur])
            else:
                groups[-1].append(cur)
        for number, group in enumerate(groups, start=1):
            sessions.append(Session(
                user=user,
                number=number,
                start=group[0].ts,
                end=group[-1].ts,
                events=len(group),
                purchases=sum(1 for e in group if e.type == "purchase"),
                cents=sum(e.cents for e in group),
            ))
    return sessions


# ------------------------------------------------------ R8, R9: outputs

def fmt_ts(dt):
    """UTC timestamp without fractional seconds (truncation, R8.5)."""
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
            f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z")


def fmt_date(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def fmt_money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def sessions_csv_text(sessions):
    ordered = sorted(sessions, key=lambda s: (s.start, s.user, s.number))  # R8.3, R8.4
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")  # R8.9, R8.10
    writer.writerow(["session_id", "user", "start", "end", "duration_s",
                     "events", "purchases", "revenue"])
    for s in ordered:
        duration = (s.end - s.start) // timedelta(seconds=1)  # R7.9
        writer.writerow([s.session_id, s.user, fmt_ts(s.start), fmt_ts(s.end),
                         duration, s.events, s.purchases, fmt_money(s.cents)])
    return buf.getvalue()


def daily_summary(sessions, duplicates_dropped, rejected):
    days = {}
    for s in sessions:
        day = days.setdefault(fmt_date(s.start), {"sessions": 0, "revenue": 0, "users": {}})
        day["sessions"] += 1
        day["revenue"] += s.cents
        day["users"][s.user] = day["users"].get(s.user, 0) + s.cents

    entries = []
    for date in sorted(days):
        day = days[date]
        if day["revenue"] == 0:
            top = None  # R9.12
        else:
            # highest summed revenue; ties go to the name that sorts first (R9.10, R9.11)
            top = min(day["users"], key=lambda u: (-day["users"][u], u))
        entries.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": fmt_money(day["revenue"]),
            "top_user": top,
        })
    return {"days": entries, "duplicates_dropped": duplicates_dropped, "rejected": rejected}


def build_outputs(data):
    """Return [(file_name, bytes)] for the three outputs, in OUTPUT_NAMES order."""
    kept, rejects, duplicates = process(data)
    sessions = sessionize(kept)

    csv_bytes = sessions_csv_text(sessions).encode("utf-8")
    daily = daily_summary(sessions, duplicates, len(rejects))
    daily_bytes = (json.dumps(daily, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    rejects_bytes = "".join(
        json.dumps({"line": n, "reason": r}, separators=(",", ":")) + "\n"
        for n, r in rejects
    ).encode("utf-8")
    return [
        ("sessions.csv", csv_bytes),
        ("daily.json", daily_bytes),
        ("rejects.jsonl", rejects_bytes),
    ]


def _umask():
    current = os.umask(0)
    os.umask(current)
    return current


def write_outputs(out_dir, outputs):
    """Write temp files, then rename all three (R1.8 to R1.13)."""
    os.makedirs(out_dir, exist_ok=True)  # R1.8
    pending = []  # temp files not yet renamed
    try:
        for name, data in outputs:
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name + ".", suffix=".tmp")
            pending.append(tmp)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o666 & ~_umask())  # mkstemp creates 0600; use normal permissions
        for name, _ in outputs:
            tmp = pending[0]
            os.replace(tmp, os.path.join(out_dir, name))  # R1.11, R1.12
            pending.pop(0)
    except BaseException:
        for tmp in pending:
            try:
                os.remove(tmp)  # R1.13
            except OSError:
                pass
        raise


# ------------------------------------------------------------------ CLI

def main(argv):
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Turn clickstream events into sessions and a daily summary.")
    parser.add_argument("--in", dest="input_dir", required=True, metavar="INPUT_DIR",
                        help="directory containing events.jsonl")
    parser.add_argument("--out", dest="output_dir", required=True, metavar="OUTPUT_DIR",
                        help="directory for sessions.csv, daily.json and rejects.jsonl")
    args = parser.parse_args(argv)  # exits with 2 and usage on error (R1.7)

    path = os.path.join(args.input_dir, "events.jsonl")
    if not os.path.exists(path):  # R1.5
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        with open(path, "rb") as f:  # R1.6
            data = f.read()
    except OSError as exc:
        print(f"error: cannot read input file: {path} ({exc.strerror})", file=sys.stderr)
        return 2
    if not os.path.isfile(path):
        print(f"error: cannot read input file: {path}", file=sys.stderr)
        return 2

    try:
        outputs = build_outputs(data)
    except Exception as exc:  # programming error: no traceback in normal output
        print(f"error: unexpected failure processing {path}: {exc!r}", file=sys.stderr)
        return 1

    try:
        write_outputs(args.output_dir, outputs)
    except OSError as exc:  # R1.9
        print(f"error: cannot write output directory: {args.output_dir} ({exc.strerror or exc})",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: unexpected failure writing {args.output_dir}: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
