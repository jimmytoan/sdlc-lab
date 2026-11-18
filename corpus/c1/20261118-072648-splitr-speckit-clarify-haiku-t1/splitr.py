"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Standard library only. Reads the whole ledger, validates every row, splits each
expense among its participants in whole cents (largest-remainder rounding), and
prints either per-person balances or a greedy settlement.

Exit codes: 0 success, 2 ledger data error, 3 ledger unreadable, 64 usage error.
"""

import csv
import datetime
import io
import json
import re
import sys
import types
from collections import namedtuple

REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

EXIT_OK = 0
EXIT_DATA = 2
EXIT_READ = 3
EXIT_USAGE = 64

AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
WEIGHT_RE = re.compile(r"[0-9]+")

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")

USAGE = (
    "usage: splitr.py {balances,settle} <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

Expense = namedtuple("Expense", "date payer amount participants note")
RowError = namedtuple("RowError", "line reason")

HELP = object()


class UsageError(Exception):
    """Raised for any command-line usage error (exit 64)."""


def write_out(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


def write_err(text):
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.flush()


# ---------------------------------------------------------------- parsing

def parse_date(text):
    """Return datetime.date for a strict YYYY-MM-DD real calendar date, else None."""
    if text is None or not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return integer cents for a positive amount with at most two decimals, else None."""
    if text is None or not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int((frac or "0").ljust(2, "0"))
    return cents if cents > 0 else None


def parse_participants(text):
    """Return [(name, weight), ...] or None if the field is invalid."""
    if text is None or text.strip() == "":
        return None
    result = []
    seen = set()
    for token in text.split(";"):
        token = token.strip()
        if not token:
            return None
        name, sep, wtext = token.rpartition(":")
        if sep:
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
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


def parse_args(argv):
    """Parse the command line. Returns a namespace, or HELP for --help.

    Every usage problem raises UsageError (exit 64)."""
    opts = {"format": None, "since": None, "until": None}
    positionals = []
    only_positionals = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if only_positionals or arg == "-" or not arg.startswith("-"):
            positionals.append(arg)
            continue
        if arg == "--":
            only_positionals = True
            continue
        if arg in ("-h", "--help"):
            return HELP
        name, eq, value = arg.partition("=")
        if name not in ("--format", "--since", "--until"):
            raise UsageError(f"unknown option: {arg}")
        if not eq:
            if i >= len(argv):
                raise UsageError(f"option {name} requires a value")
            value = argv[i]
            i += 1
        opts[name[2:]] = value

    if not positionals:
        raise UsageError("missing command (balances or settle)")
    command = positionals[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if len(positionals) != 2:
        raise UsageError("expected exactly one ledger path")

    fmt = "text" if opts["format"] is None else opts["format"]
    if fmt not in FORMATS:
        raise UsageError(f"invalid --format value: {fmt} (use text or json)")

    dates = {}
    for key in ("since", "until"):
        raw = opts[key]
        if raw is None:
            dates[key] = None
            continue
        parsed = parse_date(raw)
        if parsed is None:
            raise UsageError(f"invalid --{key} date: {raw} (use YYYY-MM-DD)")
        dates[key] = parsed

    return types.SimpleNamespace(
        command=command,
        ledger=positionals[1],
        format=fmt,
        since=dates["since"],
        until=dates["until"],
    )


# ---------------------------------------------------------------- ledger

def read_text(path):
    """Return the decoded ledger text, or None if the file cannot be read."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def iter_records(text):
    """Yield (start_line, record) for each CSV record.

    Blank and '#' comment lines are skipped where a record would start.
    start_line is the 1-based physical line where the record begins. A record
    that the csv module cannot parse is yielded as (start_line, None)."""
    lines = list(io.StringIO(text, newline=""))
    pos = [0]

    def feeder():
        while pos[0] < len(lines):
            line = lines[pos[0]]
            pos[0] += 1
            yield line

    while True:
        while pos[0] < len(lines):
            s = lines[pos[0]]
            if s.strip() == "" or s.startswith("#"):
                pos[0] += 1
            else:
                break
        if pos[0] >= len(lines):
            return
        start = pos[0] + 1
        reader = csv.reader(feeder())
        try:
            record = next(reader)
        except StopIteration:
            record = []
        except csv.Error:
            record = None
        yield start, record


def parse_row(record, column_index, line_no):
    """Return an Expense or a RowError (first failing field wins)."""

    def field(name):
        i = column_index[name]
        return record[i] if i < len(record) else ""

    date = parse_date(field("date").strip())
    if date is None:
        return RowError(line_no, "bad-date")
    payer = field("payer").strip()
    if not payer:
        return RowError(line_no, "bad-payer")
    amount = parse_amount(field("amount").strip())
    if amount is None:
        return RowError(line_no, "bad-amount")
    participants = parse_participants(field("participants"))
    if participants is None:
        return RowError(line_no, "bad-participants")
    return Expense(date, payer, amount, participants, field("note"))


def load_ledger(path):
    """Read and validate the ledger.

    Returns (exit_code, expenses, stderr_lines). On any error, expenses is empty."""
    text = read_text(path)
    if text is None:
        return EXIT_READ, [], [f"error: cannot read {path}"]

    records = iter_records(text)
    header = next(records, None)
    if header is None or header[1] is None:
        return EXIT_DATA, [], [f"header: missing {REQUIRED_COLUMNS[0]}"]

    column_index = {}
    for i, name in enumerate(header[1]):
        column_index.setdefault(name.strip(), i)
    for col in REQUIRED_COLUMNS:
        if col not in column_index:
            return EXIT_DATA, [], [f"header: missing {col}"]

    expenses = []
    errors = []
    for line_no, record in records:
        if record is None:
            errors.append(f"line {line_no}: bad-date")
            continue
        result = parse_row(record, column_index, line_no)
        if isinstance(result, RowError):
            errors.append(f"line {result.line}: {result.reason}")
        else:
            expenses.append(result)

    if errors:
        return EXIT_DATA, [], errors
    return EXIT_OK, expenses, []


# ---------------------------------------------------------------- computation

def split_shares(amount_cents, participants):
    """Split amount among (name, weight) pairs by largest remainder.

    Shares sum exactly to amount_cents. Leftover cents go to the largest
    remainders; ties go to the participant listed first."""
    total_weight = sum(w for _, w in participants)
    bases = []
    remainders = []
    for _, w in participants:
        product = amount_cents * w
        bases.append(product // total_weight)
        remainders.append(product % total_weight)
    leftover = amount_cents - sum(bases)
    order = sorted(range(len(participants)), key=lambda i: (-remainders[i], i))
    shares = list(bases)
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def compute_balances(expenses):
    """Return {name: cents} where cents = paid - owed over the given expenses."""
    balances = {}
    for e in expenses:
        balances[e.payer] = balances.get(e.payer, 0) + e.amount
        shares = split_shares(e.amount, e.participants)
        for (name, _), share in zip(e.participants, shares):
            balances[name] = balances.get(name, 0) - share
    return balances


def compute_transfers(balances):
    """Greedy settlement: pair the largest debtor with the largest creditor."""
    debtors = [[name, -c] for name, c in balances.items() if c < 0]
    creditors = [[name, c] for name, c in balances.items() if c > 0]
    transfers = []
    while debtors and creditors:
        debtors.sort(key=lambda x: (-x[1], x[0]))
        creditors.sort(key=lambda x: (-x[1], x[0]))
        d = debtors[0]
        c = creditors[0]
        amount = min(d[1], c[1])
        transfers.append((d[0], c[0], amount))
        d[1] -= amount
        c[1] -= amount
        if d[1] == 0:
            debtors.pop(0)
        if c[1] == 0:
            creditors.pop(0)
    return transfers


# ---------------------------------------------------------------- rendering

def fmt_cents(cents):
    """Format a non-negative cent count as d.dd."""
    return f"{cents // 100}.{cents % 100:02d}"


def render_balances_text(balances):
    out = []
    for name in sorted(balances):
        c = balances[name]
        if c > 0:
            amount = "+" + fmt_cents(c)
        elif c < 0:
            amount = "-" + fmt_cents(-c)
        else:
            amount = "0.00"
        out.append(f"{name} {amount}\n")
    return "".join(out)


def render_balances_json(balances):
    payload = {"balances": [{"name": n, "cents": balances[n]} for n in sorted(balances)]}
    return json.dumps(payload) + "\n"


def render_settle_text(transfers):
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{d} -> {c} {fmt_cents(a)}\n" for d, c, a in transfers)


def render_settle_json(transfers):
    payload = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
    return json.dumps(payload) + "\n"


# ---------------------------------------------------------------- main

def in_range(expense, since, until):
    if since is not None and expense.date < since:
        return False
    if until is not None and expense.date > until:
        return False
    return True


def main(argv):
    try:
        args = parse_args(argv)
    except UsageError as exc:
        write_err(f"splitr: {exc}\n{USAGE}\n")
        return EXIT_USAGE
    if args is HELP:
        write_out(USAGE + "\n")
        return EXIT_OK

    code, expenses, messages = load_ledger(args.ledger)
    if code != EXIT_OK:
        write_err("".join(m + "\n" for m in messages))
        return code

    counted = [e for e in expenses if in_range(e, args.since, args.until)]
    balances = compute_balances(counted)

    if args.command == "balances":
        if args.format == "json":
            out = render_balances_json(balances)
        else:
            out = render_balances_text(balances)
    else:
        transfers = compute_transfers(balances)
        if args.format == "json":
            out = render_settle_json(transfers)
        else:
            out = render_settle_text(transfers)

    write_out(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
