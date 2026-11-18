#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Ledger: CSV with header columns date,payer,amount,participants,note.
Money is handled as integer cents throughout.

Exit codes: 0 success, 2 invalid ledger content, 3 file cannot be read,
64 usage error.
"""

import argparse
import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WEIGHT_RE = re.compile(r"[0-9]+")


def parse_amount(text):
    """Return a positive amount in integer cents, or None if invalid."""
    text = text.strip()
    if not _AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + (int(frac.ljust(2, "0")) if frac else 0)
    if cents <= 0:
        return None
    return cents


def parse_date(text):
    """Return a real calendar date from YYYY-MM-DD, or None if invalid."""
    text = text.strip()
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_participants(text):
    """Return a list of (name, weight) tuples, or None if invalid."""
    text = text.strip()
    if not text:
        return None
    result = []
    seen = set()
    for entry in text.split(";"):
        entry = entry.strip()
        if not entry:
            return None
        name, sep, weight_text = entry.partition(":")
        name = name.strip()
        if not name:
            return None
        if sep:
            weight_text = weight_text.strip()
            if not _WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight <= 0:
                return None
        else:
            weight = 1
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


@dataclass
class Expense:
    line_no: int
    date: datetime.date
    payer: str
    amount_cents: int
    participants: list  # list of (name, weight)
    note: str


def parse_row(line_no, row):
    """Validate one data row. Returns an Expense or the first failing reason."""
    date = parse_date(row.get("date", ""))
    if date is None:
        return "bad-date"
    payer = row.get("payer", "").strip()
    if not payer:
        return "bad-payer"
    amount_cents = parse_amount(row.get("amount", ""))
    if amount_cents is None:
        return "bad-amount"
    participants = parse_participants(row.get("participants", ""))
    if participants is None:
        return "bad-participants"
    return Expense(
        line_no=line_no,
        date=date,
        payer=payer,
        amount_cents=amount_cents,
        participants=participants,
        note=row.get("note", ""),
    )


def split_cents(amount_cents, weights):
    """Split amount_cents by weights using largest-remainder allocation.

    Leftover cents go one at a time to the largest fractional remainder,
    ties broken by listing order. The result always sums to amount_cents.
    """
    total_weight = sum(weights)
    bases = [amount_cents * w // total_weight for w in weights]
    remainders = [(amount_cents * w) % total_weight for w in weights]
    leftover = amount_cents - sum(bases)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    shares = list(bases)
    for i in order[:leftover]:
        shares[i] += 1
    return shares


# ---------------------------------------------------------------------------
# Ledger reading
# ---------------------------------------------------------------------------

class HeaderError(Exception):
    def __init__(self, column):
        super().__init__(column)
        self.column = column


def _iter_records(fh):
    """Yield (physical_line_number, row) for each CSV record.

    Blank lines and lines starting with '#' are skipped. The physical line
    number is the line on which the record starts (header is line 1).
    """
    physical = []

    def feed():
        for number, line in enumerate(fh, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            physical.append(number)
            yield line

    reader = csv.reader(feed())
    consumed = 0
    for row in reader:
        start = physical[consumed]
        consumed = reader.line_num
        yield start, row


def load_expenses(path):
    """Read and validate the ledger.

    Returns (expenses, errors) where errors is a list of (line_no, reason).
    Raises OSError / UnicodeDecodeError / csv.Error if the file cannot be read,
    and HeaderError if a required column is missing.
    """
    expenses = []
    errors = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        records = _iter_records(fh)
        try:
            _, header_row = next(records)
        except StopIteration:
            header_row = []
        header = [name.strip() for name in header_row]
        for column in REQUIRED_COLUMNS:
            if column not in header:
                raise HeaderError(column)
        for line_no, row in records:
            data = {
                name: (row[i] if i < len(row) else "")
                for i, name in enumerate(header)
            }
            result = parse_row(line_no, data)
            if isinstance(result, Expense):
                expenses.append(result)
            else:
                errors.append((line_no, result))
    return expenses, errors


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def filter_expenses(expenses, since=None, until=None):
    result = []
    for e in expenses:
        if since is not None and e.date < since:
            continue
        if until is not None and e.date > until:
            continue
        result.append(e)
    return result


def compute_balances(expenses):
    """Return {name: cents}: paid minus shares, across the given expenses."""
    balances = {}
    for e in expenses:
        names = [name for name, _ in e.participants]
        weights = [weight for _, weight in e.participants]
        shares = split_cents(e.amount_cents, weights)
        if sum(shares) != e.amount_cents:
            raise AssertionError(
                f"line {e.line_no}: shares do not sum to amount"
            )
        balances[e.payer] = balances.get(e.payer, 0) + e.amount_cents
        for name in names:
            balances.setdefault(name, 0)
        for name, share in zip(names, shares):
            balances[name] -= share
    return balances


def compute_transfers(balances):
    """Greedy settlement: largest debtor pays largest creditor.

    Returns a list of (debtor, creditor, cents) in greedy order.
    Ties are broken by name (A to Z).
    """
    owed = {name: -bal for name, bal in balances.items() if bal < 0}
    credit = {name: bal for name, bal in balances.items() if bal > 0}
    transfers = []
    while owed and credit:
        debtor = min(owed, key=lambda n: (-owed[n], n))
        creditor = min(credit, key=lambda n: (-credit[n], n))
        amount = min(owed[debtor], credit[creditor])
        transfers.append((debtor, creditor, amount))
        owed[debtor] -= amount
        credit[creditor] -= amount
        if owed[debtor] == 0:
            del owed[debtor]
        if credit[creditor] == 0:
            del credit[creditor]
    return transfers


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def format_signed(cents):
    sign = "+" if cents > 0 else "-" if cents < 0 else ""
    magnitude = abs(cents)
    return f"{sign}{magnitude // 100}.{magnitude % 100:02d}"


def format_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        payload = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(payload) + "\n"
    return "".join(f"{n} {format_signed(balances[n])}\n" for n in names)


def render_transfers(transfers, fmt):
    if fmt == "json":
        payload = {
            "transfers": [
                {"from": d, "to": c, "cents": amount} for d, c, amount in transfers
            ]
        }
        return json.dumps(payload) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(
        f"{d} -> {c} {format_plain(amount)}\n" for d, c, amount in transfers
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise _UsageError(message)

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def _cli_date(text):
    parsed = parse_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"invalid date: {text!r} (expected YYYY-MM-DD)")
    return parsed


def build_parser():
    parser = _Parser(
        prog="splitr.py",
        description="Shared-expense settlement: who owes whom.",
    )
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger", help="path to the ledger CSV file")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--since", type=_cli_date, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=_cli_date, default=None, metavar="YYYY-MM-DD")
    return parser


def main(argv):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        return EXIT_USAGE
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return EXIT_USAGE

    try:
        expenses, errors = load_expenses(args.ledger)
    except HeaderError as exc:
        sys.stderr.write(f"header: missing {exc.column}\n")
        return EXIT_BAD_ROWS
    except (OSError, UnicodeDecodeError, csv.Error):
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_CANNOT_READ

    if errors:
        for line_no, reason in errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        return EXIT_BAD_ROWS

    selected = filter_expenses(expenses, args.since, args.until)
    balances = compute_balances(selected)

    if args.command == "balances":
        output = render_balances(balances, args.format)
    else:
        output = render_transfers(compute_transfers(balances), args.format)

    sys.stdout.write(output)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
