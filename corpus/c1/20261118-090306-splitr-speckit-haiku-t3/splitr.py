"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Money is handled as integer cents throughout; floats are never used.
Standard library only.
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
EXIT_READ_ERROR = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    """Raised internally; mapped to exit 64 by the argument parser."""


class UsageParser(argparse.ArgumentParser):
    """Argument parser whose usage errors exit 64 (not argparse's default 2)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(EXIT_USAGE)


# ---------------------------------------------------------------- parsing

def parse_amount_cents(text):
    """Return positive integer cents for a amount like '30', '12.5', '12.50'; else None."""
    s = text.strip()
    if not _AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    return cents if cents > 0 else None


def parse_date(text):
    """Return datetime.date for a strict YYYY-MM-DD real calendar date; else None."""
    s = text.strip()
    if not _DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_participants(text):
    """Return a list of (name, weight) in listed order, or None if invalid."""
    entries = []
    for token in text.split(";"):
        token = token.strip()
        if ":" in token:
            name, _, wtext = token.rpartition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not _WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight < 1:
                return None
        else:
            name = token
            weight = 1
        if not name:
            return None
        entries.append((name, weight))
    if not entries:
        return None
    return entries


# ---------------------------------------------------------------- formatting

def format_signed_cents(cents):
    sign = "-" if cents < 0 else "+"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def format_unsigned_cents(cents):
    a = abs(cents)
    return f"{a // 100}.{a % 100:02d}"


# ---------------------------------------------------------------- ledger

def read_text(path):
    """Read the whole ledger; raise OSError/ValueError on failure."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def load_ledger(path):
    """Return (expenses, errors).

    Exits with the appropriate code on read or header failures.
    expenses: list of dicts with keys line, date, payer, amount_cents, participants.
    errors: list of (line, reason) for bad rows, in file order.
    """
    try:
        text = read_text(path)
    except (OSError, ValueError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_READ_ERROR)

    try:
        reader = csv.reader(io.StringIO(text), strict=False)
        header = None
        header_index = {}
        expenses = []
        errors = []
        while True:
            start_line = reader.line_num + 1
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error:
                sys.stderr.write(f"error: cannot read {path}\n")
                sys.exit(EXIT_READ_ERROR)

            if not row or all(field.strip() == "" for field in row):
                continue
            if row[0].startswith("#"):
                continue

            if header is None:
                names = [field.strip() for field in row]
                for col in REQUIRED_COLUMNS:
                    if col not in names:
                        sys.stderr.write(f"header: missing {col}\n")
                        sys.exit(EXIT_BAD_ROWS)
                header = names
                for col in REQUIRED_COLUMNS:
                    header_index[col] = names.index(col)
                continue

            def field(col):
                idx = header_index[col]
                return row[idx] if idx < len(row) else ""

            date = parse_date(field("date"))
            if date is None:
                errors.append((start_line, "bad-date"))
                continue
            payer = field("payer").strip()
            if not payer:
                errors.append((start_line, "bad-payer"))
                continue
            amount = parse_amount_cents(field("amount"))
            if amount is None:
                errors.append((start_line, "bad-amount"))
                continue
            participants = parse_participants(field("participants"))
            if participants is None:
                errors.append((start_line, "bad-participants"))
                continue
            expenses.append({
                "line": start_line,
                "date": date,
                "payer": payer,
                "amount_cents": amount,
                "participants": participants,
            })
    except csv.Error:
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_READ_ERROR)

    if header is None:
        sys.stderr.write("header: missing date\n")
        sys.exit(EXIT_BAD_ROWS)

    return expenses, errors


# ---------------------------------------------------------------- computation

def split_shares(amount_cents, participants):
    """Largest-remainder split of amount into per-participant shares (cents)."""
    total_weight = sum(w for _, w in participants)
    floors = []
    rems = []
    for _, w in participants:
        num = amount_cents * w
        floors.append(num // total_weight)
        rems.append(num % total_weight)
    left = amount_cents - sum(floors)
    order = sorted(range(len(participants)), key=lambda i: (-rems[i], i))
    for i in order[:left]:
        floors[i] += 1
    return floors


def compute_balances(expenses):
    """Return dict name -> net cents (paid minus shares)."""
    balances = {}
    for exp in expenses:
        balances[exp["payer"]] = balances.get(exp["payer"], 0) + exp["amount_cents"]
        shares = split_shares(exp["amount_cents"], exp["participants"])
        for (name, _), share in zip(exp["participants"], shares):
            balances[name] = balances.get(name, 0) - share
    return balances


def compute_transfers(balances):
    """Greedy settlement: list of (from, to, cents) in computed order."""
    owe = {n: -c for n, c in balances.items() if c < 0}
    owed = {n: c for n, c in balances.items() if c > 0}
    transfers = []
    while owe and owed:
        debtor = min(owe, key=lambda n: (-owe[n], n))
        creditor = min(owed, key=lambda n: (-owed[n], n))
        amount = min(owe[debtor], owed[creditor])
        transfers.append((debtor, creditor, amount))
        owe[debtor] -= amount
        owed[creditor] -= amount
        if owe[debtor] == 0:
            del owe[debtor]
        if owed[creditor] == 0:
            del owed[creditor]
    return transfers


# ---------------------------------------------------------------- commands

def filter_expenses(expenses, since, until):
    out = []
    for exp in expenses:
        if since is not None and exp["date"] < since:
            continue
        if until is not None and exp["date"] > until:
            continue
        out.append(exp)
    return out


def cmd_balances(expenses, fmt):
    balances = compute_balances(expenses)
    names = sorted(balances)
    if fmt == "json":
        obj = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        sys.stdout.write(json.dumps(obj) + "\n")
    else:
        out = []
        for n in names:
            out.append(f"{n} {format_signed_cents(balances[n])}\n")
        sys.stdout.write("".join(out))
    return EXIT_OK


def cmd_settle(expenses, fmt):
    balances = compute_balances(expenses)
    transfers = compute_transfers(balances)
    if fmt == "json":
        obj = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        sys.stdout.write(json.dumps(obj) + "\n")
    elif not transfers:
        sys.stdout.write("nothing to settle\n")
    else:
        sys.stdout.write("".join(
            f"{f} -> {t} {format_unsigned_cents(c)}\n" for f, t, c in transfers))
    return EXIT_OK


def build_parser():
    parser = UsageParser(prog="splitr.py", add_help=True,
                         description="Shared-expense settlement CLI.")
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger", help="path to the ledger CSV file")
    parser.add_argument("--format", dest="fmt", choices=["text", "json"], default="text")
    parser.add_argument("--since", type=_date_arg, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=_date_arg, default=None, metavar="YYYY-MM-DD")
    return parser


def _date_arg(text):
    d = parse_date(text)
    if d is None:
        raise argparse.ArgumentTypeError(f"invalid date: {text!r} (expected YYYY-MM-DD)")
    return d


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    expenses, errors = load_ledger(args.ledger)
    if errors:
        for line, reason in errors:
            sys.stderr.write(f"line {line}: {reason}\n")
        return EXIT_BAD_ROWS

    expenses = filter_expenses(expenses, args.since, args.until)

    if args.command == "balances":
        return cmd_balances(expenses, args.fmt)
    return cmd_settle(expenses, args.fmt)


if __name__ == "__main__":
    sys.exit(main())
