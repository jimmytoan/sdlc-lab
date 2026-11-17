#!/usr/bin/env python3
"""sessionize: nightly clickstream batch job.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only (Python 3.13).
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

# Lift the int<->str digit limit so that arbitrarily long decimal fractions
# and amounts are handled exactly (Requirements 4.15, 5.8).
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
GAP_LIMIT_S = 1800                      # Requirement 7.3
INPUT_NAME = "events.jsonl"
OUTPUT_NAMES = ("rejects.jsonl", "daily.json", "sessions.csv")
VALID_TYPES = ("view", "click", "purchase")
REJECT_REASONS = ("bad_json", "missing_field", "bad_ts", "bad_type", "bad_amount")

_EPOCH = datetime(1970, 1, 1)           # naive; all instants are UTC
_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_AMOUNT_RE = re.compile(r"(?P<whole>[0-9]+)(?:\.(?P<frac>[0-9]{1,2}))?")


# ---------------------------------------------------------------- data types

@dataclass(frozen=True)
class Instant:
    """An exact UTC instant: whole seconds since the epoch (floor) plus an
    exact fraction in [0, 1)."""
    whole: int
    frac: Fraction

    def key(self):
        return (self.whole, self.frac)

    def _dt(self) -> datetime:
        return _EPOCH + timedelta(seconds=self.whole)

    def truncated_iso(self) -> str:
        d = self._dt()
        return (f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
                f"T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z")

    def utc_date(self) -> str:
        d = self._dt()
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


@dataclass(frozen=True)
class Event:
    line: int
    event_id: str
    user: str
    instant: Instant
    type: str
    cents: int | None   # purchases only


@dataclass(frozen=True)
class Reject:
    line: int
    reason: str


@dataclass(frozen=True)
class Session:
    user: str
    number: int
    start: Instant
    end: Instant
    events: int
    purchases: int
    revenue_cents: int

    @property
    def session_id(self) -> str:
        return f"{self.user}-{self.number}"

    def duration_s(self) -> int:
        # Floor of the exact difference (Requirements 7.11, 7.12).
        diff = Fraction(self.end.whole - self.start.whole) + self.end.frac - self.start.frac
        return math.floor(diff)


# ---------------------------------------------------------------- parsers

def parse_ts(text: str) -> Instant | None:
    """Parse an ISO 8601 timestamp with a mandatory UTC offset (Requirement 5).
    Returns None for any value that must be rejected as bad_ts."""
    m = _TS_RE.fullmatch(text)
    if m is None:
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    digits, off = m.group(7), m.group(8)
    try:
        local = datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    if off == "Z":
        offset_s = 0
    else:
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        offset_s = sign * (oh * 3600 + om * 60)
    try:
        utc = local - timedelta(seconds=offset_s)
    except OverflowError:
        return None
    whole = (utc - _EPOCH) // timedelta(seconds=1)
    frac = Fraction(int(digits), 10 ** len(digits)) if digits else Fraction(0)
    return Instant(whole=whole, frac=frac)


def parse_amount(text: str) -> int | None:
    """Parse a purchase amount into integer cents (Requirements 4.15-4.19)."""
    m = _AMOUNT_RE.fullmatch(text)
    if m is None:
        return None
    frac = m.group("frac") or ""
    return int(m.group("whole")) * 100 + int(frac.ljust(2, "0"))


def _reject_constant(name):
    raise ValueError(f"non-finite constant {name}")


def _has_surrogate(value) -> bool:
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in v):
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def validate_line(line_no: int, raw: bytes) -> Event | Reject | None:
    """Validate one physical line. Returns None for a whitespace-only line,
    a Reject with the first applicable reason, or a valid Event."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Reject(line_no, "bad_json")
    if text.strip() == "":
        return None
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return Reject(line_no, "bad_json")
    if not isinstance(obj, dict):
        return Reject(line_no, "bad_json")
    if _has_surrogate(obj):
        # Cannot be encoded as UTF-8 later; reject here so the run is not stopped.
        return Reject(line_no, "bad_json")

    # missing_field (Requirements 4.1-4.6)
    for name in ("event_id", "user", "ts", "type"):
        if obj.get(name) is None:
            return Reject(line_no, "missing_field")
    if obj["type"] == "purchase" and obj.get("amount") is None:
        return Reject(line_no, "missing_field")
    event_id = obj["event_id"]
    if not isinstance(event_id, str) or event_id == "":
        return Reject(line_no, "missing_field")
    user = obj["user"]
    if not isinstance(user, str) or user.strip() == "":
        return Reject(line_no, "missing_field")

    # bad_type (Requirements 4.9, 4.10)
    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        return Reject(line_no, "bad_type")

    # bad_ts (Requirements 4.11, 4.12)
    ts = obj["ts"]
    instant = parse_ts(ts) if isinstance(ts, str) else None
    if instant is None:
        return Reject(line_no, "bad_ts")

    # bad_amount (Requirements 4.2, 4.13-4.18)
    cents = None
    if etype == "purchase":
        amount = obj["amount"]
        cents = parse_amount(amount) if isinstance(amount, str) else None
        if cents is None:
            return Reject(line_no, "bad_amount")

    return Event(line=line_no, event_id=event_id, user=user.strip().lower(),
                 instant=instant, type=etype, cents=cents)


# ---------------------------------------------------------------- pipeline

def split_lines(data: bytes) -> list[tuple[int, bytes]]:
    """Split raw bytes into (line_number, raw_line) pairs (Requirements 2.2-2.4).
    A line ends at LF; a CR is removed only when it directly precedes LF."""
    pieces = data.split(b"\n")
    if pieces[-1] == b"":
        pieces.pop()
    out = []
    last = len(pieces) - 1
    for idx, raw in enumerate(pieces):
        terminated = idx < last or data.endswith(b"\n")
        if terminated and raw.endswith(b"\r"):
            raw = raw[:-1]
        out.append((idx + 1, raw))
    return out


def dedupe(events: list[Event]) -> tuple[list[Event], int]:
    """Keep the first valid event per event_id, in file order (Requirement 6)."""
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


def _gap(later: Instant, earlier: Instant) -> Fraction:
    return Fraction(later.whole - earlier.whole) + later.frac - earlier.frac


def sessionize(events: list[Event]) -> list[Session]:
    """Group events per user into sessions split by gaps over 1800 s (Requirement 7)."""
    by_user: dict[str, list[Event]] = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)

    sessions: list[Session] = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: e.instant.key())
        groups: list[list[Event]] = [[evs[0]]]
        for prev, cur in zip(evs, evs[1:]):
            if _gap(cur.instant, prev.instant) > GAP_LIMIT_S:
                groups.append([cur])
            else:
                groups[-1].append(cur)
        for number, grp in enumerate(groups, start=1):
            purchases = [e for e in grp if e.type == "purchase"]
            sessions.append(Session(
                user=user,
                number=number,
                start=grp[0].instant,
                end=grp[-1].instant,
                events=len(grp),
                purchases=len(purchases),
                revenue_cents=sum(e.cents for e in purchases),
            ))
    sessions.sort(key=lambda s: (s.start.key(), s.user, s.number))
    return sessions


def daily_summary(sessions: list[Session]) -> list[dict]:
    """One entry per UTC session-start date, sorted by date (Requirement 10)."""
    by_day: dict[str, list[Session]] = {}
    for s in sessions:
        by_day.setdefault(s.start.utc_date(), []).append(s)
    days = []
    for date in sorted(by_day):
        group = by_day[date]
        totals: dict[str, int] = {}
        for s in group:
            totals[s.user] = totals.get(s.user, 0) + s.revenue_cents
        revenue = sum(s.revenue_cents for s in group)
        if revenue == 0:
            top = None
        else:
            # Greatest revenue; ties go to the first name in string order.
            top = min(totals.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        days.append({
            "date": date,
            "sessions": len(group),
            "users": len(totals),
            "revenue_cents": revenue,
            "top_user": top,
        })
    return days


def _fmt_money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def render_sessions_csv(sessions: list[Session]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["session_id", "user", "start", "end", "duration_s",
                     "events", "purchases", "revenue"])
    for s in sorted(sessions, key=lambda s: (s.start.truncated_iso(), s.user, s.number)):
        writer.writerow([s.session_id, s.user, s.start.truncated_iso(),
                         s.end.truncated_iso(), s.duration_s(), s.events,
                         s.purchases, _fmt_money(s.revenue_cents)])
    return buf.getvalue().encode("utf-8")


def render_daily_json(days: list[dict], duplicates_dropped: int, rejected: int) -> bytes:
    doc = {
        "days": [{
            "date": d["date"],
            "sessions": d["sessions"],
            "users": d["users"],
            "revenue": _fmt_money(d["revenue_cents"]),
            "top_user": d["top_user"],
        } for d in days],
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
    }
    return (json.dumps(doc, ensure_ascii=False) + "\n").encode("utf-8")


def render_rejects_jsonl(rejects: list[Reject]) -> bytes:
    return "".join(json.dumps({"line": r.line, "reason": r.reason}) + "\n"
                   for r in rejects).encode("utf-8")


def build_outputs(data: bytes) -> dict[str, bytes]:
    """Run every stage in memory. Nothing is written here."""
    rejects: list[Reject] = []
    events: list[Event] = []
    for line_no, raw in split_lines(data):
        result = validate_line(line_no, raw)
        if result is None:
            continue
        if isinstance(result, Reject):
            rejects.append(result)
        else:
            events.append(result)
    kept, duplicates = dedupe(events)
    sessions = sessionize(kept)
    days = daily_summary(sessions)
    return {
        "rejects.jsonl": render_rejects_jsonl(rejects),
        "daily.json": render_daily_json(days, duplicates, len(rejects)),
        "sessions.csv": render_sessions_csv(sessions),
    }


def _umask_mode() -> int:
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _write_temp(out_dir: Path, name: str, content: bytes) -> str:
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, _umask_mode())
    except BaseException:
        _unlink_quiet(tmp)
        raise
    return tmp


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def write_outputs(out_dir: Path, files: dict[str, bytes]) -> None:
    """Write the three files via temp files and os.replace (Requirements 1.7,
    1.10, 1.11). If anything fails, temp files are removed and any final file
    already replaced is restored to its previous state."""
    out_dir.mkdir(parents=True, exist_ok=True)
    temps: dict[str, str] = {}
    previous: dict[str, bytes | None] = {}
    replaced: list[str] = []
    try:
        for name in OUTPUT_NAMES:
            temps[name] = _write_temp(out_dir, name, files[name])
        for name in OUTPUT_NAMES:
            final = out_dir / name
            previous[name] = final.read_bytes() if final.exists() else None
        for name in OUTPUT_NAMES:
            os.replace(temps[name], out_dir / name)
            replaced.append(name)
    except BaseException:
        for name in reversed(replaced):
            final = out_dir / name
            try:
                if previous[name] is None:
                    _unlink_quiet(str(final))
                else:
                    restore = _write_temp(out_dir, name, previous[name])
                    os.replace(restore, final)
            except OSError:
                pass
        for tmp in temps.values():
            _unlink_quiet(tmp)
        raise


def run(in_dir, out_dir) -> int:
    in_path = os.path.join(os.fspath(in_dir), INPUT_NAME)
    if not os.path.exists(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return EXIT_USAGE
    if not os.path.isfile(in_path):
        print(f"error: input is not a regular file: {in_path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        with open(in_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        print(f"error: cannot read input file {in_path}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        files = build_outputs(data)
        write_outputs(Path(os.fspath(out_dir)), files)
    except OSError as exc:
        print(f"error: cannot write output to {out_dir}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # unexpected failure: stop, leave no output
        print(f"error: internal failure: {exc!r}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Build sessions, daily summary and rejects from events.jsonl.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="directory to write the three output files to")
    args = parser.parse_args(argv)   # exits 2 with usage on stderr if missing
    return run(args.in_dir, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
