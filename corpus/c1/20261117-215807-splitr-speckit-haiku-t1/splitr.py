#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 ok, 2 bad ledger rows / header, 3 cannot read file, 64 usage.
"""
import csv
import datetime as dt
import json
import re
import sys
from fractions import Fraction

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
COMMANDS = ("balances", "settle")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?", re.ASCII)
WEIGHT_RE = re.compile(r"[0-9]+(\.[0-9]+)?", re.ASCII)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


class UsageError(Exception):
    pass


class ReadError(Exception):
    pass


def usage_fail(msg=None):
    if msg:
        print(f"error: {msg}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    sys.exit(64)


def parse_date(s):
    """Return a date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            if name not in ("format", "since", "until"):
                usage_fail(f"unknown option: {arg}")
            if not eq:
                i += 1
                if i >= len(argv):
                    usage_fail(f"option --{name} needs a value")
                value = argv[i]
            opts[name] = value
        else:
            positional.append(arg)
        i += 1
    if len(positional) != 2:
        usage_fail("expected a command and a ledger path")
    command, path = positional
    if command not in COMMANDS:
        usage_fail(f"unknown command: {command}")
    if opts["format"] not in ("text", "json"):
        usage_fail("--format must be text or json")
    for key in ("since", "until"):
        if opts[key] is not None:
            d = parse_date(opts[key])
            if d is None:
                usage_fail(f"--{key} must be YYYY-MM-DD")
            opts[key] = d
    return command, path, opts


def parse_participants(field):
    """Return list of (name, weight Fraction) or None if invalid."""
    if field.strip() == "":
        return None
    result = []
    seen = set()
    for part in field.split(";"):
        part = part.strip()
        pieces = part.split(":")
        if len(pieces) > 2:
            return None
        name = pieces[0].strip()
        if not name:
            return None
        if len(pieces) == 2:
            w = pieces[1].strip()
            if not WEIGHT_RE.fullmatch(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            weight = Fraction(1)
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def amount_to_cents(s):
    s = s.strip()
    if not AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def read_records(path):
    """Yield (line_no, row_list) for each non-blank, non-comment CSV record.

    line_no is the physical line where the record starts (1-based)."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        raise ReadError()

    lines = text.splitlines(keepends=True)
    state = {"at_start": True, "start": 0}

    def gen():
        for idx, line in enumerate(lines, 1):
            if state["at_start"]:
                if line.strip() == "" or line.lstrip().startswith("#"):
                    continue
                state["start"] = idx
                state["at_start"] = False
            yield line

    reader = csv.reader(gen())
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:
            raise ReadError()
        state["at_start"] = True
        if not row or all(c.strip() == "" for c in row):
            continue
        yield state["start"], row


def load(path, opts):
    """Return (rows, errors). rows: list of dict(date, payer, cents, parts)."""
    records = read_records(path)
    try:
        header_rec = next(records)
    except StopIteration:
        header_rec = None
    header = [h.strip().lower() for h in header_rec[1]] if header_rec else []
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        for c in missing:
            print(f"header: missing {c}", file=sys.stderr)
        sys.exit(2)
    idx = {name: header.index(name) for name in header}
    note_present = "note" in idx

    rows, errors = [], []
    for line_no, rec in records:
        def field(name):
            i = idx[name]
            return rec[i] if i < len(rec) else ""

        d = parse_date(field("date").strip())
        if d is None:
            errors.append((line_no, "bad-date"))
            continue
        payer = field("payer").strip()
        if not payer:
            errors.append((line_no, "bad-payer"))
            continue
        cents = amount_to_cents(field("amount"))
        if cents is None:
            errors.append((line_no, "bad-amount"))
            continue
        parts = parse_participants(field("participants"))
        if parts is None:
            errors.append((line_no, "bad-participants"))
            continue
        if opts["since"] and d < opts["since"]:
            continue
        if opts["until"] and d > opts["until"]:
            continue
        rows.append({"date": d, "payer": payer, "cents": cents, "parts": parts})
    return rows, errors


def split_cents(total, parts):
    """Split total cents by weights; the result sums exactly to total.

    Leftover cents go to the largest fractional remainders, ties by listing order."""
    wsum = sum(w for _, w in parts)
    exact = [Fraction(total) * w / wsum for _, w in parts]
    floors = [int(e) for e in exact]
    leftover = total - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [(parts[i][0], floors[i]) for i in range(len(parts))]


def compute_balances(rows):
    bal = {}
    for r in rows:
        bal[r["payer"]] = bal.get(r["payer"], 0) + r["cents"]
        for name, share in split_cents(r["cents"], r["parts"]):
            bal[name] = bal.get(name, 0) - share
    return bal


def compute_transfers(bal):
    debtors = sorted(([n, -c] for n, c in bal.items() if c < 0), key=lambda x: (-x[1], x[0]))
    creditors = sorted(([n, c] for n, c in bal.items() if c > 0), key=lambda x: (-x[1], x[0]))
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i][1], creditors[j][1])
        transfers.append((debtors[i][0], creditors[j][0], amt))
        debtors[i][1] -= amt
        creditors[j][1] -= amt
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers


def fmt_plain(cents):
    sign = "-" if cents < 0 else ""
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    return ("+" if cents > 0 else "-") + fmt_plain(abs(cents))


def main(argv):
    command, path, opts = parse_args(argv)
    try:
        rows, errors = load(path, opts)
    except ReadError:
        print(f"error: cannot read {path}", file=sys.stderr)
        return 3
    if errors:
        for line_no, reason in errors:
            print(f"line {line_no}: {reason}", file=sys.stderr)
        return 2

    bal = compute_balances(rows)
    names = sorted(bal)
    if command == "balances":
        if opts["format"] == "json":
            out = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {fmt_signed(bal[n])}")
    else:
        transfers = compute_transfers(bal)
        if opts["format"] == "json":
            out = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
            print(json.dumps(out))
        elif not transfers:
            print("nothing to settle")
        else:
            for f, t, c in transfers:
                print(f"{f} -> {t} {fmt_plain(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
