"""Reference implementation of specs/sessionize/SPEC.md. Never shown to a coding agent."""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from decimal import Decimal

V2 = False  # the v2 reference flips this
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
TYPES = {"view", "click", "purchase"} | ({"logout", "refund"} if V2 else set())
UTC = dt.timezone.utc


class Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"usage error: {message}\n")
        sys.exit(64)


def parse_ts(s):
    m = TS_RE.match(s.strip())
    if not m:
        return None
    date, time, frac, off = m.groups()
    try:
        base = dt.datetime.strptime(f"{date}T{time}", "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    micro = int(((frac or ".0")[1:] + "000000")[:6])
    if off == "Z":
        delta = dt.timedelta(0)
    else:
        hh, mm = int(off[1:3]), int(off[4:6])
        if hh > 23 or mm > 59:
            return None
        delta = dt.timedelta(hours=hh, minutes=mm) * (1 if off[0] == "+" else -1)
    return (base.replace(microsecond=micro) - delta).replace(tzinfo=UTC)


def fmt(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def money(d):
    return f"{d:.2f}"


def main(argv):
    ap = Parser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    if V2:
        ap.add_argument("--gap-minutes", default="30")
    a = ap.parse_args(argv)
    gap_min = 30
    if V2:
        if not re.match(r"^\d+$", a.gap_minutes) or int(a.gap_minutes) < 1:
            ap.error("--gap-minutes must be a positive integer")
        gap_min = int(a.gap_minutes)
    gap = dt.timedelta(minutes=gap_min)
    src = os.path.join(a.inp, "events.jsonl")
    if not os.path.isfile(src):
        sys.stderr.write(f"error: {src} not found\n")
        return 2
    rejects, events = [], []
    with open(src, encoding="utf-8") as f:
        for n, line in enumerate(f.read().split("\n"), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError
            except ValueError:
                rejects.append((n, "bad_json")); continue
            if any(not isinstance(obj.get(k), str) or not obj[k].strip() for k in ("event_id", "user", "ts", "type")):
                rejects.append((n, "missing_field")); continue
            ts = parse_ts(obj["ts"])
            if ts is None:
                rejects.append((n, "bad_ts")); continue
            if obj["type"] not in TYPES:
                rejects.append((n, "bad_type")); continue
            amount = Decimal(0)
            if obj["type"] in ("purchase", "refund"):
                if not isinstance(obj.get("amount"), str) or not AMOUNT_RE.match(obj["amount"].strip()):
                    rejects.append((n, "bad_amount")); continue
                amount = Decimal(obj["amount"].strip())
            events.append({"id": obj["event_id"], "user": obj["user"].strip().lower(), "ts": ts,
                           "type": obj["type"], "amount": amount, "n": n})
    best = {}
    for e in events:
        cur = best.get(e["id"])
        if cur is None or (e["ts"], e["n"]) < (cur["ts"], cur["n"]):
            best[e["id"]] = e
    kept = sorted(best.values(), key=lambda e: (e["ts"], e["n"]))
    dropped = len(events) - len(kept)

    sessions, open_s, counts = [], {}, {}
    for e in kept:
        s = open_s.get(e["user"])
        if s is None or e["ts"] - s["last"] > gap or s["closed"]:
            counts[e["user"]] = counts.get(e["user"], 0) + 1
            s = {"user": e["user"], "id": f"{e['user']}-{counts[e['user']]}", "start": e["ts"], "last": e["ts"],
                 "events": 0, "purchases": 0, "refunds": 0, "revenue": Decimal(0), "closed": False}
            open_s[e["user"]] = s
            sessions.append(s)
        s["last"] = e["ts"]
        s["events"] += 1
        if e["type"] == "purchase":
            s["purchases"] += 1
            s["revenue"] += e["amount"]
        elif e["type"] == "refund":
            s["refunds"] += 1
            s["revenue"] -= e["amount"]
        elif e["type"] == "logout":
            s["closed"] = True
    sessions.sort(key=lambda s: (s["start"], s["user"]))

    os.makedirs(a.out, exist_ok=True)
    cols = ["session_id", "user", "start", "end", "duration_s", "events", "purchases"] + (["refunds"] if V2 else []) + ["revenue"]
    with open(os.path.join(a.out, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for s in sessions:
            row = {"session_id": s["id"], "user": s["user"], "start": fmt(s["start"]), "end": fmt(s["last"]),
                   "duration_s": int((s["last"] - s["start"]).total_seconds()), "events": s["events"],
                   "purchases": s["purchases"], "refunds": s["refunds"], "revenue": money(s["revenue"])}
            w.writerow([row[c] for c in cols])
    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].strftime("%Y-%m-%d"), {"sessions": 0, "rev": {}})
        d["sessions"] += 1
        d["rev"][s["user"]] = d["rev"].get(s["user"], Decimal(0)) + s["revenue"]
    out_days = []
    for date in sorted(days):
        d = days[date]
        total = sum(d["rev"].values(), Decimal(0))
        top_user, top_rev = min(d["rev"].items(), key=lambda kv: (-kv[1], kv[0]))
        if V2:
            top = top_user if top_rev > 0 else None
        else:
            top = top_user if total > 0 else None
        out_days.append({"date": date, "sessions": d["sessions"], "users": len(d["rev"]), "revenue": money(total), "top_user": top})
    summary = {"days": out_days, "duplicates_dropped": dropped, "rejected": len(rejects)}
    if V2:
        summary["gap_minutes"] = gap_min
    with open(os.path.join(a.out, "daily.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(a.out, "rejects.jsonl"), "w", encoding="utf-8") as f:
        for n, reason in rejects:
            f.write(json.dumps({"line": n, "reason": reason}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
