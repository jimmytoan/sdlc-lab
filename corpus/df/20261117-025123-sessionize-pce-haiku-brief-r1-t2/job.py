#!/usr/bin/env python3
"""sessionize: turn raw clickstream events into user sessions and a daily summary.

Usage: python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>

Reads <INPUT_DIR>/events.jsonl and writes sessions.csv, daily.json and
rejects.jsonl into <OUTPUT_DIR>. Standard library only.

Decisions where the spec is silent:
- A blank (empty or whitespace-only) line is not valid JSON, so it is rejected
  as `bad_json` (rule 10). A trailing newline at end of file does not start a
  new line. An empty file has no lines and gives no rejects.
- A leading UTF-8 BOM on the first line is ignored.
- Non-string `ts` gives `bad_ts`; non-string `type` gives `bad_type`;
  non-string `user` gives `missing_field`. `event_id` may be a string or an
  integer (stored as its string form); anything else gives `missing_field`.
- Only null and the empty string count as missing for required fields.
  Whitespace-only values are not empty, so they are kept as given
  (`event_id`, `user`) or fail their own check (`ts` -> bad_ts,
  `amount` -> bad_amount, `type` -> bad_type).
- `user` has surrounding whitespace stripped and is then lower-cased, unless
  stripping would leave nothing; then the raw value is lower-cased instead.
- `ts` must match `YYYY-MM-DDTHH:MM[:SS[.f]](Z|+HH:MM|-HH:MM)` exactly
  (uppercase T and Z). Other ISO 8601 forms (basic format, `+HHMM`, `+HH`,
  space separator, week dates) are bad_ts, as PLAN rule 14 requires.
- `top_user` ties are broken by the alphabetically smaller user name.
- Zero-amount purchases count as purchases with revenue 0 and do not make
  `top_user` non-null (it stays null when the day's revenue is zero).
- Negative amounts are `bad_amount`. Non-finite amounts (NaN, Infinity) are
  `bad_amount`. Any other finite Decimal is accepted, with no magnitude cap.
  Amounts on view/click events are ignored entirely.
- Money is kept exact as Decimal. Sums use a 1000-digit context. When
  formatting, the quantize precision is sized to each value, and the result is
  printed in plain fixed-point notation (never scientific).
- Output timestamps are truncated to whole seconds. duration_s is computed
  from those printed (truncated) start and end values, so it always matches
  the CSV. The 30-minute gap test uses the exact timedelta.
- Purchases without `amount` are `missing_field` before the ts check (rule 12
  before rule 14).
- sessions.csv rows are sorted by (start, user, session_id).
- Output timestamps drop sub-second precision (truncated).
"""
import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, MAX_EMAX, MIN_EMIN

GAP = timedelta(minutes=30)
CENT = Decimal("0.01")
VALID_TYPES = ("view", "click", "purchase")
SESSION_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
OUTPUT_NAMES = ("sessions.csv", "daily.json", "rejects.jsonl")
# All money arithmetic runs in this context (the default 28-digit context is too small).
MONEY_CTX = Context(prec=1000, Emax=MAX_EMAX, Emin=MIN_EMIN, rounding=ROUND_HALF_EVEN)
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")


@dataclass
class Event:
    line_no: int
    event_id: str
    user: str
    ts_utc: datetime
    type: str
    amount: "Decimal | None"


@dataclass
class Session:
    session_id: str
    user: str
    start: datetime
    end: datetime
    events: int = 0
    purchases: int = 0
    revenue: Decimal = field(default_factory=lambda: Decimal("0"))


class Reject(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _is_missing(value):
    """Null or the empty string. Whitespace-only is not empty (spec wording)."""
    return value is None or value == ""


def parse_ts(value):
    """Parse an ISO 8601 string that must carry a UTC offset. Returns aware UTC datetime."""
    if not isinstance(value, str) or not TS_RE.fullmatch(value):
        raise Reject("bad_ts")
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise Reject("bad_ts")
        return dt.astimezone(timezone.utc)
    except Reject:
        raise
    except (ValueError, OverflowError, TypeError):
        raise Reject("bad_ts")


def parse_amount(value):
    if not isinstance(value, str):
        raise Reject("bad_amount")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise Reject("bad_amount")
    if not amount.is_finite() or amount < 0:
        raise Reject("bad_amount")
    return amount


def parse_event(line_no, raw):
    """Validate one physical line. Returns (Event, None) or (None, reason).

    Checks run in a fixed order; the first failure wins.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None, "bad_json"
    if not isinstance(obj, dict):
        return None, "bad_json"
    try:
        # 1. Required fields present and non-empty.
        for key in ("event_id", "user", "ts", "type"):
            if _is_missing(obj.get(key)):
                raise Reject("missing_field")
        event_id = obj["event_id"]
        if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
            raise Reject("missing_field")
        event_id = str(event_id)
        user = obj["user"]
        if not isinstance(user, str):
            raise Reject("missing_field")
        user = (user.strip() or user).lower()

        # 2. Event type.
        etype = obj["type"]
        if not isinstance(etype, str) or etype not in VALID_TYPES:
            raise Reject("bad_type")

        # 3. Purchases require an amount (rule 12 comes before the ts check).
        if etype == "purchase" and _is_missing(obj.get("amount")):
            raise Reject("missing_field")

        # 4. Timestamp with UTC offset.
        ts_utc = parse_ts(obj["ts"])

        # 5. Amount: only purchases carry revenue; view/click amounts are ignored.
        amount = None
        if etype == "purchase":
            amount = parse_amount(obj["amount"])
        return Event(line_no, event_id, user, ts_utc, etype, amount), None
    except Reject as r:
        return None, r.reason


def read_lines(path):
    """Yield (line_no, text) for each physical line, 1-based.

    Decoding is per line, so a bad byte sequence only affects its own line.
    Undecodable lines are yielded as None so the caller can reject them.
    """
    with open(path, "rb") as f:
        data = f.read()
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()  # the newline that ends the last line, not an extra line
    for idx, chunk in enumerate(parts):
        line_no = idx + 1
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        if idx == 0 and chunk.startswith(b"\xef\xbb\xbf"):
            chunk = chunk[3:]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            yield line_no, None
            continue
        yield line_no, text


def process_input(in_path):
    """Read and validate all lines. Returns (valid_events, rejects, total_rejected)."""
    events, rejects = [], []
    for line_no, text in read_lines(in_path):
        if text is None:
            reason, event = "bad_json", None
        elif text.strip() == "":
            reason, event = "bad_json", None  # blank line is not valid JSON
        else:
            event, reason = parse_event(line_no, text)
        if event is None:
            rejects.append({"line": line_no, "reason": reason})
        else:
            events.append(event)
    return events, rejects


def dedupe(events):
    """Keep the first valid occurrence of each event_id. Returns (kept, dropped_count)."""
    seen = set()
    kept = []
    dropped = 0
    for ev in events:
        if ev.event_id in seen:
            dropped += 1
            continue
        seen.add(ev.event_id)
        kept.append(ev)
    return kept, dropped


def sessionize(events):
    by_user = {}
    for ev in events:
        by_user.setdefault(ev.user, []).append(ev)
    sessions = []
    for user in sorted(by_user):
        evs = sorted(by_user[user], key=lambda e: (e.ts_utc, e.event_id))
        groups = []
        current = [evs[0]]
        for prev, cur in zip(evs, evs[1:]):
            if cur.ts_utc - prev.ts_utc > GAP:
                groups.append(current)
                current = [cur]
            else:
                current.append(cur)
        groups.append(current)
        for n, group in enumerate(groups, start=1):
            s = Session(session_id=f"{user}-{n}", user=user,
                        start=group[0].ts_utc, end=group[-1].ts_utc)
            for ev in group:
                s.events += 1
                if ev.type == "purchase":
                    s.purchases += 1
                    s.revenue = MONEY_CTX.add(s.revenue, ev.amount)
            sessions.append(s)
    sessions.sort(key=lambda s: (s.start, s.user, s.session_id))
    return sessions


def fmt_money(d):
    """Two decimals, plain notation. The context precision is sized to the value."""
    ctx = Context(prec=max(1000, d.adjusted() + 10), Emax=MAX_EMAX, Emin=MIN_EMIN, rounding=ROUND_HALF_EVEN)
    return format(ctx.quantize(d, CENT), "f")


def trunc_sec(dt):
    return dt.replace(microsecond=0)


def fmt_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_daily(sessions, duplicates_dropped, rejected):
    by_date = {}
    for s in sessions:
        by_date.setdefault(s.start.date().isoformat(), []).append(s)
    days = []
    for date in sorted(by_date):
        group = by_date[date]
        revenue = Decimal("0")
        user_rev = {}
        for s in group:
            revenue = MONEY_CTX.add(revenue, s.revenue)
            user_rev[s.user] = MONEY_CTX.add(user_rev.get(s.user, Decimal("0")), s.revenue)
        top_user = None
        if revenue != 0:
            # Highest revenue; ties go to the alphabetically smaller user name.
            top_user = min(user_rev, key=lambda u: (MONEY_CTX.subtract(Decimal(0), user_rev[u]), u))
        days.append({
            "date": date,
            "sessions": len(group),
            "users": len({s.user for s in group}),
            "revenue": fmt_money(revenue),
            "top_user": top_user,
        })
    return {"days": days, "duplicates_dropped": duplicates_dropped, "rejected": rejected}


def _atomic_write(path, writer):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer(f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_outputs(out_dir, sessions, daily, rejects):
    os.makedirs(out_dir, exist_ok=True)

    def write_csv(f):
        w = csv.writer(f, lineterminator="\n")
        w.writerow(SESSION_HEADER)
        for s in sessions:
            w.writerow([
                s.session_id, s.user, fmt_ts(s.start), fmt_ts(s.end),
                int((trunc_sec(s.end) - trunc_sec(s.start)).total_seconds()),
                s.events, s.purchases, fmt_money(s.revenue),
            ])

    def write_json(f):
        json.dump(daily, f, indent=2, ensure_ascii=False)
        f.write("\n")

    def write_rejects(f):
        for r in rejects:
            f.write(json.dumps({"line": r["line"], "reason": r["reason"]}, ensure_ascii=False) + "\n")

    _atomic_write(os.path.join(out_dir, "sessions.csv"), write_csv)
    _atomic_write(os.path.join(out_dir, "daily.json"), write_json)
    _atomic_write(os.path.join(out_dir, "rejects.jsonl"), write_rejects)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sessionize clickstream events.")
    parser.add_argument("--in", dest="in_dir", required=True, metavar="INPUT_DIR")
    parser.add_argument("--out", dest="out_dir", required=True, metavar="OUTPUT_DIR")
    args = parser.parse_args(argv)

    in_path = os.path.join(args.in_dir, "events.jsonl")
    if not os.path.isfile(in_path):
        print(f"error: input file not found: {in_path}", file=sys.stderr)
        return 2

    valid, rejects = process_input(in_path)
    events, duplicates = dedupe(valid)
    sessions = sessionize(events)
    daily = build_daily(sessions, duplicates, len(rejects))
    write_outputs(args.out_dir, sessions, daily, rejects)
    return 0


if __name__ == "__main__":
    sys.exit(main())
