#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


def usage(msg=None):
    if msg:
        print(msg, file=sys.stderr)
    print(
        "usage: splitr.py {balances,settle} <ledger.csv> "
        "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]",
        file=sys.stderr,
    )
    sys.exit(EXIT_USAGE)


def parse_date(s):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    """Return positive amount in cents, or None."""
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    """Return ordered list of (name, weight) with duplicates merged, or None."""
    weights = {}
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        name, sep, wtext = part.partition(":")
        name = name.strip()
        if not name:
            return None
        if sep:
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight <= 0:
                return None
        else:
            weight = 1
        weights[name] = weights.get(name, 0) + weight
    if not weights:
        return None
    return list(weights.items())


def split_cents(total, shares):
    """Split total cents by integer weights; leftover cents go to largest remainders."""
    wsum = sum(w for _, w in shares)
    floors = [total * w // wsum for _, w in shares]
    remainders = [total * w % wsum for _, w in shares]
    leftover = total - sum(floors)
    order = sorted(range(len(shares)), key=lambda i: (-remainders[i], i))
    for k in range(leftover):
        floors[order[k]] += 1
    return [(name, floors[i]) for i, (name, _) in enumerate(shares)]


def read_records(text):
    """Yield (line_number, fields) for every non-comment, non-blank CSV record."""
    lines = text.splitlines(keepends=True)
    records = []
    i = 0
    while i < len(lines):
        lineno = i + 1
        line = lines[i]
        i += 1
        if line.startswith("#") or not line.strip():
            continue
        buf = line
        # Join physical lines when a quoted field spans multiple lines.
        while buf.count('"') % 2 == 1 and i < len(lines):
            buf += lines[i]
            i += 1
        try:
            fields = next(csv.reader([buf]))
        except csv.Error:
            fields = [buf.strip()]
        records.append((lineno, fields))
    return records


def load_ledger(path):
    """Return (expenses, bad_rows) or exit on fatal errors.

    expenses: list of (date, payer, cents, [(name, weight), ...])
    bad_rows: list of (line_number, reason)
    """
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {path}", file=sys.stderr)
        sys.exit(EXIT_CANNOT_READ)

    records = read_records(text)
    if not records:
        header = []
        data = []
    else:
        header = [h.strip() for h in records[0][1]]
        data = records[1:]

    for col in REQUIRED_COLUMNS:
        if col not in header:
            print(f"header: missing {col}", file=sys.stderr)
            sys.exit(EXIT_BAD_ROWS)

    col_index = {name: header.index(name) for name in REQUIRED_COLUMNS}

    expenses = []
    bad_rows = []
    for lineno, fields in data:
        def get(col):
            idx = col_index[col]
            return fields[idx].strip() if idx < len(fields) else ""

        date_s = get("date")
        payer = get("payer")
        amount_s = get("amount")
        parts_s = get("participants")

        date = parse_date(date_s)
        if date is None:
            bad_rows.append((lineno, "bad-date"))
            continue
        if not payer:
            bad_rows.append((lineno, "bad-payer"))
            continue
        cents = parse_amount(amount_s)
        if cents is None:
            bad_rows.append((lineno, "bad-amount"))
            continue
        shares = parse_participants(parts_s)
        if shares is None:
            bad_rows.append((lineno, "bad-participants"))
            continue
        expenses.append((date, payer, cents, shares))

    return expenses, bad_rows


def compute_balances(expenses):
    """Return dict name -> balance cents (paid minus share)."""
    balances = {}
    for _, payer, cents, shares in expenses:
        balances.setdefault(payer, 0)
        balances[payer] += cents
        for name, share in split_cents(cents, shares):
            balances.setdefault(name, 0)
            balances[name] -= share
    return balances


def compute_settlements(balances):
    """Greedy settlement: largest debtor pays largest creditor."""
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [n for n in bal if bal[n] < 0]
        creditors = [n for n in bal if bal[n] > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda n: (bal[n], n))
        c = min(creditors, key=lambda n: (-bal[n], n))
        amt = min(-bal[d], bal[c])
        transfers.append((d, c, amt))
        bal[d] += amt
        bal[c] -= amt
    return transfers


def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def main(argv):
    if not argv:
        usage("error: missing command")
    command = argv[0]
    if command in ("-h", "--help"):
        usage()
    if command not in COMMANDS:
        usage(f"error: unknown command: {command}")

    fmt = "text"
    since = None
    until = None
    positional = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        value = None
        name = arg
        if arg.startswith("--") and "=" in arg:
            name, _, value = arg.partition("=")
        if name in ("--format", "--since", "--until"):
            if value is None:
                if i + 1 >= len(argv):
                    usage(f"error: option {name} requires a value")
                i += 1
                value = argv[i]
            if name == "--format":
                if value not in FORMATS:
                    usage(f"error: bad format: {value}")
                fmt = value
            else:
                d = parse_date(value)
                if d is None:
                    usage(f"error: bad date for {name}: {value}")
                if name == "--since":
                    since = d
                else:
                    until = d
        elif arg.startswith("-") and arg != "-":
            usage(f"error: unknown option: {arg}")
        else:
            positional.append(arg)
        i += 1

    if len(positional) != 1:
        usage("error: expected exactly one ledger file")
    path = positional[0]

    expenses, bad_rows = load_ledger(path)

    if bad_rows:
        for lineno, reason in bad_rows:
            print(f"line {lineno}: {reason}", file=sys.stderr)
        sys.exit(EXIT_BAD_ROWS)

    if since is not None or until is not None:
        expenses = [
            e for e in expenses
            if (since is None or e[0] >= since) and (until is None or e[0] <= until)
        ]

    balances = compute_balances(expenses)

    if command == "balances":
        names = sorted(balances)
        if fmt == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {fmt_signed(balances[n])}")
    else:
        transfers = compute_settlements(balances)
        if fmt == "json":
            out = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
            print(json.dumps(out))
        else:
            if not transfers:
                print("nothing to settle")
            for d, c, a in transfers:
                print(f"{d} -> {c} {fmt_plain(a)}")

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main(sys.argv[1:])
