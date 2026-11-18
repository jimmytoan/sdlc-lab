#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

GAP_SECONDS = 30 * 60
VALID_TYPES = ("view", "click", "purchase")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        raise Reject("bad_ts")
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    return dt.astimezone(timezone.utc)


def parse_amount(value):
    if isinstance(value, bool):
        raise Reject("bad_amount")
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise Reject("bad_amount")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    return amount


def parse_event(obj):
    """Validate one decoded JSON object. Returns a normalised event dict."""
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for key in ("event_id", "user", "ts", "type"):
        if obj.get(key) is None:
            raise Reject("missing_field")
    event_id = obj["event_id"]
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
        raise Reject("missing_field")
    event_id = str(event_id).strip()
    if not event_id:
        raise Reject("missing_field")
    user = obj["user"]
    if not isinstance(user, str) or not user.strip():
        raise Reject("missing_field")
    user = user.strip().lower()
    ts = parse_ts(obj["ts"])
    etype = obj["type"]
    if not isinstance(etype, str) or etype not in VALID_TYPES:
        raise Reject("bad_type")
    amount = Decimal(0)
    if etype == "purchase":
        if obj.get("amount") is None:
            raise Reject("missing_field")
        amount = parse_amount(obj["amount"])
    return {"event_id": event_id, "user": user, "ts": ts, "type": etype, "amount": amount}


def fmt_money(d):
    """Exact decimal rendering with at least two fraction digits (no rounding)."""
    text = format(d, "f")
    if "." not in text:
        return text + ".00"
    whole, frac = text.split(".", 1)
    if len(frac) < 2:
        frac = frac + "0" * (2 - len(frac))
    return whole + "." + frac


def iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def read_events(path):
    """Yields (line_no, event_or_None, reason_or_None) for each non-blank line."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                yield line_no, None, "bad_json"
                continue
            try:
                yield line_no, parse_event(obj), None
            except Reject as r:
                yield line_no, None, r.reason


def build_sessions(events):
    """events: list of normalised events (already de-duplicated)."""
    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        current = None
        number = 0
        for ev in evs:
            if current is None or (ev["ts"] - current["end"]).total_seconds() > GAP_SECONDS:
                number += 1
                current = {
                    "session_id": f"{user}-{number}",
                    "user": user,
                    "start": ev["ts"],
                    "end": ev["ts"],
                    "events": 0,
                    "purchases": 0,
                    "revenue": Decimal(0),
                }
                sessions.append(current)
            current["end"] = max(current["end"], ev["ts"])
            current["events"] += 1
            if ev["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += ev["amount"]
    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))
    return sessions


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sessions and daily summary from events.")
    parser.add_argument("--in", dest="input_dir", required=True)
    parser.add_argument("--out", dest="output_dir", required=True)
    args = parser.parse_args(argv)

    in_path = os.path.join(args.input_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        sys.stderr.write(f"error: input file not found: {in_path}\n")
        return 2

    seen = set()
    duplicates = 0
    rejects = []
    events = []
    for line_no, ev, reason in read_events(in_path):
        if reason is not None:
            rejects.append({"line": line_no, "reason": reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        events.append(ev)

    sessions = build_sessions(events)

    os.makedirs(args.output_dir, exist_ok=True)

    with open(os.path.join(args.output_dir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            writer.writerow([
                s["session_id"], s["user"], iso_z(s["start"]), iso_z(s["end"]),
                int((s["end"] - s["start"]).total_seconds()), s["events"], s["purchases"],
                fmt_money(s["revenue"]),
            ])

    # Daily summary, grouped by UTC date of session start.
    day_map = {}
    for s in sessions:
        date = s["start"].strftime("%Y-%m-%d")
        day = day_map.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal(0),
                                        "user_revenue": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["user_revenue"][s["user"]] = day["user_revenue"].get(s["user"], Decimal(0)) + s["revenue"]
    days = []
    for date in sorted(day_map):
        d = day_map[date]
        top_user = None
        if d["revenue"] > 0:
            best = max(d["user_revenue"].values())
            top_user = min(u for u, r in d["user_revenue"].items() if r == best)
        days.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top_user,
        })
    daily = {"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(args.output_dir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.output_dir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
