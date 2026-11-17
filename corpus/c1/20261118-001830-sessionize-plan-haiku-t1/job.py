"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl; writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

GAP = timedelta(minutes=30)
ONE_SECOND = timedelta(seconds=1)
CENT = Decimal("0.01")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?", re.ASCII)
VALID_TYPES = ("view", "click", "purchase")
REQUIRED = ("event_id", "user", "ts", "type")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _check_text(s):
    """Strings must be encodable as UTF-8 (lone surrogates cannot be written)."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise Reject("bad_json")


def parse_event(text):
    """Validate one non-blank line. Returns a dict with normalised fields or raises Reject."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for key in REQUIRED:
        v = obj.get(key)
        if v is None or not isinstance(v, str) or v.strip() == "":
            raise Reject("missing_field")
    _check_text(obj["event_id"])
    _check_text(obj["user"])
    _check_text(obj["type"])
    if obj["type"] == "purchase":
        if obj.get("amount") is None:
            raise Reject("missing_field")
    # timestamp
    try:
        ts = datetime.fromisoformat(obj["ts"])
        if ts.tzinfo is None or ts.utcoffset() is None:
            raise Reject("bad_ts")
        ts = ts.astimezone(timezone.utc)
    except Reject:
        raise
    except Exception:
        raise Reject("bad_ts")
    # type
    etype = obj["type"]
    if etype not in VALID_TYPES:
        raise Reject("bad_type")
    amount = None
    if etype == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or AMOUNT_RE.fullmatch(a) is None:
            raise Reject("bad_amount")
        amount = Decimal(a).quantize(CENT)
    user = obj["user"].strip().lower()
    return {
        "event_id": obj["event_id"],
        "user": user,
        "ts": ts,
        "type": etype,
        "amount": amount,
    }


def fmt_ts(dt):
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def fmt_money(d):
    return format(d.quantize(CENT), ".2f")


def read_events(path):
    """Returns (accepted_events_in_file_order, rejects, duplicates_dropped)."""
    with open(path, "rb") as f:
        data = f.read()
    accepted = []
    rejects = []
    seen = set()
    duplicates = 0
    for lineno, raw in enumerate(data.split(b"\n"), start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        if text.strip() == "":
            continue
        try:
            ev = parse_event(text)
        except Reject as r:
            rejects.append({"line": lineno, "reason": r.reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        accepted.append(ev)
    return accepted, rejects, duplicates


def build_sessions(events):
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        cur = [evs[0]]
        for prev, nxt in zip(evs, evs[1:]):
            if nxt["ts"] - prev["ts"] > GAP:
                groups.append(cur)
                cur = [nxt]
            else:
                cur.append(nxt)
        groups.append(cur)
        for n, grp in enumerate(groups, start=1):
            start = grp[0]["ts"]
            end = grp[-1]["ts"]
            purchases = [e for e in grp if e["type"] == "purchase"]
            revenue = sum((e["amount"] for e in purchases), Decimal("0.00")).quantize(CENT)
            sessions.append({
                "session_id": "%s-%d" % (user, n),
                "user": user,
                "start": start,
                "end": end,
                "duration_s": (end - start) // ONE_SECOND,
                "events": len(grp),
                "purchases": len(purchases),
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def build_daily(sessions, rejected, duplicates):
    days = {}
    for s in sessions:
        day = s["start"].date().isoformat()
        days.setdefault(day, []).append(s)
    out_days = []
    for day in sorted(days):
        ss = days[day]
        total = sum((s["revenue"] for s in ss), Decimal("0.00")).quantize(CENT)
        per_user = {}
        for s in ss:
            per_user[s["user"]] = per_user.get(s["user"], Decimal("0.00")) + s["revenue"]
        if total == 0:
            top = None
        else:
            top = sorted(per_user.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out_days.append({
            "date": day,
            "sessions": len(ss),
            "users": len({s["user"] for s in ss}),
            "revenue": fmt_money(total),
            "top_user": top,
        })
    return {
        "days": out_days,
        "duplicates_dropped": duplicates,
        "rejected": rejected,
    }


def write_outputs(out_dir, sessions, daily, rejects):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        s["duration_s"], s["events"], s["purchases"], fmt_money(s["revenue"])])
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(daily, indent=2, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print("error: input file not found: %s" % in_path, file=sys.stderr)
        return 2
    try:
        open(in_path, "rb").close()
    except OSError as e:
        print("error: cannot open input file %s: %s" % (in_path, e), file=sys.stderr)
        return 2

    events, rejects, duplicates = read_events(in_path)
    sessions = build_sessions(events)
    daily = build_daily(sessions, len(rejects), duplicates)
    write_outputs(args.out_dir, sessions, daily, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
