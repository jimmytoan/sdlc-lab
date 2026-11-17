#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only.

Design notes:
- Timestamps are kept exact as Fraction seconds since the Unix epoch (UTC),
  so ordering and gap tests use full fractional precision. Output truncates.
- Amounts are parsed strictly with a regex and kept as integer cents, which is
  exact (no float involved).
- A timestamp with surrounding whitespace is rejected (bad_ts); the same
  strictness applies to amounts (bad_amount). Only `user` and `event_id`
  checks trim (user is trimmed, event_id is compared raw).
- Years outside 1..9999 can occur after the UTC conversion (e.g. year 0000 or
  a 10000-01-01 instant); they are printed with at least four digits.
- A field string containing an unpaired surrogate escape cannot be written as
  UTF-8; such a field is treated as missing_field.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from fractions import Fraction

VALID_TYPES = ("view", "click", "purchase")
GAP_LIMIT = 30 * 60  # seconds; a gap strictly greater than this starts a new session

TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})"
)
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
BOM = "\ufeff"


# ---------------------------------------------------------------- calendar math

def _is_leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _days_in_month(y, m):
    if m == 2:
        return 29 if _is_leap(y) else 28
    return 30 if m in (4, 6, 9, 11) else 31


def _days_from_civil(y, m, d):
    """Days since 1970-01-01 for a proleptic Gregorian date (Hinnant's algorithm)."""
    y -= 1 if m <= 2 else 0
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m - 3 if m > 2 else m + 9) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z):
    """Inverse of _days_from_civil: returns (year, month, day)."""
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


# ---------------------------------------------------------------- parsing

def parse_ts(s):
    """Parse an ISO 8601 timestamp with seconds and an offset.

    Returns the exact UTC instant as a Fraction of seconds since the epoch,
    or None if the string is not accepted.
    """
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(g) for g in m.groups()[:6])
    frac_txt, off = m.group(7), m.group(8)
    if not (1 <= mo <= 12 and 1 <= d <= _days_in_month(y, mo)):
        return None
    if h > 23 or mi > 59 or se > 59:
        return None
    if off == "Z":
        off_s = 0
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        off_s = (oh * 3600 + om * 60) * (1 if off[0] == "+" else -1)
    whole = _days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + se - off_s
    if frac_txt:
        digits = frac_txt[1:]
        return whole + Fraction(int(digits), 10 ** len(digits))
    return Fraction(whole)


def parse_amount_cents(s):
    """Parse a strict non-negative decimal with up to two fraction digits.

    Returns the amount in integer cents, or None.
    """
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    return int(whole) * 100 + int(frac.ljust(2, "0"))


def format_cents(c):
    return f"{c // 100}.{c % 100:02d}"


def format_utc(t):
    """Format an exact instant as YYYY-MM-DDTHH:MM:SSZ, truncating fractions."""
    secs = math.floor(t)
    days, rem = divmod(secs, 86400)
    y, mo, d = _civil_from_days(days)
    h, rem = divmod(rem, 3600)
    mi, se = divmod(rem, 60)
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{se:02d}Z"


def utc_date(t):
    y, mo, d = _civil_from_days(math.floor(t) // 86400)
    return f"{y:04d}-{mo:02d}-{d:02d}"


# ---------------------------------------------------------------- validation

class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _reject_constant(name):
    # NaN / Infinity / -Infinity are not valid JSON.
    raise ValueError(f"invalid JSON constant {name}")


def _is_utf8_text(s):
    """False if s contains an unpaired surrogate (cannot be written as UTF-8)."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_event(raw_text, line_no):
    """Apply the five checks in order. Returns a normalized event dict or raises Reject."""
    try:
        obj = json.loads(raw_text, parse_constant=_reject_constant)
    except ValueError:
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    fields = {}
    for key in ("event_id", "user", "ts", "type"):
        v = obj.get(key)
        if not isinstance(v, str) or v.strip() == "" or not _is_utf8_text(v):
            raise Reject("missing_field")
        fields[key] = v

    ts = parse_ts(fields["ts"])
    if ts is None:
        raise Reject("bad_ts")

    etype = fields["type"]
    if etype not in VALID_TYPES:
        raise Reject("bad_type")

    amount = None
    if etype == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str):
            raise Reject("bad_amount")
        amount = parse_amount_cents(a)
        if amount is None:
            raise Reject("bad_amount")

    return {
        "line": line_no,
        "event_id": fields["event_id"],
        "user": fields["user"].strip().lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,  # cents, or None
    }


def read_lines(path):
    """Yield (line_no, text) for every physical line that is not blank.

    Splits on b"\\n" only. Lines that fail UTF-8 decoding are yielded as None
    so the caller reports them as bad_json. Blank lines are skipped but counted.
    """
    with open(path, "rb") as f:
        data = f.read()
    for idx, raw in enumerate(data.split(b"\n"), start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            yield idx, None
            continue
        if idx == 1 and text.startswith(BOM):
            text = text[1:]
        if text.strip() == "":
            continue
        yield idx, text


# ---------------------------------------------------------------- pipeline

def dedupe(events):
    """Keep one event per exact event_id: earliest timestamp, then earliest line."""
    best = {}
    dropped = 0
    for e in events:
        cur = best.get(e["event_id"])
        if cur is None:
            best[e["event_id"]] = e
            continue
        dropped += 1
        if (e["ts"], e["line"]) < (cur["ts"], cur["line"]):
            best[e["event_id"]] = e
    return list(best.values()), dropped


def build_sessions(events):
    """Return session records sorted by (start, user, index)."""
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        cur = None
        prev_t = None
        n = 0
        for e in evs:
            if prev_t is None or e["ts"] - prev_t > GAP_LIMIT:
                n += 1
                cur = {
                    "user": user,
                    "index": n,
                    "session_id": f"{user}-{n}",
                    "start": e["ts"],
                    "end": e["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": 0,  # cents
                }
                sessions.append(cur)
            cur["end"] = e["ts"]
            cur["events"] += 1
            if e["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]
            prev_t = e["ts"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["index"]))
    return sessions


def write_sessions_csv(path, sessions):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([
                s["session_id"],
                s["user"],
                format_utc(s["start"]),
                format_utc(s["end"]),
                math.floor(s["end"] - s["start"]),
                s["events"],
                s["purchases"],
                format_cents(s["revenue"]),
            ])


def build_daily(sessions, duplicates_dropped, rejected):
    days = {}
    for s in sessions:
        d = utc_date(s["start"])
        rec = days.setdefault(d, {"sessions": 0, "users": set(), "revenue": 0, "user_rev": {}})
        rec["sessions"] += 1
        rec["users"].add(s["user"])
        rec["revenue"] += s["revenue"]
        rec["user_rev"][s["user"]] = rec["user_rev"].get(s["user"], 0) + s["revenue"]

    out_days = []
    for d in sorted(days):
        rec = days[d]
        top = None
        if rec["revenue"] != 0:
            # highest revenue; ties go to the user that sorts first
            top = min(rec["user_rev"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out_days.append({
            "date": d,
            "sessions": rec["sessions"],
            "users": len(rec["users"]),
            "revenue": format_cents(rec["revenue"]),
            "top_user": top,
        })
    return {"days": out_days, "duplicates_dropped": duplicates_dropped, "rejected": rejected}


def write_daily_json(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


def write_rejects_jsonl(path, rejects):
    with open(path, "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n")


def parse_args(argv):
    p = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    p.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    p.add_argument("--out", dest="out", required=True, help="output directory (created if missing)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)

    valid = []
    rejects = []
    for line_no, text in read_lines(in_path):
        if text is None:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        try:
            valid.append(check_event(text, line_no))
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})

    kept, dropped = dedupe(valid)
    sessions = build_sessions(kept)

    write_sessions_csv(os.path.join(args.out, "sessions.csv"), sessions)
    write_daily_json(os.path.join(args.out, "daily.json"),
                     build_daily(sessions, dropped, len(rejects)))
    write_rejects_jsonl(os.path.join(args.out, "rejects.jsonl"), rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
