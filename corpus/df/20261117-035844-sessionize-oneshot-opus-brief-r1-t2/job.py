#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

GAP = timedelta(minutes=30)
TYPES = {"view", "click", "purchase"}
CENT = Decimal("0.01")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def fmt_money(d):
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def fmt_ts(dt):
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def present(obj, key):
    v = obj.get(key)
    if v is None:
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    return True


def parse_event(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    for key in ("event_id", "user", "ts", "type"):
        if not present(obj, key):
            raise Reject("missing_field")
    eid = obj["event_id"]
    if isinstance(eid, bool) or not isinstance(eid, (str, int)):
        raise Reject("missing_field")
    eid = str(eid)
    user = obj["user"]
    if not isinstance(user, str):
        raise Reject("missing_field")
    user = user.strip().lower()

    etype = obj["type"]
    if etype == "purchase" and not present(obj, "amount"):
        raise Reject("missing_field")

    ts = obj["ts"]
    if not isinstance(ts, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    try:
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise Reject("bad_ts")

    if not isinstance(etype, str) or etype not in TYPES:
        raise Reject("bad_type")

    amount = Decimal(0)
    if etype == "purchase":
        a = obj["amount"]
        if not isinstance(a, str):
            raise Reject("bad_amount")
        try:
            amount = Decimal(a.strip())
        except (InvalidOperation, ValueError):
            raise Reject("bad_amount")
        if not amount.is_finite() or amount < 0:
            raise Reject("bad_amount")

    return {"event_id": eid, "user": user, "ts": dt, "type": etype, "amount": amount}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args(argv)

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"error: cannot read input file {path}: {e}", file=sys.stderr)
        return 2

    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    rejects = []
    seen = set()
    duplicates = 0
    events = []
    for idx, bline in enumerate(lines, start=1):
        if idx == 1 and bline.startswith(b"\xef\xbb\xbf"):
            bline = bline[3:]
        if bline.endswith(b"\r"):
            bline = bline[:-1]
        if bline.strip() == b"":
            continue
        try:
            try:
                text = bline.decode("utf-8")
            except UnicodeDecodeError:
                raise Reject("bad_json")
            e = parse_event(text)
        except Reject as r:
            rejects.append({"line": idx, "reason": r.reason})
            continue
        if e["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(e["event_id"])
        e["line"] = idx
        events.append(e)

    # group per user
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        cur = None
        n = 0
        for e in evs:
            if cur is None or e["ts"] - cur["end"] > GAP:
                n += 1
                cur = {"session_id": f"{user}-{n}", "user": user, "num": n,
                       "start": e["ts"], "end": e["ts"], "events": 0,
                       "purchases": 0, "revenue": Decimal(0)}
                sessions.append(cur)
            cur["end"] = e["ts"]
            cur["events"] += 1
            if e["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]

    sessions.sort(key=lambda s: (s["start"], s["user"], s["num"]))

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s",
                    "events", "purchases", "revenue"])
        for s in sessions:
            dur = int((s["end"] - s["start"]).total_seconds())
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        dur, s["events"], s["purchases"], fmt_money(s["revenue"])])

    days = {}
    for s in sessions:
        d = s["start"].date().isoformat()
        day = days.setdefault(d, {"sessions": 0, "users": set(), "revenue": Decimal(0), "by_user": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["by_user"][s["user"]] = day["by_user"].get(s["user"], Decimal(0)) + s["revenue"]

    out_days = []
    for d in sorted(days):
        day = days[d]
        top = None
        best = Decimal(0)
        for u in sorted(day["by_user"]):
            r = day["by_user"][u]
            if r > best:
                best = r
                top = u
        out_days.append({"date": d, "sessions": day["sessions"], "users": len(day["users"]),
                         "revenue": fmt_money(day["revenue"]), "top_user": top})

    daily = {"days": out_days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
