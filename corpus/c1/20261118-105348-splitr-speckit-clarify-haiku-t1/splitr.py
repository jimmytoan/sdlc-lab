"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Reads a CSV ledger (header: date,payer,amount,participants,note), splits each
expense among its weighted participants in exact integer cents, and prints
either per-person balances or the payments that settle them.

Exit codes: 0 success, 2 invalid ledger data, 3 unreadable ledger,
64 wrong usage.
"""
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_DATA = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] "
         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]")

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]{1,2})?|\.[0-9]{1,2})")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    """Wrong command-line usage (exit 64)."""


class Expense:
    __slots__ = ("line", "date", "payer", "amount_cents", "participants")

    def __init__(self, line, date, payer, amount_cents, participants):
        self.line = line
        self.date = date
        self.payer = payer
        self.amount_cents = amount_cents
        # list of (name, weight), duplicates merged, first-appearance order
        self.participants = participants


# ---------------------------------------------------------------- parsing

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return a positive amount in integer cents, else None."""
    text = text.strip()
    if not _AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole or "0") * 100 + int(frac.ljust(2, "0") or "0")
    return cents if cents > 0 else None


def parse_participants(text):
    """Return a list of (name, weight) with duplicate names merged, else None."""
    merged = {}
    for entry in text.split(";"):
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
        merged[name] = merged.get(name, 0) + weight
    return list(merged.items())


def split_shares(amount_cents, participants):
    """Split amount_cents by weight; leftover cents go to earliest listed.

    The returned shares always sum exactly to amount_cents.
    """
    total_weight = sum(w for _, w in participants)
    shares = [amount_cents * w // total_weight for _, w in participants]
    leftover = amount_cents - sum(shares)
    for i in range(leftover):
        shares[i] += 1
    return shares


def read_ledger_text(path):
    """Return the ledger text, or None after reporting an unreadable file."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {path}", file=sys.stderr)
        return None


def iter_records(text):
    """Yield (physical_line_number, fields) for every non-comment, non-blank line.

    fields is None when the line cannot be parsed as one CSV record.
    """
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line.startswith("#") or not line.strip():
            continue
        try:
            fields = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            fields = None
        yield number, fields


def validate_ledger(text):
    """Return (expenses, errors). errors is a list of stderr lines."""
    records = iter_records(text)
    header = next(records, None)
    columns = [c.strip() for c in (header[1] or [])] if header else []

    for required in REQUIRED_COLUMNS:
        if required not in columns:
            return [], [f"header: missing {required}"]

    index = {}
    for pos, col in enumerate(columns):
        index.setdefault(col, pos)

    expenses = []
    errors = []
    for number, fields in records:
        if fields is None or len(fields) != len(columns):
            errors.append(f"line {number}: bad-columns")
            continue
        row_errors = []

        date = parse_date(fields[index["date"]].strip())
        if date is None:
            row_errors.append("bad-date")

        payer = fields[index["payer"]].strip()
        if not payer:
            row_errors.append("bad-payer")

        amount = parse_amount(fields[index["amount"]])
        if amount is None:
            row_errors.append("bad-amount")

        participants = parse_participants(fields[index["participants"]])
        if participants is None:
            row_errors.append("bad-participants")

        if row_errors:
            errors.extend(f"line {number}: {reason}" for reason in row_errors)
        else:
            expenses.append(Expense(number, date, payer, amount, participants))
    return expenses, errors


# ---------------------------------------------------------- computation

def compute_balances(expenses, since=None, until=None):
    """Return {name: cents}: paid minus shares, over expenses in the range."""
    balances = {}
    for expense in expenses:
        if since is not None and expense.date < since:
            continue
        if until is not None and expense.date > until:
            continue
        balances[expense.payer] = balances.get(expense.payer, 0) + expense.amount_cents
        shares = split_shares(expense.amount_cents, expense.participants)
        for (name, _), share in zip(expense.participants, shares):
            balances[name] = balances.get(name, 0) - share
    return balances


def settle(balances):
    """Greedy settlement: largest debtor pays largest creditor, repeatedly.

    Returns a list of (debtor, creditor, cents). Ties broken by name.
    """
    debtors = [[name, -cents] for name, cents in balances.items() if cents < 0]
    creditors = [[name, cents] for name, cents in balances.items() if cents > 0]
    transfers = []
    while debtors and creditors:
        debtors.sort(key=lambda item: (-item[1], item[0]))
        creditors.sort(key=lambda item: (-item[1], item[0]))
        debtor, creditor = debtors[0], creditors[0]
        amount = min(debtor[1], creditor[1])
        transfers.append((debtor[0], creditor[0], amount))
        debtor[1] -= amount
        creditor[1] -= amount
        if debtor[1] == 0:
            debtors.pop(0)
        if creditor[1] == 0:
            creditors.pop(0)
    return transfers


# ------------------------------------------------------------- formatting

def format_signed(cents):
    """'+22.00', '-8.00' or '0.00' (no sign for zero)."""
    sign = "+" if cents > 0 else "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def format_plain(cents):
    """Unsigned amount, e.g. '14.00'."""
    return f"{abs(cents) // 100}.{abs(cents) % 100:02d}"


# ------------------------------------------------------------------- CLI

def parse_args(argv):
    """Return (command, path, fmt, since, until). Raises UsageError."""
    options = {"format": "text", "since": None, "until": None}
    positional = []
    only_positional = False
    i = 0
    while i < len(argv):
        token = argv[i]
        i += 1
        if only_positional or token == "-" or not token.startswith("-"):
            positional.append(token)
            continue
        if token == "--":
            only_positional = True
            continue
        if not token.startswith("--"):
            raise UsageError(f"unknown option: {token}")
        name, has_value, value = token[2:].partition("=")
        if name not in options:
            raise UsageError(f"unknown option: --{name}")
        if not has_value:
            if i >= len(argv):
                raise UsageError(f"option --{name} needs a value")
            value = argv[i]
            i += 1
        options[name] = value

    if not positional:
        raise UsageError("missing command")
    if len(positional) < 2:
        raise UsageError("missing ledger path")
    if len(positional) > 2:
        raise UsageError(f"unexpected argument: {positional[2]}")

    command, path = positional
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if options["format"] not in FORMATS:
        raise UsageError(f"invalid --format value: {options['format']}")

    since = until = None
    if options["since"] is not None:
        since = parse_date(options["since"])
        if since is None:
            raise UsageError(f"invalid --since date: {options['since']}")
    if options["until"] is not None:
        until = parse_date(options["until"])
        if until is None:
            raise UsageError(f"invalid --until date: {options['until']}")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")

    return command, path, options["format"], since, until


def render(command, fmt, balances):
    """Return the stdout text for a successful run."""
    if command == "balances":
        if fmt == "json":
            payload = {"balances": [{"name": name, "cents": balances[name]}
                                    for name in sorted(balances)]}
            return json.dumps(payload) + "\n"
        return "".join(f"{name} {format_signed(balances[name])}\n"
                       for name in sorted(balances))

    transfers = settle(balances)
    if fmt == "json":
        payload = {"transfers": [{"from": debtor, "to": creditor, "cents": cents}
                                 for debtor, creditor, cents in transfers]}
        return json.dumps(payload) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{debtor} -> {creditor} {format_plain(cents)}\n"
                   for debtor, creditor, cents in transfers)


def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except UsageError as exc:
        print(USAGE, file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    text = read_ledger_text(path)
    if text is None:
        return EXIT_UNREADABLE

    expenses, errors = validate_ledger(text)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return EXIT_BAD_DATA

    balances = compute_balances(expenses, since, until)
    output = render(command, fmt, balances)
    sys.stdout.write(output)
    sys.stdout.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
