"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Ledger header: date,payer,amount,participants[,note]
Participants are ';'-separated names, each optionally 'name:weight'.
All money is handled as integer cents.

Exit codes:
    0   success
    2   invalid ledger (bad header column or bad rows)
    3   ledger file cannot be read
    64  usage error
"""

import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_INVALID_LEDGER = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not isinstance(text, str) or not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount_cents(text):
    """Return a positive amount as integer cents, else None."""
    if not isinstance(text, str) or not _AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    return cents if cents > 0 else None


def parse_participants(text):
    """Return an ordered list of (name, weight) with duplicates merged, else None."""
    if not isinstance(text, str):
        return None
    merged = {}
    order = []
    for item in text.split(";"):
        item = item.strip()
        if not item:
            return None
        name, sep, weight_text = item.partition(":")
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
        if name not in merged:
            merged[name] = 0
            order.append(name)
        merged[name] += weight
    return [(name, merged[name]) for name in order]


def parse_header(line):
    """Return {column: index} for the header line, or raise UsageError-like
    ValueError with the 'header: missing <column>' message."""
    fields = next(csv.reader([line]), [])
    index = {}
    for i, field in enumerate(fields):
        name = field.strip()
        if name and name not in index:
            index[name] = i
    for column in REQUIRED_COLUMNS:
        if column not in index:
            raise ValueError("header: missing %s" % column)
    return index


def read_ledger_lines(path):
    """Return the physical lines (text, without line ending) of the ledger.

    Raises OSError / UnicodeDecodeError if the file cannot be read.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8-sig")
    lines = text.split("\n")
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def split_row(line):
    try:
        return next(csv.reader([line]), [])
    except csv.Error:
        return line.split(",")


def build_expense(fields, index, line_no):
    """Validate one row. Return (expense_dict, None) or (None, reason)."""

    def field(column):
        pos = index[column]
        return fields[pos] if pos < len(fields) else ""

    date = parse_date(field("date").strip())
    if date is None:
        return None, "bad-date"
    payer = field("payer").strip()
    if not payer:
        return None, "bad-payer"
    amount = parse_amount_cents(field("amount").strip())
    if amount is None:
        return None, "bad-amount"
    participants = parse_participants(field("participants"))
    if participants is None:
        return None, "bad-participants"
    note = field("note") if "note" in index else ""
    return {
        "line": line_no,
        "date": date,
        "payer": payer,
        "amount_cents": amount,
        "participants": participants,
        "note": note,
    }, None


def load_expenses(lines, index):
    """Validate all data rows. Return (expenses, errors) where errors is a list
    of 'line N: reason' strings in line order."""
    expenses = []
    errors = []
    for line_no, line in enumerate(lines[1:], start=2):
        if not line.strip() or line.startswith("#"):
            continue
        fields = split_row(line)
        expense, reason = build_expense(fields, index, line_no)
        if reason is not None:
            errors.append("line %d: %s" % (line_no, reason))
        else:
            expenses.append(expense)
    return expenses, errors


# ---------------------------------------------------------------- computing

def split_shares(amount_cents, participants):
    """Split amount_cents by weight; shares sum exactly to amount_cents.

    Leftover cents go one at a time to participants in first-appearance order.
    Returns a list of (name, share_cents) in the same order as participants.
    """
    total_weight = sum(weight for _, weight in participants)
    shares = [amount_cents * weight // total_weight for _, weight in participants]
    remainder = amount_cents - sum(shares)
    for i in range(remainder):
        shares[i] += 1
    assert sum(shares) == amount_cents
    return [(name, share) for (name, _), share in zip(participants, shares)]


def in_range(day, since, until):
    if since is not None and day < since:
        return False
    if until is not None and day > until:
        return False
    return True


def filter_expenses(expenses, since, until):
    return [e for e in expenses if in_range(e["date"], since, until)]


def compute_balances(expenses):
    """Return a list of (name, cents) sorted by name."""
    balances = {}
    for e in expenses:
        payer = e["payer"]
        balances[payer] = balances.get(payer, 0) + e["amount_cents"]
        for name, share in split_shares(e["amount_cents"], e["participants"]):
            balances[name] = balances.get(name, 0) - share
    assert sum(balances.values()) == 0
    return sorted(balances.items(), key=lambda item: item[0])


def compute_transfers(balances):
    """Greedy settlement: largest debtor pays largest creditor.

    Returns a list of (from_name, to_name, cents).
    """
    debtors = sorted(
        ((name, -cents) for name, cents in balances if cents < 0),
        key=lambda item: (-item[1], item[0]),
    )
    creditors = sorted(
        ((name, cents) for name, cents in balances if cents > 0),
        key=lambda item: (-item[1], item[0]),
    )
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d_name, d_amt = debtors[i]
        c_name, c_amt = creditors[j]
        amount = min(d_amt, c_amt)
        transfers.append((d_name, c_name, amount))
        d_amt -= amount
        c_amt -= amount
        debtors[i] = (d_name, d_amt)
        creditors[j] = (c_name, c_amt)
        if d_amt == 0:
            i += 1
        if c_amt == 0:
            j += 1
    return transfers


# ---------------------------------------------------------------- rendering

def format_signed(cents):
    sign = "-" if cents < 0 else "+"
    a = abs(cents)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def format_unsigned(cents):
    a = abs(cents)
    return "%d.%02d" % (a // 100, a % 100)


def render_balances_text(balances):
    return "".join("%s %s\n" % (name, format_signed(cents)) for name, cents in balances)


def render_balances_json(balances):
    obj = {"balances": [{"name": name, "cents": cents} for name, cents in balances]}
    return json.dumps(obj) + "\n"


def render_settle_text(transfers):
    if not transfers:
        return "nothing to settle\n"
    return "".join(
        "%s -> %s %s\n" % (src, dst, format_unsigned(cents)) for src, dst, cents in transfers
    )


def render_settle_json(transfers):
    obj = {"transfers": [
        {"from": src, "to": dst, "cents": cents} for src, dst, cents in transfers
    ]}
    return json.dumps(obj) + "\n"


# ---------------------------------------------------------------- CLI

def parse_args(argv):
    """Return a dict with command, path, format, since, until.

    Raises UsageError on any usage problem. Returns None when help was asked.
    """
    positionals = []
    opts = {"format": "text", "since": None, "until": None}
    value_options = {"--format", "--since", "--until"}
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if tok in ("-h", "--help"):
            return None
        if tok == "--":
            positionals.extend(argv[i:])
            break
        if tok.startswith("--") and "=" in tok:
            name, _, value = tok.partition("=")
            if name not in value_options:
                raise UsageError("unknown option: %s" % name)
        elif tok in value_options:
            if i >= len(argv):
                raise UsageError("option %s requires a value" % tok)
            name, value = tok, argv[i]
            i += 1
        elif tok.startswith("-") and tok != "-":
            raise UsageError("unknown option: %s" % tok)
        else:
            positionals.append(tok)
            continue
        key = name[2:]
        if key == "format":
            if value not in FORMATS:
                raise UsageError("invalid --format value: %r (use text or json)" % value)
            opts["format"] = value
        else:
            day = parse_date(value)
            if day is None:
                raise UsageError("invalid --%s date: %r (use YYYY-MM-DD)" % (key, value))
            opts[key] = day

    if not positionals:
        raise UsageError("missing command and ledger file")
    command = positionals[0]
    if command not in COMMANDS:
        raise UsageError("unknown command: %s (use balances or settle)" % command)
    if len(positionals) < 2:
        raise UsageError("missing ledger file")
    if len(positionals) > 2:
        raise UsageError("unexpected argument: %s" % positionals[2])
    opts["command"] = command
    opts["path"] = positionals[1]
    return opts


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        opts = parse_args(argv)
    except UsageError as exc:
        print("splitr: %s" % exc, file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    if opts is None:
        print(USAGE)
        return EXIT_OK

    try:
        lines = read_ledger_lines(opts["path"])
    except (OSError, UnicodeDecodeError):
        print("error: cannot read %s" % opts["path"], file=sys.stderr)
        return EXIT_UNREADABLE

    if not lines or not lines[0].strip():
        print("header: missing %s" % REQUIRED_COLUMNS[0], file=sys.stderr)
        return EXIT_INVALID_LEDGER
    try:
        index = parse_header(lines[0])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_INVALID_LEDGER

    expenses, errors = load_expenses(lines, index)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return EXIT_INVALID_LEDGER

    expenses = filter_expenses(expenses, opts["since"], opts["until"])
    balances = compute_balances(expenses)

    if opts["command"] == "balances":
        if opts["format"] == "json":
            out = render_balances_json(balances)
        else:
            out = render_balances_text(balances)
    else:
        transfers = compute_transfers(balances)
        if opts["format"] == "json":
            out = render_settle_json(transfers)
        else:
            out = render_settle_text(transfers)

    sys.stdout.write(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
