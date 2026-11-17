#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

GAP = timedelta(minutes=30)
TYPES = {"view", "click", "purchase"}
AMOUNT_RE = re.compile(r"^[+-]?(\d+(\.\d{0,2})?|\.\d{1,2})$")
TWO = Decimal("0.01")


def fmt_money(d):
    return str(d.quantize(TWO))


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse_amount(value):
    if not isinstance(value, str) or not AMOUNT_RE.match(value):
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0:
        return None
    return abs(d)  # normalises "-0" to 0


def validate(text):
    """Return (event, None) or (None, reason)."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for key in ("event_id", "user"):
        v = obj.get(key)
        if not isinstance(v, str) or v == "":
            return None, "missing_field"
    for key in ("ts", "type"):
        if obj.get(key) is None:
            return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    etype = obj["type"]
    if not isinstance(etype, str) or etype not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if etype == "purchase":
        if "amount" not in obj:
            return None, "bad_amount"
        amount = parse_amount(obj["amount"])
        if amount is None:
            return None, "bad_amount"
    return {
        "event_id": obj["event_id"],
        "user": obj["user"].lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,
    }, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="indir", required=True)
    ap.add_argument("--out", dest="outdir", required=True)
    args = ap.parse_args(argv)

    path = os.path.join(args.indir, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        print(f"error: cannot read input file {path}: {e}", file=sys.stderr)
        return 2

    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    rejects = []
    best = {}  # event_id -> (ts, line_no, event)
    valid_count = 0
    for idx, bline in enumerate(lines, start=1):
        if bline.endswith(b"\r"):
            bline = bline[:-1]
        try:
            text = bline.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": idx, "reason": "bad_json"})
            continue
        if not text.strip():
            continue
        event, reason = validate(text)
        if reason:
            rejects.append({"line": idx, "reason": reason})
            continue
        valid_count += 1
        eid = event["event_id"]
        cur = best.get(eid)
        if cur is None or event["ts"] < cur[0]:
            best[eid] = (event["ts"], idx, event)
    duplicates = valid_count - len(best)

    by_user = {}
    for ts, idx, event in best.values():
        by_user.setdefault(event["user"], []).append((ts, idx, event))

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda t: (t[0], t[1]))
        current = None
        n = 0
        for ts, _, event in evs:
            if current is None or ts - current["end"] > GAP:
                n += 1
                current = {"session_id": f"{user}-{n}", "user": user, "start": ts, "end": ts,
                           "events": 0, "purchases": 0, "revenue": Decimal(0)}
                sessions.append(current)
            current["end"] = ts
            current["events"] += 1
            if event["type"] == "purchase":
                current["purchases"] += 1
                current["revenue"] += event["amount"]
    sessions.sort(key=lambda s: (s["start"], s["user"], s["session_id"]))

    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(args.outdir, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            start = s["start"].replace(microsecond=0)
            dur = int((s["end"] - s["start"]) // timedelta(seconds=1))
            w.writerow([s["session_id"], s["user"], fmt_ts(start), fmt_ts(s["end"]), dur,
                        s["events"], s["purchases"], fmt_money(s["revenue"])])

    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].date().isoformat(),
                            {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal(0)) + s["revenue"]
    day_list = []
    for date in sorted(days):
        d = days[date]
        top = None
        cands = [(-rev, u) for u, rev in d["per_user"].items() if rev > 0]
        if cands:
            top = min(cands)[1]
        day_list.append({"date": date, "sessions": d["sessions"], "users": len(d["users"]),
                         "revenue": fmt_money(d["revenue"]), "top_user": top})
    with open(os.path.join(args.outdir, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")
    with open(os.path.join(args.outdir, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
