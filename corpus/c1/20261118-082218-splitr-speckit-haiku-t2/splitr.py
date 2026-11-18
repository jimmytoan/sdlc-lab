#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Ledger: CSV with header columns date,payer,amount,participants (note optional).
Money is handled as integer cents throughout. Standard library only.
"""
import argparse
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REASON_DATE = "bad-date"
REASON_PAYER = "bad-payer"
REASON_AMOUNT = "bad-amount"
REASON_PARTICIPANTS = "bad-participants"

REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")
COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
_WHOLE_RE = re.compile(r"[0-9]+")


class _Unreadable(Exception):
    """The ledger file could not be read or decoded."""


class _UsageParser(argparse.ArgumentParser):
    """Argument parser that exits with EXIT_USAGE (64) on any usage error."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def parse_amount_cents(text):
    """Return the amount as an int number of cents, or None if invalid."""
    m = _AMOUNT_RE.fullmatch(text)
    if m is None:
        return None
    whole = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    return whole * 100 + int(frac)


def parse_date(text):
    """Return a datetime.date for a real YYYY-MM-DD date, or None."""
    if _DATE_RE.fullmatch(text) is None:
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_participants(text):
    """Parse 'name' or 'name:weight' entries separated by ';'.

    Returns a list of (name, weight) tuples, or None if invalid."""
    result = []
    seen = set()
    for segment in text.split(";"):
        name_part, sep, weight_part = segment.partition(":")
        name = name_part.strip()
        if not name:
            return None
        if sep:
            weight_text = weight_part.strip()
            if _WHOLE_RE.fullmatch(weight_text) is None:
                return None
            weight = int(weight_text)
            if weight < 1:
                return None
        else:
            weight = 1
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def split_expense(amount_cents, participants):
    """Split amount_cents among (name, weight) pairs; returns {name: cents}.

    Each share is floor(T * w / W). Leftover cents go one at a time to
    participants in name order, so the shares always sum to T exactly."""
    total_weight = sum(weight for _, weight in participants)
    shares = {name: amount_cents * weight // total_weight for name, weight in participants}
    leftover = amount_cents - sum(shares.values())
    for name in sorted(shares)[:leftover]:
        shares[name] += 1
    return shares


def validate_row(line, row, cols):
    """Validate one CSV record. Returns (expense, None) or (None, reason)."""

    def cell(column):
        index = cols[column]
        return row[index].strip() if index < len(row) else ""

    day = parse_date(cell("date"))
    if day is None:
        return None, REASON_DATE
    payer = cell("payer")
    if not payer:
        return None, REASON_PAYER
    amount = parse_amount_cents(cell("amount"))
    if amount is None:
        return None, REASON_AMOUNT
    participants = parse_participants(cell("participants"))
    if participants is None:
        return None, REASON_PARTICIPANTS
    note = cell("note") if "note" in cols else ""
    return {
        "line": line,
        "date": day,
        "payer": payer,
        "amount_cents": amount,
        "participants": participants,
        "note": note,
    }, None


def read_ledger(path):
    """Read the ledger. Returns a list of (physical_line, record_cells).

    Blank lines and lines starting with '#' are skipped. Each record keeps the
    physical line number where it starts. Raises _Unreadable on I/O, decoding
    or CSV-level failure."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            raw_lines = handle.readlines()
    except (OSError, UnicodeDecodeError, ValueError):
        raise _Unreadable()

    kept = []  # (physical line number, text) for lines that are not skipped
    for number, text in enumerate(raw_lines, start=1):
        if not text.strip() or text.startswith("#"):
            continue
        kept.append((number, text))

    reader = csv.reader(text for _, text in kept)
    records = []
    consumed = 0
    try:
        for cells in reader:
            records.append((kept[consumed][0], cells))
            consumed = reader.line_num
    except csv.Error:
        raise _Unreadable()
    return records


def header_columns(header):
    """Map required/optional column names to indexes.

    Returns (cols, None) on success or (None, missing_column)."""
    cols = {}
    for index, cell in enumerate(header):
        name = cell.strip().lower()
        if name and name not in cols:
            cols[name] = index
    for column in REQUIRED_COLUMNS:
        if column not in cols:
            return None, column
    return cols, None


def compute_balances(expenses):
    """Return {name: cents}: total paid minus total shares, per person."""
    balances = {}
    for expense in expenses:
        payer = expense["payer"]
        balances[payer] = balances.get(payer, 0) + expense["amount_cents"]
        shares = split_expense(expense["amount_cents"], expense["participants"])
        for name, share in shares.items():
            balances[name] = balances.get(name, 0) - share
    return balances


def settle(balances):
    """Greedy settlement. Returns a list of (debtor, creditor, cents)."""
    debtors = [[name, -amount] for name, amount in balances.items() if amount < 0]
    creditors = [[name, amount] for name, amount in balances.items() if amount > 0]
    debtors.sort(key=lambda item: (-item[1], item[0]))
    creditors.sort(key=lambda item: (-item[1], item[0]))

    transfers = []
    di = ci = 0
    while di < len(debtors) and ci < len(creditors):
        debtor, owed = debtors[di]
        creditor, due = creditors[ci]
        amount = min(owed, due)
        if amount > 0:
            transfers.append((debtor, creditor, amount))
        debtors[di][1] -= amount
        creditors[ci][1] -= amount
        if debtors[di][1] == 0:
            di += 1
        if creditors[ci][1] == 0:
            ci += 1
    return transfers


def format_cents(cents, signed=False):
    """Format integer cents as 'D.CC'; with signed=True, prefix '+' or '-'."""
    magnitude = abs(cents)
    body = f"{magnitude // 100}.{magnitude % 100:02d}"
    if signed:
        return ("-" if cents < 0 else "+") + body
    return body


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        payload = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(payload) + "\n"
    return "".join(f"{n} {format_cents(balances[n], signed=True)}\n" for n in names)


def render_transfers(transfers, fmt):
    if fmt == "json":
        payload = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
        return json.dumps(payload) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{d} -> {c} {format_cents(a)}\n" for d, c, a in transfers)


def _date_arg(text):
    day = parse_date(text)
    if day is None:
        raise argparse.ArgumentTypeError(f"invalid date {text!r} (expected YYYY-MM-DD)")
    return day


def build_parser():
    parser = _UsageParser(prog="splitr.py", description="Shared-expense settlement CLI.")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("ledger")
    parser.add_argument("--format", choices=FORMATS, default="text")
    parser.add_argument("--since", type=_date_arg, default=None)
    parser.add_argument("--until", type=_date_arg, default=None)
    return parser


def main(argv):
    args = build_parser().parse_args(argv)

    try:
        records = read_ledger(args.ledger)
    except _Unreadable:
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_UNREADABLE

    if not records:
        sys.stderr.write("header: missing date\n")
        return EXIT_BAD_ROWS

    cols, missing = header_columns(records[0][1])
    if cols is None:
        sys.stderr.write(f"header: missing {missing}\n")
        return EXIT_BAD_ROWS

    expenses = []
    errors = []
    for line, cells in records[1:]:
        expense, reason = validate_row(line, cells, cols)
        if reason is not None:
            errors.append(f"line {line}: {reason}")
        else:
            expenses.append(expense)
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return EXIT_BAD_ROWS

    if args.since is not None:
        expenses = [e for e in expenses if e["date"] >= args.since]
    if args.until is not None:
        expenses = [e for e in expenses if e["date"] <= args.until]

    balances = compute_balances(expenses)
    if args.command == "balances":
        output = render_balances(balances, args.format)
    else:
        output = render_transfers(settle(balances), args.format)

    sys.stdout.write(output)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass
    sys.exit(main(sys.argv[1:]))
