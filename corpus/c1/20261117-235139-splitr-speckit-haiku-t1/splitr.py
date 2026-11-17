"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 success, 2 bad ledger (rows or header), 3 unreadable file,
64 usage error.
"""

import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_LEDGER = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")
OPTIONS = ("--format", "--since", "--until")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- arguments

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, or None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    """Return (command, path, fmt, since, until). Raises UsageError."""
    positionals = []
    values = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("-") and arg != "-":
            name, eq, inline = arg.partition("=")
            if name not in OPTIONS:
                raise UsageError(f"unknown option: {name}")
            if eq:
                value = inline
            else:
                if i + 1 >= len(argv):
                    raise UsageError(f"option {name} needs a value")
                i += 1
                value = argv[i]
            values[name] = value  # last occurrence wins
        else:
            positionals.append(arg)
        i += 1

    if len(positionals) < 1:
        raise UsageError("missing command")
    command = positionals[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if len(positionals) < 2:
        raise UsageError("missing ledger path")
    if len(positionals) > 2:
        raise UsageError("unexpected extra argument")
    path = positionals[1]

    fmt = values.get("--format", "text")
    if fmt not in FORMATS:
        raise UsageError(f"invalid --format: {fmt}")

    since = until = None
    if "--since" in values:
        since = parse_date(values["--since"])
        if since is None:
            raise UsageError(f"invalid --since: {values['--since']}")
    if "--until" in values:
        until = parse_date(values["--until"])
        if until is None:
            raise UsageError(f"invalid --until: {values['--until']}")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")

    return command, path, fmt, since, until


# ------------------------------------------------------------------ reading

def read_ledger(path):
    """Return the decoded file text, or None if it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def logical_lines(text):
    """Yield (physical_line_number, line) for lines that are not comments or blank."""
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        if line.startswith("#") or not line.strip():
            continue
        yield number, line


def split_csv(line):
    try:
        return next(csv.reader([line]))
    except (csv.Error, StopIteration):
        return line.split(",")


# --------------------------------------------------------------- validation

def validate_date(text):
    return parse_date(text.strip())


def validate_payer(text):
    name = text.strip()
    return name or None


def validate_amount(text):
    """Return integer cents for a positive amount with at most 2 decimals."""
    s = text.strip()
    if not AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    return cents if cents > 0 else None


def validate_participants(text):
    """Return a list of (name, weight) or None if malformed."""
    entries = text.split(";")
    result = []
    seen = set()
    for entry in entries:
        entry = entry.strip()
        name, sep, weight_text = entry.partition(":")
        name = name.strip()
        if not name:
            return None
        if sep:
            weight_text = weight_text.strip()
            if not WEIGHT_RE.fullmatch(weight_text):
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
    return result or None


def parse_row(fields, index, header_len, line_number):
    """Return (expense_dict, None) or (None, reason)."""
    def get(col):
        i = index[col]
        return fields[i] if i < len(fields) else ""

    date = validate_date(get("date"))
    if date is None:
        return None, "bad-date"
    payer = validate_payer(get("payer"))
    if payer is None:
        return None, "bad-payer"
    amount = validate_amount(get("amount"))
    if amount is None:
        return None, "bad-amount"
    participants = validate_participants(get("participants"))
    if participants is None:
        return None, "bad-participants"
    # Non-empty fields beyond the header are not part of any column.
    if any(f.strip() for f in fields[header_len:]):
        return None, "bad-participants"
    return {
        "line": line_number,
        "date": date,
        "payer": payer,
        "amount": amount,
        "participants": participants,
        "note": get("note"),
    }, None


def load_expenses(text):
    """Return (expenses, errors, header_errors). Only one of the lists is meaningful."""
    lines = logical_lines(text)
    header = None
    for number, line in lines:
        header = (number, split_csv(line))
        break
    if header is None:
        return None, [], [f"header: missing {c}" for c in REQUIRED_COLUMNS]

    _, names = header
    header_len = len(names)
    index = {}
    for pos, name in enumerate(names):
        key = name.strip()
        if key not in index:
            index[key] = pos
    missing = [c for c in REQUIRED_COLUMNS if c not in index]
    if missing:
        return None, [], [f"header: missing {c}" for c in missing]

    expenses = []
    errors = []
    for number, line in lines:
        fields = split_csv(line)
        expense, reason = parse_row(fields, index, header_len, number)
        if reason is not None:
            errors.append(f"line {number}: {reason}")
        else:
            expenses.append(expense)
    return expenses, errors, []


# ------------------------------------------------------------- computation

def split_shares(amount, participants):
    """Largest-remainder split of `amount` cents by weight. Returns {name: cents}."""
    total_weight = sum(w for _, w in participants)
    shares = {}
    remainders = []
    for name, weight in participants:
        product = amount * weight
        shares[name] = product // total_weight
        remainders.append((-(product % total_weight), name))
    leftover = amount - sum(shares.values())
    remainders.sort()
    for _, name in remainders[:leftover]:
        shares[name] += 1
    return shares


def compute_balances(expenses):
    balances = {}
    for exp in expenses:
        balances.setdefault(exp["payer"], 0)
        balances[exp["payer"]] += exp["amount"]
        for name, share in split_shares(exp["amount"], exp["participants"]).items():
            balances.setdefault(name, 0)
            balances[name] -= share
    return balances


def compute_transfers(balances):
    """Greedy largest-debtor / largest-creditor settlement. Returns [(from, to, cents)]."""
    bal = {name: cents for name, cents in balances.items() if cents != 0}
    transfers = []
    while True:
        debtors = [n for n in bal if bal[n] < 0]
        creditors = [n for n in bal if bal[n] > 0]
        if not debtors or not creditors:
            break
        debtor = min(debtors, key=lambda n: (bal[n], n))
        creditor = min(creditors, key=lambda n: (-bal[n], n))
        amount = min(-bal[debtor], bal[creditor])
        transfers.append((debtor, creditor, amount))
        bal[debtor] += amount
        bal[creditor] -= amount
        bal = {n: c for n, c in bal.items() if c != 0}
    return transfers


# -------------------------------------------------------------- formatting

def fmt_amount(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt_amount(cents)
    return fmt_amount(cents)


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        obj = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(obj) + "\n"
    return "".join(f"{n} {fmt_signed(balances[n])}\n" for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        obj = {"transfers": [
            {"from": f, "to": t, "cents": c} for f, t, c in transfers
        ]}
        return json.dumps(obj) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {fmt_amount(c)}\n" for f, t, c in transfers)


# ------------------------------------------------------------------ main

def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except UsageError as exc:
        sys.stderr.write(f"usage: splitr.py <balances|settle> <ledger.csv> "
                         f"[--format text|json] [--since YYYY-MM-DD] "
                         f"[--until YYYY-MM-DD]\n{exc}\n")
        return EXIT_USAGE

    text = read_ledger(path)
    if text is None:
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_UNREADABLE

    expenses, row_errors, header_errors = load_expenses(text)
    if header_errors:
        sys.stderr.write("".join(e + "\n" for e in header_errors))
        return EXIT_BAD_LEDGER
    if row_errors:
        sys.stderr.write("".join(e + "\n" for e in row_errors))
        return EXIT_BAD_LEDGER

    selected = [
        e for e in expenses
        if (since is None or e["date"] >= since)
        and (until is None or e["date"] <= until)
    ]
    balances = compute_balances(selected)

    if command == "balances":
        out = render_balances(balances, fmt)
    else:
        out = render_settle(compute_transfers(balances), fmt)
    sys.stdout.write(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
