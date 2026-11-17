"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl (one JSON event per line) and writes to
<OUTPUT_DIR>:
    sessions.csv   one row per user session (30 minute inactivity rule)
    daily.json     per-UTC-day summary plus counters
    rejects.jsonl  one line per rejected input line

Exit codes: 0 on completion (rejected lines do not change this); 2 when
events.jsonl is missing or cannot be read/written, with a message on stderr.
Standard library only. Output is deterministic: it does not depend on the
order of input lines.
"""

import argparse
import codecs
import csv
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, getcontext
from pathlib import Path

SESSION_GAP = timedelta(minutes=30)
EVENT_TYPES = ("view", "click", "purchase")

REASON_BAD_JSON = "bad_json"
REASON_MISSING_FIELD = "missing_field"
REASON_BAD_TS = "bad_ts"
REASON_BAD_TYPE = "bad_type"
REASON_BAD_AMOUNT = "bad_amount"

SESSIONS_HEADER = "session_id,user,start,end,duration_s,events,purchases,revenue"
INPUT_FILE_NAME = "events.jsonl"
SESSIONS_FILE_NAME = "sessions.csv"
DAILY_FILE_NAME = "daily.json"
REJECTS_FILE_NAME = "rejects.jsonl"

CENT = Decimal("0.01")
# Plain decimal string: optional sign, digits with optional fraction, optional exponent.
AMOUNT_RE = re.compile(r"[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)


class BadLine(Exception):
    """Raised by the validator; carries the reject reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Event:
    event_id: str
    user: str
    ts: datetime  # timezone-aware, UTC, full precision
    etype: str
    amount: Decimal | None  # purchases only
    amount_text: str  # original text (for a stable sort key)


@dataclass
class Session:
    user: str
    first_ts: datetime
    last_ts: datetime
    events: int = 0
    purchases: int = 0
    revenue: Decimal = Decimal("0")
    number: int = 0  # per-user running number, set after building
    session_id: str = ""

    @property
    def start(self):
        return self.first_ts.replace(microsecond=0)

    @property
    def end(self):
        return self.last_ts.replace(microsecond=0)

    def add(self, ev):
        self.last_ts = ev.ts
        self.events += 1
        if ev.etype == "purchase":
            self.purchases += 1
            self.revenue += ev.amount


def _nonempty_str(value):
    return isinstance(value, str) and value.strip() != ""


def validate_line(text):
    """Validate one decoded, non-blank line. Returns an Event or raises BadLine.

    Rules are applied in order; the first failure gives the reason.
    """
    try:
        obj = json.loads(text)
    except ValueError:
        raise BadLine(REASON_BAD_JSON)
    if not isinstance(obj, dict):
        raise BadLine(REASON_BAD_JSON)

    for key in ("event_id", "user", "ts", "type"):
        if not _nonempty_str(obj.get(key)):
            raise BadLine(REASON_MISSING_FIELD)
    etype = obj["type"]
    if etype == "purchase" and obj.get("amount") is None:
        raise BadLine(REASON_MISSING_FIELD)

    try:
        ts = datetime.fromisoformat(obj["ts"].strip())
        if ts.utcoffset() is None:
            raise BadLine(REASON_BAD_TS)
        ts = ts.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise BadLine(REASON_BAD_TS)

    if etype not in EVENT_TYPES:
        raise BadLine(REASON_BAD_TYPE)

    amount = None
    amount_text = ""
    if etype == "purchase":
        raw = obj["amount"]
        if not isinstance(raw, str) or AMOUNT_RE.fullmatch(raw) is None:
            raise BadLine(REASON_BAD_AMOUNT)
        try:
            value = Decimal(raw)
            if not value.is_finite() or value < 0:
                raise BadLine(REASON_BAD_AMOUNT)
            # Make sure the value can be rounded to cents later on.
            value.quantize(CENT, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            raise BadLine(REASON_BAD_AMOUNT)
        amount = value
        amount_text = raw

    return Event(
        event_id=obj["event_id"],
        user=obj["user"].strip().lower(),
        ts=ts,
        etype=etype,
        amount=amount,
        amount_text=amount_text,
    )


def read_events(path):
    """Read events.jsonl. Returns (valid events, rejects as (line_no, reason))."""
    data = path.read_bytes()
    events = []
    rejects = []
    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        if line_no == 1 and raw.startswith(codecs.BOM_UTF8):
            raw = raw[len(codecs.BOM_UTF8):]
        if raw.strip() == b"":
            continue  # blank lines are ignored, not rejects
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append((line_no, REASON_BAD_JSON))
            continue
        try:
            events.append(validate_line(text))
        except BadLine as exc:
            rejects.append((line_no, exc.reason))
    return events, rejects


def dedupe(events):
    """Order events by a fixed total key and keep the first per event_id.

    Returns (kept events in time order, number of duplicates dropped).
    """
    ordered = sorted(events, key=lambda e: (e.ts, e.user, e.event_id, e.etype, e.amount_text))
    seen = set()
    kept = []
    dropped = 0
    for ev in ordered:
        if ev.event_id in seen:
            dropped += 1
        else:
            seen.add(ev.event_id)
            kept.append(ev)
    return kept, dropped


def sessionize(events):
    """Split each user's time-ordered events on gaps > SESSION_GAP."""
    by_user = {}
    for ev in events:  # events are already in time order
        by_user.setdefault(ev.user, []).append(ev)

    sessions = []
    for user in sorted(by_user):
        current = None
        number = 0
        for ev in by_user[user]:
            if current is None or ev.ts - current.last_ts > SESSION_GAP:
                number += 1
                current = Session(user=user, first_ts=ev.ts, last_ts=ev.ts, number=number)
                current.session_id = f"{user}-{number}"
                sessions.append(current)
            current.add(ev)
    sessions.sort(key=lambda s: (s.start, s.user, s.number))
    return sessions


def money(value):
    """Exactly two decimal places, ROUND_HALF_UP, plain notation."""
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def fmt_ts(dt_value):
    return dt_value.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_days(sessions):
    """One entry per UTC date on which a session starts, in date order."""
    by_day = {}
    for s in sessions:
        date = s.start.date().isoformat()  # start is UTC
        day = by_day.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                       "user_revenue": {}, "purchasers": set()})
        day["sessions"] += 1
        day["users"].add(s.user)
        day["revenue"] += s.revenue
        if s.purchases > 0:
            day["purchasers"].add(s.user)
            day["user_revenue"][s.user] = day["user_revenue"].get(s.user, Decimal("0")) + s.revenue

    days = []
    for date in sorted(by_day):
        day = by_day[date]
        # Highest revenue wins; ties go to the alphabetically lower name.
        # A user is a candidate if they have at least one purchase event that day.
        top_user = None
        if day["purchasers"]:
            top_user = min(day["purchasers"],
                           key=lambda u: (-day["user_revenue"].get(u, Decimal("0")), u))
        days.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": money(day["revenue"]),
            "top_user": top_user,
        })
    return days


def _umask():
    current = os.umask(0)
    os.umask(current)
    return current


def atomic_write(out_dir, name, text):
    """Write text to a temp file in out_dir, then replace the target atomically."""
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, 0o666 & ~_umask())
        os.replace(tmp, os.path.join(out_dir, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_sessions(sessions):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SESSIONS_HEADER.split(","))
    for s in sessions:
        start, end = s.start, s.end
        writer.writerow([
            s.session_id,
            s.user,
            fmt_ts(start),
            fmt_ts(end),
            int((end - start).total_seconds()),
            s.events,
            s.purchases,
            money(s.revenue),
        ])
    return buf.getvalue()


def render_daily(days, duplicates_dropped, rejected):
    obj = {
        "days": days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
    }
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def render_rejects(rejects):
    return "".join(json.dumps({"line": n, "reason": r}) + "\n" for n, r in rejects)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="folder to write outputs to (created if missing)")
    args = parser.parse_args(argv)

    getcontext().prec = 80

    in_path = Path(args.in_dir) / INPUT_FILE_NAME
    if not os.path.exists(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    try:
        events, rejects = read_events(in_path)
    except OSError as exc:
        print(f"error: cannot read {in_path}: {exc}", file=sys.stderr)
        return 2

    valid, duplicates_dropped = dedupe(events)
    sessions = sessionize(valid)
    days = build_days(sessions)

    try:
        os.makedirs(args.out_dir, exist_ok=True)
        atomic_write(args.out_dir, SESSIONS_FILE_NAME, render_sessions(sessions))
        atomic_write(args.out_dir, DAILY_FILE_NAME,
                     render_daily(days, duplicates_dropped, len(rejects)))
        atomic_write(args.out_dir, REJECTS_FILE_NAME, render_rejects(rejects))
    except OSError as exc:
        print(f"error: cannot write outputs to {args.out_dir}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
