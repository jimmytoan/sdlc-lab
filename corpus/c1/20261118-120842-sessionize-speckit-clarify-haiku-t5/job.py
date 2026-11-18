"""sessionize: clickstream sessions batch job.

Usage:
    python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl to <OUTPUT_DIR>. Standard library only.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

INPUT_FILE = "events.jsonl"
SESSION_GAP = timedelta(minutes=30)
EVENT_TYPES = ("view", "click", "purchase")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]+)?")
CENT = Decimal("0.01")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def money(value):
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def whole(dt):
    """Whole-second UTC instant as an integer epoch second (fraction dropped)."""
    return (dt.replace(microsecond=0) - EPOCH) // timedelta(seconds=1)


def fmt_time(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_line(text, line_no):
    """Validate one physical line. Returns an event dict or raises Reject."""
    try:
        obj = json.loads(text)
    except ValueError:
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("missing_field")

    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            raise Reject("missing_field")
    event_id, user, ts, etype = obj["event_id"], obj["user"], obj["ts"], obj["type"]
    if not isinstance(event_id, str) or not isinstance(user, str) or not isinstance(etype, str):
        raise Reject("missing_field")
    if event_id.strip() == "":
        raise Reject("missing_field")
    user_key = user.strip().lower()
    if user_key == "":
        raise Reject("missing_field")
    if etype == "purchase" and obj.get("amount") is None:
        raise Reject("missing_field")

    if not isinstance(ts, str):
        raise Reject("bad_ts")
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("no offset")
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise Reject("bad_ts")

    if etype not in EVENT_TYPES:
        raise Reject("bad_type")

    amount = None
    if etype == "purchase":
        raw = obj["amount"]
        if not isinstance(raw, str) or not AMOUNT_RE.fullmatch(raw):
            raise Reject("bad_amount")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            raise Reject("bad_amount")
        if amount <= 0:
            raise Reject("bad_amount")

    return {
        "event_id": event_id,
        "user": user_key,
        "ts": parsed,
        "type": etype,
        "amount": amount,
        "line": line_no,
    }


def read_events(path):
    """Returns (valid_events, rejects, blank_ignored_count)."""
    valid, rejects = [], []
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, start=1):
            try:
                text = raw.decode("utf-8-sig" if line_no == 1 else "utf-8")
            except UnicodeDecodeError:
                rejects.append({"line": line_no, "reason": "bad_json"})
                continue
            text = text.rstrip("\r\n")
            if text.strip() == "":
                continue
            try:
                valid.append(parse_line(text, line_no))
            except Reject as r:
                rejects.append({"line": line_no, "reason": r.reason})
    return valid, rejects


def dedupe(events):
    """Keep the earliest (ts, line) copy per event_id. Returns (kept, dropped_count)."""
    best = {}
    dropped = 0
    for ev in events:
        key = ev["event_id"]
        cur = best.get(key)
        if cur is None:
            best[key] = ev
            continue
        dropped += 1
        if (ev["ts"], ev["line"]) < (cur["ts"], cur["line"]):
            best[key] = ev
    return list(best.values()), dropped


def sessionize(events):
    """Returns list of session dicts (unnumbered), sorted by (start, user)."""
    per_user = {}
    for ev in events:
        per_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in per_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        current = None
        for ev in evs:
            if current is None or ev["ts"] - current["last"] > SESSION_GAP:
                current = {
                    "user": user,
                    "start": ev["ts"],
                    "last": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal("0"),
                }
                sessions.append(current)
            current["last"] = ev["ts"]
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"]))
    counters = {}
    for s in sessions:
        counters[s["user"]] = counters.get(s["user"], 0) + 1
        s["session_id"] = f"{s['user']}-{counters[s['user']]}"
    return sessions


def summarize(sessions):
    by_day = {}
    for s in sessions:
        date = s["start"].astimezone(timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(date, []).append(s)

    days = []
    for date in sorted(by_day):
        group = by_day[date]
        total = Decimal("0")
        per_user_rev = {}
        for s in group:
            total += s["revenue"]
            per_user_rev[s["user"]] = per_user_rev.get(s["user"], Decimal("0")) + s["revenue"]
        if total == 0:
            top = None
        else:
            top = sorted(per_user_rev.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        days.append({
            "date": date,
            "sessions": len(group),
            "users": len({s["user"] for s in group}),
            "revenue": money(total),
            "top_user": top,
        })
    return days


def write_outputs(out_dir, sessions, days, dropped, rejects):
    os.makedirs(out_dir, exist_ok=True)

    with open(os.path.join(out_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            start, end = s["start"], s["last"]
            w.writerow([
                s["session_id"],
                s["user"],
                fmt_time(start),
                fmt_time(end),
                whole(end) - whole(start),
                s["events"],
                s["purchases"],
                money(s["revenue"]),
            ])

    summary = {"days": days, "duplicates_dropped": dropped, "rejected": len(rejects)}
    with open(os.path.join(out_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")

    with open(os.path.join(out_dir, "rejects.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build clickstream sessions and a daily summary.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input folder containing events.jsonl")
    parser.add_argument("--out", dest="out_dir", required=True, help="output folder (created if missing)")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, INPUT_FILE)
    if not os.path.isfile(in_path):
        print(f"error: {INPUT_FILE} not found in {args.in_dir}", file=sys.stderr)
        return 2

    events, rejects = read_events(in_path)
    kept, dropped = dedupe(events)
    sessions = sessionize(kept)
    days = summarize(sessions)
    write_outputs(args.out_dir, sessions, days, dropped, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
