#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from decimal import MAX_PREC, Decimal, localcontext
from fractions import Fraction

ALLOWED_TYPES = ("view", "click", "purchase", "logout", "refund")
AMOUNT_TYPES = ("purchase", "refund")
DEFAULT_GAP_MINUTES = 30
GAP_RE = re.compile(r"[0-9]+")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"]

TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?"
    r"(Z|[+-]([0-9]{2}):([0-9]{2}))"
)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")


# ---------------------------------------------------------------- time helpers

def days_from_civil(y, m, d):
    """Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's algorithm)."""
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
    if m <= 2:
        y += 1
    return y, m, d


def _is_leap(y):
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def parse_ts(s):
    """Return the exact UTC instant (Fraction seconds since epoch) or None if invalid."""
    mt = TS_RE.fullmatch(s)
    if not mt:
        return None
    y, mo, d, hh, mi, ss = (int(mt.group(i)) for i in range(1, 7))
    frac_digits = mt.group(7)
    tz = mt.group(8)
    if not (1 <= mo <= 12):
        return None
    dim = [31, 29 if _is_leap(y) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]
    if not (1 <= d <= dim):
        return None
    if hh > 23 or mi > 59 or ss > 59:
        return None
    offset = 0
    if tz != "Z":
        oh, om = int(mt.group(9)), int(mt.group(10))
        if oh > 23 or om > 59:
            return None
        offset = oh * 3600 + om * 60
        if tz[0] == "-":
            offset = -offset
    base = days_from_civil(y, mo, d) * 86400 + hh * 3600 + mi * 60 + ss
    frac = Fraction(int(frac_digits), 10 ** len(frac_digits)) if frac_digits else Fraction(0)
    return base + frac - offset


def fmt_utc_seconds(ts):
    """Format an exact instant as YYYY-MM-DDTHH:MM:SSZ, floored to whole seconds."""
    total = math.floor(ts)
    days, rem = divmod(total, 86400)
    y, m, d = civil_from_days(days)
    hh, rem = divmod(rem, 3600)
    mi, ss = divmod(rem, 60)
    return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mi:02d}:{ss:02d}Z"


def utc_date(ts):
    """UTC calendar date (YYYY-MM-DD) of an exact instant."""
    days = math.floor(ts) // 86400
    y, m, d = civil_from_days(days)
    return f"{y:04d}-{m:02d}-{d:02d}"


def fmt_money(d):
    with localcontext() as ctx:
        ctx.prec = MAX_PREC
        q = d.quantize(Decimal("0.01"))
        if q == 0:
            q = Decimal("0.00")  # never print -0.00
        return str(q)


# ------------------------------------------------------------- validation

def parse_amount(s):
    if not AMOUNT_RE.fullmatch(s):
        return None
    with localcontext() as ctx:
        ctx.prec = MAX_PREC
        return Decimal(s)


def _nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate(line_no, idx, text):
    """Return (event, None) for a valid line, or (None, reason) for a rejected line."""
    try:
        obj = json.loads(text)
    except Exception:
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"

    event_id = obj.get("event_id")
    user = obj.get("user")
    # A lone surrogate (e.g. the JSON escape \ud800) cannot be encoded as UTF-8,
    # so such a user name cannot appear in the UTF-8 output. Treat it as bad_json.
    if isinstance(user, str):
        try:
            user.encode("utf-8")
        except UnicodeEncodeError:
            return None, "bad_json"
    ts = obj.get("ts")
    typ = obj.get("type")
    if not (_nonempty_str(event_id) and _nonempty_str(user) and _nonempty_str(ts) and _nonempty_str(typ)):
        return None, "missing_field"

    ts_val = parse_ts(ts)
    if ts_val is None:
        return None, "bad_ts"

    if typ not in ALLOWED_TYPES:
        return None, "bad_type"

    amount = None
    if typ in AMOUNT_TYPES:
        raw_amount = obj.get("amount")
        if not isinstance(raw_amount, str):
            return None, "bad_amount"
        amount = parse_amount(raw_amount)
        if amount is None:
            return None, "bad_amount"

    return {
        "line": line_no,
        "idx": idx,
        "event_id": event_id,
        "user": user.strip().lower(),
        "ts": ts_val,
        "type": typ,
        "amount": amount,
    }, None


# ------------------------------------------------------------------ pipeline

def read_input(path):
    """Return (valid_events, rejects) from the raw events file."""
    with open(path, "rb") as f:
        data = f.read()
    valid, rejects = [], []
    for n, raw in enumerate(data.split(b"\n"), start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": n, "reason": "bad_json"})
            continue
        if n == 1 and text.startswith("\ufeff"):
            text = text[1:]
        if text.strip() == "":
            continue
        ev, reason = validate(n, len(valid) + len(rejects), text)
        if reason is not None:
            rejects.append({"line": n, "reason": reason})
        else:
            valid.append(ev)
    return valid, rejects


def dedupe(valid):
    """Keep the earliest event per raw event_id (ties: file order). Returns (kept, dropped_count)."""
    best = {}
    for ev in valid:
        cur = best.get(ev["event_id"])
        if cur is None or (ev["ts"], ev["idx"]) < (cur["ts"], cur["idx"]):
            best[ev["event_id"]] = ev
    return list(best.values()), len(valid) - len(best)


def build_sessions(kept, gap_minutes):
    gap = Fraction(gap_minutes * 60)
    """Split each user's events into sessions. Returns a list of session dicts."""
    by_user = {}
    for ev in kept:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["idx"]))
        groups = []
        prev = None
        for ev in evs:
            if prev is None or prev["type"] == "logout" or ev["ts"] - prev["ts"] > gap:
                groups.append([])
            groups[-1].append(ev)
            prev = ev
        for n, grp in enumerate(groups, start=1):
            purchases = [e for e in grp if e["type"] == "purchase"]
            refunds = [e for e in grp if e["type"] == "refund"]
            with localcontext() as ctx:
                ctx.prec = MAX_PREC
                revenue = (sum((e["amount"] for e in purchases), Decimal(0))
                           - sum((e["amount"] for e in refunds), Decimal(0)))
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start_ts": grp[0]["ts"],
                "end_ts": grp[-1]["ts"],
                "events": len(grp),
                "purchases": len(purchases),
                "refunds": len(refunds),
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start_ts"], s["user"]))
    return sessions


def build_daily(sessions, duplicates_dropped, rejected, gap_minutes):
    by_day = {}
    for s in sessions:
        by_day.setdefault(utc_date(s["start_ts"]), []).append(s)
    days = []
    for date in sorted(by_day):
        ss = by_day[date]
        with localcontext() as ctx:
            ctx.prec = MAX_PREC
            revenue = sum((s["revenue"] for s in ss), Decimal(0))
            per_user = {}
            for s in ss:
                per_user[s["user"]] = per_user.get(s["user"], Decimal(0)) + s["revenue"]
        top_user = None
        if per_user:
            best = min(per_user, key=lambda u: (-per_user[u], u))
            if per_user[best] > 0:
                top_user = best
        days.append({
            "date": date,
            "sessions": len(ss),
            "users": len({s["user"] for s in ss}),
            "revenue": fmt_money(revenue),
            "top_user": top_user,
        })
    return {
        "days": days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected,
        "gap_minutes": gap_minutes,
    }


def write_outputs(out_dir, sessions, daily, rejects):
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for s in sessions:
            start = s["start_ts"]
            end = s["end_ts"]
            w.writerow([
                s["session_id"],
                s["user"],
                fmt_utc_seconds(start),
                fmt_utc_seconds(end),
                math.floor(end - start),
                s["events"],
                s["purchases"],
                s["refunds"],
                fmt_money(s["revenue"]),
            ])
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build user sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory (created if missing)")
    parser.add_argument("--gap-minutes", dest="gap_minutes", type=str, default=str(DEFAULT_GAP_MINUTES),
                        help="session gap in minutes, a positive integer (default 30)")
    args = parser.parse_args(argv)

    # Validated by hand (not argparse type=int) so a bad value exits 64, not 2.
    gap_raw = args.gap_minutes
    if not GAP_RE.fullmatch(gap_raw) or int(gap_raw) <= 0:
        print(f"error: --gap-minutes must be a positive integer, got {gap_raw!r}", file=sys.stderr)
        return 64
    gap_minutes = int(gap_raw)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    os.makedirs(args.out_dir, exist_ok=True)

    valid, rejects = read_input(in_path)
    kept, dropped = dedupe(valid)
    sessions = build_sessions(kept, gap_minutes)
    daily = build_daily(sessions, dropped, len(rejects), gap_minutes)
    write_outputs(args.out_dir, sessions, daily, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
