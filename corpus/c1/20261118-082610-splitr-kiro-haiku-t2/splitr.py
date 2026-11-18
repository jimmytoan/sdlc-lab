#!/usr/bin/env python3
"""splitr: settle shared expenses from a CSV ledger.

Usage: python splitr.py {balances,settle} <ledger.csv> [--format {text,json}]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Standard library only. Money is handled as integer cents throughout.
"""
import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------- constants

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

REASON_BAD_ROW = "bad-row"
REASON_BAD_DATE = "bad-date"
REASON_BAD_PAYER = "bad-payer"
REASON_BAD_AMOUNT = "bad-amount"
REASON_BAD_PARTICIPANTS = "bad-participants"

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")

USAGE = (
    "usage: splitr.py {balances,settle} <ledger.csv> "
    "[--format {text,json}] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)
HELP = USAGE + "\n\n" + (
    "Commands:\n"
    "  balances    print each person's net balance\n"
    "  settle      print the payments that clear all debts\n\n"
    "Options:\n"
    "  --format {text,json}   output format (default: text)\n"
    "  --since YYYY-MM-DD     include only rows on or after this date\n"
    "  --until YYYY-MM-DD     include only rows on or before this date\n"
    "  -h, --help             show this message and exit\n"
)

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    """Wrong command line (exit 64)."""


class RowError(Exception):
    """A row failed a check; the message is the reason."""


@dataclass
class Options:
    command: str = ""
    path: str = ""
    fmt: str = "text"
    since: datetime.date | None = None
    until: datetime.date | None = None
    help: bool = False


@dataclass
class Record:
    start: int          # physical line number where the record starts
    text: str           # physical lines joined with "\n"
    malformed: bool     # unclosed quote at end of input


@dataclass
class Row:
    record_line: int
    date: datetime.date
    payer: str
    amount: int                                  # cents, > 0
    participants: list = field(default_factory=list)  # [(name, weight)]


# ---------------------------------------------------------------- stage 1: CLI

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, or None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    positionals = []
    fmt = None
    since_values = []
    until_values = []
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
            return Options(help=True)
        name, eq, value = arg.partition("=")
        if name not in ("--format", "--since", "--until"):
            raise UsageError(f"unknown option: {arg}")
        if not eq:
            if i >= len(argv):
                raise UsageError(f"option {name} requires a value")
            value = argv[i]
            i += 1
        if name == "--format":
            if value not in FORMATS:
                raise UsageError(f"invalid --format value: {value!r}")
            fmt = value
        elif name == "--since":
            since_values.append(value)
        else:
            until_values.append(value)

    if len(since_values) > 1:
        raise UsageError("--since given more than once")
    if len(until_values) > 1:
        raise UsageError("--until given more than once")

    if len(positionals) < 1:
        raise UsageError("missing command")
    if positionals[0] not in COMMANDS:
        raise UsageError(f"unknown command: {positionals[0]}")
    if len(positionals) < 2:
        raise UsageError("missing ledger path")
    if len(positionals) > 2:
        raise UsageError(f"unexpected argument: {positionals[2]}")

    since = None
    until = None
    if since_values:
        since = parse_date(since_values[0])
        if since is None:
            raise UsageError(f"invalid --since date: {since_values[0]!r}")
    if until_values:
        until = parse_date(until_values[0])
        if until is None:
            raise UsageError(f"invalid --until date: {until_values[0]!r}")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")

    return Options(command=positionals[0], path=positionals[1],
                   fmt=fmt or "text", since=since, until=until)


# ---------------------------------------------------------------- stage 2: read

def read_ledger_text(path):
    """Return (text, None) or (None, error). Universal newlines, BOM removed."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (OSError, ValueError):
        return None, f"error: cannot read {path}"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, f"error: cannot read {path}"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, None


# ---------------------------------------------------------------- stage 3: records

def _scan_quotes(line, in_quotes, at_field_start):
    """Advance the CSV quote state across one physical line."""
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_quotes:
            if c == '"':
                if i + 1 < n and line[i + 1] == '"':
                    i += 2
                    continue
                in_quotes = False
                at_field_start = False
        else:
            if c == '"' and at_field_start:
                in_quotes = True
                at_field_start = False
            elif c == ",":
                at_field_start = True
            else:
                at_field_start = False
        i += 1
    return in_quotes, at_field_start


def split_records(text):
    """Group physical lines into logical records, keeping the start line."""
    if text == "":
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    records = []
    start = None
    buf = []
    in_quotes = False
    at_field_start = True
    for number, line in enumerate(lines, start=1):
        if start is None:
            if line.strip() == "" or line.startswith("#"):
                continue
            start = number
            buf = []
            in_quotes = False
            at_field_start = True
        buf.append(line)
        in_quotes, at_field_start = _scan_quotes(line, in_quotes, at_field_start)
        if not in_quotes:
            records.append(Record(start, "\n".join(buf), False))
            start = None
    if start is not None:
        records.append(Record(start, "\n".join(buf), True))
    return records


def parse_fields(text):
    return next(csv.reader([text]))


# ---------------------------------------------------------------- stage 4: header

def parse_header(records):
    """Return (index, column_count, None) or (None, None, error message)."""
    if not records:
        return None, None, "header: missing date"
    fields = parse_fields(records[0].text)
    index = {}
    for i, name in enumerate(fields):
        index.setdefault(name.strip().lower(), i)
    for col in REQUIRED_COLUMNS:
        if col not in index:
            return None, None, f"header: missing {col}"
    return index, len(fields), None


# ---------------------------------------------------------------- stage 5: rows

def check_date(raw):
    d = parse_date(raw.strip())
    if d is None:
        raise RowError(REASON_BAD_DATE)
    return d


def check_payer(raw):
    name = raw.strip()
    if name == "" or ";" in name or ":" in name:
        raise RowError(REASON_BAD_PAYER)
    return name


def check_amount(raw):
    m = AMOUNT_RE.fullmatch(raw.strip())
    if m is None:
        raise RowError(REASON_BAD_AMOUNT)
    whole = m.group(1)
    frac = (m.group(2) or "").ljust(2, "0")
    try:
        cents = int(whole) * 100 + int(frac)
    except ValueError:  # absurdly long digit strings
        raise RowError(REASON_BAD_AMOUNT)
    if cents == 0:
        raise RowError(REASON_BAD_AMOUNT)
    return cents


def check_participants(raw):
    text = raw.strip()
    if text == "":
        raise RowError(REASON_BAD_PARTICIPANTS)
    result = []
    seen = set()
    for entry in text.split(";"):
        if entry.strip() == "":
            raise RowError(REASON_BAD_PARTICIPANTS)
        if entry.count(":") > 1:
            raise RowError(REASON_BAD_PARTICIPANTS)
        if ":" in entry:
            name_raw, weight_raw = entry.split(":", 1)
            name = name_raw.strip()
            weight_text = weight_raw.strip()
            if not WEIGHT_RE.fullmatch(weight_text):
                raise RowError(REASON_BAD_PARTICIPANTS)
            try:
                weight = int(weight_text)
            except ValueError:
                raise RowError(REASON_BAD_PARTICIPANTS)
            if weight <= 0:
                raise RowError(REASON_BAD_PARTICIPANTS)
        else:
            name = entry.strip()
            weight = 1
        if name == "":
            raise RowError(REASON_BAD_PARTICIPANTS)
        if name in seen:
            raise RowError(REASON_BAD_PARTICIPANTS)
        seen.add(name)
        result.append((name, weight))
    return result


def check_row(record, index, column_count):
    """Return a Row, or the reason string for the first failed check."""
    if record.malformed:
        return REASON_BAD_ROW
    try:
        fields = parse_fields(record.text)
    except csv.Error:
        return REASON_BAD_ROW
    if len(fields) != column_count:
        return REASON_BAD_ROW
    try:
        d = check_date(fields[index["date"]])
        payer = check_payer(fields[index["payer"]])
        amount = check_amount(fields[index["amount"]])
        participants = check_participants(fields[index["participants"]])
    except RowError as e:
        return str(e)
    return Row(record.start, d, payer, amount, participants)


def validate_records(records, index, column_count):
    """Validate every data record. Return (rows, errors)."""
    rows = []
    errors = []
    for record in records[1:]:
        result = check_row(record, index, column_count)
        if isinstance(result, Row):
            rows.append(result)
        else:
            errors.append((record.start, result))
    errors.sort(key=lambda e: e[0])
    return rows, errors


# ---------------------------------------------------------------- stage 6: range

def in_range(row, since, until):
    if since is not None and row.date < since:
        return False
    if until is not None and row.date > until:
        return False
    return True


# ---------------------------------------------------------------- stage 7: arithmetic

def split_cents(amount, participants):
    """Split amount (cents) by weight. participants: [(name, weight)].

    Returns shares in participant order; they always sum to amount.
    """
    total_w = sum(w for _, w in participants)
    bases = []
    remainders = []
    for _, w in participants:
        num = amount * w
        bases.append(num // total_w)
        remainders.append(num % total_w)
    leftover = amount - sum(bases)
    order = sorted(range(len(participants)), key=lambda i: (-remainders[i], i))
    shares = list(bases)
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def apply_row(balances, row):
    balances[row.payer] = balances.get(row.payer, 0) + row.amount
    shares = split_cents(row.amount, row.participants)
    for (name, _), share in zip(row.participants, shares):
        balances[name] = balances.get(name, 0) - share


def compute_balances(rows):
    balances = {}
    for row in rows:
        apply_row(balances, row)
    return balances


# ---------------------------------------------------------------- stage 8: commands

def settle(balances):
    """Return [(from, to, cents)] clearing all balances."""
    debts = {n: -c for n, c in balances.items() if c < 0}
    credits = {n: c for n, c in balances.items() if c > 0}
    transfers = []
    while debts and credits:
        d = min(debts, key=lambda n: (-debts[n], n))
        c = min(credits, key=lambda n: (-credits[n], n))
        amount = min(debts[d], credits[c])
        transfers.append((d, c, amount))
        debts[d] -= amount
        credits[c] -= amount
        if debts[d] == 0:
            del debts[d]
        if credits[c] == 0:
            del credits[c]
    return transfers


# ---------------------------------------------------------------- stage 9: output

def fmt_cents(cents, signed):
    a = abs(cents)
    body = f"{a // 100}.{a % 100:02d}"
    if not signed or cents == 0:
        return body
    return ("+" if cents > 0 else "-") + body


def format_balances_text(balances):
    return "".join(f"{name} {fmt_cents(balances[name], True)}\n"
                   for name in sorted(balances))


def format_settle_text(transfers):
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {fmt_cents(c, False)}\n" for f, t, c in transfers)


def format_json(command, balances, transfers):
    if command == "balances":
        obj = {"balances": [{"name": n, "cents": balances[n]} for n in sorted(balances)]}
    else:
        obj = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
    return json.dumps(obj) + "\n"


# ---------------------------------------------------------------- main

def _err(message):
    sys.stderr.write(message + "\n")


def main(argv):
    try:
        opts = parse_args(argv)
    except UsageError as e:
        _err(USAGE)
        _err(f"splitr: {e}")
        return EXIT_USAGE
    if opts.help:
        sys.stdout.write(HELP)
        return EXIT_OK

    text, error = read_ledger_text(opts.path)
    if error is not None:
        _err(error)
        return EXIT_UNREADABLE

    records = split_records(text)
    index, column_count, header_error = parse_header(records)
    if header_error is not None:
        _err(header_error)
        return EXIT_BAD_ROWS

    rows, errors = validate_records(records, index, column_count)
    if errors:
        for line, reason in errors:
            _err(f"line {line}: {reason}")
        return EXIT_BAD_ROWS

    rows = [r for r in rows if in_range(r, opts.since, opts.until)]
    balances = compute_balances(rows)
    transfers = settle(balances) if opts.command == "settle" else []

    if opts.fmt == "json":
        out = format_json(opts.command, balances, transfers)
    elif opts.command == "balances":
        out = format_balances_text(balances)
    else:
        out = format_settle_text(transfers)
    sys.stdout.write(out)
    return EXIT_OK


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
