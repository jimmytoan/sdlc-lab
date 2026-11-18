#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 success, 2 bad rows / bad header, 3 cannot read file,
64 usage error, 1 internal bug (last resort only).
"""
import argparse
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_DATA = 2
EXIT_READ = 3
EXIT_INTERNAL = 1
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]
KNOWN_COLUMNS = REQUIRED_COLUMNS + ["note"]

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class HelpExit(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose error path raises instead of exiting with 2."""

    def error(self, message):
        raise UsageError(message)


def _date_arg(value):
    if not DATE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid date: %r" % value)
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid date: %r" % value)


def parse_args(argv):
    parser = _Parser(prog="splitr.py", allow_abbrev=False,
                     description="Shared-expense settlement.")
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger")
    parser.add_argument("--format", dest="fmt", choices=["text", "json"],
                        default="text")
    parser.add_argument("--since", type=_date_arg, default=None)
    parser.add_argument("--until", type=_date_arg, default=None)
    try:
        return parser.parse_args(argv)
    except SystemExit as e:
        # Only --help / -h reaches here (argparse exits 0 after printing usage).
        code = e.code if isinstance(e.code, int) else 0
        raise HelpExit(code)


def read_text(path):
    """Return the decoded file text, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


class _LineSource:
    """Iterator over physical lines that tracks how many have been consumed."""

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


def read_records(text):
    """Yield (start_line, fields) for each CSV record, skipping comment and
    blank lines. start_line is the 1-based physical line where the record
    starts. fields is None if the CSV layer rejected the record."""
    lines = list(io.StringIO(text, newline=""))
    src = _LineSource(lines)
    reader = csv.reader(src)
    while True:
        # Skip comment and blank lines at a record boundary.
        while src.pos < len(lines):
            line = lines[src.pos]
            if line.startswith("#") or line.strip() == "":
                src.pos += 1
            else:
                break
        if src.pos >= len(lines):
            return
        start = src.pos + 1
        try:
            fields = next(reader)
        except StopIteration:
            return
        except csv.Error:
            yield start, None
            continue
        yield start, fields


def parse_participants(text):
    """Return list of (name, weight), or None if the field is invalid."""
    result = []
    names = set()
    for piece in text.split(";"):
        piece = piece.strip()
        if piece == "" or piece.count(":") > 1:
            return None
        if ":" in piece:
            name, weight_text = piece.split(":", 1)
            name = name.strip()
            weight_text = weight_text.strip()
            if name == "" or not WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight < 1:
                return None
        else:
            name = piece
            weight = 1
        if name in names:
            return None
        names.add(name)
        result.append((name, weight))
    return result


def amount_to_cents(text):
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def parse_row(fields, header, width):
    """Return (expense, None) or (None, reason)."""
    if len(fields) > width:
        return None, "bad-row"

    def get(col):
        idx = header.get(col)
        if idx is None or idx >= len(fields):
            return ""
        return fields[idx].strip()

    date_text = get("date")
    if not DATE_RE.fullmatch(date_text):
        return None, "bad-date"
    try:
        date = datetime.date.fromisoformat(date_text)
    except ValueError:
        return None, "bad-date"

    payer = get("payer")
    if payer == "":
        return None, "bad-payer"

    amount_text = get("amount")
    if not AMOUNT_RE.fullmatch(amount_text):
        return None, "bad-amount"
    cents = amount_to_cents(amount_text)
    if cents <= 0:
        return None, "bad-amount"

    participants = parse_participants(get("participants"))
    if participants is None:
        return None, "bad-participants"

    return {
        "date": date,
        "payer": payer,
        "cents": cents,
        "participants": participants,
    }, None


def split_cents(cents, participants):
    """Largest-remainder split. participants: list of (name, weight).
    Returns dict name -> share in cents; shares sum exactly to cents."""
    total_w = sum(w for _, w in participants)
    shares = {}
    rem = {}
    for name, w in participants:
        shares[name] = (cents * w) // total_w
        rem[name] = (cents * w) % total_w
    left = cents - sum(shares.values())
    order = sorted(participants, key=lambda p: (-rem[p[0]], p[0]))
    for i in range(left):
        shares[order[i][0]] += 1
    assert sum(shares.values()) == cents
    return shares


def compute_balances(expenses):
    balances = {}
    for exp in expenses:
        balances.setdefault(exp["payer"], 0)
        balances[exp["payer"]] += exp["cents"]
        for name, _ in exp["participants"]:
            balances.setdefault(name, 0)
        for name, share in split_cents(exp["cents"], exp["participants"]).items():
            balances[name] -= share
    assert sum(balances.values()) == 0
    return balances


def settle(balances):
    debtors = [[n, -c] for n, c in balances.items() if c < 0]
    creditors = [[n, c] for n, c in balances.items() if c > 0]
    debtors.sort(key=lambda x: (-x[1], x[0]))
    creditors.sort(key=lambda x: (-x[1], x[0]))
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d = debtors[i]
        c = creditors[j]
        amt = min(d[1], c[1])
        transfers.append({"from": d[0], "to": c[0], "cents": amt})
        d[1] -= amt
        c[1] -= amt
        if d[1] == 0:
            i += 1
        if c[1] == 0:
            j += 1
    return transfers


def format_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def format_plain(cents):
    a = abs(cents)
    return "%d.%02d" % (a // 100, a % 100)


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        return json.dumps({"balances": [
            {"name": n, "cents": balances[n]} for n in names]}) + "\n"
    return "".join("%s %s\n" % (n, format_signed(balances[n])) for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        return json.dumps({"transfers": [
            {"from": t["from"], "to": t["to"], "cents": t["cents"]}
            for t in transfers]}) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (t["from"], t["to"], format_plain(t["cents"]))
                   for t in transfers)


def run(args):
    text = read_text(args.ledger)
    if text is None:
        sys.stderr.write("error: cannot read %s\n" % args.ledger)
        return EXIT_READ

    records = read_records(text)
    # Header: the first record that is not a comment or blank line.
    header_start, header_fields = next(records, (0, []))
    if header_fields is None:
        header_fields = []
    width = len(header_fields)
    seen = {}
    dups = []
    for i, f in enumerate(header_fields):
        name = f.strip().lower()
        if name in KNOWN_COLUMNS:
            if name in seen:
                if name not in dups:
                    dups.append(name)
            else:
                seen[name] = i
    errors = ["header: missing %s" % col
              for col in REQUIRED_COLUMNS if col not in seen]
    if not errors:
        errors = ["header: duplicate %s" % col for col in dups]
    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_DATA

    expenses = []
    row_errors = []
    for lineno, fields in records:
        if fields is None:
            row_errors.append("line %d: bad-row" % lineno)
            continue
        exp, reason = parse_row(fields, seen, width)
        if reason is not None:
            row_errors.append("line %d: %s" % (lineno, reason))
        else:
            expenses.append(exp)
    if row_errors:
        for e in row_errors:
            sys.stderr.write(e + "\n")
        return EXIT_DATA

    if args.since is not None:
        expenses = [e for e in expenses if e["date"] >= args.since]
    if args.until is not None:
        expenses = [e for e in expenses if e["date"] <= args.until]

    balances = compute_balances(expenses)
    if args.command == "balances":
        out = render_balances(balances, args.fmt)
    else:
        out = render_settle(settle(balances), args.fmt)
    sys.stdout.write(out)
    return EXIT_OK


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("splitr.py: error: %s\n" % e)
        return EXIT_USAGE
    except HelpExit as e:
        return e.code
    try:
        return run(args)
    except Exception:  # last resort: never print a traceback
        sys.stderr.write("error: internal error\n")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
