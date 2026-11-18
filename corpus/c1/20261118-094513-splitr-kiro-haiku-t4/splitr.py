#!/usr/bin/env python3
"""splitr: settle shared expenses recorded in a CSV ledger.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Standard library only (Python 3.13). Money is handled as integer cents.
"""
import calendar
import datetime
import json
import re
import sys
from dataclasses import dataclass

EXIT_OK = 0
EXIT_ROW_ERROR = 2      # header errors and row errors
EXIT_READ_ERROR = 3
EXIT_USAGE = 64
EXIT_INTERNAL = 1       # unexpected internal failure (not a documented code)

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")
VALUE_OPTIONS = ("--format", "--since", "--until")
MAX_WEIGHT = 1000

USAGE_TEXT = (
    "usage: splitr.py <balances|settle> <ledger.csv> [--format text|json]\n"
    "                 [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n"
    "\n"
    "commands:\n"
    "  balances    print each person's net balance\n"
    "  settle      print the minimum set of payments that clears all balances\n"
    "\n"
    "options:\n"
    "  --format text|json    output format (default: text)\n"
    "  --since YYYY-MM-DD    include only rows on or after this date\n"
    "  --until YYYY-MM-DD    include only rows on or before this date\n"
    "  -h, --help            show this help and exit\n"
)
USAGE_LINE = "usage: splitr.py <balances|settle> <ledger.csv> [options]"


class Exit(Exception):
    """Terminates the program with a code, optional stdout text and stderr lines."""

    def __init__(self, code, out="", err=()):
        super().__init__(code)
        self.code = code
        self.out = out
        self.err = list(err)


@dataclass
class Options:
    command: str
    ledger_path: str
    fmt: str = "text"
    since: object = None
    until: object = None


@dataclass
class Record:
    start_line: int
    fields: list
    unterminated: bool = False


@dataclass
class ValidRow:
    line: int
    date: object
    payer: str
    amount_cents: int
    participants: list
    note: str


@dataclass
class RowError:
    line: int
    reason: str


# ---------------------------------------------------------------- field rules

DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_SHAPE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_SHAPE = re.compile(r"[0-9]+")


def parse_date(text):
    """Return a datetime.date for a valid YYYY-MM-DD string, else None."""
    if not DATE_SHAPE.fullmatch(text):
        return None
    year, month, day = int(text[0:4]), int(text[5:7]), int(text[8:10])
    if year < 1 or not 1 <= month <= 12:
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return datetime.date(year, month, day)


def parse_amount(text):
    """Return the amount in integer cents for a valid amount string, else None."""
    if not AMOUNT_SHAPE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    if cents <= 0:
        return None
    return cents


def parse_payer(text):
    """Return the payer name, or None if it is empty or uses a reserved character."""
    name = text.strip()
    if not name or ";" in name or ":" in name:
        return None
    return name


def parse_participants(text):
    """Return a list of (name, weight) pairs, or None if the field is invalid."""
    if text.strip() == "":
        return None
    segments = text.split(";")
    if len(segments) > 1 and segments[-1].strip() == "":
        segments = segments[:-1]          # a single trailing empty segment is ignored
    result = []
    seen = set()
    for segment in segments:
        segment = segment.strip()
        if segment.count(":") > 1:
            return None
        if ":" in segment:
            name, _, weight_text = segment.partition(":")
            name = name.strip()
            weight_text = weight_text.strip()
            if not WEIGHT_SHAPE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if not 1 <= weight <= MAX_WEIGHT:
                return None
        else:
            name = segment
            weight = 1
        if name == "" or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


# ---------------------------------------------------------------- CLI

def parse_args(argv):
    """Parse argv into Options. Raises Exit for help (0) or usage errors (64)."""
    positional = []
    values = {}
    help_requested = False
    error = None
    only_positional = False

    def fail(message):
        nonlocal error
        if error is None:
            error = message

    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if only_positional or arg == "-" or not arg.startswith("-"):
            positional.append(arg)
            continue
        if arg == "--":
            only_positional = True
            continue
        if arg in ("-h", "--help"):
            help_requested = True
            continue
        name, has_equals, inline_value = arg.partition("=")
        if name not in VALUE_OPTIONS:
            fail(f"unknown option: {arg}")
            continue
        if has_equals:
            value = inline_value
        else:
            if i >= len(argv) or argv[i].startswith("-"):
                fail(f"option {name} requires a value")
                continue
            value = argv[i]
            i += 1
        if name in values:
            fail(f"option {name} given more than once")
            continue
        values[name] = value

    if help_requested:
        raise Exit(EXIT_OK, out=USAGE_TEXT)
    if error is not None:
        raise Exit(EXIT_USAGE, err=[f"usage error: {error}", USAGE_LINE])
    if not positional:
        raise Exit(EXIT_USAGE, err=["usage error: missing command and ledger path", USAGE_LINE])
    command = positional[0]
    if command not in COMMANDS:
        raise Exit(EXIT_USAGE, err=[f"usage error: unknown command: {command}", USAGE_LINE])
    if len(positional) < 2:
        raise Exit(EXIT_USAGE, err=["usage error: missing ledger path", USAGE_LINE])
    if len(positional) > 2:
        raise Exit(EXIT_USAGE, err=["usage error: more than one ledger path", USAGE_LINE])

    fmt = values.get("--format", "text")
    if fmt not in FORMATS:
        raise Exit(EXIT_USAGE, err=[f"usage error: --format must be text or json", USAGE_LINE])

    dates = {}
    for option in ("--since", "--until"):
        if option in values:
            parsed = parse_date(values[option])
            if parsed is None:
                raise Exit(EXIT_USAGE, err=[f"usage error: {option} must be a valid YYYY-MM-DD date",
                                            USAGE_LINE])
            dates[option] = parsed
    since = dates.get("--since")
    until = dates.get("--until")
    if since is not None and until is not None and since > until:
        raise Exit(EXIT_USAGE, err=["usage error: --since is later than --until", USAGE_LINE])

    return Options(command=command, ledger_path=positional[1], fmt=fmt, since=since, until=until)


# ---------------------------------------------------------------- ledger reader

def _parse_record(lines, start):
    """Parse one CSV record beginning at lines[start]. Returns (Record, next_index)."""
    fields = []
    unterminated = False
    j = start
    line = lines[j]
    pos = 0
    while True:
        p = pos
        while p < len(line) and line[p] in " \t":
            p += 1
        if p < len(line) and line[p] == '"':
            p += 1
            chars = []
            closed = False
            while True:
                if p >= len(line):
                    if j + 1 < len(lines):
                        chars.append("\n")       # quoted field continues on next line
                        j += 1
                        line = lines[j]
                        p = 0
                        continue
                    break
                c = line[p]
                if c == '"':
                    if p + 1 < len(line) and line[p + 1] == '"':
                        chars.append('"')
                        p += 2
                        continue
                    closed = True
                    p += 1
                    break
                chars.append(c)
                p += 1
            if not closed:
                unterminated = True
            while p < len(line) and line[p] != ",":
                chars.append(line[p])
                p += 1
            text = "".join(chars)
        else:
            comma = line.find(",", pos)
            end = len(line) if comma < 0 else comma
            text = line[pos:end]
            p = end
        fields.append(text.strip())
        if p < len(line) and line[p] == ",":
            pos = p + 1
            continue
        break
    return Record(start_line=start + 1, fields=fields, unterminated=unterminated), j + 1


def read_records(path):
    """Read the ledger as a list of Records (header first). Raises Exit(3) if unreadable."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        raise Exit(EXIT_READ_ERROR, err=[f"error: cannot read {path}"])
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Exit(EXIT_READ_ERROR, err=[f"error: cannot read {path}"])

    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    records = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#") or line.strip() == "":
            i += 1
            continue
        record, i = _parse_record(lines, i)
        records.append(record)
    return records


# ---------------------------------------------------------------- header and rows

def check_header(fields):
    """Return (column index map, missing required columns in fixed order)."""
    cols = {}
    for index, name in enumerate(fields):
        key = name.strip().lower()
        if key in REQUIRED_COLUMNS and key not in cols:
            cols[key] = index
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    return cols, missing


def parse_row(record, cols, header_len):
    """Validate one data record. Returns ValidRow or RowError."""
    if record.unterminated:
        return RowError(record.start_line, "bad-row")
    fields = list(record.fields)
    note_idx = cols["note"]
    if len(fields) > header_len:
        if note_idx != header_len - 1:
            return RowError(record.start_line, "bad-row")
        fields = fields[:note_idx] + [",".join(fields[note_idx:])]

    def get(name):
        index = cols[name]
        return fields[index] if index < len(fields) else ""

    date = parse_date(get("date"))
    if date is None:
        return RowError(record.start_line, "bad-date")
    payer = parse_payer(get("payer"))
    if payer is None:
        return RowError(record.start_line, "bad-payer")
    amount = parse_amount(get("amount"))
    if amount is None:
        return RowError(record.start_line, "bad-amount")
    participants = parse_participants(get("participants"))
    if participants is None:
        return RowError(record.start_line, "bad-participants")
    return ValidRow(line=record.start_line, date=date, payer=payer, amount_cents=amount,
                    participants=participants, note=get("note"))


def in_range(row, since, until):
    if since is not None and row.date < since:
        return False
    if until is not None and row.date > until:
        return False
    return True


# ---------------------------------------------------------------- splitting and balances

def split_row(amount_cents, participants):
    """Split an amount among (name, weight) pairs. Returns {name: share_cents}."""
    total_weight = sum(weight for _, weight in participants)
    shares = {}
    remainders = []
    base_total = 0
    for name, weight in participants:
        numerator = amount_cents * weight
        base = numerator // total_weight
        shares[name] = base
        remainders.append((-(numerator % total_weight), name))
        base_total += base
    extra = amount_cents - base_total
    for _, name in sorted(remainders)[:extra]:
        shares[name] += 1
    return shares


def compute_balances(rows):
    """Return {name: balance_cents} for every name in the rows."""
    balances = {}
    for row in rows:
        balances[row.payer] = balances.get(row.payer, 0) + row.amount_cents
        for name, share in split_row(row.amount_cents, row.participants).items():
            balances[name] = balances.get(name, 0) - share
    if sum(balances.values()) != 0:
        raise AssertionError("balances do not sum to zero")
    return balances


def settle(balances):
    """Greedy settlement. Returns [(from, to, cents)] in computed order."""
    owed = {name: -cents for name, cents in balances.items() if cents < 0}
    credit = {name: cents for name, cents in balances.items() if cents > 0}
    transfers = []
    while owed and credit:
        debtor = min(owed, key=lambda n: (-owed[n], n))
        creditor = min(credit, key=lambda n: (-credit[n], n))
        amount = min(owed[debtor], credit[creditor])
        transfers.append((debtor, creditor, amount))
        owed[debtor] -= amount
        credit[creditor] -= amount
        if owed[debtor] == 0:
            del owed[debtor]
        if credit[creditor] == 0:
            del credit[creditor]
    return transfers


# ---------------------------------------------------------------- rendering

def format_money(cents, signed):
    magnitude = abs(cents)
    body = f"{magnitude // 100}.{magnitude % 100:02d}"
    if not signed:
        return body
    if cents > 0:
        return "+" + body
    if cents < 0:
        return "-" + body
    return body


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        payload = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(payload) + "\n"
    return "".join(f"{n} {format_money(balances[n], True)}\n" for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        payload = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        return json.dumps(payload) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {format_money(c, False)}\n" for f, t, c in transfers)


# ---------------------------------------------------------------- main pipeline

def run(argv):
    """Run the whole program. Returns stdout text; raises Exit for errors."""
    options = parse_args(argv)
    records = read_records(options.ledger_path)
    header_fields = records[0].fields if records else []
    cols, missing = check_header(header_fields)
    if missing:
        raise Exit(EXIT_ROW_ERROR, err=[f"header: missing {c}" for c in missing])

    rows = []
    errors = []
    for record in records[1:]:
        result = parse_row(record, cols, len(header_fields))
        if isinstance(result, RowError):
            errors.append(result)
        else:
            rows.append(result)
    if errors:
        raise Exit(EXIT_ROW_ERROR, err=[f"line {e.line}: {e.reason}" for e in errors])

    included = [r for r in rows if in_range(r, options.since, options.until)]
    balances = compute_balances(included)
    if options.command == "balances":
        if not balances and options.fmt == "text":
            return ""
        return render_balances(balances, options.fmt)
    return render_settle(settle(balances), options.fmt)


def _write(stream, text):
    stream.buffer.write(text.encode("utf-8", "surrogateescape"))
    stream.flush()


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(0)     # no maximum size for amounts (5.9)
    try:
        out = run(argv)
    except Exit as exc:
        if exc.out:
            _write(sys.stdout, exc.out)
        if exc.err:
            _write(sys.stderr, "".join(line + "\n" for line in exc.err))
        return exc.code
    except Exception:  # defect guard: never a documented code
        _write(sys.stderr, "error: internal error\n")
        return EXIT_INTERNAL
    _write(sys.stdout, out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
