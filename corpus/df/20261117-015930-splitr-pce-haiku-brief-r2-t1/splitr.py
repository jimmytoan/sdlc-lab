#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
  python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
  python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 success, 2 bad rows / bad header, 3 unreadable file, 64 usage error.
"""
import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import date

EXIT_OK = 0
EXIT_ROW = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED = ("date", "payer", "amount", "participants")
OPTIONAL_KIND = "kind"
KINDS = ("expense", "payment")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")

USAGE = ("usage: splitr.py {balances,settle} <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class UsageError(Exception):
    pass


@dataclass
class Expense:
    date: date
    payer: str
    cents: int
    shares: list  # list of (name, weight); for a payment, [(recipient, 1)]
    kind: str = "expense"


# ---------------------------------------------------------------- arguments

def parse_date_arg(text):
    """Return a date for a YYYY-MM-DD string, or None if invalid."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_args(argv):
    """Return a dict with command, path, format, since, until. Raises UsageError."""
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    value_opts = ("--format", "--since", "--until")
    i = 0
    only_positional = False
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
            print(USAGE)
            sys.exit(EXIT_OK)
        name, eq, value = arg.partition("=")
        if name not in value_opts:
            raise UsageError(f"unknown option: {arg}")
        if not eq:
            if i >= len(argv):
                raise UsageError(f"option {name} needs a value")
            value = argv[i]
            i += 1
        opts[name[2:]] = value

    if len(positional) != 2:
        raise UsageError("expected a command and a ledger path")
    command, path = positional
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if opts["format"] not in FORMATS:
        raise UsageError(f"invalid --format: {opts['format']}")
    for key in ("since", "until"):
        if opts[key] is not None:
            parsed = parse_date_arg(opts[key])
            if parsed is None:
                raise UsageError(f"invalid --{key} date: {opts[key]}")
            opts[key] = parsed
    return {"command": command, "path": path, **opts}


# ---------------------------------------------------------------- parsing fields

def parse_amount(text):
    """'12.50' -> 1250 cents. Returns None if invalid. Must be positive."""
    m = AMOUNT_RE.fullmatch(text.strip())
    if not m:
        return None
    whole = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    cents = whole * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(text):
    """'Ann;Bob:3' -> [('Ann', 1), ('Bob', 3)]. Returns None if invalid."""
    text = text.strip()
    if not text:
        return None
    weights = {}
    for entry in text.split(";"):
        parts = entry.split(":")
        if len(parts) > 2:
            return None
        name = parts[0].strip()
        if not name:
            return None
        weight = 1
        if len(parts) == 2:
            w = parts[1].strip()
            if not WEIGHT_RE.fullmatch(w) or int(w) <= 0:
                return None
            weight = int(w)
        weights[name] = weights.get(name, 0) + weight
    return sorted(weights.items())


def parse_payment_recipient(text, payer):
    """A payment names exactly one other person, without weight.

    Returns [(name, 1)] or None if invalid."""
    text = text.strip()
    if not text or ";" in text or ":" in text:
        return None
    if payer is not None and text == payer:
        return None
    return [(text, 1)]


def parse_row(lineno, get):
    """Validate one row. `get(col)` returns the raw field text ('' if absent).

    Returns (Expense, []) on success or (None, [error strings]).
    Every problem in the row is reported."""
    errors = []
    d = get("date").strip()
    row_date = parse_date_arg(d)
    if row_date is None:
        errors.append(f"line {lineno}: bad-date")

    kind = get(OPTIONAL_KIND).strip()
    if kind == "":
        kind = "expense"
    kind_ok = kind in KINDS
    if not kind_ok:
        errors.append(f"line {lineno}: bad-kind")

    payer = get("payer").strip()
    if not payer:
        errors.append(f"line {lineno}: bad-payer")

    cents = parse_amount(get("amount"))
    if cents is None:
        errors.append(f"line {lineno}: bad-amount")

    if kind == "payment":
        shares = parse_payment_recipient(get("participants"), payer or None)
    else:
        shares = parse_participants(get("participants"))
    if shares is None:
        errors.append(f"line {lineno}: bad-participants")

    if errors:
        return None, errors
    return Expense(row_date, payer, cents, shares, kind), []


# ---------------------------------------------------------------- splitting / balances

def split(cents, shares, payer=None):
    """Split `cents` by weight with largest-remainder allocation.

    If the payer is a participant and cents are left over, the payer gets the
    first leftover cent. The rest go to the largest fractional remainders,
    skipping the payer; ties go to the name that sorts first ignoring case.
    The result always sums to exactly `cents`."""
    total_weight = sum(w for _, w in shares)
    result = {}
    remainders = []
    for name, w in shares:
        q, r = divmod(cents * w, total_weight)
        result[name] = q
        remainders.append((r, name))
    leftover = cents - sum(result.values())
    if leftover > 0 and payer is not None and payer in result:
        result[payer] += 1
        leftover -= 1
        remainders = [t for t in remainders if t[1] != payer]
    remainders.sort(key=lambda t: (-t[0], t[1].casefold(), t[1]))
    for _, name in remainders[:leftover]:
        result[name] += 1
    return result


def compute_balances(expenses):
    """Return {name: cents} where cents = paid - shares. Includes everyone involved."""
    balances = {}
    for e in expenses:
        if e.kind == "payment":
            recipient = e.shares[0][0]
            balances[e.payer] = balances.get(e.payer, 0) + e.cents
            balances[recipient] = balances.get(recipient, 0) - e.cents
            continue
        balances[e.payer] = balances.get(e.payer, 0) + e.cents
        for name, share in split(e.cents, e.shares, e.payer).items():
            balances[name] = balances.get(name, 0) - share
    return balances


def settle(balances):
    """Greedy settlement: largest debtor pays largest creditor. Returns [(from, to, cents)]."""
    debtors = {n: -b for n, b in balances.items() if b < 0}
    creditors = {n: b for n, b in balances.items() if b > 0}
    transfers = []
    while debtors and creditors:
        d = min(debtors, key=lambda n: (-debtors[n], n))
        c = min(creditors, key=lambda n: (-creditors[n], n))
        amount = min(debtors[d], creditors[c])
        transfers.append((d, c, amount))
        debtors[d] -= amount
        creditors[c] -= amount
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return transfers


# ---------------------------------------------------------------- formatting

def money(cents):
    """Unsigned two-decimal amount, e.g. 800 -> '8.00'."""
    a = abs(cents)
    return f"{a // 100}.{a % 100:02d}"


def format_amount(cents):
    """Signed amount: +22.00, -8.00, 0.00."""
    sign = "+" if cents > 0 else "-" if cents < 0 else ""
    return sign + money(cents)


# ---------------------------------------------------------------- reading

def read_text(path):
    """Read the whole file as UTF-8. Raises OSError or UnicodeDecodeError."""
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("utf-8")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def scan_line(line, state):
    """Update `state` = {'in_quote', 'field_start'} for one physical line.

    Mirrors csv's default dialect: a quote only opens a quoted field when it is
    the first character of a field, and `""` inside a quoted field is an escape."""
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if state["in_quote"]:
            if c == '"':
                if i + 1 < n and line[i + 1] == '"':
                    i += 2
                    continue
                state["in_quote"] = False
        elif c == '"' and state["field_start"]:
            state["in_quote"] = True
            state["field_start"] = False
        elif c == ",":
            state["field_start"] = True
        elif c in "\r\n":
            pass
        else:
            state["field_start"] = False
        i += 1
    if not state["in_quote"] and line.endswith(("\n", "\r")):
        state["field_start"] = True  # a record ended at this line break


def find_unterminated(all_lines, pos):
    """Return the index of the record that starts a quoted field never closed
    before end of file, searching from `pos`. Returns None if all is terminated."""
    state = {"in_quote": False, "field_start": True}
    start = None
    for idx in range(pos, len(all_lines)):
        _, line = all_lines[idx]
        if not state["in_quote"]:
            if line.startswith("#"):
                continue
            start = idx
        scan_line(line, state)
    return start if state["in_quote"] else None


def parse_segment(seg):
    """Parse a stretch of physical lines known to hold only terminated records.

    Yields (physical_line_number, fields) for each record, skipping comments
    and blank records."""
    physical = []  # physical line numbers of lines handed to the csv reader
    state = {"in_quote": False, "field_start": True}

    def lines():
        for n, line in seg:
            if not state["in_quote"] and line.startswith("#"):
                continue
            physical.append(n)
            scan_line(line, state)
            yield line

    reader = csv.reader(lines())
    while True:
        consumed = reader.line_num
        try:
            fields = next(reader)
        except StopIteration:
            return
        except csv.Error:
            start = physical[consumed] if consumed < len(physical) else seg[-1][0]
            yield start, None
            return
        start = physical[consumed]
        if not fields or (len(fields) == 1 and not fields[0].strip()):
            continue
        yield start, fields


def read_records(text):
    """Yield (physical_line_number, fields) per CSV record, or (lineno, None) for
    a record that is malformed (an unterminated quoted field). Parsing resumes
    on the line after a malformed record, so later rows are still checked."""
    all_lines = list(enumerate(io.StringIO(text, newline=""), 1))
    pos = 0
    while True:
        bad = find_unterminated(all_lines, pos)
        end = len(all_lines) if bad is None else bad
        yield from parse_segment(all_lines[pos:end])
        if bad is None:
            return
        yield all_lines[bad][0], None
        pos = bad + 1


# ---------------------------------------------------------------- main

def run(argv):
    try:
        args = parse_args(argv)
    except UsageError as e:
        print(USAGE, file=sys.stderr)
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE

    path = args["path"]
    try:
        text = read_text(path)
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {path}", file=sys.stderr)
        return EXIT_READ

    records = read_records(text)
    header = next(records, None)
    if header is None:
        header_fields = []
    elif header[1] is None:
        print(f"line {header[0]}: bad-row", file=sys.stderr)
        return EXIT_ROW
    else:
        header_fields = header[1]

    names = [h.strip().lower() for h in header_fields]
    missing = [c for c in REQUIRED if c not in names]
    if missing:
        for c in missing:
            print(f"header: missing {c}", file=sys.stderr)
        return EXIT_ROW
    index = {c: names.index(c) for c in REQUIRED}
    if OPTIONAL_KIND in names:
        index[OPTIONAL_KIND] = names.index(OPTIONAL_KIND)

    errors = []
    expenses = []
    for lineno, fields in records:
        if fields is None:
            errors.append(f"line {lineno}: bad-row")
            continue

        def get(col, fields=fields):
            if col not in index:
                return ""
            i = index[col]
            return fields[i] if i < len(fields) else ""

        expense, row_errors = parse_row(lineno, get)
        if row_errors:
            errors.extend(row_errors)
        else:
            expenses.append(expense)

    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return EXIT_ROW

    # Validation covers every row; the date filter applies afterwards.
    if args["since"] is not None:
        expenses = [e for e in expenses if e.date >= args["since"]]
    if args["until"] is not None:
        expenses = [e for e in expenses if e.date <= args["until"]]

    balances = compute_balances(expenses)
    if args["command"] == "balances":
        out = render_balances(balances, args["format"])
    else:
        out = render_settle(settle(balances), args["format"])
    if out:
        sys.stdout.write(out)
    return EXIT_OK


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        return json.dumps({"balances": [{"name": n, "cents": balances[n]} for n in names]}) + "\n"
    return "".join(f"{n} {format_amount(balances[n])}\n" for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        return json.dumps({"transfers": [
            {"from": f, "to": t, "cents": c} for f, t, c in transfers]}) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {money(c)}\n" for f, t, c in transfers)


def main():
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
