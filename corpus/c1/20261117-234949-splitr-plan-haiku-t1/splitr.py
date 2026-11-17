#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: splitr.py <balances|settle> <ledger.csv> [--format text|json]
                 [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import io
import json
import math
import re
import sys
from fractions import Fraction

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
HELP = USAGE + """

commands:
  balances   print the balance of each person
  settle     print the payments that clear all balances

options:
  --format text|json      output format (default: text)
  --since YYYY-MM-DD      only rows on or after this date
  --until YYYY-MM-DD      only rows on or before this date
  -h, --help              show this help and exit
"""

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+(\.[0-9]*)?|\.[0-9]+")
BLANK_RE = re.compile(r"[\s,]*")
COMMENT_RE = re.compile(r"\s*#")


def usage_fail(msg):
    sys.stderr.write(f"error: {msg}\n{USAGE}\n")
    sys.exit(64)


def valid_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    positionals = []
    opts = {"--format": "text", "--since": None, "--until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a in ("-h", "--help"):
            sys.stdout.write(HELP)
            sys.exit(0)
        if a.startswith("-") and len(a) > 1:
            if a.startswith("--") and "=" in a:
                name, val = a.split("=", 1)
            else:
                name, val = a, None
            if name not in opts:
                usage_fail(f"unknown option {name}")
            if val is None:
                if i >= len(argv):
                    usage_fail(f"option {name} needs a value")
                val = argv[i]
                i += 1
            opts[name] = val
        else:
            positionals.append(a)

    if opts["--format"] not in ("text", "json"):
        usage_fail("--format must be text or json")
    for name in ("--since", "--until"):
        if opts[name] is not None and valid_date(opts[name]) is None:
            usage_fail(f"{name} must be a date in YYYY-MM-DD form")

    if not positionals:
        usage_fail("missing command")
    if positionals[0] not in ("balances", "settle"):
        usage_fail(f"unknown command {positionals[0]}")
    if len(positionals) < 2:
        usage_fail("missing ledger path")
    if len(positionals) > 2:
        usage_fail("too many positional arguments")

    since = valid_date(opts["--since"]) if opts["--since"] is not None else None
    until = valid_date(opts["--until"]) if opts["--until"] is not None else None
    return {
        "command": positionals[0],
        "path": positionals[1],
        "format": opts["--format"],
        "since": since,
        "until": until,
    }


class LineSource:
    """Iterator over physical lines that tracks how many have been consumed."""

    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.i >= len(self.lines):
            raise StopIteration
        line = self.lines[self.i]
        self.i += 1
        return line


def read_records(text):
    """Yield (start_line_number, row) for each non-comment, non-blank CSV record."""
    lines = io.StringIO(text, newline="").readlines()
    src = LineSource(lines)
    while src.i < len(lines):
        line = lines[src.i]
        if BLANK_RE.fullmatch(line) or COMMENT_RE.match(line):
            src.i += 1
            continue
        start = src.i + 1
        reader = csv.reader(src)
        try:
            row = next(reader)
        except csv.Error:
            yield start, None
            continue
        yield start, row


def split_cents(cents, parts):
    """Largest-remainder split of cents by weights. parts: list of (name, Fraction)."""
    total_w = sum(w for _, w in parts)
    exacts = [Fraction(cents) * w / total_w for _, w in parts]
    base = [math.floor(e) for e in exacts]
    leftover = cents - sum(base)
    order = sorted(range(len(parts)), key=lambda i: (-(exacts[i] - base[i]), i))
    for i in order[:leftover]:
        base[i] += 1
    assert sum(base) == cents, "shares do not sum to amount"
    return base


def parse_amount(s):
    if not AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(field, extra):
    if extra or field == "":
        return None
    result = []
    seen = set()
    for entry in field.split(";"):
        entry = entry.strip()
        if entry == "":
            return None
        pieces = entry.split(":")
        if len(pieces) > 2:
            return None
        name = pieces[0].strip()
        if name == "":
            return None
        if len(pieces) == 2:
            wtext = pieces[1].strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            weight = Fraction(1)
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def load_ledger(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError, ValueError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(3)

    records = list(read_records(text))
    header = records[0][1] if records else None
    if header is None:
        header = []
    header_norm = [h.replace("\ufeff", "").strip().lower() for h in (header or [])]
    index = {}
    for pos, name in enumerate(header_norm):
        index.setdefault(name, pos)
    for col in REQUIRED_COLUMNS:
        if col not in index:
            sys.stderr.write(f"header: missing {col}\n")
            sys.exit(2)

    rows = []
    errors = []
    width = len(header_norm)
    for lineno, row in records[1:]:
        if row is None:
            errors.append((lineno, "bad-participants"))
            continue
        extra = len(row) > width
        fields = row + [""] * (width - len(row)) if len(row) < width else row
        def get(col):
            return fields[index[col]].strip()

        date = valid_date(get("date"))
        if date is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = get("payer")
        if payer == "" or ";" in payer or ":" in payer:
            errors.append((lineno, "bad-payer"))
            continue
        cents = parse_amount(get("amount"))
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(get("participants"), extra)
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        rows.append((date, payer, cents, parts))

    if errors:
        for lineno, reason in errors:
            sys.stderr.write(f"line {lineno}: {reason}\n")
        sys.exit(2)
    return rows


def compute_balances(rows, since, until):
    bal = {}
    for date, payer, cents, parts in rows:
        if since is not None and date < since:
            continue
        if until is not None and date > until:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        shares = split_cents(cents, parts)
        for (name, _), share in zip(parts, shares):
            bal.setdefault(name, 0)
            bal[name] -= share
    return bal


def settle(bal):
    amounts = {n: b for n, b in bal.items() if b != 0}
    transfers = []
    while amounts:
        creditor = min((n for n in amounts if amounts[n] > 0),
                       key=lambda n: (-amounts[n], n))
        debtor = min((n for n in amounts if amounts[n] < 0),
                     key=lambda n: (amounts[n], n))
        amt = min(amounts[creditor], -amounts[debtor])
        transfers.append((debtor, creditor, amt))
        amounts[creditor] -= amt
        amounts[debtor] += amt
        amounts = {n: b for n, b in amounts.items() if b != 0}
    return transfers


def fmt_cents(cents):
    sign = "-" if cents < 0 else ""
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    args = parse_args(argv)
    rows = load_ledger(args["path"])
    bal = compute_balances(rows, args["since"], args["until"])
    assert sum(bal.values()) == 0, "balances do not sum to zero"

    out = []
    if args["command"] == "balances":
        names = sorted(bal)
        if args["format"] == "json":
            obj = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            out.append(json.dumps(obj))
        else:
            for n in names:
                b = bal[n]
                if b > 0:
                    amt = "+" + fmt_cents(b)
                else:
                    amt = fmt_cents(b)
                out.append(f"{n} {amt}")
    else:
        transfers = settle(bal)
        if args["format"] == "json":
            obj = {"transfers": [{"from": d, "to": c, "cents": a}
                                 for d, c, a in transfers]}
            out.append(json.dumps(obj))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for d, c, a in transfers:
                out.append(f"{d} -> {c} {fmt_cents(a)}")

    for line in out:
        sys.stdout.write(line + "\n")
    sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
