#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

GAP = timedelta(minutes=30)
ONE_SECOND = timedelta(seconds=1)
INPUT_NAME = "events.jsonl"
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
VALID_TYPES = ("view", "click", "purchase")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
UTF8_BOM = b"\xef\xbb\xbf"


class InputError(Exception):
    """Input file missing or unreadable: exit code 2."""


def fmt_ts(dt):
    """Format a UTC datetime as whole seconds, Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_money(d):
    return f"{d:.2f}"


def _nonempty_str(obj, key):
    v = obj.get(key)
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v if v else None


def validate(obj):
    """Return (event_dict, None) for a valid event or (None, reason)."""
    if not isinstance(obj, dict):
        return None, "bad_json"

    event_id = _nonempty_str(obj, "event_id")
    user = _nonempty_str(obj, "user")
    ts_raw = _nonempty_str(obj, "ts")
    type_raw = _nonempty_str(obj, "type")
    if event_id is None or user is None or ts_raw is None or type_raw is None:
        return None, "missing_field"

    etype = type_raw
    if etype == "purchase" and obj.get("amount") is None:
        return None, "missing_field"

    if etype not in VALID_TYPES:
        return None, "bad_type"

    try:
        ts = datetime.fromisoformat(ts_raw)
    except (ValueError, TypeError):
        return None, "bad_ts"
    if ts.tzinfo is None or ts.utcoffset() is None:
        return None, "bad_ts"
    ts = ts.astimezone(timezone.utc)

    amount = Decimal(0)
    if etype == "purchase":
        raw = obj.get("amount")
        if not isinstance(raw, str):
            return None, "bad_amount"
        raw = raw.strip()
        if not AMOUNT_RE.fullmatch(raw):
            return None, "bad_amount"
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return None, "bad_amount"

    return {
        "event_id": event_id,
        "user": user.lower(),
        "ts": ts,
        "type": etype,
        "amount": amount,
    }, None


def read_events(path):
    """Stream the input file. Yields ('ok', lineno, event) or ('reject', lineno, reason)."""
    with open(path, "rb") as f:
        lineno = 0
        for raw in f:
            lineno += 1
            if lineno == 1 and raw.startswith(UTF8_BOM):
                raw = raw[len(UTF8_BOM):]
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if raw.strip() == b"":
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                yield ("reject", lineno, "bad_json")
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                yield ("reject", lineno, "bad_json")
                continue
            event, reason = validate(obj)
            if reason is not None:
                yield ("reject", lineno, reason)
            else:
                yield ("ok", lineno, event)


def build(events_path):
    """Process the input file. Returns (sessions, daily, rejects, duplicates_dropped)."""
    rejects = []
    seen_ids = set()
    duplicates = 0
    by_user = {}

    for kind, lineno, payload in read_events(events_path):
        if kind == "reject":
            rejects.append({"line": lineno, "reason": payload})
            continue
        if payload["event_id"] in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(payload["event_id"])
        by_user.setdefault(payload["user"], []).append(payload)

    sessions = []
    for user, evs in by_user.items():
        evs.sort(key=lambda e: (e["ts"], e["event_id"]))
        groups = []
        current = []
        prev = None
        for e in evs:
            if prev is not None and (e["ts"] - prev) > GAP:
                groups.append(current)
                current = []
            current.append(e)
            prev = e["ts"]
        if current:
            groups.append(current)

        for n, group in enumerate(groups, start=1):
            start = group[0]["ts"]
            end = group[-1]["ts"]
            purchases = [e for e in group if e["type"] == "purchase"]
            revenue = sum((e["amount"] for e in purchases), Decimal(0))
            sessions.append({
                "session_id": f"{user}-{n}",
                "user": user,
                "start": fmt_ts(start.replace(microsecond=0)),
                "start_dt": start,
                "end": fmt_ts(end.replace(microsecond=0)),
                "duration_s": (end - start) // ONE_SECOND,
                "events": len(group),
                "purchases": len(purchases),
                "revenue": revenue,
                "n": n,
            })

    sessions.sort(key=lambda s: (s["start"], s["user"], s["n"]))

    days = {}
    for s in sessions:
        date = s["start_dt"].date().isoformat()
        d = days.setdefault(date, {"sessions": 0, "users": set(), "revenue": Decimal(0),
                                   "per_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["revenue"] += s["revenue"]
        d["per_user"][s["user"]] = d["per_user"].get(s["user"], Decimal(0)) + s["revenue"]

    day_list = []
    for date in sorted(days):
        d = days[date]
        if d["revenue"] > 0:
            top = min((u for u, r in d["per_user"].items() if r == max(d["per_user"].values())))
        else:
            top = None
        day_list.append({
            "date": date,
            "sessions": d["sessions"],
            "users": len(d["users"]),
            "revenue": fmt_money(d["revenue"]),
            "top_user": top,
        })

    daily = {
        "days": day_list,
        "duplicates_dropped": duplicates,
        "rejected": len(rejects),
    }
    return sessions, daily, rejects


def render(sessions, daily, rejects):
    """Return dict of filename -> text content."""
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for s in sessions:
        writer.writerow([
            s["session_id"], s["user"], s["start"], s["end"], s["duration_s"],
            s["events"], s["purchases"], fmt_money(s["revenue"]),
        ])
    out = {"sessions.csv": buf.getvalue()}
    out["daily.json"] = json.dumps(daily, indent=2, ensure_ascii=False) + "\n"
    out["rejects.jsonl"] = "".join(
        json.dumps({"line": r["line"], "reason": r["reason"]}) + "\n" for r in rejects
    )
    return out


def write_outputs(out_dir, contents):
    os.makedirs(out_dir, exist_ok=True)
    temps = []
    try:
        for name, text in contents.items():
            fd, tmp = tempfile.mkstemp(dir=out_dir, prefix="." + name + ".", suffix=".tmp")
            temps.append((tmp, name))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        for tmp, name in temps:
            os.replace(tmp, os.path.join(out_dir, name))
        temps = []
    finally:
        for tmp, _ in temps:
            try:
                os.remove(tmp)
            except OSError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sessionize clickstream events.")
    parser.add_argument("--in", dest="in_dir", required=True, help="input directory")
    parser.add_argument("--out", dest="out_dir", required=True, help="output directory")
    args = parser.parse_args(argv)

    events_path = os.path.join(args.in_dir, INPUT_NAME)
    try:
        if not os.path.isfile(events_path):
            raise InputError(f"input file not found: {events_path}")
        if not os.access(events_path, os.R_OK):
            raise InputError(f"input file not readable: {events_path}")
        # Verify it can actually be opened.
        with open(events_path, "rb"):
            pass
    except InputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: cannot read input file {events_path}: {e}", file=sys.stderr)
        return 2

    try:
        sessions, daily, rejects = build(events_path)
        contents = render(sessions, daily, rejects)
        write_outputs(args.out_dir, contents)
    except Exception as e:  # unexpected internal error
        print(f"error: unexpected failure: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
