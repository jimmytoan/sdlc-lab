#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Money is handled as integer cents throughout.
Exit codes: 0 success, 2 bad rows / missing header columns,
3 unreadable file, 64 wrong usage.
"""
import csv
import datetime
import io
import json
import re
import sys
from collections import namedtuple

EXIT_OK = 0
EXIT_BAD_DATA = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")
OPTIONAL_COLUMNS = ("note", "kind")
VALUE_OPTIONS = ("--format", "--since", "--until")

USAGE = (
    "usage: splitr.py {balances,settle} <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")

Row = namedtuple("Row", "date payer amount shares kind")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- arguments

def _parse_date_arg(value):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    m = DATE_RE.fullmatch(value)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_args(argv):
    """Parse argv. Returns a dict of options. Raises UsageError on misuse.

    Returns the string "help" when -h/--help is requested.
    """
    positionals = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    rest_positional = False
    while i < len(argv):
        a = argv[i]
        i += 1
        if rest_positional:
            positionals.append(a)
            continue
        if a == "--":
            rest_positional = True
            continue
        if a in ("-h", "--help"):
            return "help"
        if a.startswith("--") and "=" in a:
            name, value = a.split("=", 1)
            if name not in VALUE_OPTIONS:
                raise UsageError(f"unknown option: {name}")
        elif a in VALUE_OPTIONS:
            if i >= len(argv):
                raise UsageError(f"option {a} needs a value")
            name, value = a, argv[i]
            i += 1
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option: {a}")
        else:
            positionals.append(a)
            continue
        key = name[2:]
        opts[key] = value

    if len(positionals) != 2:
        raise UsageError("expected a command and a ledger path")
    command, path = positionals
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if opts["format"] not in FORMATS:
        raise UsageError(f"bad --format value: {opts['format']}")
    for key in ("since", "until"):
        if opts[key] is not None:
            d = _parse_date_arg(opts[key])
            if d is None:
                raise UsageError(f"bad --{key} date: {opts[key]}")
            opts[key] = d
    opts["command"] = command
    opts["path"] = path
    return opts


# ------------------------------------------------------------------ parsing

def parse_date(text):
    m = DATE_RE.fullmatch(text)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_amount(text):
    """Positive decimal with at most two fractional digits -> int cents."""
    m = AMOUNT_RE.fullmatch(text)
    if not m:
        return None
    whole = int(m.group(1))
    frac = m.group(2) or ""
    cents = whole * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (name, weight) or None if invalid."""
    if text.strip() == "":
        return None
    result = []
    seen = set()
    for part in text.split(";"):
        name, sep, weight_text = part.partition(":")
        name = name.strip()
        if name == "":
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
    return result


def parse_kind(text):
    """Return "expense", "payment", or None if the kind value is invalid."""
    value = text.strip()
    if value == "":
        return "expense"
    if value in ("expense", "payment"):
        return value
    return None


def parse_payment_recipient(text, payer):
    """A payment's participants must be exactly one unweighted name, not the payer."""
    if ";" in text or ":" in text:
        return None
    name = text.strip()
    if name == "" or name == payer:
        return None
    return name


def parse_row(fields_by_col):
    """Validate one row. Returns (Row or None, list of reason strings)."""
    reasons = []
    date = parse_date(fields_by_col["date"])
    if date is None:
        reasons.append("bad-date")
    kind = parse_kind(fields_by_col.get("kind", ""))
    if kind is None:
        reasons.append("bad-kind")
    payer = fields_by_col["payer"].strip()
    if payer == "":
        reasons.append("bad-payer")
    amount = parse_amount(fields_by_col["amount"])
    if amount is None:
        reasons.append("bad-amount")
    if kind == "payment":
        recipient = parse_payment_recipient(fields_by_col["participants"], payer)
        shares = None if recipient is None else [(recipient, 1)]
    else:
        shares = parse_participants(fields_by_col["participants"])
    if shares is None:
        reasons.append("bad-participants")
    if reasons:
        return None, reasons
    return Row(date, payer, amount, shares, kind), []


def split_cents(amount_cents, weighted_participants, payer=None):
    """Largest-remainder split of amount_cents by weight. Returns list of (name, cents).

    If the payer is a participant and cents are left over, the payer takes the
    first leftover cent. The rest go by largest remainder, ties by name
    ascending (ignoring case), skipping the payer.
    """
    total_weight = sum(w for _, w in weighted_participants)
    names = [name for name, _ in weighted_participants]
    floors = []
    remainders = []
    for _, w in weighted_participants:
        num = amount_cents * w
        floors.append(num // total_weight)
        remainders.append(num % total_weight)
    leftover = amount_cents - sum(floors)
    payer_k = None
    if payer is not None and payer in names:
        payer_k = names.index(payer)
    if payer_k is not None and leftover > 0:
        floors[payer_k] += 1
        leftover -= 1
    order = sorted(
        (k for k in range(len(names)) if k != payer_k),
        key=lambda k: (-remainders[k], names[k].casefold(), names[k]),
    )
    for k in order[:leftover]:
        floors[k] += 1
    return list(zip(names, floors))


def compute_balances(rows):
    balances = {}
    for row in rows:
        if row.kind == "payment":
            (recipient, _), = row.shares
            balances[row.payer] = balances.get(row.payer, 0) + row.amount
            balances[recipient] = balances.get(recipient, 0) - row.amount
            continue
        balances[row.payer] = balances.get(row.payer, 0) + row.amount
        for name, cents in split_cents(row.amount, row.shares, row.payer):
            balances[name] = balances.get(name, 0) - cents
        for name, _ in row.shares:
            balances.setdefault(name, 0)
    return balances


def settle(balances):
    """Greedy settlement. Returns list of (from, to, cents)."""
    debtors = {n: -c for n, c in balances.items() if c < 0}
    creditors = {n: c for n, c in balances.items() if c > 0}
    transfers = []
    while debtors and creditors:
        d = min(debtors, key=lambda n: (-debtors[n], n))
        c = min(creditors, key=lambda n: (-creditors[n], n))
        amt = min(debtors[d], creditors[c])
        transfers.append((d, c, amt))
        debtors[d] -= amt
        creditors[c] -= amt
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return transfers


# ---------------------------------------------------------------- rendering

def fmt_cents(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt_cents(cents)
    if cents < 0:
        return "-" + fmt_cents(-cents)
    return "0.00"


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        obj = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(obj) + "\n"
    return "".join(f"{n} {fmt_signed(balances[n])}\n" for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        obj = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        return json.dumps(obj) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {fmt_cents(c)}\n" for f, t, c in transfers)


# ----------------------------------------------------------------- ledger IO

class _LineFeed:
    """Iterator over physical lines that tracks how far the reader has consumed."""

    def __init__(self, lines):
        self.lines = lines
        self.pos = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.pos >= len(self.lines):
            raise StopIteration
        line = self.lines[self.pos]
        self.pos += 1
        return line


def iter_records(lines):
    """Yield (physical_line_number, fields) for each record, skipping comments and blanks."""
    feed = _LineFeed(lines)
    reader = csv.reader(feed)
    while True:
        while feed.pos < len(lines) and (
            lines[feed.pos].strip() == "" or lines[feed.pos].startswith("#")
        ):
            feed.pos += 1
        if feed.pos >= len(lines):
            return
        start = feed.pos + 1
        try:
            fields = next(reader)
        except StopIteration:
            return
        yield start, fields


# Allow very long fields instead of failing the csv parser (default limit is 128 KiB).
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def read_lines(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        text = f.read()
    return io.StringIO(text, newline="").readlines()


def load_rows(path, since, until, err):
    """Returns (rows, exit_code_or_None). Errors are written to err."""
    try:
        lines = read_lines(path)
        # Parse every record now so csv-level failures are caught here.
        records = list(iter_records(lines))
    except (OSError, UnicodeDecodeError, ValueError, csv.Error):
        err.write(f"error: cannot read {path}\n")
        return None, EXIT_READ

    header = None
    col_index = {}
    errors = []
    rows = []
    for lineno, fields in records:
        if header is None:
            header = [f.strip() for f in fields]
            for i, name in enumerate(header):
                col_index.setdefault(name, i)
            missing = [c for c in REQUIRED_COLUMNS if c not in col_index]
            if missing:
                for c in missing:
                    err.write(f"header: missing {c}\n")
                return None, EXIT_BAD_DATA
            continue

        values = {}
        for col in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
            i = col_index.get(col)
            values[col] = fields[i] if i is not None and i < len(fields) else ""
        row, reasons = parse_row(values)
        for reason in reasons:
            errors.append(f"line {lineno}: {reason}")
        if row is not None:
            rows.append(row)

    if header is None:
        for c in REQUIRED_COLUMNS:
            err.write(f"header: missing {c}\n")
        return None, EXIT_BAD_DATA

    if errors:
        for e in errors:
            err.write(e + "\n")
        return None, EXIT_BAD_DATA

    if since is not None:
        rows = [r for r in rows if r.date >= since]
    if until is not None:
        rows = [r for r in rows if r.date <= until]
    return rows, None


# ---------------------------------------------------------------------- main

def main(argv, out=None, err=None):
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    try:
        opts = parse_args(argv)
    except UsageError as e:
        err.write(f"splitr: {e}\n{USAGE}\n")
        return EXIT_USAGE
    if opts == "help":
        out.write(USAGE + "\n")
        return EXIT_OK

    rows, code = load_rows(opts["path"], opts["since"], opts["until"], err)
    if code is not None:
        return code

    balances = compute_balances(rows)
    if opts["command"] == "balances":
        if not rows and opts["format"] == "text":
            return EXIT_OK
        out.write(render_balances(balances, opts["format"]))
    else:
        transfers = settle(balances)
        out.write(render_settle(transfers, opts["format"]))
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
