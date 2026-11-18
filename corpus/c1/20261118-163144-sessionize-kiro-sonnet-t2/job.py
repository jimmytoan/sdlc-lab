import argparse
import csv
import io
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

GAP = 1800
TYPES = {"view", "click", "purchase"}


def validate_line(text):
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return "bad_json"
    if not isinstance(obj, dict):
        return "bad_json"
    for k in ("event_id", "user", "ts", "type"):
        if not isinstance(obj.get(k), str):
            return "missing_field"
    if not obj["event_id"].strip() or not obj["user"].strip():
        return "missing_field"
    try:
        ts = datetime.fromisoformat(obj["ts"])
        if ts.utcoffset() is None:
            return "bad_ts"
        ts = ts.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return "bad_ts"
    typ = obj["type"]
    if typ not in TYPES:
        return "bad_type"
    amount = Decimal(0)
    if typ == "purchase":
        a = obj.get("amount")
        if a is None:
            return "missing_field"
        if not isinstance(a, str):
            return "bad_amount"
        s = a.strip()
        if not s or "_" in s:
            return "bad_amount"
        try:
            amount = Decimal(s)
        except InvalidOperation:
            return "bad_amount"
        if not amount.is_finite() or amount < 0:
            return "bad_amount"
    return {"id": obj["event_id"], "user": obj["user"].strip().lower(),
            "ts": ts, "type": typ, "amount": amount}


def read_events(path):
    data = Path(path).read_bytes()
    events, rejects = [], []
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    for i, raw in enumerate(lines, 1):
        if i == 1 and raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            rejects.append((i, "bad_json"))
            continue
        if not text.strip():
            continue
        r = validate_line(text)
        if isinstance(r, str):
            rejects.append((i, r))
        else:
            r["line"] = i
            events.append(r)
    return events, rejects


def dedupe(events):
    seen, out, dropped = set(), [], 0
    for e in events:
        if e["id"] in seen:
            dropped += 1
        else:
            seen.add(e["id"])
            out.append(e)
    return out, dropped


def build_sessions(events):
    by_user = {}
    for e in events:
        by_user.setdefault(e["user"], []).append(e)
    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["id"]))
        groups = []
        for e in evs:
            if groups and (e["ts"] - groups[-1][-1]["ts"]).total_seconds() <= GAP:
                groups[-1].append(e)
            else:
                groups.append([e])
        for n, g in enumerate(groups, 1):
            rev = sum((x["amount"] for x in g if x["type"] == "purchase"), Decimal(0))
            sessions.append({
                "id": f"{user}-{n}", "user": user, "n": n,
                "start": g[0]["ts"], "end": g[-1]["ts"],
                "events": len(g),
                "purchases": sum(1 for x in g if x["type"] == "purchase"),
                "revenue": rev.quantize(Decimal("0.01"), ROUND_HALF_UP),
            })
    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))
    return sessions


def fmt_ts(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    d = d.quantize(Decimal("0.01"), ROUND_HALF_UP)
    if d == 0:
        d = Decimal("0.00")
    return format(d, "f")


def summarise_days(sessions):
    days = {}
    for s in sessions:
        days.setdefault(s["start"].strftime("%Y-%m-%d"), []).append(s)
    out = []
    for date in sorted(days):
        ss = days[date]
        per_user = {}
        for s in ss:
            per_user[s["user"]] = per_user.get(s["user"], Decimal(0)) + s["revenue"]
        total = sum(per_user.values(), Decimal(0))
        top = None
        if total > 0:
            top = sorted(per_user.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append({"date": date, "sessions": len(ss), "users": len(per_user),
                    "revenue": fmt_money(total), "top_user": top})
    return out


def render(sessions, days, dropped, rejects):
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"])
    for s in sessions:
        w.writerow([s["id"], s["user"], fmt_ts(s["start"]), fmt_ts(s["end"]),
                    int((s["end"] - s["start"]).total_seconds()), s["events"],
                    s["purchases"], fmt_money(s["revenue"])])
    rj = "".join(json.dumps({"line": l, "reason": r}) + "\n" for l, r in rejects)
    dj = json.dumps({"days": days, "duplicates_dropped": dropped, "rejected": len(rejects)},
                    ensure_ascii=False, indent=2) + "\n"
    return buf.getvalue(), dj, rj


def main(argv=None):
    p = argparse.ArgumentParser(prog="job.py")
    p.add_argument("--in", dest="in_dir", required=True)
    p.add_argument("--out", dest="out_dir", required=True)
    args = p.parse_args(argv)
    src = Path(args.in_dir) / "events.jsonl"
    if not src.is_file():
        print(f"error: input file not found: {src}", file=sys.stderr)
        return 2
    try:
        events, rejects = read_events(src)
        events, dropped = dedupe(events)
        sessions = build_sessions(events)
        days = summarise_days(sessions)
        csv_t, dj, rj = render(sessions, days, dropped, rejects)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, content in (("sessions.csv", csv_t), ("daily.json", dj), ("rejects.jsonl", rj)):
            with open(out / name, "w", encoding="utf-8", newline="") as f:
                f.write(content)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
