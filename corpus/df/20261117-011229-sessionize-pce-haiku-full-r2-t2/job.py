#!/usr/bin/env python3
"""sessionize: turn raw web events (events.jsonl) into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import decimal
import io
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

DEFAULT_GAP_MINUTES = 30
ALLOWED_TYPES = ("view", "click", "purchase", "logout", "refund")
AMOUNT_TYPES = ("purchase", "refund")
GAP_RE = re.compile(r"[0-9]+")  # ASCII digits only

# Strict ISO 8601 timestamp with seconds and a mandatory offset.
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|([+-])([0-9]{2}):([0-9]{2}))"
)
# Non-negative decimal, at most two fraction digits.
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")

MONEY_Q = Decimal("0.01")
DEC_CTX = decimal.Context(prec=decimal.MAX_PREC, Emax=decimal.MAX_EMAX, Emin=decimal.MIN_EMIN)


class GapError(Exception):
    pass


def parse_gap(s):
    """Return the gap in minutes (positive int) or raise GapError."""
    if s is None:
        return DEFAULT_GAP_MINUTES
    if not GAP_RE.fullmatch(s):
        raise GapError(f"invalid --gap-minutes value: {s!r} (expected a positive integer)")
    value = int(s)
    if value < 1:
        raise GapError(f"invalid --gap-minutes value: {s!r} (expected a positive integer)")
    return value


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- helpers

def parse_ts(s):
    """Return an instant key (utc_naive_datetime_without_fraction, Decimal fraction in [0,1)).

    The key keeps full precision and compares equal for equal instants in any offset.
    Raises ValueError if the timestamp is invalid.
    """
    m = TS_RE.fullmatch(s)
    if not m:
        raise ValueError("format")
    year, month, day, hour, minute, sec = (int(m.group(i)) for i in range(1, 7))
    frac_digits = m.group(7)
    tz_part = m.group(8)
    if hour > 23 or minute > 59 or sec > 59:
        raise ValueError("time")
    naive = datetime(year, month, day, hour, minute, sec)  # raises on impossible dates
    if tz_part == "Z":
        offset = timedelta(0)
    else:
        oh, om = int(m.group(10)), int(m.group(11))
        if oh > 23 or om > 59:
            raise ValueError("offset")
        offset = timedelta(hours=oh, minutes=om)
        if m.group(9) == "-":
            offset = -offset
    base = naive - offset  # UTC wall-clock, may raise OverflowError
    frac = Decimal("0." + frac_digits) if frac_digits else Decimal(0)
    return base, frac


def diff_seconds(a, b):
    """Exact (a - b) in seconds as Decimal, for instant keys a and b."""
    whole = (a[0] - b[0]) // timedelta(seconds=1)
    return Decimal(whole) + (a[1] - b[1])


def fmt_ts(key):
    d = key[0]
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"


def parse_amount(s):
    if not AMOUNT_RE.fullmatch(s):
        raise ValueError("amount")
    return Decimal(s)


def fmt_money(d):
    q = d.quantize(MONEY_Q, context=DEC_CTX)
    if q == 0:
        q = Decimal("0.00")  # never write -0.00
    return f"{q:f}"


# ---------------------------------------------------------------- pipeline

def _reject_constant(name):
    # NaN, Infinity and -Infinity are accepted by Python's json but are not valid JSON.
    raise ValueError(name)


def validate_line(raw_text):
    """Return a normalised event dict or raise Reject(reason)."""
    try:
        obj = json.loads(raw_text, parse_constant=_reject_constant)
    except Exception:
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    # Strings holding lone surrogates (e.g. "\\udc80") cannot be written as UTF-8: treat as undecodable.
    try:
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise Reject("bad_json")

    def get_str(name):
        v = obj.get(name)
        if not isinstance(v, str) or v.strip() == "":
            raise Reject("missing_field")
        return v

    event_id = get_str("event_id")
    user_raw = get_str("user")
    ts_raw = get_str("ts")
    type_raw = get_str("type")

    try:
        key = parse_ts(ts_raw)
    except (ValueError, OverflowError):
        raise Reject("bad_ts")

    if type_raw not in ALLOWED_TYPES:
        raise Reject("bad_type")

    amount = None
    if type_raw in AMOUNT_TYPES:
        a = obj.get("amount")
        if not isinstance(a, str):
            raise Reject("bad_amount")
        try:
            amount = parse_amount(a)
        except ValueError:
            raise Reject("bad_amount")

    return {
        "event_id": event_id,
        "user": user_raw.strip().lower(),
        "key": key,
        "type": type_raw,
        "amount": amount,
    }


def read_events(path):
    """Yield (line_no, text_or_None, decode_ok) for each non-blank line."""
    with open(path, "rb") as f:
        data = f.read()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()  # trailing newline does not create an extra line
    results = []
    for idx, raw in enumerate(parts):
        line_no = idx + 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            results.append((line_no, None))
            continue
        if idx == 0 and text.startswith("\ufeff"):
            text = text[1:]
        if text.strip() == "":
            continue
        results.append((line_no, text))
    return results


def dedup(events):
    """events: list of (line, event). Keep earliest instant per event_id; ties: lower line."""
    best = {}
    for line, ev in events:
        k = ev["event_id"]
        cur = best.get(k)
        if cur is None or (ev["key"], line) < (cur[1]["key"], cur[0]):
            best[k] = (line, ev)
    kept = sorted(best.values(), key=lambda p: (p[1]["key"], p[0]))
    return kept, len(events) - len(best)


def sessionize(kept, gap_minutes):
    gap_s = Decimal(gap_minutes * 60)
    by_user = {}
    for line, ev in kept:
        by_user.setdefault(ev["user"], []).append((ev["key"], line, ev))
    sessions = []
    for user, items in by_user.items():
        items.sort(key=lambda t: (t[0], t[1]))
        current = None
        n = 0
        prev_key = None
        closed = False
        for key, _line, ev in items:
            if current is None or closed or diff_seconds(key, prev_key) > gap_s:
                closed = False
                if current is not None:
                    sessions.append(current)
                n += 1
                current = {
                    "session_id": f"{user}-{n}",
                    "user": user,
                    "start": key,
                    "end": key,
                    "events": 0,
                    "purchases": 0,
                    "refunds": 0,
                    "revenue": Decimal(0),
                }
            current["end"] = key
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] = DEC_CTX.add(current["revenue"], ev["amount"])
            elif ev["type"] == "refund":
                current["refunds"] += 1
                current["revenue"] = DEC_CTX.subtract(current["revenue"], ev["amount"])
            elif ev["type"] == "logout":
                closed = True
            prev_key = key
        if current is not None:
            sessions.append(current)
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    return sessions


def write_outputs(out_dir, sessions, dups, rejects, gap_minutes):
    os.makedirs(out_dir, exist_ok=True)

    # sessions.csv
    rows = []
    for s in sessions:
        duration = int(diff_seconds(s["end"], s["start"]) // 1)
        rows.append([
            s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
            str(duration), str(s["events"]), str(s["purchases"]), str(s["refunds"]), fmt_money(s["revenue"]),
        ])

    # daily.json
    per_day = {}
    for s in sessions:
        d = s["start"][0].date()
        agg = per_day.setdefault(d, {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        agg["sessions"] += 1
        agg["users"].add(s["user"])
        agg["revenue"] = DEC_CTX.add(agg["revenue"], s["revenue"])
        agg["per_user"][s["user"]] = DEC_CTX.add(agg["per_user"].get(s["user"], Decimal(0)), s["revenue"])
    days = []
    for d in sorted(per_day):
        agg = per_day[d]
        best_user, best_rev = min(agg["per_user"].items(), key=lambda kv: (-kv[1], kv[0]))
        top = best_user if best_rev > 0 else None
        days.append({
            "date": d.isoformat(),
            "sessions": agg["sessions"],
            "users": len(agg["users"]),
            "revenue": fmt_money(agg["revenue"]),
            "top_user": top,
        })
    daily = {"gap_minutes": gap_minutes, "days": days, "duplicates_dropped": dups, "rejected": len(rejects)}

    # rejects.jsonl
    reject_lines = [json.dumps({"line": ln, "reason": r}, ensure_ascii=False) + "\n" for ln, r in rejects]

    files = {}
    sio = io.StringIO(newline="")
    w = csv.writer(sio, lineterminator="\n")
    w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"])
    w.writerows(rows)
    files["sessions.csv"] = sio.getvalue()
    files["daily.json"] = json.dumps(daily, ensure_ascii=False) + "\n"
    files["rejects.jsonl"] = "".join(reject_lines)

    tmp_paths = []
    try:
        for name, content in files.items():
            tmp = os.path.join(out_dir, f".{name}.{os.getpid()}.tmp")
            tmp_paths.append(tmp)
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        for name, tmp in zip(files.keys(), tmp_paths):
            os.replace(tmp, os.path.join(out_dir, name))
        tmp_paths = []
    finally:
        for tmp in tmp_paths:
            try:
                os.remove(tmp)
            except OSError:
                pass


def split_gap_arg(argv):
    """Remove --gap-minutes from argv by hand and return (rest_argv, raw_value_or_None).

    argparse would exit 2 on a missing or dash-prefixed value; the spec requires 64 for any
    value that is not a positive integer, so the option is validated here instead.
    A missing value is returned as "" (invalid).
    """
    rest, raw, found, i = [], None, False, 0
    while i < len(argv):
        a = argv[i]
        if a == "--gap-minutes":
            found = True
            if i + 1 < len(argv):
                raw = argv[i + 1]
                i += 2
            else:
                raw = ""
                i += 1
            continue
        if a.startswith("--gap-minutes="):
            found = True
            raw = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    return rest, (raw if found else None)


def main(argv=None):
    # Gap values may be arbitrarily long positive integers; lift the int-parsing digit limit.
    sys.set_int_max_str_digits(0)
    argv = sys.argv[1:] if argv is None else list(argv)
    argv, gap_raw = split_gap_arg(argv)
    ap = argparse.ArgumentParser(description="Sessionize raw web events.")
    ap.add_argument("--in", dest="inp", required=True, help="input directory containing events.jsonl")
    ap.add_argument("--out", dest="out", required=True, help="output directory")
    ap.add_argument("--gap-minutes", dest="gap_minutes", default=None,
                    help="session gap in minutes (positive integer, default 30)")
    args = ap.parse_args(argv)
    args.gap_minutes = gap_raw

    try:
        gap_minutes = parse_gap(args.gap_minutes)
    except GapError as e:
        print(f"error: {e}", file=sys.stderr)
        return 64

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    rejects = []
    valid = []
    for line_no, text in read_events(in_path):
        if text is None:
            rejects.append((line_no, "bad_json"))
            continue
        try:
            ev = validate_line(text)
        except Reject as r:
            rejects.append((line_no, r.reason))
            continue
        valid.append((line_no, ev))

    kept, dups = dedup(valid)
    sessions = sessionize(kept, gap_minutes)
    write_outputs(args.out, sessions, dups, rejects, gap_minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
