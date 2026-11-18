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

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


class UsageError(Exception):
    pass


def usage_fail(msg):
    sys.stderr.write(f"usage: splitr.py <balances|settle> <ledger.csv> "
                     f"[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
    if msg:
        sys.stderr.write(f"error: {msg}\n")
    sys.exit(EXIT_USAGE)


def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_args(argv):
    positionals = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name, eq, value = arg[2:].partition("=")
            if name not in ("format", "since", "until"):
                usage_fail(f"unknown option: {arg}")
            if not eq:
                if i + 1 >= len(argv):
                    usage_fail(f"option --{name} needs a value")
                i += 1
                value = argv[i]
            opts[name] = value
        elif arg.startswith("-") and arg != "-":
            usage_fail(f"unknown option: {arg}")
        else:
            positionals.append(arg)
        i += 1

    if len(positionals) != 2:
        usage_fail("expected a command and a ledger file")
    command, path = positionals
    if command not in COMMANDS:
        usage_fail(f"unknown command: {command}")
    if opts["format"] not in FORMATS:
        usage_fail(f"unknown format: {opts['format']}")
    for key in ("since", "until"):
        if opts[key] is not None:
            d = parse_date(opts[key])
            if d is None:
                usage_fail(f"bad --{key} date: {opts[key]}")
            opts[key] = d
    return command, path, opts


def format_cents(cents):
    sign = "-" if cents < 0 else ""
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def format_signed(cents):
    if cents > 0:
        return "+" + format_cents(cents)
    return format_cents(cents)


def split_cents(total, weighted):
    """Split integer cents among (name, weight) pairs by weight, exactly.

    Leftover cents go to the largest fractional remainders; ties go to the
    earlier participant in the row."""
    wsum = sum(w for _, w in weighted)
    exact = [(Fraction(total) * w / wsum) for _, w in weighted]
    floors = [int(x) for x in exact]
    leftover = total - sum(floors)
    order = sorted(range(len(weighted)),
                   key=lambda k: (-(exact[k] - floors[k]), k))
    for k in order[:leftover]:
        floors[k] += 1
    return [(weighted[k][0], floors[k]) for k in range(len(weighted))]


def parse_row(fields, col):
    """Validate one ledger row. Returns (date, payer, cents, shares) or raises
    RowError(reason)."""

    def get(name):
        idx = col.get(name)
        if idx is None or idx >= len(fields):
            return ""
        return fields[idx].strip()

    date_text = get("date")
    d = parse_date(date_text)
    if d is None:
        raise RowError("bad-date")

    payer = get("payer")
    if not payer or ":" in payer or ";" in payer:
        raise RowError("bad-payer")

    amount_text = get("amount")
    if not AMOUNT_RE.match(amount_text):
        raise RowError("bad-amount")
    whole, _, frac = amount_text.partition(".")
    cents = int(whole) * 100 + int((frac + "0")[:2])
    if cents <= 0:
        raise RowError("bad-amount")

    part_text = get("participants")
    if not part_text:
        raise RowError("bad-participants")
    weighted = []
    seen = set()
    for item in part_text.split(";"):
        item = item.strip()
        name, sep, wtext = item.partition(":")
        name = name.strip()
        if not name or name in seen or ":" in wtext:
            raise RowError("bad-participants")
        if sep:
            wtext = wtext.strip()
            if not WEIGHT_RE.match(wtext):
                raise RowError("bad-participants")
            weight = Fraction(wtext)
            if weight <= 0:
                raise RowError("bad-participants")
        else:
            weight = Fraction(1)
        seen.add(name)
        weighted.append((name, weight))
    return d, payer, cents, split_cents(cents, weighted)


class RowError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_ledger(path):
    """Return (entries, errors). Each entry: (date, payer, cents, shares)."""
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_READ)

    # Blank out comment lines but keep physical line numbering intact.
    physical = text.splitlines(keepends=True)
    cleaned = ["\n" if ln.lstrip().startswith("#") else ln for ln in physical]
    numbered = []
    reader = csv.reader(cleaned)
    for fields in reader:
        numbered.append((reader.line_num, fields))

    # Skip leading blank rows to find the header.
    idx = 0
    while idx < len(numbered) and not any(f.strip() for f in numbered[idx][1]):
        idx += 1
    header = [] if idx >= len(numbered) else [h.strip().lower() for h in numbered[idx][1]]
    col = {name: i for i, name in enumerate(header) if name}
    missing = [c for c in REQUIRED_COLUMNS if c not in col]
    if missing:
        for c in missing:
            sys.stderr.write(f"header: missing {c}\n")
        sys.exit(EXIT_BAD_ROWS)

    entries = []
    errors = []
    for lineno, fields in numbered[idx + 1:]:
        if not any(f.strip() for f in fields):
            continue
        try:
            d, payer, cents, shares = parse_row(fields, col)
        except RowError as e:
            errors.append(f"line {lineno}: {e.reason}")
            continue
        entries.append((d, payer, cents, shares))
    return entries, errors


def compute_balances(entries, since, until):
    bal = {}
    for d, payer, cents, shares in entries:
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        bal.setdefault(payer, 0)
        bal[payer] += cents
        for name, share in shares:
            bal.setdefault(name, 0)
            bal[name] -= share
    return bal


def compute_settlement(bal):
    debtors = sorted([(n, -v) for n, v in bal.items() if v < 0],
                     key=lambda t: (-t[1], t[0]))
    creditors = sorted([(n, v) for n, v in bal.items() if v > 0],
                       key=lambda t: (-t[1], t[0]))
    debtors = [[n, a] for n, a in debtors]
    creditors = [[n, a] for n, a in creditors]
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        if pay > 0:
            transfers.append((debtors[i][0], creditors[j][0], pay))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers


def main(argv):
    command, path, opts = parse_args(argv)
    entries, errors = load_ledger(path)
    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_BAD_ROWS

    bal = compute_balances(entries, opts["since"], opts["until"])
    out = []
    if command == "balances":
        names = sorted(bal)
        if opts["format"] == "json":
            out.append(json.dumps({"balances": [
                {"name": n, "cents": bal[n]} for n in names]}))
        else:
            for n in names:
                out.append(f"{n} {format_signed(bal[n])}")
    else:
        transfers = compute_settlement(bal)
        if opts["format"] == "json":
            out.append(json.dumps({"transfers": [
                {"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for f, t, c in transfers:
                out.append(f"{f} -> {t} {format_cents(c)}")
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
