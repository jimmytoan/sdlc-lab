#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Reads a ledger CSV with the header ``date,payer,amount,participants[,note]``
and prints either each person's balance (``balances``) or the payments that
settle all balances (``settle``). Money is handled as integer cents.
"""

import argparse
import csv
import dataclasses
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_DATA = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
_WEIGHT_RE = re.compile(r"[0-9]+")


@dataclasses.dataclass
class Expense:
    line: int
    date: datetime.date
    payer: str
    amount_cents: int
    participants: list  # list of (name, weight) tuples, in row order
    note: str = ""


# ---------------------------------------------------------------------------
# Field parsing and validation
# ---------------------------------------------------------------------------

def parse_date_value(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    text = text.strip()
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_amount(text):
    """Return the amount in integer cents, or None if invalid or not positive."""
    m = _AMOUNT_RE.fullmatch(text.strip())
    if not m:
        return None
    whole = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    cents = whole * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_payer(text):
    name = text.strip()
    return name or None


def parse_participants(text):
    """Return a list of (name, weight) tuples, or None if invalid."""
    if not text.strip():
        return None
    result = []
    seen = set()
    for item in text.split(";"):
        if item.count(":") > 1:
            return None
        if ":" in item:
            name_part, weight_part = item.split(":", 1)
            name = name_part.strip()
            weight_text = weight_part.strip()
            if not _WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight <= 0:
                return None
        else:
            name = item.strip()
            weight = 1
        if not name or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def validate_row(line_no, fields, column, width):
    """Return an Expense, or the reason string for the first bad field."""
    if len(fields) != width:
        return "bad-row"
    date = parse_date_value(fields[column["date"]])
    if date is None:
        return "bad-date"
    payer = parse_payer(fields[column["payer"]])
    if payer is None:
        return "bad-payer"
    amount = parse_amount(fields[column["amount"]])
    if amount is None:
        return "bad-amount"
    participants = parse_participants(fields[column["participants"]])
    if participants is None:
        return "bad-participants"
    note = fields[column["note"]] if "note" in column else ""
    return Expense(line=line_no, date=date, payer=payer,
                   amount_cents=amount, participants=participants, note=note)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def allocate_shares(expense):
    """Split the expense into whole-cent shares that sum exactly to the amount.

    Returns a list of (name, cents) in row order. Leftover cents go one at a
    time to the earliest-listed participants.
    """
    total_weight = sum(w for _, w in expense.participants)
    amount = expense.amount_cents
    shares = [amount * w // total_weight for _, w in expense.participants]
    leftover = amount - sum(shares)
    for i in range(leftover):
        shares[i] += 1
    return [(name, share) for (name, _), share in zip(expense.participants, shares)]


def compute_balances(expenses):
    """Return {name: signed cents} for every payer and participant."""
    balances = {}
    for e in expenses:
        balances.setdefault(e.payer, 0)
        balances[e.payer] += e.amount_cents
        for name, share in allocate_shares(e):
            balances.setdefault(name, 0)
            balances[name] -= share
    return balances


def settle_transfers(balances):
    """Greedy settlement: largest debtor pays largest creditor, repeatedly.

    Returns a list of (from, to, cents) tuples with cents > 0.
    """
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [n for n, v in bal.items() if v < 0]
        creditors = [n for n, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        debtor = min(debtors, key=lambda n: (bal[n], n))
        creditor = min(creditors, key=lambda n: (-bal[n], n))
        amount = min(-bal[debtor], bal[creditor])
        transfers.append((debtor, creditor, amount))
        bal[debtor] += amount
        bal[creditor] -= amount
    return transfers


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_amount(cents):
    """Unsigned two-decimal amount, e.g. 2200 -> '22.00'."""
    cents = abs(cents)
    return f"{cents // 100}.{cents % 100:02d}"


def format_signed(cents):
    if cents > 0:
        return "+" + format_amount(cents)
    if cents < 0:
        return "-" + format_amount(cents)
    return format_amount(0)


def format_balances_text(balances):
    return [f"{name} {format_signed(balances[name])}" for name in sorted(balances)]


def format_balances_json(balances):
    return json.dumps({"balances": [
        {"name": name, "cents": balances[name]} for name in sorted(balances)]})


def format_transfers_text(transfers):
    if not transfers:
        return ["nothing to settle"]
    return [f"{src} -> {dst} {format_amount(cents)}" for src, dst, cents in transfers]


def format_transfers_json(transfers):
    return json.dumps({"transfers": [
        {"from": src, "to": dst, "cents": cents} for src, dst, cents in transfers]})


# ---------------------------------------------------------------------------
# Reading the ledger
# ---------------------------------------------------------------------------

def read_records(text):
    """Yield (physical_line_number, fields) for each meaningful CSV record.

    Blank lines and lines whose first character is '#' are skipped. The line
    number is that of the record's first physical line (header = line 1).
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    while True:
        start = reader.line_num + 1
        try:
            fields = next(reader)
        except StopIteration:
            return
        if not fields or (len(fields) == 1 and not fields[0].strip()):
            continue
        if fields[0].startswith("#"):
            continue
        yield start, fields


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

class UsageParser(argparse.ArgumentParser):
    """Argument parser whose usage errors exit with EXIT_USAGE (64)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _date_arg(text):
    value = parse_date_value(text)
    if value is None:
        raise argparse.ArgumentTypeError(f"invalid date {text!r} (expected YYYY-MM-DD)")
    return value


def build_parser():
    parser = UsageParser(
        prog="splitr.py",
        description="Shared-expense settlement: who owes whom.")
    parser.add_argument("command", choices=["balances", "settle"],
                        help="balances: per-person totals; settle: payments that clear them")
    parser.add_argument("ledger", help="path to the ledger CSV file")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="output format (default: text)")
    parser.add_argument("--since", type=_date_arg, default=None,
                        metavar="YYYY-MM-DD", help="include expenses on or after this date")
    parser.add_argument("--until", type=_date_arg, default=None,
                        metavar="YYYY-MM-DD", help="include expenses on or before this date")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since must not be later than --until")

    # Read and parse the whole file first; any failure here is "cannot read".
    try:
        with open(args.ledger, encoding="utf-8-sig", newline="") as f:
            text = f.read()
        records = list(read_records(text))
    except (OSError, UnicodeDecodeError, csv.Error):
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_UNREADABLE

    if not records:
        sys.stderr.write("header: missing date\n")
        return EXIT_BAD_DATA

    _, header = records[0]
    column = {}
    for index, name in enumerate(header):
        column.setdefault(name.strip(), index)
    for required in REQUIRED_COLUMNS:
        if required not in column:
            sys.stderr.write(f"header: missing {required}\n")
            return EXIT_BAD_DATA

    width = len(header)
    expenses = []
    errors = []
    for line_no, fields in records[1:]:
        result = validate_row(line_no, fields, column, width)
        if isinstance(result, Expense):
            expenses.append(result)
        else:
            errors.append((line_no, result))

    if errors:
        for line_no, reason in errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        return EXIT_BAD_DATA

    included = [e for e in expenses
                if (args.since is None or e.date >= args.since)
                and (args.until is None or e.date <= args.until)]
    balances = compute_balances(included)

    if args.command == "balances":
        if args.format == "json":
            out = [format_balances_json(balances)]
        else:
            out = format_balances_text(balances)
    else:
        transfers = settle_transfers(balances)
        if args.format == "json":
            out = [format_transfers_json(transfers)]
        else:
            out = format_transfers_text(transfers)

    for line in out:
        sys.stdout.write(line + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
