"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Ledger: CSV with header `date,payer,amount,participants,note`.
All money is handled as integer cents.
"""

import argparse
import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass

EXIT_OK = 0
EXIT_BAD_LEDGER = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_WEIGHT = re.compile(r"[0-9]+")


@dataclass
class Expense:
    line: int
    date: datetime.date
    payer: str
    amount_cents: int
    participants: list  # list of (name, weight) tuples
    note: str


class UsageParser(argparse.ArgumentParser):
    """Argument parser that reports usage errors with exit code 64."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _date_arg(text):
    if not _DATE_SHAPE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"invalid date: {text!r} (expected YYYY-MM-DD)")
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date: {text!r} (expected YYYY-MM-DD)")


def build_parser():
    parser = UsageParser(prog="splitr.py", description="Shared-expense settlement CLI.")
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger", help="path to the ledger CSV file")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--since", type=_date_arg, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=_date_arg, default=None, metavar="YYYY-MM-DD")
    return parser


# ---------------------------------------------------------------- field parsers

def parse_date(text):
    s = text.strip()
    if not _DATE_SHAPE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_payer(text):
    s = text.strip()
    return s or None


def parse_amount(text):
    s = text.strip()
    if not _AMOUNT.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    return cents if cents > 0 else None


def parse_participants(text):
    if text.strip() == "":
        return None
    result = []
    seen = set()
    for token in text.split(";"):
        token = token.strip()
        if not token:
            return None
        name, sep, weight_text = token.rpartition(":")
        if sep:
            weight_text = weight_text.strip()
            if not _WEIGHT.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight < 1:
                return None
        else:
            name = token
            weight = 1
        name = name.strip()
        if not name or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


# ---------------------------------------------------------------- ledger reading

def _logical_records(text):
    """Yield (physical_line_number, csv_fields) for every non-blank, non-comment line."""
    for line_no, raw in enumerate(text.split("\n"), start=1):
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            continue
        try:
            rows = list(csv.reader([raw]))
            fields = rows[0] if rows else []
        except csv.Error:
            fields = raw.split(",")
        yield line_no, fields


def parse_row(fields, line_no, cols):
    """Return an Expense, or an error string 'line N: reason' for the first failing field."""

    def get(column):
        i = cols[column]
        return fields[i] if i < len(fields) else ""

    date = parse_date(get("date"))
    if date is None:
        return f"line {line_no}: bad-date"
    payer = parse_payer(get("payer"))
    if payer is None:
        return f"line {line_no}: bad-payer"
    amount = parse_amount(get("amount"))
    if amount is None:
        return f"line {line_no}: bad-amount"
    participants = parse_participants(get("participants"))
    if participants is None:
        return f"line {line_no}: bad-participants"
    return Expense(line=line_no, date=date, payer=payer, amount_cents=amount,
                   participants=participants, note=get("note").strip())


def load_expenses(text):
    """Validate the whole ledger text. Returns (expenses, error_lines, header_error)."""
    records = list(_logical_records(text))
    if not records:
        return None, [], "header: missing date"
    header = [h.strip().lower() for h in records[0][1]]
    cols = {}
    for column in REQUIRED_COLUMNS:
        if column not in header:
            return None, [], f"header: missing {column}"
        cols[column] = header.index(column)

    expenses = []
    errors = []
    for line_no, fields in records[1:]:
        result = parse_row(fields, line_no, cols)
        if isinstance(result, str):
            errors.append(result)
        else:
            expenses.append(result)
    return expenses, errors, None


# ---------------------------------------------------------------- calculations

def filter_counted(expenses, since, until):
    """Keep expenses dated within the inclusive [since, until] range (either bound optional)."""
    return [
        e for e in expenses
        if (since is None or e.date >= since) and (until is None or e.date <= until)
    ]


def split_expense(amount_cents, participants):
    """Split amount_cents by weight; leftover cents go one at a time in ascending name order."""
    total_weight = sum(w for _, w in participants)
    shares = {name: amount_cents * w // total_weight for name, w in participants}
    remainder = amount_cents - sum(shares.values())
    for name in sorted(shares)[:remainder]:
        shares[name] += 1
    return shares


def compute_balances(expenses):
    """Return {name: cents}: paid minus shares, over all given expenses."""
    balances = {}
    for e in expenses:
        balances[e.payer] = balances.get(e.payer, 0) + e.amount_cents
        for name, share in split_expense(e.amount_cents, e.participants).items():
            balances[name] = balances.get(name, 0) - share
    return balances


def settle(balances):
    """Greedy settlement: largest debtor pays largest creditor, repeat until all zero."""
    owed = {n: -b for n, b in balances.items() if b < 0}
    due = {n: b for n, b in balances.items() if b > 0}
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


# ---------------------------------------------------------------- rendering

def format_signed_cents(cents):
    sign = "+" if cents > 0 else "-" if cents < 0 else ""
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def format_cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        return json.dumps({"balances": [{"name": n, "cents": balances[n]} for n in names]})
    return "\n".join(f"{n} {format_signed_cents(balances[n])}" for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        return json.dumps({"transfers": [
            {"from": f, "to": t, "cents": c} for f, t, c in transfers]})
    if not transfers:
        return "nothing to settle"
    return "\n".join(f"{f} -> {t} {format_cents(c)}" for f, t, c in transfers)


# ---------------------------------------------------------------- main

def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        with open(args.ledger, encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {args.ledger}", file=sys.stderr)
        return EXIT_UNREADABLE

    expenses, errors, header_error = load_expenses(text)
    if header_error:
        print(header_error, file=sys.stderr)
        return EXIT_BAD_LEDGER
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return EXIT_BAD_LEDGER

    counted = filter_counted(expenses, args.since, args.until)
    balances = compute_balances(counted)

    if args.command == "balances":
        if args.format == "json" or balances:
            print(render_balances(balances, args.format))
    else:
        print(render_settle(settle(balances), args.format))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
