"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Reads a CSV ledger with the header `date,payer,amount,participants,note`
and prints either each person's balance or the payments that settle the
group. All money is handled as integer cents.

Exit codes: 0 success, 2 bad rows or header, 3 unreadable ledger,
64 usage error. On any failure nothing is written to stdout.
"""
import csv
import datetime
import io
import json
import re
import sys
from collections import namedtuple

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
OPTIONS = ("--format", "--since", "--until")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] "
         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")

Args = namedtuple("Args", "command path fmt since until")
Expense = namedtuple("Expense", "date payer cents parts")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount_cents(text):
    """Return positive integer cents for a decimal with at most 2 places, else None."""
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    return cents if cents > 0 else None


def parse_participants(text):
    """Return a list of (name, weight) in listed order, or None if invalid."""
    text = text.strip()
    if not text:
        return None
    result = []
    seen = set()
    for entry in text.split(";"):
        name, sep, wtext = entry.partition(":")
        name = name.strip()
        if not name:
            return None
        if sep:
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext) or int(wtext) == 0:
                return None
            weight = int(wtext)
        else:
            weight = 1
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def parse_args(argv):
    """Parse argv (without program name). Raises UsageError on any usage problem."""
    if not argv:
        raise UsageError(USAGE)
    command = argv[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}\n{USAGE}")

    rest = argv[1:]
    path = None
    opts = {}
    i = 0
    while i < len(rest):
        arg = rest[i]
        i += 1
        if arg.startswith("-") and arg != "-":
            if not arg.startswith("--"):
                raise UsageError(f"unknown option: {arg}")
            name, eq, value = arg.partition("=")
            if name not in OPTIONS:
                raise UsageError(f"unknown option: {name}")
            if name in opts:
                raise UsageError(f"repeated option: {name}")
            if not eq:
                if i >= len(rest) or rest[i].startswith("--"):
                    raise UsageError(f"missing value for {name}")
                value = rest[i]
                i += 1
            opts[name] = value
        else:
            if path is not None:
                raise UsageError(f"unexpected argument: {arg}")
            path = arg

    if path is None:
        raise UsageError(f"missing ledger path\n{USAGE}")

    fmt = opts.get("--format", "text")
    if fmt not in FORMATS:
        raise UsageError(f"invalid --format value: {fmt} (use text or json)")

    dates = {}
    for name in ("--since", "--until"):
        if name in opts:
            parsed = parse_date(opts[name])
            if parsed is None:
                raise UsageError(f"invalid {name} date: {opts[name]} (use YYYY-MM-DD)")
            dates[name] = parsed

    return Args(command, path, fmt, dates.get("--since"), dates.get("--until"))


# ---------------------------------------------------------------- ledger

def read_records(path):
    """Return [(physical_line_no, fields)] for every record after comments/blanks.

    The first record is the header. Raises OSError, UnicodeDecodeError or
    csv.Error if the file cannot be read.
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        text = fh.read()

    physical = []  # physical line numbers of lines handed to csv.reader

    def source():
        for number, line in enumerate(io.StringIO(text, newline=""), start=1):
            if line.startswith("#") or not line.strip():
                continue
            physical.append(number)
            yield line

    reader = csv.reader(source())
    records = []
    for fields in reader:
        # reader.line_num is how many source lines were consumed for this record;
        # the record ends on the last of them.
        records.append((physical[reader.line_num - 1], fields))
    return records


def column_index(header):
    """Map each required column to its index in the header, or raise on the first missing one."""
    names = [cell.strip() for cell in header]
    index = {}
    for col in REQUIRED_COLUMNS:
        if col not in names:
            raise LookupError(col)
        index[col] = names.index(col)
    return index


def validate_row(fields, index):
    """Return (Expense, None) for a valid row, or (None, reason) for an invalid one."""
    def get(col):
        i = index[col]
        return fields[i] if i < len(fields) else ""

    day = parse_date(get("date").strip())
    if day is None:
        return None, "bad-date"

    payer = get("payer").strip()
    if not payer or ";" in payer or ":" in payer:
        return None, "bad-payer"

    cents = parse_amount_cents(get("amount").strip())
    if cents is None:
        return None, "bad-amount"

    parts = parse_participants(get("participants"))
    if parts is None:
        return None, "bad-participants"

    return Expense(day, payer, cents, parts), None


# ---------------------------------------------------------------- computation

def allocate(amount, parts):
    """Split amount cents among (name, weight) parts; shares sum exactly to amount.

    Largest-remainder method; leftover cents go to the earliest-listed
    participants with the largest remainders.
    """
    total_weight = sum(w for _, w in parts)
    bases = []
    remainders = []
    for _, w in parts:
        q, r = divmod(amount * w, total_weight)
        bases.append(q)
        remainders.append(r)
    leftover = amount - sum(bases)
    shares = list(bases)
    order = sorted(range(len(parts)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def compute_balances(expenses, since, until):
    """Return {name: cents} for every person in the ledger (all valid rows).

    Only expenses inside the inclusive date range count towards amounts.
    """
    balances = {}
    for e in expenses:
        balances.setdefault(e.payer, 0)
        for name, _ in e.parts:
            balances.setdefault(name, 0)

    for e in expenses:
        if since is not None and e.date < since:
            continue
        if until is not None and e.date > until:
            continue
        balances[e.payer] += e.cents
        shares = allocate(e.cents, e.parts)
        for (name, _), share in zip(e.parts, shares):
            balances[name] -= share
    return balances


def settle(balances):
    """Greedy settlement: return [(debtor, creditor, cents)] in order."""
    remaining = {name: c for name, c in balances.items() if c != 0}
    transfers = []
    while True:
        debtors = [n for n, c in remaining.items() if c < 0]
        creditors = [n for n, c in remaining.items() if c > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda n: (remaining[n], n))
        c = min(creditors, key=lambda n: (-remaining[n], n))
        amount = min(-remaining[d], remaining[c])
        transfers.append((d, c, amount))
        remaining[d] += amount
        remaining[c] -= amount
        if remaining[d] == 0:
            del remaining[d]
        if remaining[c] == 0:
            del remaining[c]
    return transfers


# ---------------------------------------------------------------- output

def format_cents(cents, signed):
    sign = ""
    if signed and cents != 0:
        sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def format_balances_text(balances):
    return [f"{name} {format_cents(balances[name], True)}" for name in sorted(balances)]


def format_balances_json(balances):
    items = [{"name": name, "cents": balances[name]} for name in sorted(balances)]
    return json.dumps({"balances": items})


def format_settle_text(transfers):
    if not transfers:
        return ["nothing to settle"]
    return [f"{d} -> {c} {format_cents(a, False)}" for d, c, a in transfers]


def format_settle_json(transfers):
    items = [{"from": d, "to": c, "cents": a} for d, c, a in transfers]
    return json.dumps({"transfers": items})


# ---------------------------------------------------------------- main

def main(argv):
    try:
        args = parse_args(argv)
    except UsageError as exc:
        print(f"splitr: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        records = read_records(args.path)
    except (OSError, UnicodeDecodeError, csv.Error):
        print(f"error: cannot read {args.path}", file=sys.stderr)
        return EXIT_UNREADABLE

    if not records:
        print("header: missing date", file=sys.stderr)
        return EXIT_BAD_ROWS
    try:
        index = column_index(records[0][1])
    except LookupError as exc:
        print(f"header: missing {exc.args[0]}", file=sys.stderr)
        return EXIT_BAD_ROWS

    expenses = []
    problems = []
    for lineno, fields in records[1:]:
        expense, reason = validate_row(fields, index)
        if expense is None:
            problems.append(f"line {lineno}: {reason}")
        else:
            expenses.append(expense)
    if problems:
        for line in problems:
            print(line, file=sys.stderr)
        return EXIT_BAD_ROWS

    balances = compute_balances(expenses, args.since, args.until)

    if args.command == "balances":
        if args.fmt == "json":
            out = [format_balances_json(balances)]
        else:
            out = format_balances_text(balances)
    else:
        transfers = settle(balances)
        if args.fmt == "json":
            out = [format_settle_json(transfers)]
        else:
            out = format_settle_text(transfers)

    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
