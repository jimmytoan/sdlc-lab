#!/usr/bin/env python3
"""splitr: settle shared expenses recorded in a CSV ledger.

Usage: python splitr.py {balances,settle} <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

All money is handled as integer cents. Standard library only.
"""

import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass

EXIT_OK = 0
EXIT_ROW_ERROR = 2      # bad header or bad data rows
EXIT_READ_ERROR = 3     # ledger cannot be read
EXIT_USAGE = 64         # wrong command line

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
OPTIONS = ("--format", "--since", "--until")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

USAGE = ("usage: splitr.py {balances,settle} <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")

HELP = USAGE + """

Settle shared expenses recorded in a CSV ledger.

commands:
  balances              print each person's net balance
  settle                print the transfers that clear all debts

options:
  --format text|json    output format (default: text)
  --since YYYY-MM-DD    include only expenses on or after this date
  --until YYYY-MM-DD    include only expenses on or before this date
  -h, --help            show this help and exit
"""

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


# ---------------------------------------------------------------- output

def _out(text):
    """Write text to stdout as UTF-8 bytes (independent of locale)."""
    sys.stdout.buffer.write(text.encode("utf-8", "surrogateescape"))
    sys.stdout.buffer.flush()


def _err(text):
    """Write text to stderr as UTF-8 bytes."""
    sys.stderr.buffer.write(text.encode("utf-8", "surrogateescape"))
    sys.stderr.buffer.flush()


# ---------------------------------------------------------------- CLI

@dataclass(frozen=True)
class Config:
    command: str
    path: str
    fmt: str
    since: datetime.date | None
    until: datetime.date | None


class _Help(Exception):
    pass


class _Usage(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def parse_date_text(text):
    """Return the date for a strict YYYY-MM-DD ASCII string, or None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    """Parse the command line. Raises _Help or _Usage; returns a Config."""
    if any(tok in ("-h", "--help") for tok in argv):
        raise _Help()

    positionals = []
    opts = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name not in OPTIONS:
                raise _Usage(f"unrecognised option: {name}")
            if name in opts:
                raise _Usage(f"option given more than once: {name}")
            if not eq:
                if i >= len(argv) or argv[i].startswith("-"):
                    raise _Usage(f"option needs a value: {name}")
                value = argv[i]
                i += 1
            opts[name] = value
        elif tok.startswith("-") and tok != "-":
            raise _Usage(f"unrecognised option: {tok}")
        else:
            positionals.append(tok)

    if not positionals:
        raise _Usage("missing command")
    command = positionals[0]
    if command not in COMMANDS:
        raise _Usage(f"unknown command: {command}")
    if len(positionals) < 2:
        raise _Usage("missing ledger path")
    if len(positionals) > 2:
        raise _Usage("too many arguments")
    path = positionals[1]

    fmt = opts.get("--format", "text")
    if fmt not in FORMATS:
        raise _Usage("--format must be text or json")

    dates = {}
    for name in ("--since", "--until"):
        if name in opts:
            d = parse_date_text(opts[name])
            if d is None:
                raise _Usage(f"{name} must be a real date YYYY-MM-DD")
            dates[name] = d
    since = dates.get("--since")
    until = dates.get("--until")
    if since is not None and until is not None and since > until:
        raise _Usage("--since is later than --until")

    return Config(command, path, fmt, since, until)


# ---------------------------------------------------------------- ledger reading

class _ReadError(Exception):
    pass


def read_ledger(path):
    """Read the ledger as UTF-8 text with any BOM removed. Never writes."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise _ReadError()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _ReadError()


# ---------------------------------------------------------------- record splitting

@dataclass(frozen=True)
class Record:
    line: int                    # physical line on which the record starts
    fields: list | None          # None when the csv reader could not parse it


class _LineFeed:
    """Iterator over physical lines that remembers how far it has been read."""

    def __init__(self, lines, pos):
        self.lines = lines
        self.pos = pos

    def __iter__(self):
        return self

    def __next__(self):
        if self.pos >= len(self.lines):
            raise StopIteration
        line = self.lines[self.pos]
        self.pos += 1
        return line


def _physical_lines(text):
    """Split on LF only, keeping line endings. str.splitlines is not used
    because it also breaks on characters such as \\x0b and \\u2028."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _is_skippable(line):
    return line.startswith("#") or line.strip() == ""


def split_records(text):
    """Group physical lines into CSV records, skipping blank and comment lines
    that begin a record. Quoted fields may span several physical lines."""
    csv.field_size_limit(sys.maxsize)
    lines = _physical_lines(text)
    records = []
    i = 0
    while i < len(lines):
        if _is_skippable(lines[i]):
            i += 1
            continue
        start = i + 1
        feed = _LineFeed(lines, i)
        try:
            fields = next(csv.reader(feed), None)
        except csv.Error:
            fields = None
        if fields is None and feed.pos == i:
            # Defensive: always make progress.
            feed.pos = i + 1
        records.append(Record(start, fields))
        i = feed.pos
    return records


# ---------------------------------------------------------------- header

@dataclass(frozen=True)
class Columns:
    index: dict          # required column name -> column index
    note: int | None     # index of the optional note column
    width: int           # number of header columns


def parse_header(fields):
    """Return (Columns, [error messages]). `fields` is None or [] when there
    is no usable header row, which reports all required columns as missing."""
    names = [f.strip().lower() for f in fields] if fields else []
    index = {}
    messages = []
    for col in REQUIRED_COLUMNS:
        found = [k for k, name in enumerate(names) if name == col]
        if not found:
            messages.append(f"header: missing {col}")
        elif len(found) > 1:
            messages.append(f"header: duplicate {col}")
        else:
            index[col] = found[0]
    note = names.index("note") if "note" in names else None
    return Columns(index, note, len(names)), messages


# ---------------------------------------------------------------- row validation

@dataclass(frozen=True)
class Participant:
    name: str
    weight: int


@dataclass(frozen=True)
class Expense:
    line: int
    date: datetime.date
    payer: str
    cents: int
    participants: tuple


@dataclass(frozen=True)
class RowError:
    line: int
    reason: str


def _get(fields, cols, key):
    idx = cols.index[key]
    return fields[idx] if idx < len(fields) else ""


def parse_amount(text):
    """Return integer cents for an amount such as 12.5, or None if invalid."""
    m = AMOUNT_RE.fullmatch(text)
    if m is None:
        return None
    whole = int(m.group(1))
    frac = int((m.group(2) or "").ljust(2, "0"))
    cents = whole * 100 + frac
    if cents == 0:
        return None
    return cents


def parse_participants(text):
    """Return a tuple of Participant, or None if the list is invalid."""
    text = text.strip()
    if not text:
        return None
    result = []
    seen = set()
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            return None
        pieces = segment.split(":")
        if len(pieces) > 2:
            return None
        name = pieces[0].strip()
        if not name:
            return None
        if len(pieces) == 2:
            weight_text = pieces[1].strip()
            if not WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight < 1:
                return None
        else:
            weight = 1
        if name in seen:
            return None
        seen.add(name)
        result.append(Participant(name, weight))
    return tuple(result)


def validate_row(record, cols):
    """Return an Expense or a RowError. Only the first failing field is
    reported, checked in the order date, payer, amount, participants."""
    line = record.line
    if record.fields is None:
        return RowError(line, "bad-participants")
    fields = record.fields

    date = parse_date_text(_get(fields, cols, "date").strip())
    if date is None:
        return RowError(line, "bad-date")

    payer = _get(fields, cols, "payer").strip()
    if not payer or ";" in payer or ":" in payer:
        return RowError(line, "bad-payer")

    cents = parse_amount(_get(fields, cols, "amount").strip())
    if cents is None:
        return RowError(line, "bad-amount")

    if len(fields) > cols.width:
        return RowError(line, "bad-participants")
    participants = parse_participants(_get(fields, cols, "participants"))
    if participants is None:
        return RowError(line, "bad-participants")

    return Expense(line, date, payer, cents, participants)


def in_range(expense, since, until):
    if since is not None and expense.date < since:
        return False
    if until is not None and expense.date > until:
        return False
    return True


# ---------------------------------------------------------------- money

def split_cents(cents, participants):
    """Split `cents` by weight using integer arithmetic only. Returns a list
    of (name, share) in the order of `participants`; shares sum to `cents`."""
    total = sum(p.weight for p in participants)
    bases = []
    remainders = []
    for p in participants:
        numerator = cents * p.weight
        base = numerator // total
        bases.append(base)
        remainders.append(numerator - base * total)
    leftover = cents - sum(bases)
    order = sorted(range(len(participants)),
                   key=lambda k: (-remainders[k], participants[k].name))
    shares = list(bases)
    for k in order[:leftover]:
        shares[k] += 1
    return [(participants[k].name, shares[k]) for k in range(len(participants))]


def compute_balances(expenses):
    """Map each person who appears as payer or participant to net cents."""
    balances = {}
    for e in expenses:
        balances[e.payer] = balances.get(e.payer, 0) + e.cents
        for name, share in split_cents(e.cents, e.participants):
            balances[name] = balances.get(name, 0) - share
    return balances


def format_unsigned(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def format_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    return sign + format_unsigned(abs(cents))


def settle(balances):
    """Greedy settlement. Returns a list of (from, to, cents) in produced order."""
    owes = {n: -b for n, b in balances.items() if b < 0}
    owed = {n: b for n, b in balances.items() if b > 0}
    transfers = []
    while owes and owed:
        debtor = min(owes, key=lambda n: (-owes[n], n))
        creditor = min(owed, key=lambda n: (-owed[n], n))
        amount = min(owes[debtor], owed[creditor])
        transfers.append((debtor, creditor, amount))
        owes[debtor] -= amount
        owed[creditor] -= amount
        if owes[debtor] == 0:
            del owes[debtor]
        if owed[creditor] == 0:
            del owed[creditor]
    return transfers


# ---------------------------------------------------------------- reports

def balances_report(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        obj = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    return "".join(f"{n} {format_signed(balances[n])}\n" for n in names)


def settle_report(transfers, fmt):
    if fmt == "json":
        obj = {"transfers": [{"from": f, "to": t, "cents": c}
                             for f, t, c in transfers]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {format_unsigned(c)}\n" for f, t, c in transfers)


# ---------------------------------------------------------------- main

def main(argv):
    # Allow arbitrarily large amounts and weights in decimal text.
    sys.set_int_max_str_digits(0)

    try:
        cfg = parse_args(argv)
    except _Help:
        _out(HELP)
        return EXIT_OK
    except _Usage as e:
        _err(f"splitr: {e.message}\n{USAGE}\n")
        return EXIT_USAGE

    try:
        text = read_ledger(cfg.path)
    except _ReadError:
        _err(f"error: cannot read {cfg.path}\n")
        return EXIT_READ_ERROR

    records = split_records(text)
    header_fields = records[0].fields if records else None
    cols, header_errors = parse_header(header_fields)
    if header_errors:
        _err("".join(f"{m}\n" for m in header_errors))
        return EXIT_ROW_ERROR

    expenses = []
    row_errors = []
    for record in records[1:]:
        result = validate_row(record, cols)
        if isinstance(result, RowError):
            row_errors.append(result)
        else:
            expenses.append(result)
    if row_errors:
        _err("".join(f"line {e.line}: {e.reason}\n" for e in row_errors))
        return EXIT_ROW_ERROR

    expenses = [e for e in expenses if in_range(e, cfg.since, cfg.until)]
    balances = compute_balances(expenses)

    if cfg.command == "balances":
        _out(balances_report(balances, cfg.fmt))
    else:
        _out(settle_report(settle(balances), cfg.fmt))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
