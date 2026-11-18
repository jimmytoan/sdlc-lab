#!/usr/bin/env python3
"""sessionize: nightly clickstream sessions batch job.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only. See the specification
in .kiro/specs/sessionize/ (requirements.md, design.md).

Exit codes: 0 success (rejects allowed), 2 usage / missing input file,
1 unreadable input, unusable output directory or unexpected failure.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Iterator, NamedTuple, Optional

if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)  # R5.9: no limit on amount length

INPUT_NAME = "events.jsonl"
OUT_NAMES = ("sessions.csv", "daily.json", "rejects.jsonl")
SESSION_GAP = timedelta(minutes=30)
ONE_SECOND = timedelta(seconds=1)
VALID_TYPES = ("view", "click", "purchase")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


# ---------------------------------------------------------------- data models

class Reject(NamedTuple):
    line_no: int
    reason: str


class ValidEvent(NamedTuple):
    line_no: int
    event_id: str
    user: str          # normalised: strip().lower()
    instant: datetime  # aware, UTC, microseconds kept
    type: str
    cents: Optional[int]  # purchases only


class Session(NamedTuple):
    session_id: str
    user: str
    number: int
    start: datetime
    end: datetime
    events: int
    purchases: int
    revenue_cents: int
    duration_s: int
    date: str  # YYYY-MM-DD of start (UTC)


class DayEntry(NamedTuple):
    date: str
    sessions: int
    users: int
    revenue_cents: int
    top_user: Optional[str]


# ---------------------------------------------------------------- reader

def _is_blank(raw: bytes) -> bool:
    try:
        return raw.decode("utf-8").strip() == ""
    except UnicodeDecodeError:
        return False


def iter_lines(fileobj) -> Iterator[tuple[int, bytes]]:
    """Yield (1-based physical line number, raw bytes) for non-blank lines (R2.1-R2.5)."""
    data = fileobj.read()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()  # file ended with \n (or is empty): no extra line
    for line_no, raw in enumerate(parts, start=1):
        if line_no == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]  # decision: tolerate a UTF-8 byte-order mark at the start of the file
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if _is_blank(raw):
            continue
        yield line_no, raw


# ---------------------------------------------------------------- validator

_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)
_AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant: {name}")


def _parse_object(raw: bytes) -> Optional[dict]:
    """bad_json stage (R2.6-R2.8). Returns the dict or None."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    # Lone surrogates from \uD800-style escapes cannot be written as UTF-8.
    for key in ("event_id", "user", "ts", "type"):
        v = obj.get(key)
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError:
                return None
    return obj


def parse_ts(value) -> Optional[datetime]:
    """Return the UTC instant of an ISO 8601 timestamp, or None for bad_ts (R4)."""
    if not isinstance(value, str):
        return None
    m = _TS_RE.fullmatch(value)
    if m is None:
        return None
    year, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    frac = m.group(7) or ""
    micro = int(frac.ljust(6, "0")) if frac else 0
    tz_text = m.group(8)
    if tz_text == "Z":
        offset = timedelta(0)
    else:
        oh, om = int(tz_text[1:3]), int(tz_text[4:6])
        if oh >= 24 or om >= 60:
            return None
        offset = timedelta(hours=oh, minutes=om)
        if tz_text[0] == "-":
            offset = -offset
    try:
        local = datetime(year, month, day, hour, minute, second, micro, tzinfo=timezone(offset))
        utc = local.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    if not (1 <= utc.year <= 9999):
        return None
    return utc


def parse_amount(value) -> Optional[int]:
    """Return integer cents for a purchase amount string, or None for bad_amount (R5)."""
    if not isinstance(value, str):
        return None
    m = _AMOUNT_RE.fullmatch(value)
    if m is None:
        return None
    int_part = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    return int_part * 100 + int(frac)


def validate(line_no: int, raw: bytes):
    """Return ValidEvent or Reject. Stages in R3.8 order."""
    obj = _parse_object(raw)
    if obj is None:
        return Reject(line_no, "bad_json")

    for key in ("event_id", "user", "ts", "type"):
        v = obj.get(key)
        if not isinstance(v, str) or v.strip() == "":
            return Reject(line_no, "missing_field")
    etype = obj["type"]
    if etype == "purchase" and obj.get("amount") is None:
        return Reject(line_no, "missing_field")

    if etype not in VALID_TYPES:
        return Reject(line_no, "bad_type")

    instant = parse_ts(obj["ts"])
    if instant is None:
        return Reject(line_no, "bad_ts")

    cents = None
    if etype == "purchase":
        cents = parse_amount(obj["amount"])
        if cents is None:
            return Reject(line_no, "bad_amount")

    return ValidEvent(line_no, obj["event_id"], obj["user"].strip().lower(), instant, etype, cents)


# ---------------------------------------------------------------- dedupe

def dedupe(valid_events):
    """Keep first valid occurrence of each event_id in file order. Returns (kept, dropped_count)."""
    seen = set()
    kept = []
    dropped = 0
    for ev in valid_events:
        if ev.event_id in seen:
            dropped += 1
            continue
        seen.add(ev.event_id)
        kept.append(ev)
    return kept, dropped


# ---------------------------------------------------------------- sessionize

def sessionize(kept) -> list:
    groups = {}
    for ev in kept:
        groups.setdefault(ev.user, []).append(ev)

    sessions = []
    for user in sorted(groups):
        evs = sorted(groups[user], key=lambda e: (e.instant, e.event_id))
        pieces = []
        current = [evs[0]]
        for prev, nxt in zip(evs, evs[1:]):
            if nxt.instant - prev.instant > SESSION_GAP:
                pieces.append(current)
                current = [nxt]
            else:
                current.append(nxt)
        pieces.append(current)

        for number, piece in enumerate(pieces, start=1):
            start = piece[0].instant
            end = piece[-1].instant
            purchases = [e for e in piece if e.type == "purchase"]
            sessions.append(Session(
                session_id=f"{user}-{number}",
                user=user,
                number=number,
                start=start,
                end=end,
                events=len(piece),
                purchases=len(purchases),
                revenue_cents=sum(e.cents for e in purchases),
                duration_s=(end - start) // ONE_SECOND,
                date=f"{start.year:04d}-{start.month:02d}-{start.day:02d}",
            ))
    return sessions


# ---------------------------------------------------------------- aggregation / formatting

def format_ts(dt: datetime) -> str:
    """YYYY-MM-DDTHH:MM:SSZ in UTC; fractional seconds truncated (R4.14, R4.15)."""
    dt = dt.astimezone(timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def format_money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def build_daily(sessions) -> list:
    by_date = {}
    for s in sessions:
        by_date.setdefault(s.date, []).append(s)

    days = []
    for date in sorted(by_date):
        group = by_date[date]
        per_user = {}
        for s in group:
            per_user[s.user] = per_user.get(s.user, 0) + s.revenue_cents
        # Highest revenue wins; ties go to the name first in string order.
        candidates = [(-rev, user) for user, rev in per_user.items() if rev > 0]
        top_user = min(candidates)[1] if candidates else None
        days.append(DayEntry(
            date=date,
            sessions=len(group),
            users=len({s.user for s in group}),
            revenue_cents=sum(s.revenue_cents for s in group),
            top_user=top_user,
        ))
    return days


def render_sessions_csv(sessions) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for s in sorted(sessions, key=lambda s: (s.start, s.user, s.number)):
        writer.writerow([
            s.session_id, s.user, format_ts(s.start), format_ts(s.end),
            s.duration_s, s.events, s.purchases, format_money(s.revenue_cents),
        ])
    return buf.getvalue().encode("utf-8")


def render_daily_json(days, duplicates_dropped: int, rejected: int) -> bytes:
    obj = {
        "days": [
            {
                "date": d.date,
                "sessions": d.sessions,
                "users": d.users,
                "revenue": format_money(d.revenue_cents),
                "top_user": d.top_user,
            }
            for d in days
        ],
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
    }
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def render_rejects_jsonl(rejects) -> bytes:
    text = "".join(
        json.dumps({"line": r.line_no, "reason": r.reason}, ensure_ascii=False) + "\n"
        for r in rejects
    )
    return text.encode("utf-8")


# ---------------------------------------------------------------- writer

def _umask_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def write_outputs(out_dir: str, files: list) -> None:
    """files: list of (final_name, bytes). Temp files first, then rename all (R12.2, R12.3)."""
    os.makedirs(out_dir, exist_ok=True)
    mode = _umask_mode()
    temps = []
    try:
        for _name, data in files:
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=".sessionize-", suffix=".tmp")
            temps.append(tmp)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
        for (name, _data), tmp in zip(files, temps):
            os.replace(tmp, os.path.join(out_dir, name))
    finally:
        for tmp in temps:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- CLI

def run(in_dir: str, out_dir: str) -> int:
    events_path = os.path.join(in_dir, INPUT_NAME)
    try:
        with open(events_path, "rb") as f:
            lines = list(iter_lines(f))
    except (FileNotFoundError, NotADirectoryError):
        print(f"sessionize: input file not found: {events_path}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"sessionize: cannot read {events_path}: {exc}", file=sys.stderr)
        return 1

    rejects = []
    valid = []
    for line_no, raw in lines:
        result = validate(line_no, raw)
        if isinstance(result, Reject):
            rejects.append(result)
        else:
            valid.append(result)

    kept, duplicates_dropped = dedupe(valid)
    sessions = sessionize(kept)
    days = build_daily(sessions)

    files = [
        ("sessions.csv", render_sessions_csv(sessions)),
        ("daily.json", render_daily_json(days, duplicates_dropped, len(rejects))),
        ("rejects.jsonl", render_rejects_jsonl(rejects)),
    ]
    try:
        write_outputs(out_dir, files)
    except OSError as exc:
        print(f"sessionize: cannot write output directory {out_dir}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Build user sessions, a daily summary and rejects from events.jsonl.",
    )
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="directory to write sessions.csv, daily.json and rejects.jsonl")
    args = parser.parse_args(argv)  # exits 2 with usage on stderr when args are missing
    try:
        return run(args.in_dir, args.out_dir)
    except Exception as exc:  # catch-all: one line on stderr, exit 1
        print(f"sessionize: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
