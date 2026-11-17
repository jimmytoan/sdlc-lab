#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 ok, 2 bad ledger rows / bad header, 3 unreadable file, 64 wrong usage.

Design notes:
- All money is integer cents. Floats are never used.
- Names are case-sensitive and trimmed: "ann" and "Ann" are different people.
  Do not "fix" this.
- The payer does not need to be a participant.
- The `note` column is tolerated when absent (it is not used for any calculation).
- Every row is validated before the date filter is applied, so bad data is never hidden.
- Line numbers are physical file line numbers (comments and blank lines count).
- Each physical line is one record; quoted fields may not span lines.
- Inverted date ranges (--since later than --until) yield an empty result, exit 0.
"""

import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_ROW_ERRORS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+$")

USAGE = (
    "usage: splitr.py {balances,settle} <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- arguments

def parse_date_arg(text):
    if not DATE_RE.match(text):
        raise UsageError("invalid date: %r" % text)
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise UsageError("invalid date: %r" % text)


def parse_args(argv):
    """Return dict with command, path, format, since, until. Raises UsageError."""
    positionals = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            raise SystemExit_Help()
        if arg.startswith("--") and "=" in arg:
            name, value = arg.split("=", 1)
            i += 1
        elif arg in ("--format", "--since", "--until"):
            if i + 1 >= len(argv):
                raise UsageError("option %s needs a value" % arg)
            name, value = arg, argv[i + 1]
            i += 2
        elif arg.startswith("-") and arg != "-":
            raise UsageError("unknown option: %s" % arg)
        else:
            positionals.append(arg)
            i += 1
            continue
        if name == "--format":
            if value not in ("text", "json"):
                raise UsageError("bad --format value: %r" % value)
            opts["format"] = value
        elif name in ("--since", "--until"):
            opts[name[2:]] = parse_date_arg(value)
        else:
            raise UsageError("unknown option: %s" % name)

    if len(positionals) < 1:
        raise UsageError("missing command")
    command = positionals[0]
    if command not in ("balances", "settle"):
        raise UsageError("unknown command: %r" % command)
    if len(positionals) < 2:
        raise UsageError("missing ledger path")
    if len(positionals) > 2:
        raise UsageError("unexpected argument: %r" % positionals[2])
    opts["command"] = command
    opts["path"] = positionals[1]
    return opts


class SystemExit_Help(Exception):
    pass


# ---------------------------------------------------------------- file reading

def read_text(path):
    """Return decoded text (BOM tolerant). Raises OSError / UnicodeDecodeError."""
    with open(path, "rb") as f:
        data = f.read()
    return data.decode("utf-8-sig")


def physical_records(text):
    """Yield (lineno, [fields]) for each data record, one record per physical line.

    Blank lines and lines starting with '#' are skipped. Quoted fields must stay
    on one line; an unbalanced quote is taken literally and affects only its own
    line, so it can never swallow or hide other rows. Line numbers are physical.
    """
    for lineno, line in enumerate(io.StringIO(text, newline=""), start=1):
        if line.strip() == "" or line.startswith("#"):
            continue
        rows = list(csv.reader(io.StringIO(line, newline="")))
        yield lineno, (rows[0] if rows else [])


def parse_header(fieldnames):
    """Return (index map, list of error strings)."""
    names = [f.strip() for f in fieldnames]
    index = {}
    for i, n in enumerate(names):
        if n not in index:
            index[n] = i
    errors = []
    for col in REQUIRED_COLUMNS:
        if col not in index:
            errors.append("header: missing %s" % col)
    return index, errors


# ---------------------------------------------------------------- row parsing

def parse_amount(text):
    """Return integer cents or None. Plain decimal, at most two fraction digits, > 0."""
    if not AMOUNT_RE.match(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (name, weight) or None if invalid."""
    if text.strip() == "":
        return None
    result = []
    seen = set()
    for entry in text.split(";"):
        entry = entry.strip()
        if entry == "":
            return None
        if ":" in entry:
            name, _, wtext = entry.rpartition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not WEIGHT_RE.match(wtext):
                return None
            weight = int(wtext)
            if weight <= 0:
                return None
        else:
            name = entry
            weight = 1
        if name == "":
            return None
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def parse_row(lineno, get):
    """get(column) -> raw string. Returns (row, None) or (None, reason)."""
    date_text = get("date")  # not stripped: must be exactly YYYY-MM-DD
    if not DATE_RE.match(date_text):
        return None, "bad-date"
    try:
        date = datetime.date.fromisoformat(date_text)
    except ValueError:
        return None, "bad-date"

    payer = get("payer").strip()
    if payer == "":
        return None, "bad-payer"

    amount = parse_amount(get("amount").strip())
    if amount is None:
        return None, "bad-amount"

    shares = parse_participants(get("participants"))
    if shares is None:
        return None, "bad-participants"

    return (lineno, date, payer, amount, shares), None


def read_rows(path):
    """Return (rows, errors) or raise. errors is a list of (lineno, message) strings.

    Header errors are returned as ("header", message) entries with lineno None.
    """
    text = read_text(path)
    records = physical_records(text)
    try:
        header_line, header_fields = next(records)
    except StopIteration:
        return None, [(None, "header: missing %s" % c) for c in REQUIRED_COLUMNS]

    index, header_errors = parse_header(header_fields)
    if header_errors:
        return None, [(None, msg) for msg in header_errors]

    rows = []
    errors = []
    for lineno, fields in records:
        def get(col, fields=fields):
            i = index[col]
            return fields[i] if i < len(fields) else ""
        row, reason = parse_row(lineno, get)
        if reason is not None:
            errors.append((lineno, reason))
        else:
            rows.append(row)
    return rows, errors


# ---------------------------------------------------------------- computation

def split_cents(amount_cents, weighted_names):
    """Largest-remainder split. weighted_names: list of (name, weight).

    Returns dict name -> cents summing exactly to amount_cents.
    """
    total_weight = sum(w for _, w in weighted_names)
    shares = {}
    remainders = []
    allocated = 0
    for name, w in weighted_names:
        num = amount_cents * w
        base = num // total_weight
        shares[name] = base
        allocated += base
        remainders.append((-(num % total_weight), name))
    leftover = amount_cents - allocated
    remainders.sort()
    for k in range(leftover):
        _, name = remainders[k]
        shares[name] += 1
    return shares


def compute_balances(rows):
    balances = {}
    for _, _, payer, amount, shares in rows:
        balances[payer] = balances.get(payer, 0) + amount
        for name, cents in split_cents(amount, shares).items():
            balances[name] = balances.get(name, 0) - cents
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


# ---------------------------------------------------------------- formatting

def fmt_plain(cents):
    return "%d.%02d" % (cents // 100, cents % 100) if cents >= 0 else \
        "-%d.%02d" % ((-cents) // 100, (-cents) % 100)


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    if cents > 0:
        return "+" + fmt_plain(cents)
    return fmt_plain(cents)


def format_text_balances(balances):
    return "".join("%s %s\n" % (n, fmt_signed(balances[n])) for n in sorted(balances))


def format_text_settle(transfers):
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (f, t, fmt_plain(c)) for f, t, c in transfers)


def format_json_balances(balances):
    data = {"balances": [{"name": n, "cents": balances[n]} for n in sorted(balances)]}
    return json.dumps(data, ensure_ascii=False) + "\n"


def format_json_settle(transfers):
    data = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
    return json.dumps(data, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------- main

def _write(stream, text):
    stream.write(text)
    stream.flush()


def main(argv):
    try:
        opts = parse_args(argv)
    except SystemExit_Help:
        _write(sys.stdout, USAGE + "\n")
        return EXIT_OK
    except UsageError as e:
        _write(sys.stderr, "usage error: %s\n%s\n" % (e, USAGE))
        return EXIT_USAGE

    path = opts["path"]
    try:
        rows, errors = read_rows(path)
    except (OSError, UnicodeDecodeError):
        _write(sys.stderr, "error: cannot read %s\n" % path)
        return EXIT_CANNOT_READ

    if rows is None:
        for _, msg in errors:
            _write(sys.stderr, msg + "\n")
        return EXIT_ROW_ERRORS

    if errors:
        for lineno, reason in sorted(errors, key=lambda e: e[0]):
            _write(sys.stderr, "line %d: %s\n" % (lineno, reason))
        return EXIT_ROW_ERRORS

    since, until = opts["since"], opts["until"]
    selected = [r for r in rows
                if (since is None or r[1] >= since) and (until is None or r[1] <= until)]

    balances = compute_balances(selected)
    if opts["command"] == "balances":
        if opts["format"] == "json":
            _write(sys.stdout, format_json_balances(balances))
        else:
            _write(sys.stdout, format_text_balances(balances))
    else:
        nonzero = {n: c for n, c in balances.items() if c != 0}
        transfers = settle(nonzero)
        if opts["format"] == "json":
            _write(sys.stdout, format_json_settle(transfers))
        else:
            _write(sys.stdout, format_text_settle(transfers))
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
