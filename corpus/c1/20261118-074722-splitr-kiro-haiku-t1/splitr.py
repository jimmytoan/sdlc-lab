"""splitr: split shared expenses from a CSV ledger and report balances or settlements.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Standard library only. All money is handled as integer cents.
"""

import csv
import datetime
import json
import re
import sys
from collections import namedtuple

EXIT_OK = 0
EXIT_ROW_ERROR = 2
EXIT_FILE_ERROR = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
VALUE_OPTIONS = ("--format", "--since", "--until")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")
MAX_CENTS = 100000000  # 1000000.00

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] "
         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]")

HELP = """\
usage: splitr.py <balances|settle> <ledger.csv> [options]

Commands:
  balances          print each person's balance
  settle            print the payments that clear all debts

Options:
  --format text|json      output format (default: text)
  --since YYYY-MM-DD      only rows on or after this date
  --until YYYY-MM-DD      only rows on or before this date
  -h, --help              show this help

Exit codes: 0 success, 2 ledger content errors, 3 file read error,
64 usage error.
"""

DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WHOLE_RE = re.compile(r"[0-9]+")

Options = namedtuple("Options", "help command path format since until")
Record = namedtuple("Record", "start_line fields")
Row = namedtuple("Row", "line date payer amount_cents participants note")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- output

def _write(stream, text):
    data = text.encode("utf-8", "surrogateescape")
    if hasattr(stream, "buffer"):
        stream.buffer.write(data)
        stream.buffer.flush()
    else:
        stream.write(text)
        stream.flush()


# ---------------------------------------------------------- command line

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD calendar date, else None."""
    m = DATE_RE.fullmatch(text)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_args(argv):
    """Parse argv. Raises UsageError on any usage problem.

    Help is checked first, so -h/--help anywhere prints the usage summary."""
    if any(a in ("-h", "--help") for a in argv):
        return Options(True, None, None, "text", None, None)

    positionals = []
    values = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if arg.startswith("-") and arg != "-":
            if "=" in arg:
                name, _, value = arg.partition("=")
            else:
                name, value = arg, None
            if name not in VALUE_OPTIONS:
                raise UsageError(f"unknown option: {name}")
            if name in values:
                raise UsageError(f"option given more than once: {name}")
            if value is None:
                if i >= len(argv):
                    raise UsageError(f"option requires a value: {name}")
                value = argv[i]
                i += 1
            values[name] = value
        else:
            positionals.append(arg)

    if not positionals:
        raise UsageError("missing command and ledger path")
    command = positionals[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if len(positionals) < 2:
        raise UsageError("missing ledger path")
    if len(positionals) > 2:
        raise UsageError("more than one ledger path given")

    fmt = values.get("--format", "text")
    if fmt not in FORMATS:
        raise UsageError(f"--format must be text or json, not {fmt}")

    since = until = None
    if "--since" in values:
        since = parse_date(values["--since"])
        if since is None:
            raise UsageError(f"--since is not a valid YYYY-MM-DD date: {values['--since']}")
    if "--until" in values:
        until = parse_date(values["--until"])
        if until is None:
            raise UsageError(f"--until is not a valid YYYY-MM-DD date: {values['--until']}")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")

    return Options(False, command, positionals[1], fmt, since, until)


# ------------------------------------------------------------ file input

def read_ledger_text(path):
    """Return the ledger as text with \\n line endings, or None if unreadable."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        return None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _scan_quotes(s, inq, fstart):
    """Track CSV quoting over a piece of text, following the csv module rules.

    A quote opens a quoted field only at the start of a field. Inside a quoted
    field, a doubled quote is a literal quote and a single quote closes it.
    Returns (inside_quotes, at_field_start) after the piece."""
    k = 0
    n = len(s)
    while k < n:
        ch = s[k]
        if inq:
            if ch == '"':
                if k + 1 < n and s[k + 1] == '"':
                    k += 2
                    continue
                inq = False
                fstart = False
        else:
            if ch == ",":
                fstart = True
            elif ch == '"' and fstart:
                inq = True
                fstart = False
            else:
                fstart = False
        k += 1
    return inq, fstart


def split_records(text):
    """Group physical lines into logical CSV records.

    Blank and '#' comment lines are skipped but still counted. Each record
    keeps the physical line number it starts on. Fields are stripped.
    A record with an unterminated quoted field has fields None."""
    physical = text.split("\n")
    n = len(physical)
    records = []
    i = 0
    while i < n:
        line = physical[i]
        start = i + 1
        i += 1
        if line.strip() == "" or line.startswith("#"):
            continue
        buf = line
        inq, _ = _scan_quotes(line, False, True)
        while inq and i < n:
            seg = physical[i]
            i += 1
            buf += "\n" + seg
            inq, _ = _scan_quotes(seg, True, False)
        if inq:
            records.append(Record(start, None))
            continue
        try:
            fields = next(csv.reader([buf]))
        except (csv.Error, StopIteration):
            records.append(Record(start, None))
            continue
        records.append(Record(start, [f.strip() for f in fields]))
    return records


# ---------------------------------------------------------------- header

def validate_header(fields):
    """Return (positions, None) or (None, message)."""
    if fields is None:
        return None, "header: missing date"
    names = [f.strip().lower() for f in fields]
    positions = {}
    for col in REQUIRED_COLUMNS:
        idxs = [i for i, name in enumerate(names) if name == col]
        if not idxs:
            return None, f"header: missing {col}"
        if len(idxs) > 1:
            return None, f"header: duplicate {col}"
        positions[col] = idxs[0]
    return positions, None


# ------------------------------------------------------------ row checks

def parse_amount(text):
    """Return amount in cents, or None if invalid (form, zero, or out of range)."""
    m = AMOUNT_RE.fullmatch(text)
    if not m:
        return None
    whole = m.group(1).lstrip("0") or "0"
    if len(whole) > 9:
        return None
    frac = (m.group(2) or "0").ljust(2, "0")
    cents = int(whole) * 100 + int(frac)
    if cents < 1 or cents > MAX_CENTS:
        return None
    return cents


def parse_participants(text):
    """Return a list of (name, weight), or None if invalid."""
    if text == "":
        return None
    result = []
    seen = set()
    for entry in text.split(";"):
        e = entry.strip()
        if e == "":
            return None
        if e.count(":") > 1:
            return None
        if ":" in e:
            name, _, weight_text = e.partition(":")
            name = name.strip()
            weight_text = weight_text.strip()
            if not WHOLE_RE.fullmatch(weight_text):
                return None
            digits = weight_text.lstrip("0") or "0"
            if len(digits) > 4:
                return None
            weight = int(digits)
            if weight < 1 or weight > 1000:
                return None
        else:
            name = e
            weight = 1
        if name == "":
            return None
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def validate_row(record, positions, ncols):
    """Return (Row, None) or (None, reason). Reports only the first failing check."""
    fields = record.fields
    if fields is None or len(fields) != ncols:
        return None, "bad-row"
    date = parse_date(fields[positions["date"]])
    if date is None:
        return None, "bad-date"
    payer = fields[positions["payer"]]
    if payer == "" or ";" in payer or ":" in payer:
        return None, "bad-payer"
    amount = parse_amount(fields[positions["amount"]])
    if amount is None:
        return None, "bad-amount"
    participants = parse_participants(fields[positions["participants"]])
    if participants is None:
        return None, "bad-participants"
    note = fields[positions["note"]]
    return Row(record.start_line, date, payer, amount, participants, note), None


# ------------------------------------------------------ calculations

def split_row(row):
    """Split one row's amount by weight into integer cents. Shares sum exactly to the amount."""
    total_weight = sum(w for _, w in row.participants)
    shares = {}
    remainders = []
    used = 0
    for name, weight in row.participants:
        raw = row.amount_cents * weight
        share = raw // total_weight
        shares[name] = share
        used += share
        remainders.append((-(raw % total_weight), name))
    leftover = row.amount_cents - used
    for _, name in sorted(remainders)[:leftover]:
        shares[name] += 1
    return shares


def filter_rows(rows, since, until):
    out = []
    for row in rows:
        if since is not None and row.date < since:
            continue
        if until is not None and row.date > until:
            continue
        out.append(row)
    return out


def compute_balances(rows):
    balances = {}
    for row in rows:
        balances[row.payer] = balances.get(row.payer, 0) + row.amount_cents
        for name, share in split_row(row).items():
            balances[name] = balances.get(name, 0) - share
    assert sum(balances.values()) == 0, "balances must sum to zero"
    return balances


def settle(balances):
    """Greedy settlement. Returns a list of (from, to, cents) in generation order."""
    debts = {n: -v for n, v in balances.items() if v < 0}
    credits = {n: v for n, v in balances.items() if v > 0}
    transfers = []
    while debts and credits:
        debtor = min(debts, key=lambda n: (-debts[n], n))
        creditor = min(credits, key=lambda n: (-credits[n], n))
        amount = min(debts[debtor], credits[creditor])
        transfers.append((debtor, creditor, amount))
        debts[debtor] -= amount
        credits[creditor] -= amount
        if debts[debtor] == 0:
            del debts[debtor]
        if credits[creditor] == 0:
            del credits[creditor]
    return transfers


# ---------------------------------------------------------- formatting

def format_cents(cents):
    """Absolute amount as d.cc, no sign."""
    a = abs(cents)
    return f"{a // 100}.{a % 100:02d}"


def format_balance_text(name, cents):
    if cents == 0:
        return f"{name} 0.00"
    if cents > 0:
        return f"{name} +{format_cents(cents)}"
    return f"{name} -{format_cents(cents)}"


def format_output(command, fmt, balances):
    """Return the complete stdout text for a successful run."""
    if command == "balances":
        names = sorted(balances)
        if fmt == "json":
            obj = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            return json.dumps(obj, ensure_ascii=False) + "\n"
        return "".join(format_balance_text(n, balances[n]) + "\n" for n in names)

    transfers = settle(balances)
    if fmt == "json":
        obj = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {format_cents(c)}\n" for f, t, c in transfers)


# ---------------------------------------------------------------- main

def main(argv):
    try:
        opts = parse_args(argv)
    except UsageError as exc:
        _write(sys.stderr, f"splitr: {exc}\n{USAGE}\n")
        return EXIT_USAGE
    if opts.help:
        _write(sys.stdout, HELP)
        return EXIT_OK

    text = read_ledger_text(opts.path)
    if text is None:
        _write(sys.stderr, f"error: cannot read {opts.path}\n")
        return EXIT_FILE_ERROR

    records = split_records(text)
    if not records:
        _write(sys.stderr, "header: missing date\n")
        return EXIT_ROW_ERROR
    positions, problem = validate_header(records[0].fields)
    if problem:
        _write(sys.stderr, problem + "\n")
        return EXIT_ROW_ERROR
    ncols = len(records[0].fields)

    rows = []
    errors = []
    for record in records[1:]:
        row, reason = validate_row(record, positions, ncols)
        if reason:
            errors.append(f"line {record.start_line}: {reason}")
        else:
            rows.append(row)
    if errors:
        _write(sys.stderr, "\n".join(errors) + "\n")
        return EXIT_ROW_ERROR

    included = filter_rows(rows, opts.since, opts.until)
    balances = compute_balances(included)
    _write(sys.stdout, format_output(opts.command, opts.format, balances))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
