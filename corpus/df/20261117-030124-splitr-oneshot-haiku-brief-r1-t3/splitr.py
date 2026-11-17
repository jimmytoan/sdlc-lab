#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import io
import json
import math
import re
import sys
from datetime import date
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AMOUNT_RE = re.compile(r"(\d+)(?:\.(\d{1,2}))?")
WEIGHT_RE = re.compile(r"\d+(?:\.\d+)?")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> [--format text|json]\n"
    "                 [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n"
)


def usage_error():
    sys.stderr.write(USAGE)
    return EXIT_USAGE


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    """Return cents (positive int) or None."""
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    """Return ordered dict name -> Fraction weight, or None if invalid."""
    weights = {}
    for piece in s.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        name, sep, w = piece.partition(":")
        name = name.strip()
        if not name:
            return None
        if sep:
            w = w.strip()
            if not WEIGHT_RE.fullmatch(w):
                return None
            wt = Fraction(w)
            if wt <= 0:
                return None
        else:
            wt = Fraction(1)
        weights[name] = weights.get(name, 0) + wt
    return weights or None


def split_cents(total, weights):
    """Split total cents by weights; returns dict name -> cents (sums exactly to total)."""
    names = list(weights)
    wsum = sum(weights.values())
    raws = [Fraction(total) * weights[n] / wsum for n in names]
    floors = [math.floor(r) for r in raws]
    rem = total - sum(floors)
    order = sorted(range(len(names)), key=lambda i: (-(raws[i] - floors[i]), i))
    for i in order[:rem]:
        floors[i] += 1
    return dict(zip(names, floors))


def fmt_signed(c):
    if c > 0:
        sign = "+"
    elif c < 0:
        sign = "-"
    else:
        sign = ""
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


def fmt_plain(c):
    return f"{abs(c) // 100}.{abs(c) % 100:02d}"


def main(argv):
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a in ("-h", "--help"):
            sys.stdout.write(USAGE)
            return EXIT_OK
        if a.startswith("--"):
            key, eq, val = a[2:].partition("=")
            if key not in opts:
                return usage_error()
            if not eq:
                if i >= len(argv):
                    return usage_error()
                val = argv[i]
                i += 1
            opts[key] = val
        elif a.startswith("-") and len(a) > 1:
            return usage_error()
        else:
            positional.append(a)

    if len(positional) != 2:
        return usage_error()
    command, path = positional
    if command not in ("balances", "settle"):
        return usage_error()
    if opts["format"] not in ("text", "json"):
        return usage_error()
    since = until = None
    if opts["since"] is not None:
        since = parse_date(opts["since"])
        if since is None:
            return usage_error()
    if opts["until"] is not None:
        until = parse_date(opts["until"])
        if until is None:
            return usage_error()

    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_CANNOT_READ

    reader = csv.reader(io.StringIO(text, newline=""))
    header = None
    cols = {}
    rows = []
    prev = 0
    for row in reader:
        start = prev + 1
        prev = reader.line_num
        if not row or all(not c.strip() for c in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            for name in REQUIRED_COLUMNS:
                if name not in header:
                    sys.stderr.write(f"header: missing {name}\n")
                    return EXIT_BAD_ROWS
            cols = {n: header.index(n) for n in REQUIRED_COLUMNS}
            continue
        rows.append((start, row))

    if header is None:
        sys.stderr.write("header: missing date\n")
        return EXIT_BAD_ROWS

    errors = []
    entries = []
    for start, row in rows:

        def field(n, row=row):
            idx = cols[n]
            return row[idx].strip() if idx < len(row) else ""

        d = parse_date(field("date"))
        payer = field("payer")
        cents = parse_amount(field("amount"))
        weights = parse_participants(field("participants"))
        reason = None
        if d is None:
            reason = "bad-date"
        elif not payer:
            reason = "bad-payer"
        elif cents is None:
            reason = "bad-amount"
        elif weights is None:
            reason = "bad-participants"
        if reason:
            errors.append(f"line {start}: {reason}")
            continue
        entries.append((d, payer, cents, weights))

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_BAD_ROWS

    bal = {}
    for d, payer, cents, weights in entries:
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        for name, share in split_cents(cents, weights).items():
            bal[name] = bal.get(name, 0) - share

    if command == "balances":
        names = sorted(bal)
        if opts["format"] == "json":
            out = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            sys.stdout.write(json.dumps(out) + "\n")
        else:
            for n in names:
                sys.stdout.write(f"{n} {fmt_signed(bal[n])}\n")
        return EXIT_OK

    # settle
    creditors = {n: c for n, c in bal.items() if c > 0}
    debtors = {n: -c for n, c in bal.items() if c < 0}
    transfers = []
    while creditors and debtors:
        debtor = min(debtors, key=lambda n: (-debtors[n], n))
        creditor = min(creditors, key=lambda n: (-creditors[n], n))
        pay = min(debtors[debtor], creditors[creditor])
        transfers.append((debtor, creditor, pay))
        debtors[debtor] -= pay
        creditors[creditor] -= pay
        if debtors[debtor] == 0:
            del debtors[debtor]
        if creditors[creditor] == 0:
            del creditors[creditor]

    if opts["format"] == "json":
        out = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        sys.stdout.write(json.dumps(out) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        for f, t, c in transfers:
            sys.stdout.write(f"{f} -> {t} {fmt_plain(c)}\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
