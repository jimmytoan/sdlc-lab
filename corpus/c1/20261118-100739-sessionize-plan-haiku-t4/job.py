#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>.
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
from decimal import Decimal, InvalidOperation

SESSION_GAP = timedelta(minutes=30)
EVENT_TYPES = ("view", "click", "purchase")
TS_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})$"
)
AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


class InputMissing(Exception):
    pass


class _Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _parse_ts(text):
    """Return an aware UTC datetime, or raise ValueError if not acceptable."""
    m = TS_RE.match(text)
    if not m:
        raise ValueError("bad ts format")
    year, month, day, hour, minute, second = (int(g) for g in m.group(1, 2, 3, 4, 5, 6))
    frac = m.group(7) or ""
    micro = int((frac + "000000")[:6]) if frac else 0
    off = m.group(8)
    if off == "Z":
        tzinfo = timezone.utc
    else:
        sign = 1 if off[0] == "+" else -1
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            raise ValueError("bad offset")
        tzinfo = timezone(sign * timedelta(hours=oh, minutes=om))
    local = datetime(year, month, day, hour, minute, second, micro, tzinfo=tzinfo)  # may raise ValueError
    return local.astimezone(timezone.utc)


def _reject_constant(name):
    raise ValueError("non-standard constant " + name)


def _validate_line(raw_text):
    """Validate one non-blank line. Returns a dict for a valid event or raises _Reject."""
    try:
        obj = json.loads(raw_text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise _Reject("bad_json")
    if not isinstance(obj, dict):
        raise _Reject("bad_json")

    is_purchase = obj.get("type") == "purchase"
    for key in ("event_id", "user", "ts", "type"):
        val = obj.get(key)
        if not isinstance(val, str) or val.strip() == "":
            raise _Reject("missing_field")
    if is_purchase and obj.get("amount") is None:
        raise _Reject("missing_field")

    try:
        ts_utc = _parse_ts(obj["ts"].strip())
    except (ValueError, OverflowError):
        raise _Reject("bad_ts")

    etype = obj["type"]
    if etype not in EVENT_TYPES:
        raise _Reject("bad_type")

    amount = None
    if is_purchase:
        raw_amount = obj["amount"]
        if not isinstance(raw_amount, str):
            raise _Reject("bad_amount")
        text = raw_amount.strip()
        if not AMOUNT_RE.match(text):
            raise _Reject("bad_amount")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise _Reject("bad_amount")
        if amount <= 0:
            raise _Reject("bad_amount")

    return {
        "event_id": obj["event_id"],
        "user": obj["user"].strip().lower(),
        "ts_utc": ts_utc,
        "type": etype,
        "amount": amount,
    }


def read_events(path):
    """Return (valid_events, rejects) where each valid event carries its line number."""
    with open(path, "rb") as f:
        data = f.read()
    valid, rejects = [], []
    for lineno, raw in enumerate(data.split(b"\n"), start=1):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        if text.endswith("\r"):
            text = text[:-1]
        if text.strip() == "":
            continue
        try:
            ev = _validate_line(text)
        except _Reject as r:
            rejects.append({"line": lineno, "reason": r.reason})
            continue
        ev["line"] = lineno
        valid.append(ev)
    return valid, rejects


def dedupe(events):
    """Keep first occurrence (file order) per event_id. Returns (kept, dropped_count)."""
    seen = set()
    kept = []
    dropped = 0
    for ev in events:
        if ev["event_id"] in seen:
            dropped += 1
            continue
        seen.add(ev["event_id"])
        kept.append(ev)
    return kept, dropped


def build_sessions(events):
    """Return list of session dicts sorted by start, user, session_id."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts_utc"], e["event_id"], e["line"]))
        groups = []
        current = None
        prev_ts = None
        for ev in evs:
            if current is None or ev["ts_utc"] - prev_ts > SESSION_GAP:
                current = []
                groups.append(current)
            current.append(ev)
            prev_ts = ev["ts_utc"]
        for n, group in enumerate(groups, start=1):
            start = group[0]["ts_utc"]
            end = group[-1]["ts_utc"]
            revenue = Decimal("0")
            purchases = 0
            for ev in group:
                if ev["type"] == "purchase":
                    purchases += 1
                    revenue += ev["amount"]
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start,
                "end": end,
                "duration_s": (end - start) // timedelta(seconds=1),
                "events": len(group),
                "purchases": purchases,
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def _fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_money(d):
    return f"{d:.2f}"


def build_daily(sessions, duplicates_dropped, rejected_count):
    days = {}
    for s in sessions:
        date = s["start"].date().isoformat()
        day = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"), "by_user": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["by_user"][s["user"]] = day["by_user"].get(s["user"], Decimal("0")) + s["revenue"]

    out_days = []
    for date in sorted(days):
        day = days[date]
        top_user = None
        best = None
        for user, rev in day["by_user"].items():
            if rev <= 0:
                continue
            if best is None or rev > best or (rev == best and user < top_user):
                best = rev
                top_user = user
        out_days.append({
            "date": date,
            "sessions": day["sessions"],
            "users": len(day["users"]),
            "revenue": _fmt_money(day["revenue"]),
            "top_user": top_user,
        })
    return {
        "days": out_days,
        "duplicates_dropped": duplicates_dropped,
        "rejected": rejected_count,
    }


def render_sessions_csv(sessions):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for s in sessions:
        writer.writerow([
            s["session_id"], s["user"], _fmt_ts(s["start"]), _fmt_ts(s["end"]),
            s["duration_s"], s["events"], s["purchases"], _fmt_money(s["revenue"]),
        ])
    return buf.getvalue()


def render_daily_json(daily):
    return json.dumps(daily, indent=2, ensure_ascii=False) + "\n"


def render_rejects(rejects):
    return "".join(json.dumps({"line": r["line"], "reason": r["reason"]}, ensure_ascii=False) + "\n"
                   for r in rejects)


def _current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_outputs(out_dir, files):
    """Atomically write {name: text} into out_dir. Temp files are removed on failure."""
    os.makedirs(out_dir, exist_ok=True)
    mode = 0o666 & ~_current_umask()
    temps = []
    try:
        for name, text in files.items():
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name + ".", suffix=".tmp")
            temps.append((tmp, name))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.chmod(tmp, mode)
        for tmp, name in temps:
            os.replace(tmp, os.path.join(out_dir, name))
        temps = []
    finally:
        for tmp, _ in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass


def run(in_dir, out_dir):
    path = os.path.join(in_dir, "events.jsonl")
    if not os.path.isfile(path):
        raise InputMissing(path)

    valid, rejects = read_events(path)
    unique, dropped = dedupe(valid)
    sessions = build_sessions(unique)
    daily = build_daily(sessions, dropped, len(rejects))

    write_outputs(out_dir, {
        "sessions.csv": render_sessions_csv(sessions),
        "daily.json": render_daily_json(daily),
        "rejects.jsonl": render_rejects(rejects),
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sessions and a daily summary from events.jsonl.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)

    try:
        run(args.in_dir, args.out_dir)
    except InputMissing as e:
        print(f"error: input file not found: {e.args[0]}", file=sys.stderr)
        return 2
    except PermissionError as e:
        print(f"error: cannot write output or read input: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: output directory not usable: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # unexpected: report, no partial output
        print(f"error: unexpected failure: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
