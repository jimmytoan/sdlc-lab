#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

EXIT_BAD_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]
COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
OPTIONS_WITH_VALUE = ("--format", "--since", "--until")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] "
    "[--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def usage_fail(msg):
    sys.stderr.write(f"error: {msg}\n{USAGE}\n")
    sys.exit(EXIT_USAGE)


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        if arg.startswith("--") and "=" in arg:
            name, value = arg.split("=", 1)
            if name not in OPTIONS_WITH_VALUE:
                usage_fail(f"unknown option {name}")
        elif arg in OPTIONS_WITH_VALUE:
            if i + 1 >= len(argv):
                usage_fail(f"option {arg} needs a value")
            name, value = arg, argv[i + 1]
            i += 1
        elif arg.startswith("-") and len(arg) > 1:
            usage_fail(f"unknown option {arg}")
        else:
            positional.append(arg)
            i += 1
            continue
        key = name[2:]
        opts[key] = value
        i += 1

    if len(positional) != 2:
        usage_fail("expected a command and a ledger file")
    command, path = positional
    if command not in COMMANDS:
        usage_fail(f"unknown command {command}")
    if opts["format"] not in FORMATS:
        usage_fail(f"bad --format {opts['format']}")
    for key in ("since", "until"):
        if opts[key] is not None and parse_date(opts[key]) is None:
            usage_fail(f"bad --{key} date {opts[key]}")
    return command, path, opts


def parse_date(text):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return integer cents, or None if invalid. Must be positive."""
    text = text.strip()
    if not AMOUNT_RE.match(text):
        return None
    if "." in text:
        whole, frac = text.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = text, "00"
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (name, weight Fraction), or None if invalid."""
    parts = []
    seen = set()
    for item in text.split(";"):
        item = item.strip()
        if not item:
            return None
        if ":" in item:
            name, wtext = item.split(":", 1)
            name = name.strip()
            wtext = wtext.strip()
            if not name or not re.fullmatch(r"\d+(\.\d+)?", wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            name = item
            weight = Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        parts.append((name, weight))
    if not parts:
        return None
    return parts


def split_cents(cents, parts):
    """Split cents among (name, weight) by weight; exact, largest remainder,
    ties broken by listing order."""
    total_w = sum(w for _, w in parts)
    exact = [(Fraction(cents) * w / total_w) for _, w in parts]
    floors = [int(e) for e in exact]
    remainder = cents - sum(floors)
    order = sorted(range(len(parts)), key=lambda k: (-(exact[k] - floors[k]), k))
    for k in order[:remainder]:
        floors[k] += 1
    return [(parts[k][0], floors[k]) for k in range(len(parts))]


def read_rows(path):
    """Read the file; return (header_list, rows) where rows is a list of
    (line_number, list_of_fields). Comment and blank lines are skipped."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_READ)

    physical = text.splitlines(keepends=True)
    fed_phys = []  # physical line number (1-based) for each fed line

    def feeder():
        for idx, line in enumerate(physical):
            if line.startswith("#"):
                continue
            fed_phys.append(idx + 1)
            yield line

    reader = csv.reader(feeder())
    header = None
    rows = []
    prev = 0
    for record in reader:
        end = reader.line_num
        start_fed = prev  # 0-based index into fed_phys of first line of record
        prev = end
        if not record or all(not c.strip() for c in record):
            continue
        line_no = fed_phys[start_fed] if start_fed < len(fed_phys) else end
        if header is None:
            header = [c.strip().lower() for c in record]
            continue
        rows.append((line_no, record))
    return header or [], rows


def main(argv):
    command, path, opts = parse_args(argv)
    header, rows = read_rows(path)

    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        for c in missing:
            sys.stderr.write(f"header: missing {c}\n")
        return EXIT_BAD_ROWS

    idx = {c: header.index(c) for c in REQUIRED_COLUMNS}
    since = parse_date(opts["since"]) if opts["since"] else None
    until = parse_date(opts["until"]) if opts["until"] else None

    expenses = []
    errors = []
    for line_no, record in rows:
        def field(name):
            i = idx[name]
            return record[i] if i < len(record) else ""

        date = parse_date(field("date").strip())
        if date is None:
            errors.append((line_no, "bad-date"))
            continue
        payer = field("payer").strip()
        if not payer:
            errors.append((line_no, "bad-payer"))
            continue
        cents = parse_amount(field("amount"))
        if cents is None:
            errors.append((line_no, "bad-amount"))
            continue
        parts = parse_participants(field("participants"))
        if parts is None:
            errors.append((line_no, "bad-participants"))
            continue
        expenses.append((date, payer, cents, parts))

    if errors:
        for line_no, reason in errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        return EXIT_BAD_ROWS

    balances = {}
    for date, payer, cents, parts in expenses:
        if since is not None and date < since:
            continue
        if until is not None and date > until:
            continue
        balances.setdefault(payer, 0)
        balances[payer] += cents
        for name, share in split_cents(cents, parts):
            balances.setdefault(name, 0)
            balances[name] -= share

    if command == "balances":
        return print_balances(balances, opts["format"])
    return print_settle(balances, opts["format"])


def fmt_signed(cents):
    sign = "-" if cents < 0 else ("+" if cents > 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def print_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        print(json.dumps(out))
    else:
        for n in names:
            print(f"{n} {fmt_signed(balances[n])}")
    return 0


def compute_transfers(balances):
    debtors = sorted([[-v, n] for n, v in balances.items() if v < 0], key=lambda x: (-x[0], x[1]))
    creditors = sorted([[v, n] for n, v in balances.items() if v > 0], key=lambda x: (-x[0], x[1]))
    transfers = []
    di = ci = 0
    while di < len(debtors) and ci < len(creditors):
        amt = min(debtors[di][0], creditors[ci][0])
        transfers.append((debtors[di][1], creditors[ci][1], amt))
        debtors[di][0] -= amt
        creditors[ci][0] -= amt
        if debtors[di][0] == 0:
            di += 1
        if creditors[ci][0] == 0:
            ci += 1
    return transfers


def print_settle(balances, fmt):
    transfers = compute_transfers(balances)
    if fmt == "json":
        out = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        print(json.dumps(out))
    else:
        if not transfers:
            print("nothing to settle")
        for f, t, c in transfers:
            print(f"{f} -> {t} {fmt_plain(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
