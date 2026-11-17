#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
from decimal import Decimal
from fractions import Fraction

SESSION_GAP = 1800
TYPES = ("view", "click", "purchase")
EPOCH = dt.datetime(1970, 1, 1)

TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
    r"(?:([Zz])|([+-])(\d{2}):?(\d{2}))$"
)
AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    """Return UTC epoch seconds as an exact Fraction, or raise Reject('bad_ts')."""
    if not isinstance(value, str):
        raise Reject("bad_ts")
    m = TS_RE.match(value)
    if not m:
        raise Reject("bad_ts")
    y, mo, d, h, mi, s, frac, z, sign, oh, om = m.groups()
    try:
        naive = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError:
        raise Reject("bad_ts")
    if z:
        offset = 0
    else:
        oh, om = int(oh), int(om)
        if oh > 23 or om > 59:
            raise Reject("bad_ts")
        offset = (oh * 3600 + om * 60) * (1 if sign == "+" else -1)
    delta = naive - EPOCH
    whole = delta.days * 86400 + delta.seconds - offset
    # make sure the UTC instant is representable for output
    try:
        EPOCH + dt.timedelta(seconds=whole + 1)
        EPOCH + dt.timedelta(seconds=whole)
    except OverflowError:
        raise Reject("bad_ts")
    f = Fraction(int(frac), 10 ** len(frac)) if frac else Fraction(0)
    return Fraction(whole) + f


def fmt_ts(epoch):
    t = EPOCH + dt.timedelta(seconds=math.floor(epoch))
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (t.year, t.month, t.day, t.hour, t.minute, t.second)


def utc_date(epoch):
    t = EPOCH + dt.timedelta(seconds=math.floor(epoch))
    return "%04d-%02d-%02d" % (t.year, t.month, t.day)


def fmt_money(d):
    return str(d.quantize(Decimal("0.01")))


def validate(line):
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for key in ("event_id", "user"):
        v = obj.get(key)
        if not isinstance(v, str) or v.strip() == "":
            raise Reject("missing_field")
    for key in ("ts", "type"):
        if obj.get(key) is None:
            raise Reject("missing_field")
    ts = parse_ts(obj["ts"])
    typ = obj["type"]
    if not isinstance(typ, str) or typ not in TYPES:
        raise Reject("bad_type")
    amount = Decimal(0)
    if typ == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or not AMOUNT_RE.match(a):
            raise Reject("bad_amount")
        amount = Decimal(a)
    return {"event_id": obj["event_id"], "user": obj["user"].lower(), "ts": ts,
            "type": typ, "amount": amount}


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
            raw = f.read()
    except OSError as e:
        print(f"error: cannot read input file {path}: {e}", file=sys.stderr)
        return 2
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]

    rejects = []
    valid = []  # (line_no, event)
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for no, bline in enumerate(lines, start=1):
        if bline.endswith(b"\r"):
            bline = bline[:-1]
        try:
            line = bline.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": no, "reason": "bad_json"})
            continue
        if line.strip() == "":
            continue
        try:
            ev = validate(line)
        except Reject as r:
            rejects.append({"line": no, "reason": r.reason})
            continue
        ev["line"] = no
        valid.append(ev)

    # de-duplicate: keep earliest ts, ties -> first in file
    kept = {}
    for ev in valid:
        cur = kept.get(ev["event_id"])
        if cur is None or (ev["ts"], ev["line"]) < (cur["ts"], cur["line"]):
            kept[ev["event_id"]] = ev
    duplicates = len(valid) - len(kept)

    by_user = {}
    for ev in kept.values():
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        groups = []
        for ev in evs:
            if groups and ev["ts"] - groups[-1][-1]["ts"] <= SESSION_GAP:
                groups[-1].append(ev)
            else:
                groups.append([ev])
        for i, g in enumerate(groups, start=1):
            start, end = g[0]["ts"], g[-1]["ts"]
            sessions.append({
                "session_id": f"{user}-{i}", "user": user, "n": i,
                "start": start, "end": end,
                "duration_s": math.floor(end - start),
                "events": len(g),
                "purchases": sum(1 for e in g if e["type"] == "purchase"),
                "revenue": sum((e["amount"] for e in g), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    days = {}
    for s in sessions:
        d = days.setdefault(utc_date(s["start"]), {"sessions": 0, "users": {}, "revenue": Decimal(0)})
        d["sessions"] += 1
        d["users"][s["user"]] = d["users"].get(s["user"], Decimal(0)) + s["revenue"]
        d["revenue"] += s["revenue"]
    day_list = []
    for date in sorted(days):
        d = days[date]
        top = None
        best = max(d["users"].values())
        if best > 0:
            top = min(u for u, r in d["users"].items() if r == best)
        day_list.append({"date": date, "sessions": d["sessions"], "users": len(d["users"]),
                         "revenue": fmt_money(d["revenue"]), "top_user": top})

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]), s["duration_s"],
                        s["events"], s["purchases"], fmt_money(s["revenue"])])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects)},
                  f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
