#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Ledger: CSV with header columns date,payer,amount,participants[,note].
All money is handled as integer cents.

Exit codes: 0 success, 2 invalid ledger (header or rows), 3 unreadable ledger,
64 usage error. Stdout is empty on every error.
"""
import argparse
import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass

REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64


class UsageParser(argparse.ArgumentParser):
    """Argument parser whose usage failures exit with 64 instead of 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(EXIT_USAGE)


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------

def parse_date(raw):
    """Return a datetime.date for a real YYYY-MM-DD date, else None."""
    if not DATE_RE.fullmatch(raw):
        return None
    try:
        return datetime.date(int(raw[0:4]), int(raw[5:7]), int(raw[8:10]))
    except ValueError:
        return None


def parse_cli_date(raw):
    """argparse type for --since / --until."""
    value = parse_date(raw)
    if value is None:
        raise argparse.ArgumentTypeError(f"invalid date {raw!r}, expected YYYY-MM-DD")
    return value


def parse_amount(raw):
    """Return integer cents for a positive amount with at most 2 decimals, else None."""
    if not AMOUNT_RE.fullmatch(raw):
        return None
    whole, _, frac = raw.partition(".")
    try:
        cents = int(whole) * 100 + (int(frac.ljust(2, "0")) if frac else 0)
    except ValueError:
        return None
    return cents if cents > 0 else None


def parse_payer(raw):
    """Return the payer name, or None if empty."""
    return raw if raw else None


def parse_participants(raw):
    """Return an ordered mapping name -> weight, or None if invalid."""
    result = {}
    for item in raw.split(";"):
        item = item.strip()
        if ":" in item:
            name, _, weight_text = item.partition(":")
            name = name.strip()
            weight_text = weight_text.strip()
            if not WEIGHT_RE.fullmatch(weight_text):
                return None
            try:
                weight = int(weight_text)
            except ValueError:
                return None
            if weight <= 0:
                return None
        else:
            name = item
            weight = 1
        if not name or name in result:
            return None
        result[name] = weight
    return result or None


# ---------------------------------------------------------------------------
# Ledger reading
# ---------------------------------------------------------------------------

@dataclass
class Expense:
    date: datetime.date
    payer: str
    amount_cents: int
    participants: dict
    note: str


def read_ledger_text(path):
    """Return the ledger text, or None after reporting an unreadable file."""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        return None


def iter_ledger_records(text):
    """Return (physical_line_no, fields) for every non-blank, non-comment line."""
    records = []
    for line_no, line in enumerate(text.split("\n"), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            fields = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            fields = []
        records.append((line_no, [field.strip() for field in fields]))
    return records


def check_header(records):
    """Return (column_index, errors). Errors list 'header: missing <column>' lines."""
    if not records:
        return None, [f"header: missing {column}" for column in REQUIRED_COLUMNS]
    index = {}
    for position, name in enumerate(records[0][1]):
        index.setdefault(name, position)
    missing = [column for column in REQUIRED_COLUMNS if column not in index]
    if missing:
        return None, [f"header: missing {column}" for column in missing]
    return index, []


def _field(fields, position):
    if position is None or position >= len(fields):
        return ""
    return fields[position]


def parse_row(fields, index, line_no):
    """Return an Expense, or (line_no, reason) for the first failing field."""
    raw_date = _field(fields, index["date"])
    date = parse_date(raw_date)
    if date is None:
        return (line_no, "bad-date")
    payer = parse_payer(_field(fields, index["payer"]))
    if payer is None:
        return (line_no, "bad-payer")
    amount = parse_amount(_field(fields, index["amount"]))
    if amount is None:
        return (line_no, "bad-amount")
    participants = parse_participants(_field(fields, index["participants"]))
    if participants is None:
        return (line_no, "bad-participants")
    note = _field(fields, index.get("note"))
    return Expense(date, payer, amount, participants, note)


def filter_expenses(expenses, since, until):
    """Keep expenses with since <= date <= until for each bound given (inclusive)."""
    kept = []
    for expense in expenses:
        if since is not None and expense.date < since:
            continue
        if until is not None and expense.date > until:
            continue
        kept.append(expense)
    return kept


# ---------------------------------------------------------------------------
# Money logic
# ---------------------------------------------------------------------------

def format_cents(cents, signed):
    """Format cents as dd.cc. When signed, prefix + for positive and - for negative."""
    magnitude = abs(cents)
    body = f"{magnitude // 100}.{magnitude % 100:02d}"
    if signed:
        if cents > 0:
            return "+" + body
        if cents < 0:
            return "-" + body
        return body
    return ("-" if cents < 0 else "") + body


def allocate_shares(amount_cents, participants):
    """Split amount_cents by weight using largest remainders; shares sum exactly to amount."""
    total_weight = sum(participants.values())
    base = {}
    remainder = {}
    for name, weight in participants.items():
        base[name], remainder[name] = divmod(amount_cents * weight, total_weight)
    leftover = amount_cents - sum(base.values())
    order = sorted(participants, key=lambda name: (-remainder[name], name))
    for name in order[:leftover]:
        base[name] += 1
    assert sum(base.values()) == amount_cents
    return base


def accumulate_balances(expenses):
    """Net balance per person: amounts paid minus shares owed."""
    balances = {}
    for expense in expenses:
        balances[expense.payer] = balances.get(expense.payer, 0) + expense.amount_cents
        shares = allocate_shares(expense.amount_cents, expense.participants)
        for name, share in shares.items():
            balances[name] = balances.get(name, 0) - share
    return balances


def compute_transfers(balances):
    """Greedy settlement: largest debtor pays largest creditor until all are zero."""
    remaining = {name: cents for name, cents in balances.items() if cents != 0}
    transfers = []
    while True:
        debtors = [name for name in remaining if remaining[name] < 0]
        creditors = [name for name in remaining if remaining[name] > 0]
        if not debtors or not creditors:
            break
        debtor = min(debtors, key=lambda name: (remaining[name], name))
        creditor = min(creditors, key=lambda name: (-remaining[name], name))
        amount = min(-remaining[debtor], remaining[creditor])
        transfers.append((debtor, creditor, amount))
        remaining[debtor] += amount
        remaining[creditor] -= amount
        remaining = {name: cents for name, cents in remaining.items() if cents != 0}
    return transfers


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_balances_text(balances):
    return [f"{name} {format_cents(balances[name], signed=True)}" for name in sorted(balances)]


def render_balances_json(balances):
    payload = {"balances": [{"name": name, "cents": balances[name]} for name in sorted(balances)]}
    return json.dumps(payload) + "\n"


def render_settle_text(transfers):
    if not transfers:
        return ["nothing to settle"]
    return [f"{frm} -> {to} {format_cents(cents, signed=False)}" for frm, to, cents in transfers]


def render_settle_json(transfers):
    payload = {"transfers": [{"from": frm, "to": to, "cents": cents} for frm, to, cents in transfers]}
    return json.dumps(payload) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = UsageParser(prog="splitr.py", description="Shared-expense settlement CLI.",
                         allow_abbrev=False)
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger", help="path to the CSV ledger")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--since", type=parse_cli_date, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=parse_cli_date, default=None, metavar="YYYY-MM-DD")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    text = read_ledger_text(args.ledger)
    if text is None:
        return EXIT_UNREADABLE

    records = iter_ledger_records(text)
    index, header_errors = check_header(records)
    if header_errors:
        for message in header_errors:
            sys.stderr.write(message + "\n")
        return EXIT_BAD_ROWS

    expenses = []
    row_errors = []
    for line_no, fields in records[1:]:
        result = parse_row(fields, index, line_no)
        if isinstance(result, Expense):
            expenses.append(result)
        else:
            row_errors.append(result)
    if row_errors:
        for line_no, reason in row_errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        return EXIT_BAD_ROWS

    expenses = filter_expenses(expenses, args.since, args.until)
    balances = accumulate_balances(expenses)

    if args.command == "balances":
        if args.format == "json":
            output = render_balances_json(balances)
        else:
            output = "".join(line + "\n" for line in render_balances_text(balances))
    else:
        transfers = compute_transfers(balances)
        if args.format == "json":
            output = render_settle_json(transfers)
        else:
            output = "".join(line + "\n" for line in render_settle_text(transfers))

    sys.stdout.write(output)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
