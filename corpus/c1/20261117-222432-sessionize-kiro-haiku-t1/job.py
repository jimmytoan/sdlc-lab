#!/usr/bin/env python3
"""sessionize: nightly batch job turning raw clickstream events into sessions.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only, no network access.
Exit codes: 0 success, 2 input or usage problem, 1 output failure.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GAP_SECONDS = 1800
KNOWN_TYPES = ("view", "click", "purchase")
BOM = b"\xef\xbb\xbf"
INPUT_FILE_NAME = "events.jsonl"
OUTPUT_NAMES = ("sessions.csv", "daily.json", "rejects.jsonl")
SESSIONS_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]

# ASCII digits only; fractional seconds are matched and then discarded.
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
AMOUNT_RE = re.compile(r"[0-9]*\.[0-9]{1,2}|[0-9]+")


class InputError(Exception):
    """A problem with the input directory or input file (exit code 2)."""


@dataclass(frozen=True)
class Event:
    line: int
    event_id: str
    user: str
    ts: datetime
    type: str
    amount_cents: int | None


@dataclass(frozen=True)
class Reject:
    line: int
    reason: str


@dataclass
class Session:
    session_id: str
    user: str
    number: int
    start: datetime
    end: datetime
    events: int
    purchases: int
    revenue_cents: int


def _err(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- input

def check_input(input_dir: str) -> str:
    """Validate the input location and return the path of events.jsonl."""
    if not os.path.exists(input_dir):
        raise InputError(f"error: input directory does not exist: {input_dir}")
    if not os.path.isdir(input_dir):
        raise InputError(f"error: input path is not a directory: {input_dir}")
    path = os.path.join(input_dir, INPUT_FILE_NAME)
    if not os.path.exists(path):
        raise InputError(f"error: missing input file: {path}")
    if not os.path.isfile(path):
        raise InputError(f"error: input is not a regular file: {path}")
    return path


def read_input(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise InputError(f"error: cannot read input file {path}: {e}") from e


def read_lines(data: bytes):
    """Yield (line_no, raw_bytes) for each physical line, numbered from 1."""
    if not data:
        return
    parts = data.split(b"\n")
    if parts[-1] == b"":
        # The text after the final "\n" is not a physical line.
        parts.pop()
    for line_no, raw in enumerate(parts, 1):
        if line_no == 1 and raw.startswith(BOM):
            raw = raw[len(BOM):]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        yield line_no, raw


def process_line(line_no: int, raw: bytes) -> Event | Reject | None:
    """Return None for a blank line, otherwise an Event or a Reject."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Reject(line_no, "bad_json")
    if text.strip() == "":
        return None
    return validate_line(line_no, text)


# ---------------------------------------------------------------- validation

def _reject_constant(name: str):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_ts(value: object) -> datetime | None:
    """Return the UTC datetime (whole seconds) for a valid ts value, else None."""
    if not isinstance(value, str):
        return None
    m = TS_RE.fullmatch(value)
    if m is None:
        return None
    try:
        local = datetime(*(int(g) for g in m.groups()[:6]))
    except ValueError:
        return None
    tz = m.group(7)
    if tz == "Z":
        offset = timedelta(0)
    else:
        hh, mm = int(tz[1:3]), int(tz[4:6])
        if hh > 23 or mm > 59:
            return None
        offset = timedelta(hours=hh, minutes=mm)
        if tz[0] == "-":
            offset = -offset
    try:
        utc = local - offset
    except OverflowError:
        return None
    return utc.replace(tzinfo=timezone.utc)


def parse_amount(value: str) -> int:
    """Convert a pattern-checked amount string to integer cents."""
    if "." in value:
        whole, frac = value.split(".", 1)
        return int(whole or "0") * 100 + int(frac.ljust(2, "0"))
    return int(value) * 100


def validate_line(line_no: int, text: str) -> Event | Reject:
    # 1. JSON parse
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return Reject(line_no, "bad_json")

    # 2. Object shape
    if not isinstance(obj, dict):
        return Reject(line_no, "missing_field")

    # 3. Required fields
    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            return Reject(line_no, "missing_field")
    event_id = obj["event_id"]
    user = obj["user"]
    if not isinstance(event_id, str) or event_id.strip() == "":
        return Reject(line_no, "missing_field")
    if not isinstance(user, str) or user.strip() == "":
        return Reject(line_no, "missing_field")
    # Strings that cannot be encoded as UTF-8 (lone surrogate escapes) cannot be written out.
    try:
        event_id.encode("utf-8")
        user.encode("utf-8")
    except UnicodeEncodeError:
        return Reject(line_no, "bad_json")
    if obj["type"] == "purchase" and obj.get("amount") is None:
        return Reject(line_no, "missing_field")

    # 4. ts
    ts = parse_ts(obj["ts"])
    if ts is None:
        return Reject(line_no, "bad_ts")

    # 5. type
    etype = obj["type"]
    if not isinstance(etype, str) or etype not in KNOWN_TYPES:
        return Reject(line_no, "bad_type")

    # 6. amount (purchases only)
    amount_cents = None
    if etype == "purchase":
        amount = obj["amount"]
        if not isinstance(amount, str) or AMOUNT_RE.fullmatch(amount) is None:
            return Reject(line_no, "bad_amount")
        amount_cents = parse_amount(amount)

    return Event(line_no, event_id, user.strip().lower(), ts, etype, amount_cents)


# ---------------------------------------------------------------- aggregation

def dedupe(events: list[Event]) -> tuple[list[Event], int]:
    """Keep the first event per exact event_id (file order); count the rest."""
    seen: set[str] = set()
    kept: list[Event] = []
    dropped = 0
    for e in events:
        if e.event_id in seen:
            dropped += 1
        else:
            seen.add(e.event_id)
            kept.append(e)
    return kept, dropped


def sessionize(events: list[Event]) -> list[Session]:
    by_user: dict[str, list[Event]] = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)

    sessions: list[Session] = []
    for user in sorted(by_user):
        ordered = sorted(by_user[user], key=lambda e: (e.ts, e.event_id))
        groups: list[list[Event]] = []
        current: list[Event] = []
        prev_ts: datetime | None = None
        for e in ordered:
            if current and (e.ts - prev_ts).total_seconds() > GAP_SECONDS:
                groups.append(current)
                current = []
            current.append(e)
            prev_ts = e.ts
        if current:
            groups.append(current)

        for number, group in enumerate(groups, 1):
            start = group[0].ts
            end = group[-1].ts
            purchases = [g for g in group if g.type == "purchase"]
            sessions.append(Session(
                session_id=f"{user}-{number}",
                user=user,
                number=number,
                start=start,
                end=end,
                events=len(group),
                purchases=len(purchases),
                revenue_cents=sum(g.amount_cents for g in purchases),
            ))

    sessions.sort(key=lambda s: (s.start, s.user, s.number))
    return sessions


def _fmt_money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _fmt_date(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def _fmt_ts(dt: datetime) -> str:
    return f"{_fmt_date(dt)}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def build_daily(sessions: list[Session], duplicates_dropped: int, rejected: int) -> dict:
    by_day: dict[str, list[Session]] = {}
    for s in sessions:
        by_day.setdefault(_fmt_date(s.start), []).append(s)

    days = []
    for date in sorted(by_day):
        day_sessions = by_day[date]
        revenue_by_user: dict[str, int] = {}
        for s in day_sessions:
            revenue_by_user[s.user] = revenue_by_user.get(s.user, 0) + s.revenue_cents
        candidates = [(u, r) for u, r in revenue_by_user.items() if r > 0]
        top_user = min(candidates, key=lambda ur: (-ur[1], ur[0]))[0] if candidates else None
        days.append({
            "date": date,
            "sessions": len(day_sessions),
            "users": len({s.user for s in day_sessions}),
            "revenue": _fmt_money(sum(s.revenue_cents for s in day_sessions)),
            "top_user": top_user,
        })

    return {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejected}


# ---------------------------------------------------------------- rendering

def render_outputs(sessions: list[Session], daily: dict, rejects: list[Reject]) -> dict[str, bytes]:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SESSIONS_HEADER)
    for s in sessions:
        writer.writerow([
            s.session_id,
            s.user,
            _fmt_ts(s.start),
            _fmt_ts(s.end),
            int((s.end - s.start).total_seconds()),
            s.events,
            s.purchases,
            _fmt_money(s.revenue_cents),
        ])
    sessions_csv = buf.getvalue().encode("utf-8")

    daily_json = (json.dumps(daily, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    rejects_text = "".join(
        json.dumps({"line": r.line, "reason": r.reason}) + "\n"
        for r in sorted(rejects, key=lambda r: r.line)
    )
    rejects_jsonl = rejects_text.encode("utf-8")

    return {
        "sessions.csv": sessions_csv,
        "daily.json": daily_json,
        "rejects.jsonl": rejects_jsonl,
    }


# ---------------------------------------------------------------- output

def write_outputs(out_dir: str, files: dict[str, bytes]) -> None:
    """Write all files via temporary files, then replace. Raises OSError on failure."""
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isdir(out_dir):
        raise OSError(f"output path is not a directory: {out_dir}")

    temps: list[tuple[str, str]] = []  # (temp path, final path) not yet replaced
    try:
        for name in OUTPUT_NAMES:
            final = os.path.join(out_dir, name)
            tmp = os.path.join(out_dir, f".{name}.{uuid.uuid4().hex}.tmp")
            temps.append((tmp, final))
            with open(tmp, "xb") as f:
                f.write(files[name])
                f.flush()
                os.fsync(f.fileno())
        while temps:
            tmp, final = temps[0]
            os.replace(tmp, final)
            temps.pop(0)
    except BaseException:
        for tmp, _ in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Build clickstream sessions, a daily summary and rejects from events.jsonl.",
    )
    parser.add_argument("--in", dest="input_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="output_dir", required=True, help="output directory")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    try:
        events_path = check_input(args.input_dir)
        data = read_input(events_path)
    except InputError as e:
        _err(str(e))
        return 2

    try:
        valid: list[Event] = []
        rejects: list[Reject] = []
        for line_no, raw in read_lines(data):
            result = process_line(line_no, raw)
            if result is None:
                continue
            if isinstance(result, Reject):
                rejects.append(result)
            else:
                valid.append(result)
        events, duplicates = dedupe(valid)
        sessions = sessionize(events)
        daily = build_daily(sessions, duplicates, len(rejects))
        files = render_outputs(sessions, daily, rejects)
    except Exception as e:  # unexpected failure while computing outputs
        _err(f"error: failed to build outputs: {type(e).__name__}: {e}")
        return 1

    try:
        write_outputs(args.output_dir, files)
    except Exception as e:  # OSError and anything unexpected on the output side
        _err(f"error: cannot write outputs to {args.output_dir}: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
