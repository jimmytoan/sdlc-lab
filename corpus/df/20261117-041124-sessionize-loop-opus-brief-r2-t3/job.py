#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

TYPES = {"view", "click", "purchase", "logout", "refund"}
AMOUNT_TYPES = {"purchase", "refund"}
CENT = Decimal("0.01")


def money(d):
    if d == 0:
        d = Decimal(0)
    return str(d.quantize(CENT, rounding=ROUND_HALF_UP))


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if "T" not in s and " " not in s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def parse_amount(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    try:
        d = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def missing(v):
    return v is None or (isinstance(v, str) and not v.strip())


def validate(obj):
    """Return (event, None) or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "bad_json"
    for f in ("event_id", "user", "ts", "type"):
        if f not in obj or missing(obj[f]):
            return None, "missing_field"
    eid, user = obj["event_id"], obj["user"]
    if isinstance(eid, (dict, list, bool)) or not isinstance(user, str):
        return None, "missing_field"
    typ = obj["type"]
    if typ in AMOUNT_TYPES and ("amount" not in obj or missing(obj["amount"])):
        return None, "missing_field"
    ts = parse_ts(obj["ts"])
    if ts is None:
        return None, "bad_ts"
    if not isinstance(typ, str) or typ not in TYPES:
        return None, "bad_type"
    amount = Decimal(0)
    if typ in AMOUNT_TYPES:
        amount = parse_amount(obj["amount"])
        if amount is None:
            return None, "bad_amount"
    return {"event_id": json.dumps(eid) if not isinstance(eid, str) else eid,
            "user": user.strip().lower(), "ts": ts, "type": typ, "amount": amount}, None


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_gap(value):
    if value is None:
        return 30
    s = value.strip()
    if not s or not s.isascii() or not s.isdigit():
        return None
    n = int(s)
    return n if n > 0 else None


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)
    # Validate --gap-minutes before argparse so that a missing/bad value exits 64.
    gap_raw = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--gap-minutes":
            if i + 1 >= len(argv):
                print("error: --gap-minutes requires a positive integer", file=sys.stderr)
                return 64
            gap_raw = argv[i + 1]
            i += 2
            continue
        if a.startswith("--gap-minutes="):
            gap_raw = a[len("--gap-minutes="):]
            i += 1
            continue
        rest.append(a)
        i += 1
    gap_minutes = parse_gap(gap_raw)
    if gap_minutes is None:
        print(f"error: --gap-minutes must be a positive integer, got {gap_raw!r}", file=sys.stderr)
        return 64
    gap_seconds = gap_minutes * 60

    ap = argparse.ArgumentParser(description="Sessionize clickstream events")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    args = ap.parse_args(rest)

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
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    rejects = []
    seen = set()
    duplicates = 0
    events = []
    for no, bline in enumerate(lines, start=1):
        if bline.endswith(b"\r"):
            bline = bline[:-1]
        try:
            text = bline.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": no, "reason": "bad_json"})
            continue
        if not text.strip():
            continue
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            rejects.append({"line": no, "reason": "bad_json"})
            continue
        ev, reason = validate(obj)
        if reason:
            rejects.append({"line": no, "reason": reason})
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        ev["line"] = no
        events.append(ev)

    by_user = {}
    for ev in events:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["line"]))
        groups = []
        for ev in evs:
            if (groups and groups[-1][-1]["type"] != "logout"
                    and (ev["ts"] - groups[-1][-1]["ts"]).total_seconds() <= gap_seconds):
                groups[-1].append(ev)
            else:
                groups.append([ev])
        for i, g in enumerate(groups, start=1):
            start, end = g[0]["ts"], g[-1]["ts"]
            purchases = [e for e in g if e["type"] == "purchase"]
            refunds = [e for e in g if e["type"] == "refund"]
            sessions.append({
                "session_id": f"{user}-{i}", "user": user, "n": i,
                "start": start, "end": end,
                "duration_s": int((end - start).total_seconds()),
                "events": len(g), "purchases": len(purchases),
                "refunds": len(refunds),
                "revenue": (sum((e["amount"] for e in purchases), Decimal(0))
                            - sum((e["amount"] for e in refunds), Decimal(0))),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].date().isoformat(),
                            {"sessions": 0, "users": set(), "revenue": Decimal(0), "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], Decimal(0)) + s["revenue"]
    day_list = []
    for date in sorted(days):
        d = days[date]
        top = None
        cands = [(-r, u) for u, r in d["by_user"].items() if r > 0]
        if cands:
            top = min(cands)[1]
        day_list.append({"date": date, "sessions": d["sessions"], "users": len(d["users"]),
                         "revenue": money(d["revenue"]), "top_user": top})

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"])
        for s in sessions:
            w.writerow([s["session_id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        s["duration_s"], s["events"], s["purchases"], s["refunds"], money(s["revenue"])])
    with open(os.path.join(args.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump({"days": day_list, "duplicates_dropped": duplicates, "rejected": len(rejects),
                   "gap_minutes": gap_minutes},
                  f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(os.path.join(args.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
