#!/usr/bin/env python3
"""sessionize: clickstream sessions batch job."""
import argparse
import calendar
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

GAP_S = 30 * 60
TYPES = {"view", "click", "purchase"}
TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII)
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?", re.ASCII)
TWO = Decimal("0.01")


def parse_ts(s):
    """Return (epoch_seconds_int, Fraction full precision) or None."""
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
    try:
        datetime(y, mo, d, h, mi, se)
    except ValueError:
        return None
    off = m.group(8)
    if off == "Z":
        off_s = 0
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        off_s = (oh * 3600 + om * 60) * (1 if off[0] == "+" else -1)
    epoch = calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)) - off_s
    try:
        datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None
    frac = m.group(7)
    full = Fraction(epoch)
    if frac:
        full += Fraction(int(frac), 10 ** len(frac))
    return full


def fmt_ts(full):
    sec = full.numerator // full.denominator  # floor
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date(full):
    sec = full.numerator // full.denominator
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d")


def nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate(raw):
    """Return (event dict, None) or (None, reason)."""
    try:
        text = raw.decode("utf-8")
        obj = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for f in ("event_id", "user", "ts", "type"):
        if not nonempty_str(obj.get(f)):
            return None, "missing_field"
    ts = parse_ts(obj["ts"].strip())
    if ts is None:
        return None, "bad_ts"
    typ = obj["type"].strip()
    if typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or not AMOUNT_RE.fullmatch(a):
            return None, "bad_amount"
        amount = Decimal(a)
    return {"event_id": obj["event_id"], "user": obj["user"].strip().lower(), "ts": ts,
            "type": typ, "amount": amount}, None


def money(d):
    return str(d.quantize(TWO))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="job.py")
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
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 2
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]

    rejects = []
    valid = []
    for lineno, raw in enumerate(data.split(b"\n"), start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        try:
            if raw.decode("utf-8").strip() == "":
                continue
        except UnicodeDecodeError:
            pass
        ev, reason = validate(raw)
        if reason:
            rejects.append({"line": lineno, "reason": reason})
        else:
            ev["order"] = lineno
            valid.append(ev)

    # Deduplicate
    best = {}
    for ev in valid:
        cur = best.get(ev["event_id"])
        if cur is None or ev["ts"] < cur["ts"]:
            best[ev["event_id"]] = ev
    kept = list(best.values())
    duplicates = len(valid) - len(kept)

    # Sessions
    by_user = {}
    for ev in kept:
        by_user.setdefault(ev["user"], []).append(ev)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["order"]))
        groups = []
        for e in evs:
            if groups and e["ts"] - groups[-1][-1]["ts"] <= GAP_S:
                groups[-1].append(e)
            else:
                groups.append([e])
        for n, g in enumerate(groups, start=1):
            start, end = g[0]["ts"], g[-1]["ts"]
            diff = end - start
            purchases = [e for e in g if e["type"] == "purchase"]
            sessions.append({
                "session_id": f"{user}-{n}", "user": user, "start": start, "end": end,
                "duration_s": diff.numerator // diff.denominator, "events": len(g),
                "purchases": len(purchases), "revenue": sum((e["amount"] for e in purchases), Decimal(0)),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]), s["duration_s"],
                        s["events"], s["purchases"], money(s["revenue"])])

    days = {}
    for s in sessions:
        days.setdefault(utc_date(s["start"]), []).append(s)
    day_list = []
    for date in sorted(days):
        ss = days[date]
        per_user = {}
        for s in ss:
            per_user[s["user"]] = per_user.get(s["user"], Decimal(0)) + s["revenue"]
        total = sum((s["revenue"] for s in ss), Decimal(0))
        top = None
        if total != 0:
            top = min(per_user, key=lambda u: (-per_user[u], u))
        day_list.append({"date": date, "sessions": len(ss), "users": len(per_user),
                         "revenue": money(total), "top_user": top})
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects)}, f, indent=2)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
