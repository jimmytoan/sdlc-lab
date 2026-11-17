#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

GAP = timedelta(minutes=30)
TYPES = {"view", "click", "purchase", "logout", "refund"}
AMOUNT_TYPES = {"purchase", "refund"}
GAP_RE = re.compile(r"[0-9]+")
AMOUNT_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)")
CENT = Decimal("0.01")


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _present(v):
    return v is not None and not (isinstance(v, str) and v.strip() == "")


def parse_ts(v):
    if not isinstance(v, str):
        raise Reject("bad_ts")
    s = v.strip()
    if len(s) < 11:  # date-only strings cannot carry an offset
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    try:
        return dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise Reject("bad_ts")


def parse_amount(v):
    if not isinstance(v, str):
        raise Reject("bad_amount")
    s = v.strip()
    if not AMOUNT_RE.fullmatch(s):
        raise Reject("bad_amount")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise Reject("bad_amount")
    if not d.is_finite() or d < 0:
        raise Reject("bad_amount")
    return d


def parse_event(text):
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")
    for f in ("event_id", "user", "ts", "type"):
        if f not in obj or not _present(obj[f]):
            raise Reject("missing_field")
    eid = obj["event_id"]
    if isinstance(eid, bool) or not isinstance(eid, (str, int)):
        raise Reject("missing_field")
    user = obj["user"]
    if not isinstance(user, str):
        raise Reject("missing_field")
    etype = obj["type"]
    if etype in AMOUNT_TYPES and ("amount" not in obj or not _present(obj["amount"])):
        raise Reject("missing_field")
    ts = parse_ts(obj["ts"])
    if not isinstance(etype, str) or etype not in TYPES:
        raise Reject("bad_type")
    amount = Decimal(0)
    if etype in AMOUNT_TYPES:
        amount = parse_amount(obj["amount"])
    return {
        "id": eid if isinstance(eid, str) else str(eid),
        "user": user.strip().lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,
    }


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return str(q)


def read_lines(path):
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        if "--gap-minutes" in message:
            self.print_usage(sys.stderr)
            print(f"{self.prog}: error: {message}", file=sys.stderr)
            sys.exit(64)
        super().error(message)


def main(argv=None):
    ap = _Parser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--gap-minutes", dest="gap", default=None)
    args = ap.parse_args(argv)

    gap_minutes = 30
    if args.gap is not None:
        g = args.gap.strip()
        if not GAP_RE.fullmatch(g) or int(g) <= 0:
            print(f"error: --gap-minutes must be a positive integer, got {args.gap!r}", file=sys.stderr)
            return 64
        gap_minutes = int(g)
    gap = timedelta(minutes=gap_minutes)

    path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(path):
        print(f"error: input file not found: {path}", file=sys.stderr)
        return 2
    try:
        raw_lines = read_lines(path)
    except OSError as e:
        print(f"error: cannot read input file {path}: {e}", file=sys.stderr)
        return 2

    rejects = []
    seen = set()
    duplicates = 0
    events = []
    for no, raw in enumerate(raw_lines, start=1):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append({"line": no, "reason": "bad_json"})
            continue
        if text.strip() == "":
            continue
        try:
            e = parse_event(text)
        except Reject as r:
            rejects.append({"line": no, "reason": r.reason})
            continue
        if e["id"] in seen:
            duplicates += 1
            continue
        seen.add(e["id"])
        e["seq"] = no
        events.append(e)

    # Build sessions per user
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda x: (x["ts"], x["seq"]))
        cur = None
        n = 0
        for e in evs:
            if cur is None or cur["closed"] or e["ts"] - cur["end"] > gap:
                n += 1
                cur = {"user": user, "n": n, "start": e["ts"], "end": e["ts"], "closed": False,
                       "events": 0, "purchases": 0, "refunds": 0, "revenue": Decimal(0)}
                sessions.append(cur)
            cur["end"] = e["ts"]
            cur["events"] += 1
            if e["type"] == "purchase":
                cur["purchases"] += 1
                cur["revenue"] += e["amount"]
            elif e["type"] == "refund":
                cur["refunds"] += 1
                cur["revenue"] -= e["amount"]
            elif e["type"] == "logout":
                cur["closed"] = True
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"])
        for s in sessions:
            dur = int((s["end"] - s["start"]).total_seconds())
            w.writerow([f"{s['user']}-{s['n']}", s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                        dur, s["events"], s["purchases"], s["refunds"], fmt_money(s["revenue"])])

    days = {}
    for s in sessions:
        d = s["start"].date().isoformat()
        day = days.setdefault(d, {"sessions": 0, "users": set(), "revenue": Decimal(0), "per_user": {}})
        day["sessions"] += 1
        day["users"].add(s["user"])
        day["revenue"] += s["revenue"]
        day["per_user"][s["user"]] = day["per_user"].get(s["user"], Decimal(0)) + s["revenue"]
    day_list = []
    for d in sorted(days):
        day = days[d]
        top = None
        best = Decimal(0)
        for u in sorted(day["per_user"]):
            if day["per_user"][u] > best:
                best = day["per_user"][u]
                top = u
        day_list.append({"date": d, "sessions": day["sessions"], "users": len(day["users"]),
                         "revenue": fmt_money(day["revenue"]), "top_user": top})
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
