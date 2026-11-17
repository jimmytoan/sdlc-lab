"""sessionize: clickstream sessions batch job."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from fractions import Fraction

SESSION_GAP = 1800
ALLOWED_TYPES = {"view", "click", "purchase"}

TS_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})"
)
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
EPOCH_NAIVE = datetime(1970, 1, 1)
EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_ts(s):
    """Return (exact epoch seconds as Fraction, utc datetime truncated to seconds) or None."""
    m = TS_RE.fullmatch(s)
    if not m:
        return None
    y, mo, d, h, mi, se, frac, off = m.groups()
    try:
        naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    except ValueError:
        return None
    if off == "Z":
        off_s = 0
    else:
        oh, om = int(off[1:3]), int(off[4:6])
        if oh > 23 or om > 59:
            return None
        off_s = (oh * 3600 + om * 60) * (1 if off[0] == "+" else -1)
    delta = naive - EPOCH_NAIVE
    sec = delta.days * 86400 + delta.seconds - off_s
    try:
        utc = EPOCH_UTC + timedelta(seconds=sec)
    except (OverflowError, ValueError):
        return None
    exact = Fraction(sec)
    if frac:
        exact += Fraction(int(frac), 10 ** len(frac))
    return exact, utc


def parse_amount(a):
    """Return amount in integer cents or None."""
    if not isinstance(a, str):
        return None
    m = AMOUNT_RE.fullmatch(a)
    if not m:
        return None
    whole, fr = m.groups()
    fr = (fr or "").ljust(2, "0")
    return int(whole) * 100 + int(fr)


def fmt_cents(c):
    return f"{c // 100}.{c % 100:02d}"


def fmt_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S").rjust(19, "0") + "Z" if dt.year >= 1000 else \
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


def fmt_date(dt):
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def validate(text):
    """Return (event dict, None) or (None, reason)."""
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    for f in ("event_id", "user", "ts", "type"):
        v = obj.get(f)
        if not isinstance(v, str) or not v.strip():
            return None, "missing_field"
    parsed = parse_ts(obj["ts"])
    if parsed is None:
        return None, "bad_ts"
    if obj["type"] not in ALLOWED_TYPES:
        return None, "bad_type"
    cents = 0
    if obj["type"] == "purchase":
        if "amount" not in obj:
            return None, "bad_amount"
        cents = parse_amount(obj["amount"])
        if cents is None:
            return None, "bad_amount"
    return {
        "event_id": obj["event_id"],
        "user": obj["user"].strip().lower(),
        "ts": parsed[0],
        "utc": parsed[1],
        "type": obj["type"],
        "cents": cents,
    }, None


def main(argv=None):
    ap = argparse.ArgumentParser(description="sessionize clickstream events")
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
    events = []
    raw_lines = data.split(b"\n")
    for idx, raw in enumerate(raw_lines):
        lineno = idx + 1
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if idx == 0 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if not raw.strip():
                continue
            rejects.append({"line": lineno, "reason": "bad_json"})
            continue
        if not text.strip():
            continue
        ev, reason = validate(text)
        if reason:
            rejects.append({"line": lineno, "reason": reason})
            continue
        ev["order"] = idx
        events.append(ev)

    # Deduplicate
    kept = {}
    dups = 0
    for ev in events:
        cur = kept.get(ev["event_id"])
        if cur is None:
            kept[ev["event_id"]] = ev
        else:
            dups += 1
            if ev["ts"] < cur["ts"]:
                kept[ev["event_id"]] = ev

    by_user = {}
    for ev in kept.values():
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["order"]))
        groups = []
        for ev in evs:
            if groups and ev["ts"] - groups[-1][-1]["ts"] <= SESSION_GAP:
                groups[-1].append(ev)
            else:
                groups.append([ev])
        for n, g in enumerate(groups, 1):
            first, last = g[0], g[-1]
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start_exact": first["ts"],
                "start_dt": first["utc"],
                "end_dt": last["utc"],
                "duration": int((last["ts"] - first["ts"]) // 1),
                "events": len(g),
                "purchases": sum(1 for e in g if e["type"] == "purchase"),
                "cents": sum(e["cents"] for e in g if e["type"] == "purchase"),
            })
    sessions.sort(key=lambda s: (s["start_exact"], s["user"]))

    os.makedirs(args.out, exist_ok=True)

    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_utc(s["start_dt"]), fmt_utc(s["end_dt"]),
                        s["duration"], s["events"], s["purchases"], fmt_cents(s["cents"])])

    days = {}
    for s in sessions:
        d = fmt_date(s["start_dt"])
        info = days.setdefault(d, {"sessions": 0, "users": {}, "cents": 0})
        info["sessions"] += 1
        info["users"][s["user"]] = info["users"].get(s["user"], 0) + s["cents"]
        info["cents"] += s["cents"]
    day_list = []
    for d in sorted(days):
        info = days[d]
        top = None
        if info["cents"] > 0:
            top = sorted(info["users"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        day_list.append({
            "date": d,
            "sessions": info["sessions"],
            "users": len(info["users"]),
            "revenue": fmt_cents(info["cents"]),
            "top_user": top,
        })
    daily = {"days": day_list, "duplicates_dropped": dups, "rejected": len(rejects)}
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
