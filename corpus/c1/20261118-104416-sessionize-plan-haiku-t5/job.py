#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
Reads <INPUT_DIR>/events.jsonl; writes sessions.csv, daily.json, rejects.jsonl.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

GAP = timedelta(minutes=30)
CENT = Decimal("0.01")
VALID_TYPES = ("view", "click", "purchase")
DECIMAL_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)$")
SESSION_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _nonblank_str(v):
    return isinstance(v, str) and v.strip() != ""


def parse_line(raw_bytes, first_line):
    """Validate one physical line. Returns a dict of normalized fields or raises Reject."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise Reject("bad_json")
    if first_line and text.startswith("\ufeff"):
        text = text[1:]
    if text.endswith("\r"):
        text = text[:-1]
    if text.strip() == "":
        return None  # blank line: skipped silently
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        raise Reject("bad_json")
    if not isinstance(obj, dict):
        raise Reject("bad_json")

    # 2. missing_field
    event_id = obj.get("event_id")
    user = obj.get("user")
    ts = obj.get("ts")
    typ = obj.get("type")
    for key in ("event_id", "user", "ts", "type"):
        v = obj.get(key)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            raise Reject("missing_field")
    if not isinstance(event_id, str) or not isinstance(user, str):
        raise Reject("missing_field")
    if isinstance(typ, str) and typ == "purchase":
        if obj.get("amount") is None:
            raise Reject("missing_field")
    user = user.strip().lower()
    if user == "":
        raise Reject("missing_field")
    event_id = event_id.strip()

    # 3. bad_ts
    if not isinstance(ts, str):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError:
        raise Reject("bad_ts")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise Reject("bad_ts")
    dt = dt.astimezone(timezone.utc)

    # 4. bad_type
    if not isinstance(typ, str) or typ not in VALID_TYPES:
        raise Reject("bad_type")

    # 5. bad_amount (purchase only)
    amount = None
    if typ == "purchase":
        a = obj.get("amount")
        if not isinstance(a, str) or not DECIMAL_RE.match(a):
            raise Reject("bad_amount")
        try:
            d = Decimal(a)
        except (InvalidOperation, ValueError):
            raise Reject("bad_amount")
        if not d.is_finite() or d < 0:
            raise Reject("bad_amount")
        if d != d.quantize(CENT):
            raise Reject("bad_amount")
        amount = d.quantize(CENT)

    return {"event_id": event_id, "user": user, "ts": dt, "type": typ, "amount": amount}


def fmt_ts(dt):
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return format(d.quantize(CENT), "f")


def build(input_bytes):
    rejects = []
    duplicates = 0
    seen = set()
    kept = []
    lines = input_bytes.split(b"\n")
    if lines and lines[-1] == b"" :
        lines.pop()  # file ends with newline (or is empty)
    for idx, raw in enumerate(lines):
        lineno = idx + 1
        try:
            ev = parse_line(raw, first_line=(idx == 0))
        except Reject as r:
            rejects.append({"line": lineno, "reason": r.reason})
            continue
        if ev is None:
            continue
        if ev["event_id"] in seen:
            duplicates += 1
            continue
        seen.add(ev["event_id"])
        kept.append(ev)

    by_user = {}
    for ev in kept:
        by_user.setdefault(ev["user"], []).append(ev)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        cur = []
        for ev in evs:
            if cur and ev["ts"] - cur[-1]["ts"] > GAP:
                groups.append(cur)
                cur = []
            cur.append(ev)
        if cur:
            groups.append(cur)
        for n, grp in enumerate(groups, start=1):
            start = grp[0]["ts"]
            end = grp[-1]["ts"]
            duration = int((end - start).total_seconds() // 1)
            purchases = [e for e in grp if e["type"] == "purchase"]
            revenue = sum((e["amount"] for e in purchases), Decimal("0.00")).quantize(CENT)
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start_dt": start,
                "start": fmt_ts(start),
                "end": fmt_ts(end),
                "duration_s": duration,
                "events": len(grp),
                "purchases": len(purchases),
                "revenue": revenue,
            })
    sessions.sort(key=lambda s: (s["start_dt"], s["user"], int(s["session_id"].rsplit("-", 1)[1])))

    # daily summary, by UTC date of session start
    day_map = {}
    for s in sessions:
        date = s["start_dt"].date().isoformat()
        d = day_map.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal("0.00"), "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal("0.00")) + s["revenue"]
    days = []
    for date in sorted(day_map):
        d = day_map[date]
        total = d["revenue"].quantize(CENT)
        top = None
        if total > 0:
            best = max(d["per_user"].values())
            top = min(u for u, v in d["per_user"].items() if v == best)
        days.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(total),
            "top_user": top,
        })

    daily = {"days": days, "duplicates_dropped": duplicates, "rejected": len(rejects)}
    return sessions, daily, rejects


def render_sessions(sessions):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(SESSION_HEADER)
    for s in sessions:
        w.writerow([s["session_id"], s["user"], s["start"], s["end"], s["duration_s"],
                    s["events"], s["purchases"], fmt_money(s["revenue"])])
    return buf.getvalue()


def render_daily(daily):
    return json.dumps(daily, indent=2, ensure_ascii=False) + "\n"


def render_rejects(rejects):
    return "".join(json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n" for r in rejects)


def write_outputs(out_dir, files):
    """Write all files via temp files in out_dir, then os.replace into place."""
    tmps = []
    try:
        for name, content in files:
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name + ".", suffix=".tmp")
            tmps.append((tmp, name))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        for tmp, name in tmps:
            os.replace(tmp, os.path.join(out_dir, name))
        tmps = []
    finally:
        for tmp, _ in tmps:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sessionize clickstream events.")
    ap.add_argument("--in", dest="inp", required=True, metavar="INPUT_DIR")
    ap.add_argument("--out", dest="out", required=True, metavar="OUTPUT_DIR")
    args = ap.parse_args(argv)

    in_path = os.path.join(args.inp, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2
    try:
        with open(in_path, "rb") as f:
            data = f.read()
        os.makedirs(args.out, exist_ok=True)
        sessions, daily, rejects = build(data)
        write_outputs(args.out, [
            ("sessions.csv", render_sessions(sessions)),
            ("daily.json", render_daily(daily)),
            ("rejects.jsonl", render_rejects(rejects)),
        ])
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
