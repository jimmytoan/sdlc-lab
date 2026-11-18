"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only. Exit codes:
0 success, 1 output or internal failure, 2 input problem or bad usage.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field

# ---------------------------------------------------------------- constants

EXIT_OK = 0
EXIT_OUTPUT_ERROR = 1
EXIT_INPUT_ERROR = 2

INPUT_NAME = "events.jsonl"
SESSIONS_NAME = "sessions.csv"
DAILY_NAME = "daily.json"
REJECTS_NAME = "rejects.jsonl"
OUTPUT_ORDER = (SESSIONS_NAME, DAILY_NAME, REJECTS_NAME)

REASON_BAD_JSON = "bad_json"
REASON_MISSING = "missing_field"
REASON_BAD_TS = "bad_ts"
REASON_BAD_TYPE = "bad_type"
REASON_BAD_AMOUNT = "bad_amount"

VALID_TYPES = ("view", "click", "purchase")
SESSION_GAP_US = 1_800_000_000  # 30 minutes in microseconds
US_PER_S = 1_000_000
SECS_PER_DAY = 86_400

BOM = b"\xef\xbb\xbf"
BLANK_BYTES = b" \t\r"

CSV_HEADER = ["session_id", "user", "start", "end", "duration_s",
              "events", "purchases", "revenue"]


class InputError(Exception):
    """Problem with the input file (exit code 2)."""


class OutputError(Exception):
    """Problem creating or writing the output (exit code 1)."""


# ---------------------------------------------------------------- time helpers

_TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,6}))?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)


def _is_leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _days_in_month(y, m):
    if m == 2:
        return 29 if _is_leap(y) else 28
    return 30 if m in (4, 6, 9, 11) else 31


def days_from_civil(y, m, d):
    """Days since 1970-01-01 for a proleptic Gregorian date (integer only)."""
    y -= 1 if m <= 2 else 0
    era = y // 400
    yoe = y - era * 400
    mp = (m + 9) % 12
    doy = (153 * mp + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def civil_from_days(z):
    """Inverse of days_from_civil: returns (year, month, day)."""
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + 1 if m <= 2 else y), m, d


def parse_ts(text):
    """Return exact UTC instant in integer microseconds, or None if invalid."""
    mt = _TS_RE.fullmatch(text)
    if not mt:
        return None
    y, mo, d, hh, mi, ss = (int(mt.group(i)) for i in range(1, 7))
    frac = mt.group(7) or ""
    off = mt.group(8)
    if y < 1 or mo < 1 or mo > 12:
        return None
    if d < 1 or d > _days_in_month(y, mo):
        return None
    if hh > 23 or mi > 59 or ss > 59:
        return None
    if off == "Z":
        off_s = 0
    else:
        sign = -1 if off[0] == "-" else 1
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        off_s = sign * (oh * 3600 + om * 60)
    frac_us = int(frac.ljust(6, "0")) if frac else 0
    secs = days_from_civil(y, mo, d) * SECS_PER_DAY + hh * 3600 + mi * 60 + ss - off_s
    return secs * US_PER_S + frac_us


def whole_seconds(us):
    """Truncate (floor) microseconds to whole seconds."""
    return us // US_PER_S


def format_ts(us):
    """Render an instant as YYYY-MM-DDTHH:MM:SSZ, fraction truncated."""
    secs = whole_seconds(us)
    days, rem = divmod(secs, SECS_PER_DAY)
    y, m, d = civil_from_days(days)
    hh, rem = divmod(rem, 3600)
    mi, ss = divmod(rem, 60)
    return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mi:02d}:{ss:02d}Z"


def format_date(days):
    y, m, d = civil_from_days(days)
    return f"{y:04d}-{m:02d}-{d:02d}"


# ---------------------------------------------------------------- money helpers

_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


def parse_amount(value):
    """Return integer cents for a valid amount string, else None."""
    if not isinstance(value, str):
        return None
    if not _AMOUNT_RE.fullmatch(value):
        return None
    whole, _, frac = value.partition(".")
    return int(whole) * 100 + int(frac.ljust(2, "0") or "0")


def format_cents(c):
    return f"{c // 100}.{c % 100:02d}"


# ---------------------------------------------------------------- reading input

def read_lines(data):
    """Yield (line_no, content) for each physical line; content has no line ending.

    A UTF-8 BOM at the start of the data is removed. A final line without a
    newline is a normal line; a trailing newline does not create an extra line.
    """
    if data.startswith(BOM):
        data = data[len(BOM):]
    if data == b"":
        return
    parts = data.split(b"\n")
    if parts[-1] == b"":
        parts.pop()
    for idx, content in enumerate(parts):
        if content.endswith(b"\r"):
            content = content[:-1]
        yield idx + 1, content


def is_blank(content):
    return content.strip(BLANK_BYTES) == b""


# ---------------------------------------------------------------- parsing and validation

@dataclass(frozen=True)
class Event:
    line_no: int
    event_id: str
    user: str
    instant_us: int
    type: str
    amount_cents: int = None


@dataclass(frozen=True)
class Reject:
    line_no: int
    reason: str


class _NonStandardConstant(ValueError):
    pass


def _reject_constant(name):
    raise _NonStandardConstant(name)


def _is_blank_str(v):
    return isinstance(v, str) and v.strip() == ""


def _encodable(s):
    try:
        s.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def parse_event(line_no, content):
    """Return an Event or a Reject for one non-blank physical line."""
    # bad_json
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return Reject(line_no, REASON_BAD_JSON)
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return Reject(line_no, REASON_BAD_JSON)
    if not isinstance(obj, dict):
        return Reject(line_no, REASON_BAD_JSON)
    for key in ("event_id", "user", "ts", "type", "amount"):
        v = obj.get(key)
        if isinstance(v, str) and not _encodable(v):
            # Lone surrogates cannot be written as UTF-8 output.
            return Reject(line_no, REASON_BAD_JSON)

    # missing_field
    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    etype = obj.get("type")
    for v in (event_id, user, ts, etype):
        if v is None or _is_blank_str(v):
            return Reject(line_no, REASON_MISSING)
    for v in (event_id, user, etype):
        if not isinstance(v, str):
            return Reject(line_no, REASON_MISSING)
    if etype == "purchase" and obj.get("amount") is None:
        return Reject(line_no, REASON_MISSING)

    # bad_ts
    if not isinstance(ts, str):
        return Reject(line_no, REASON_BAD_TS)
    instant = parse_ts(ts)
    if instant is None:
        return Reject(line_no, REASON_BAD_TS)

    # bad_type
    if etype not in VALID_TYPES:
        return Reject(line_no, REASON_BAD_TYPE)

    # bad_amount
    amount_cents = None
    if etype == "purchase":
        amount_cents = parse_amount(obj.get("amount"))
        if amount_cents is None:
            return Reject(line_no, REASON_BAD_AMOUNT)

    return Event(
        line_no=line_no,
        event_id=event_id,
        user=user.strip().lower(),
        instant_us=instant,
        type=etype,
        amount_cents=amount_cents,
    )


# ---------------------------------------------------------------- dedup

class Deduplicator:
    def __init__(self):
        self._seen = set()
        self.dropped = 0

    def keep(self, event):
        if event.event_id in self._seen:
            self.dropped += 1
            return False
        self._seen.add(event.event_id)
        return True


# ---------------------------------------------------------------- sessionize

@dataclass(frozen=True)
class Session:
    user: str
    number: int
    start_us: int
    end_us: int
    events: int
    purchases: int
    revenue_cents: int

    @property
    def session_id(self):
        return f"{self.user}-{self.number}"

    @property
    def duration_s(self):
        return whole_seconds(self.end_us) - whole_seconds(self.start_us)


def sessionize(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e.user, []).append(e)
    sessions = []
    for user in sorted(by_user):
        evs = sorted(by_user[user], key=lambda e: (e.instant_us, e.line_no))
        groups = [[evs[0]]]
        for prev, cur in zip(evs, evs[1:]):
            if cur.instant_us - prev.instant_us > SESSION_GAP_US:
                groups.append([cur])
            else:
                groups[-1].append(cur)
        for number, group in enumerate(groups, start=1):
            purchases = [e for e in group if e.type == "purchase"]
            sessions.append(Session(
                user=user,
                number=number,
                start_us=group[0].instant_us,
                end_us=group[-1].instant_us,
                events=len(group),
                purchases=len(purchases),
                revenue_cents=sum(e.amount_cents for e in purchases),
            ))
    return sessions


# ---------------------------------------------------------------- aggregation

def aggregate_daily(sessions):
    """Return a list of day dicts sorted by date (only days with sessions)."""
    days = {}
    for s in sessions:
        day = whole_seconds(s.start_us) // SECS_PER_DAY
        d = days.setdefault(day, {"sessions": 0, "users": set(),
                                  "revenue": 0, "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s.user)
        d["revenue"] += s.revenue_cents
        d["per_user"][s.user] = d["per_user"].get(s.user, 0) + s.revenue_cents
    result = []
    for day in sorted(days):
        d = days[day]
        candidates = [(-rev, name) for name, rev in d["per_user"].items() if rev > 0]
        top = min(candidates)[1] if candidates else None
        result.append({
            "date": format_date(day),
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": format_cents(d["revenue"]),
            "top_user": top,
        })
    return result


# ---------------------------------------------------------------- rendering

def render_sessions_csv(sessions):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    ordered = sorted(sessions, key=lambda s: (whole_seconds(s.start_us), s.user, s.number))
    for s in ordered:
        writer.writerow([
            s.session_id,
            s.user,
            format_ts(s.start_us),
            format_ts(s.end_us),
            s.duration_s,
            s.events,
            s.purchases,
            format_cents(s.revenue_cents),
        ])
    return buf.getvalue().encode("utf-8")


def render_daily_json(days, duplicates_dropped, rejected):
    obj = {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejected}
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def render_rejects(rejects):
    out = []
    for r in sorted(rejects, key=lambda r: r.line_no):
        out.append(json.dumps({"line": r.line_no, "reason": r.reason}, ensure_ascii=False) + "\n")
    return "".join(out).encode("utf-8")


# ---------------------------------------------------------------- atomic write

def _umask():
    current = os.umask(0)
    os.umask(current)
    return current


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def atomic_write(out_dir, files):
    """Write files (name -> bytes) into out_dir atomically per the design.

    Phase 1 writes every temporary file fully; any failure removes the temporary
    files and re-raises, leaving the existing targets untouched. Phase 2 renames
    each temporary file onto its target in OUTPUT_ORDER.
    """
    mode = 0o666 & ~_umask()
    temps = []
    try:
        for name in OUTPUT_ORDER:
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name + ".", suffix=".tmp")
            temps.append(tmp)
            try:
                os.chmod(tmp, mode)
                with os.fdopen(fd, "wb") as f:
                    f.write(files[name])
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
    except BaseException:
        for tmp in temps:
            _remove_quietly(tmp)
        raise

    replaced = 0
    try:
        for name, tmp in zip(OUTPUT_ORDER, temps):
            os.replace(tmp, os.path.join(out_dir, name))
            replaced += 1
    except BaseException:
        for tmp in temps[replaced:]:
            _remove_quietly(tmp)
        raise
    _fsync_dir(out_dir)


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------- pipeline

def run(in_path, out_path):
    """Process in_path and write the three outputs into out_path."""
    try:
        with open(in_path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        raise InputError(f"missing input file: {in_path}")
    except OSError as e:
        raise InputError(f"cannot read input file {in_path}: {e.strerror or e}")

    events = []
    rejects = []
    for line_no, content in read_lines(data):
        if is_blank(content):
            continue
        result = parse_event(line_no, content)
        if isinstance(result, Reject):
            rejects.append(result)
        else:
            events.append(result)

    dedup = Deduplicator()
    kept = [e for e in events if dedup.keep(e)]

    sessions = sessionize(kept)
    days = aggregate_daily(sessions)

    files = {
        SESSIONS_NAME: render_sessions_csv(sessions),
        DAILY_NAME: render_daily_json(days, dedup.dropped, len(rejects)),
        REJECTS_NAME: render_rejects(rejects),
    }

    try:
        os.makedirs(out_path, exist_ok=True)
    except OSError as e:
        raise OutputError(f"cannot create output directory {out_path}: {e.strerror or e}")
    try:
        atomic_write(out_path, files)
    except OSError as e:
        raise OutputError(f"cannot write output files to {out_path}: {e.strerror or e}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="job.py",
        description="Turn clickstream events into sessions.csv, daily.json and rejects.jsonl.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR",
                        help="directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR",
                        help="directory to write the three output files into")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.in_dir):
        print(f"error: input directory does not exist or is not a directory: {args.in_dir}",
              file=sys.stderr)
        return EXIT_INPUT_ERROR
    in_path = os.path.join(args.in_dir, INPUT_NAME)

    try:
        run(in_path, args.out_dir)
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except OutputError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR
    except Exception as e:  # unexpected internal error
        print(f"error: internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
