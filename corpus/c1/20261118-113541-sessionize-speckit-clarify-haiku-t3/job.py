"""sessionize: turn raw web events into user sessions and a daily summary.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only (Python 3.13).

Exit codes: 0 success (rejects do not change this), 2 missing or unreadable
input or usage error, 1 unexpected internal error.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

GAP = timedelta(minutes=30)
INPUT_FILE = "events.jsonl"
SESSIONS_FILE = "sessions.csv"
DAILY_FILE = "daily.json"
REJECTS_FILE = "rejects.jsonl"
SESSION_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
EVENT_TYPES = ("view", "click", "purchase")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
UTF8_BOM = b"\xef\xbb\xbf"


class RejectLine(Exception):
    """Raised by validate_line; carries one of the five reject reasons."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- formatting

def fmt_utc(dt):
    """Format an aware datetime as YYYY-MM-DDTHH:MM:SSZ in UTC (fraction dropped)."""
    u = dt.astimezone(timezone.utc)
    return f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}Z"


def fmt_date(dt):
    """Format the UTC calendar date of an aware datetime as YYYY-MM-DD."""
    u = dt.astimezone(timezone.utc)
    return f"{u.year:04d}-{u.month:02d}-{u.day:02d}"


def fmt_money(cents):
    """Format an integer amount of cents as a string with exactly two decimals."""
    return f"{cents // 100}.{cents % 100:02d}"


# ---------------------------------------------------------------- validation

def _blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def parse_amount_cents(text):
    """Parse a validated amount string such as '19.99', '20' or '0.5' into cents."""
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def validate_line(line_no, raw):
    """Validate one non-blank raw line (bytes without line terminator).

    Returns a tuple (event_id, user, ts_utc, cents) where cents is None for
    view/click events and an int for purchases. Raises RejectLine otherwise.
    Checks run in the fixed order of FR-010 and stop at the first failure.
    """
    if line_no == 1 and raw.startswith(UTF8_BOM):
        raw = raw[len(UTF8_BOM):]
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RejectLine("bad_json")
    if not isinstance(obj, dict):
        raise RejectLine("bad_json")

    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    etype = obj.get("type")
    # missing_field: absent, null or empty; event_id and user must be strings
    if _blank(event_id) or _blank(user) or _blank(ts) or _blank(etype):
        raise RejectLine("missing_field")
    if not isinstance(event_id, str) or not isinstance(user, str):
        raise RejectLine("missing_field")
    if etype == "purchase" and _blank(obj.get("amount")):
        raise RejectLine("missing_field")

    # bad_ts: must be a string ISO 8601 timestamp with a UTC offset
    if not isinstance(ts, str):
        raise RejectLine("bad_ts")
    try:
        ts_dt = datetime.fromisoformat(ts)
        if ts_dt.tzinfo is None or ts_dt.utcoffset() is None:
            raise ValueError("no offset")
        ts_utc = ts_dt.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError):
        raise RejectLine("bad_ts")

    # bad_type: exact, case-sensitive match
    if not isinstance(etype, str) or etype not in EVENT_TYPES:
        raise RejectLine("bad_type")

    # bad_amount: purchases only; view/click ignore the amount entirely
    cents = None
    if etype == "purchase":
        amount = obj.get("amount")
        if not isinstance(amount, str) or AMOUNT_RE.fullmatch(amount) is None:
            raise RejectLine("bad_amount")
        cents = parse_amount_cents(amount)

    user = user.strip().lower()
    try:
        user.encode("utf-8")
    except UnicodeEncodeError:
        raise RejectLine("bad_json")
    return event_id, user, ts_utc, cents


# ---------------------------------------------------------------- pipeline

def read_events(path):
    """Stream events.jsonl. Returns (kept, rejects, duplicates_dropped).

    kept: list of (user, ts_utc, line_no, cents) for first occurrences of each event_id.
    rejects: list of (line_no, reason) in file order.
    """
    kept = []
    rejects = []
    seen = set()
    duplicates = 0
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw.strip():
                continue
            try:
                event_id, user, ts_utc, cents = validate_line(line_no, raw)
            except RejectLine as rej:
                rejects.append((line_no, rej.reason))
                continue
            if event_id in seen:
                duplicates += 1
                continue
            seen.add(event_id)
            kept.append((user, ts_utc, line_no, cents))
    return kept, rejects, duplicates


def sessionize(kept):
    """Build sessions from kept events. Returns a list sorted by (start, user, n).

    Each session is a dict with keys: session_id, user, n, start, end,
    events, purchases, revenue (cents).
    """
    kept.sort()  # (user, ts_utc, line_no); line_no is unique so cents never compared
    sessions = []
    cur = None
    prev_user = None
    prev_ts = None
    n = 0
    for user, ts, _line_no, cents in kept:
        if user != prev_user:
            n = 0
            new_session = True
        else:
            new_session = (ts - prev_ts) > GAP
        if new_session:
            n += 1
            cur = {"session_id": f"{user}-{n}", "user": user, "n": n,
                   "start": ts, "end": ts, "events": 0, "purchases": 0, "revenue": 0}
            sessions.append(cur)
        cur["end"] = ts
        cur["events"] += 1
        if cents is not None:
            cur["purchases"] += 1
            cur["revenue"] += cents
        prev_user = user
        prev_ts = ts
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))
    return sessions


def build_sessions_csv(sessions):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(SESSION_HEADER)
    for s in sessions:
        duration = (s["end"] - s["start"]) // timedelta(seconds=1)
        writer.writerow([s["session_id"], s["user"], fmt_utc(s["start"]), fmt_utc(s["end"]),
                         duration, s["events"], s["purchases"], fmt_money(s["revenue"])])
    return buf.getvalue()


def build_daily(sessions, duplicates, rejected):
    by_day = {}
    for s in sessions:
        key = fmt_date(s["start"])
        day = by_day.setdefault(key, {"sessions": 0, "users": set(), "revenue": 0, "user_rev": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["user_rev"][s["user"]] = day["user_rev"].get(s["user"], 0) + s["revenue"]
    days = []
    for key in sorted(by_day):
        day = by_day[key]
        if day["revenue"] > 0:
            # highest revenue wins; ties go to the alphabetically first name
            top = min(day["user_rev"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        else:
            top = None
        days.append({"date": key, "sessions": day["sessions"], "users": len(day["users"]),
                     "revenue": fmt_money(day["revenue"]), "top_user": top})
    doc = {"days": days, "duplicates_dropped": duplicates, "rejected": rejected}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def build_rejects(rejects):
    return "".join(json.dumps({"line": line_no, "reason": reason}) + "\n" for line_no, reason in rejects)


def write_atomic(out_dir, filename, text):
    """Write text to out_dir/filename via a temporary file and os.replace.

    Only the target name is created; the temporary file is removed on failure.
    """
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, filename)
    tmp = os.path.join(out_dir, f".{filename}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def eprint(message):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- entry point

def parse_args(argv):
    parser = argparse.ArgumentParser(prog="job.py", description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="inp", required=True, metavar="INPUT_DIR",
                        help="folder containing events.jsonl")
    parser.add_argument("--out", dest="out", required=True, metavar="OUTPUT_DIR",
                        help="folder to write outputs into (created if missing)")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        args = parse_args(argv)
        events_path = os.path.join(args.inp, INPUT_FILE)
        if not os.path.isfile(events_path):
            eprint(f"error: input file not found: {events_path}")
            return 2
        try:
            open(events_path, "rb").close()
        except OSError as exc:
            eprint(f"error: cannot read input file {events_path}: {exc.strerror or exc}")
            return 2

        kept, rejects, duplicates = read_events(events_path)
        sessions = sessionize(kept)
        sessions_text = build_sessions_csv(sessions)
        daily_text = build_daily(sessions, duplicates, len(rejects))
        rejects_text = build_rejects(rejects)

        write_atomic(args.out, SESSIONS_FILE, sessions_text)
        write_atomic(args.out, DAILY_FILE, daily_text)
        write_atomic(args.out, REJECTS_FILE, rejects_text)
        return 0
    except Exception as exc:  # unexpected: one line, no traceback
        eprint(f"error: unexpected failure: {exc.__class__.__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
