"""splitr: report who owes whom from a CSV ledger of shared expenses.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Standard library only. All money is integer cents.
"""
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] "
         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]")
HELP = (USAGE + "\n\n"
        "Commands:\n"
        "  balances   print each person's net balance\n"
        "  settle     print the payments that clear all balances\n\n"
        "Options:\n"
        "  --format text|json     output format (default text)\n"
        "  --since YYYY-MM-DD     only rows on or after this date\n"
        "  --until YYYY-MM-DD     only rows on or before this date\n"
        "  -h, --help             show this help and exit\n")

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_ROW_ERROR = 2
EXIT_READ_ERROR = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")
KNOWN_COLUMNS = set(REQUIRED_COLUMNS) | {"note"}
VALUE_OPTIONS = ("--format", "--since", "--until")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class HelpRequested(Exception):
    pass


class InvariantError(Exception):
    pass


@dataclass(frozen=True)
class Options:
    command: str
    path: str
    fmt: str
    since: object
    until: object


@dataclass(frozen=True)
class Row:
    line: int
    date: date
    payer: str
    amount: int
    participants: tuple


@dataclass(frozen=True)
class RowError:
    line: int
    reason: str


@dataclass(frozen=True)
class Transfer:
    source: str
    target: str
    cents: int


# ---------------------------------------------------------------- CLI

def parse_iso_date(text):
    """Return a date for a strict YYYY-MM-DD string (ASCII digits), else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    positionals = []
    values = {}
    seen = set()
    help_count = 0
    scan_error = None
    end_of_options = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if end_of_options or tok == "-" or not tok.startswith("-"):
            positionals.append(tok)
            continue
        if tok == "--":
            end_of_options = True
            continue
        if tok.startswith("--") and "=" in tok:
            name, value = tok.split("=", 1)
            if name in VALUE_OPTIONS:
                if name in seen:
                    scan_error = scan_error or f"option {name} given more than once"
                seen.add(name)
                values[name] = value
            elif name == "--help" or name == "-h":
                scan_error = scan_error or f"option {name} does not take a value"
            else:
                scan_error = scan_error or f"unknown option '{name}'"
            continue
        if tok in ("-h", "--help"):
            help_count += 1
            continue
        if tok in VALUE_OPTIONS:
            if tok in seen:
                scan_error = scan_error or f"option {tok} given more than once"
            seen.add(tok)
            if i < len(argv) and not argv[i].startswith("-"):
                values[tok] = argv[i]
                i += 1
            else:
                scan_error = scan_error or f"option {tok} requires a value"
            continue
        scan_error = scan_error or f"unknown option '{tok}'"

    # Help wins over other option problems, but a repeated help flag is an error.
    if help_count == 1:
        raise HelpRequested()
    if help_count > 1:
        raise UsageError("option -h given more than once")
    if scan_error is not None:
        raise UsageError(scan_error)

    if not positionals:
        raise UsageError("missing command (expected balances or settle)")
    command = positionals[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command '{command}'")
    if len(positionals) < 2:
        raise UsageError("missing ledger path")
    if len(positionals) > 2:
        raise UsageError("too many positional arguments")
    path = positionals[1]

    fmt = values.get("--format", "text")
    if fmt not in FORMATS:
        raise UsageError("--format must be text or json")

    since = None
    until = None
    if "--since" in values:
        since = parse_iso_date(values["--since"])
        if since is None:
            raise UsageError("--since must be a valid YYYY-MM-DD date")
    if "--until" in values:
        until = parse_iso_date(values["--until"])
        if until is None:
            raise UsageError("--until must be a valid YYYY-MM-DD date")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")

    return Options(command, path, fmt, since, until)


# ---------------------------------------------------------------- reading

def read_ledger_text(path):
    """Return the decoded ledger text, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, ValueError):
        return None


def _scan_quotes(line, in_quote, field_start):
    """Track CSV quoting across one physical line; return (in_quote, field_start)."""
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if in_quote:
            if c == '"':
                if i + 1 < n and line[i + 1] == '"':
                    i += 2
                    continue
                in_quote = False
                field_start = False
        else:
            if c == ",":
                field_start = True
            elif c == '"' and field_start:
                in_quote = True
                field_start = False
            else:
                field_start = False
        i += 1
    return in_quote, field_start


def _parse_csv_record(text):
    """Parse one logical record. Returns a list of fields, or None if malformed."""
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        return None
    if len(rows) != 1:
        return None
    return rows[0]


def split_records(text):
    """Return [(start_line, fields_or_None)] for the header and each data row."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]

    records = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "" or line.startswith("#"):
            i += 1
            continue
        start = i + 1
        buf = [line]
        in_quote, field_start = _scan_quotes(line, False, True)
        i += 1
        while in_quote and i < n:
            buf.append(lines[i])
            in_quote, field_start = _scan_quotes(lines[i], in_quote, field_start)
            i += 1
        records.append((start, _parse_csv_record("\n".join(buf))))
    return records


# ---------------------------------------------------------------- header

def resolve_header(header_fields):
    """Return (colmap, missing_columns, column_count) for the header record."""
    if not header_fields:
        return {}, list(REQUIRED_COLUMNS), 0
    colmap = {}
    for idx, name in enumerate(header_fields):
        key = name.strip().lower()
        if key in KNOWN_COLUMNS and key not in colmap:
            colmap[key] = idx
    missing = [c for c in REQUIRED_COLUMNS if c not in colmap]
    return colmap, missing, len(header_fields)


# ---------------------------------------------------------------- rows

def parse_amount(text):
    """Return cents for a positive amount string, or None."""
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    return cents if cents > 0 else None


def parse_participants(raw):
    """Return a tuple of (name, weight), or None if invalid."""
    if raw == "" or "\r" in raw or "\n" in raw:
        return None
    result = []
    seen = set()
    for segment in raw.split(";"):
        seg = segment.strip()
        if seg == "":
            return None
        parts = seg.split(":")
        if len(parts) > 2:
            return None
        name = parts[0].strip()
        if name == "":
            return None
        if len(parts) == 2:
            weight_text = parts[1].strip()
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
    return tuple(result)


def parse_row(fields, line, colmap, column_count):
    if fields is None:
        return RowError(line, "bad-participants")

    def get(name):
        idx = colmap.get(name)
        if idx is None or idx >= len(fields):
            return ""
        return fields[idx].strip()

    day_text = get("date")
    day = parse_iso_date(day_text)
    if day is None:
        return RowError(line, "bad-date")

    payer = get("payer")
    if payer == "" or any(ch in payer for ch in ";:\r\n"):
        return RowError(line, "bad-payer")

    amount = parse_amount(get("amount"))
    if amount is None:
        return RowError(line, "bad-amount")

    participants = parse_participants(get("participants"))
    if participants is None:
        return RowError(line, "bad-participants")

    if len(fields) > column_count:
        return RowError(line, "bad-participants")

    return Row(line, day, payer, amount, participants)


def filter_rows(rows, since, until):
    return [r for r in rows
            if (since is None or r.date >= since)
            and (until is None or r.date <= until)]


# ---------------------------------------------------------------- money

def split_row(amount, participants):
    """Split amount among (name, weight) pairs; return {name: cents}."""
    total_weight = sum(w for _, w in participants)
    floors = [amount * w // total_weight for _, w in participants]
    rems = [amount * w % total_weight for _, w in participants]
    leftover = amount - sum(floors)
    if not (0 <= leftover < len(participants)):
        raise InvariantError("leftover out of range")
    order = sorted(range(len(participants)), key=lambda i: (-rems[i], i))
    for i in order[:leftover]:
        floors[i] += 1
    if sum(floors) != amount:
        raise InvariantError("shares do not sum to amount")
    return {name: floors[i] for i, (name, _) in enumerate(participants)}


def aggregate(rows):
    balance = {}
    for r in rows:
        balance[r.payer] = balance.get(r.payer, 0) + r.amount
        shares = split_row(r.amount, r.participants)
        for name, share in shares.items():
            balance.setdefault(name, 0)
            balance[name] -= share
    if sum(balance.values()) != 0:
        raise InvariantError("balances do not sum to zero")
    return balance


def balances_result(balance):
    return sorted(balance.items(), key=lambda kv: kv[0])


def settle(balance):
    work = dict(balance)
    transfers = []
    while True:
        debtors = [n for n, v in work.items() if v < 0]
        creditors = [n for n, v in work.items() if v > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda n: (work[n], n))
        c = min(creditors, key=lambda n: (-work[n], n))
        amount = min(-work[d], work[c])
        transfers.append(Transfer(d, c, amount))
        work[d] += amount
        work[c] -= amount
    if any(v != 0 for v in work.values()):
        raise InvariantError("settle left a non-zero balance")
    return transfers


# ---------------------------------------------------------------- output

def cents_to_text(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def signed_text(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    return sign + cents_to_text(abs(cents))


def format_balances_text(items):
    return "".join(f"{name} {signed_text(cents)}\n" for name, cents in items)


def format_balances_json(items):
    obj = {"balances": [{"name": name, "cents": cents} for name, cents in items]}
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": ")) + "\n"


def format_settle_text(transfers):
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{t.source} -> {t.target} {cents_to_text(t.cents)}\n"
                   for t in transfers)


def format_settle_json(transfers):
    obj = {"transfers": [{"from": t.source, "to": t.target, "cents": t.cents}
                         for t in transfers]}
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": ")) + "\n"


def write_stdout(text):
    try:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass


def write_stderr(text):
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- main

def run(argv):
    try:
        opts = parse_args(argv)
    except HelpRequested:
        write_stdout(HELP)
        return EXIT_OK
    except UsageError as exc:
        write_stderr(USAGE)
        write_stderr(f"error: {exc}")
        return EXIT_USAGE

    text = read_ledger_text(opts.path)
    if text is None:
        write_stderr(f"error: cannot read {opts.path}")
        return EXIT_READ_ERROR

    records = split_records(text)
    if records:
        header_start, header_fields = records[0]
        data_records = records[1:]
    else:
        header_fields = None
        data_records = []
    colmap, missing, column_count = resolve_header(header_fields)
    if missing:
        for column in missing:
            write_stderr(f"header: missing {column}")
        return EXIT_ROW_ERROR

    rows = []
    errors = []
    for start, fields in data_records:
        result = parse_row(fields, start, colmap, column_count)
        if isinstance(result, RowError):
            errors.append(result)
        else:
            rows.append(result)
    if errors:
        for err in sorted(errors, key=lambda e: e.line):
            write_stderr(f"line {err.line}: {err.reason}")
        return EXIT_ROW_ERROR

    rows = filter_rows(rows, opts.since, opts.until)
    balance = aggregate(rows)

    if opts.command == "balances":
        items = balances_result(balance)
        if opts.fmt == "json":
            out = format_balances_json(items)
        else:
            out = format_balances_text(items)
    else:
        transfers = settle(balance)
        if opts.fmt == "json":
            out = format_settle_json(transfers)
        else:
            out = format_settle_text(transfers)
    write_stdout(out)
    return EXIT_OK


def main(argv):
    try:
        return run(argv)
    except InvariantError:
        write_stderr("error: internal invariant failed")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
