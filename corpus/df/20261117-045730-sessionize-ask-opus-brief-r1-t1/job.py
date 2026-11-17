#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

SESSION_GAP = timedelta(minutes=30)
TYPES = ("view", "click", "purchase")

TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:[.,](\d+))?"
    r"(?:(Z|z)|([+-])(\d{2})(?::?(\d{2}))?)$"
)
AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(s):
    m = TS_RE.match(s)
    if not m:
        return None
    y, mo, d, h, mi, se, frac, z, sign, oh, om = m.groups()
    try:
        micro = int((frac or "0")[:6].ljust(6, "0"))
        if z:
            tz = timezone.utc
        else:
            ohi, omi = int(oh), int(om or 0)
            if ohi > 23 or omi > 59:
                return None
            off = timedelta(hours=ohi, minutes=omi)
            tz = timezone(-off if sign == "-" else off)
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se), micro, tzinfo=tz)
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate(text):
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for k in ("event_id", "user", "ts", "type"):
        if not nonempty_str(obj.get(k)):
            raise Reject("missing_field")
    ts = parse_ts(obj["ts"])
    if ts is None:
        raise Reject("bad_ts")
    typ = obj["type"]
    if typ not in TYPES:
        raise Reject("bad_type")
    amount = Decimal("0")
    if typ == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or not AMOUNT_RE.match(a):
            raise Reject("bad_amount")
        amount = Decimal(a)
    return {
        "event_id": obj["event_id"],
        "user": obj["user"].strip().lower(),
        "ts": ts,
        "type": typ,
        "amount": amount,
    }


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return str(d.quantize(Decimal("0.01")))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
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

    rejects = []
    valid = []  # (line_no, event)
    raw_lines = data.split(b"\n")
    for idx, raw in enumerate(raw_lines):
        line_no = idx + 1
        if idx == 0 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": line_no, "reason": "bad_json"})
            continue
        if text.strip() == "":
            continue
        try:
            ev = validate(text)
        except Reject as r:
            rejects.append({"line": line_no, "reason": r.reason})
            continue
        valid.append((line_no, ev))

    # Deduplicate: keep the copy with earliest ts, ties -> first in file.
    kept = {}
    for line_no, ev in valid:
        cur = kept.get(ev["event_id"])
        if cur is None or (ev["ts"], line_no) < (cur[1]["ts"], cur[0]):
            kept[ev["event_id"]] = (line_no, ev)
    duplicates = len(valid) - len(kept)

    by_user = defaultdict(list)
    for line_no, ev in kept.values():
        by_user[ev["user"]].append((ev["ts"], line_no, ev))

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda x: (x[0], x[1]))
        groups = []
        for ts, _, ev in evs:
            if groups and ts - groups[-1][-1]["ts"] <= SESSION_GAP:
                groups[-1].append(ev)
            else:
                groups.append([ev])
        for n, g in enumerate(groups, 1):
            start, end = g[0]["ts"], g[-1]["ts"]
            purchases = [e for e in g if e["type"] == "purchase"]
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": start,
                "end": end,
                "duration_s": (end - start) // timedelta(seconds=1),
                "events": len(g),
                "purchases": len(purchases),
                "revenue": sum((e["amount"] for e in purchases), Decimal("0")),
            })
    sessions.sort(key=lambda s: (s["start"].replace(microsecond=0), s["user"], s["start"]))

    days = {}
    for s in sessions:
        date = s["start"].date().isoformat()
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0"),
                                   "user_rev": defaultdict(Decimal)})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["user_rev"][s["user"]] += s["revenue"]
    days_out = []
    for date in sorted(days):
        d = days[date]
        top = None
        cands = [(-rev, u) for u, rev in d["user_rev"].items() if rev > 0]
        if cands:
            top = min(cands)[1]
        days_out.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })

    rejects.sort(key=lambda r: r["line"])
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        s["duration_s"], s["events"], s["purchases"], fmt_money(s["revenue"])])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": days_out, "duplicates_dropped": duplicates, "rejected": len(rejects)},
                  f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
