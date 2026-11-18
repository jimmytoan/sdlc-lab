#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import argparse
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


class UsageError(Exception):
    pass


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"usage error: {message}\n")
        sys.exit(EXIT_USAGE)


def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, or None."""
    if not DATE_RE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return amount in cents (int) or None."""
    text = text.strip()
    if not AMOUNT_RE.match(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def parse_participants(text):
    """Return list of (name, weight Fraction) in order of appearance, or None."""
    result = []
    seen = {}
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if ":" in segment:
            name, _, weight_text = segment.rpartition(":")
            name = name.strip()
            weight_text = weight_text.strip()
            if not WEIGHT_RE.match(weight_text):
                return None
            weight = Fraction(weight_text)
            if weight <= 0:
                return None
        else:
            name = segment
            weight = Fraction(1)
        if not name:
            return None
        if name in seen:
            idx = seen[name]
            result[idx] = (name, result[idx][1] + weight)
        else:
            seen[name] = len(result)
            result.append((name, weight))
    if not result:
        return None
    return result


def split_cents(total_cents, parts):
    """Split total_cents among (name, weight) parts; returns list of (name, cents).

    Uses floor shares, then hands out leftover cents by largest fractional
    remainder, ties broken by order of appearance. Cents always sum exactly.
    """
    total_weight = sum(w for _, w in parts)
    exact = [(name, Fraction(total_cents) * w / total_weight) for name, w in parts]
    floors = [(name, int(x)) for name, x in exact]
    leftover = total_cents - sum(c for _, c in floors)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i][1] - floors[i][1]), i))
    cents = [c for _, c in floors]
    for i in order[:leftover]:
        cents[i] += 1
    return [(parts[i][0], cents[i]) for i in range(len(parts))]


def read_rows(path):
    """Return list of (line_no, row) for non-comment, non-blank records; header first."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_CANNOT_READ)
    records = []
    try:
        reader = csv.reader(text.splitlines(keepends=True))
        prev = 0
        for row in reader:
            start = prev + 1
            prev = reader.line_num
            if not row or all(not f.strip() for f in row):
                continue
            if row[0].lstrip().startswith("#"):
                continue
            records.append((start, row))
    except csv.Error:
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_CANNOT_READ)
    return records


def load_ledger(path):
    """Return list of valid entries dicts; exits on header/row errors."""
    records = read_rows(path)
    if not records:
        sys.stderr.write("header: missing date\n")
        sys.exit(EXIT_BAD_ROWS)
    header_line, header = records[0]
    index = {}
    for i, name in enumerate(header):
        key = name.strip().lower()
        if key and key not in index:
            index[key] = i
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        for c in missing:
            sys.stderr.write(f"header: missing {c}\n")
        sys.exit(EXIT_BAD_ROWS)

    def field(row, col):
        i = index[col]
        return row[i] if i < len(row) else ""

    entries = []
    errors = []
    for line_no, row in records[1:]:
        date_text = field(row, "date").strip()
        date = parse_date(date_text)
        if date is None:
            errors.append((line_no, "bad-date"))
            continue
        payer = field(row, "payer").strip()
        if not payer:
            errors.append((line_no, "bad-payer"))
            continue
        cents = parse_amount(field(row, "amount"))
        if cents is None:
            errors.append((line_no, "bad-amount"))
            continue
        parts = parse_participants(field(row, "participants"))
        if parts is None:
            errors.append((line_no, "bad-participants"))
            continue
        entries.append({"date": date, "payer": payer, "cents": cents, "parts": parts})

    if errors:
        for line_no, reason in errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        sys.exit(EXIT_BAD_ROWS)
    return entries


def compute_balances(entries, since, until):
    balances = {}
    for e in entries:
        if since is not None and e["date"] < since:
            continue
        if until is not None and e["date"] > until:
            continue
        balances[e["payer"]] = balances.get(e["payer"], 0) + e["cents"]
        for name, share in split_cents(e["cents"], e["parts"]):
            balances[name] = balances.get(name, 0) - share
    return balances


def format_signed(cents):
    if cents > 0:
        return f"+{cents // 100}.{cents % 100:02d}"
    if cents < 0:
        a = -cents
        return f"-{a // 100}.{a % 100:02d}"
    return "0.00"


def format_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def compute_transfers(balances):
    owed = {n: -c for n, c in balances.items() if c < 0}   # debtors: amount they owe
    due = {n: c for n, c in balances.items() if c > 0}     # creditors: amount owed to them
    transfers = []
    while owed and due:
        debtor = min(owed, key=lambda n: (-owed[n], n))
        creditor = min(due, key=lambda n: (-due[n], n))
        amount = min(owed[debtor], due[creditor])
        transfers.append((debtor, creditor, amount))
        owed[debtor] -= amount
        due[creditor] -= amount
        if owed[debtor] == 0:
            del owed[debtor]
        if due[creditor] == 0:
            del due[creditor]
    return transfers


def main(argv=None):
    parser = UsageParser(prog="splitr.py", add_help=True)
    parser.add_argument("command")
    parser.add_argument("ledger")
    parser.add_argument("--format", default="text", choices=["text", "json"])
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    args = parser.parse_args(argv)

    if args.command not in COMMANDS:
        sys.stderr.write(f"usage error: unknown command {args.command!r}\n")
        return EXIT_USAGE

    since = until = None
    if args.since is not None:
        since = parse_date(args.since)
        if since is None:
            sys.stderr.write(f"usage error: invalid --since date {args.since!r}\n")
            return EXIT_USAGE
    if args.until is not None:
        until = parse_date(args.until)
        if until is None:
            sys.stderr.write(f"usage error: invalid --until date {args.until!r}\n")
            return EXIT_USAGE

    entries = load_ledger(args.ledger)
    balances = compute_balances(entries, since, until)

    if args.command == "balances":
        names = sorted(balances)
        if args.format == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {format_signed(balances[n])}")
    else:
        transfers = compute_transfers(balances)
        if args.format == "json":
            out = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
            print(json.dumps(out))
        elif not transfers:
            print("nothing to settle")
        else:
            for d, c, a in transfers:
                print(f"{d} -> {c} {format_plain(a)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
