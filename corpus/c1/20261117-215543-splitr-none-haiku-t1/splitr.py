#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import io
import json
import re
import sys
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]

AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UsageError(Exception):
    pass


def usage_fail(msg=None):
    if msg:
        sys.stderr.write("error: %s\n" % msg)
    sys.stderr.write(USAGE + "\n")
    sys.exit(EXIT_USAGE)


def parse_date(s):
    """Return a date object for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    """Return integer cents for a positive amount like 12.50, else None."""
    s = s.strip()
    if not AMOUNT_RE.match(s):
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    """Return ordered list of (name, weight Fraction), merged by name, or None."""
    s = s.strip()
    if not s:
        return None
    result = []  # list of [name, weight]
    index = {}
    for part in s.split(";"):
        part = part.strip()
        if not part:
            return None
        if ":" in part:
            name, _, wtext = part.rpartition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not name or not WEIGHT_RE.match(wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            name = part
            weight = Fraction(1)
        if name in index:
            result[index[name]][1] += weight
        else:
            index[name] = len(result)
            result.append([name, weight])
    return [(n, w) for n, w in result]


def split_cents(total, shares):
    """Split `total` cents by weights so the parts sum exactly to total.

    Leftover cents go to the earliest participants with the largest fractional
    remainders (ties broken by order in the participant list)."""
    wsum = sum(w for _, w in shares)
    exact = [Fraction(total) * w / wsum for _, w in shares]
    base = [int(e) for e in exact]  # exact is non-negative, so int() floors
    leftover = total - sum(base)
    order = sorted(range(len(shares)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[:leftover]:
        base[i] += 1
    return [(shares[i][0], base[i]) for i in range(len(shares))]


def read_logical_lines(text):
    """Yield (line_number, raw_line) for physical lines, joining quoted multi-line records."""
    physical = text.splitlines()
    i = 0
    while i < len(physical):
        start = i + 1
        buf = physical[i]
        i += 1
        while buf.count('"') % 2 == 1 and i < len(physical):
            buf += "\n" + physical[i]
            i += 1
        yield start, buf


def load_ledger(text):
    """Return (rows, errors). rows: list of dicts; errors: list of (lineno, msg) or header errors."""
    header = None
    rows = []
    errors = []
    header_errors = []
    for lineno, raw in read_logical_lines(text):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            fields = next(csv.reader(io.StringIO(raw)))
        except (csv.Error, StopIteration):
            fields = None
        if header is None:
            cols = [c.strip().lower() for c in (fields or [])]
            for col in REQUIRED_COLUMNS:
                if col not in cols:
                    header_errors.append("header: missing %s" % col)
            header = {c: i for i, c in enumerate(cols) if c}
            if header_errors:
                return None, header_errors
            continue
        if fields is None:
            errors.append((lineno, "bad-participants"))
            continue
        width = max(header.values()) + 1
        if len(fields) < width:
            fields = fields + [""] * (width - len(fields))

        def field(name):
            return fields[header[name]]

        date = parse_date(field("date").strip())
        payer = field("payer").strip()
        cents = parse_amount(field("amount"))
        parts = parse_participants(field("participants"))
        note = field("note").strip() if "note" in header else ""
        if date is None:
            errors.append((lineno, "bad-date"))
        elif not payer:
            errors.append((lineno, "bad-payer"))
        elif cents is None:
            errors.append((lineno, "bad-amount"))
        elif parts is None:
            errors.append((lineno, "bad-participants"))
        else:
            rows.append({"line": lineno, "date": date, "payer": payer,
                         "cents": cents, "parts": parts, "note": note})
    if header is None:
        return None, ["header: missing %s" % c for c in REQUIRED_COLUMNS]
    return (rows, errors), None


def compute_balances(rows, since, until):
    bal = {}
    for r in rows:
        if since and r["date"] < since:
            continue
        if until and r["date"] > until:
            continue
        bal.setdefault(r["payer"], 0)
        for name, _ in r["parts"]:
            bal.setdefault(name, 0)
        bal[r["payer"]] += r["cents"]
        for name, cents in split_cents(r["cents"], r["parts"]):
            bal[name] -= cents
    return bal


def compute_transfers(bal):
    debtors = {n: -c for n, c in bal.items() if c < 0}
    creditors = {n: c for n, c in bal.items() if c > 0}
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


def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def fmt_plain(cents):
    return "%d.%02d" % (cents // 100, cents % 100)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        if argv:
            print(USAGE)
            return EXIT_OK
        usage_fail()

    command = None
    path = None
    fmt = "text"
    since = None
    until = None
    i = 0
    positional = []
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name not in ("--format", "--since", "--until"):
                usage_fail("unknown option: %s" % name)
            if not eq:
                i += 1
                if i >= len(argv):
                    usage_fail("option %s needs a value" % name)
                value = argv[i]
            if name == "--format":
                if value not in ("text", "json"):
                    usage_fail("bad format: %s" % value)
                fmt = value
            elif name == "--since":
                since = parse_date(value)
                if since is None:
                    usage_fail("bad date for --since: %s" % value)
            else:
                until = parse_date(value)
                if until is None:
                    usage_fail("bad date for --until: %s" % value)
        elif arg.startswith("-") and arg != "-":
            usage_fail("unknown option: %s" % arg)
        else:
            positional.append(arg)
        i += 1

    if len(positional) != 2:
        usage_fail("expected a command and a ledger file")
    command, path = positional
    if command not in ("balances", "settle"):
        usage_fail("unknown command: %s" % command)

    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return EXIT_READ

    result, header_err = load_ledger(text)
    if header_err:
        for msg in header_err:
            sys.stderr.write(msg + "\n")
        return EXIT_BAD_ROWS
    rows, errors = result
    if errors:
        for lineno, msg in errors:
            sys.stderr.write("line %d: %s\n" % (lineno, msg))
        return EXIT_BAD_ROWS

    bal = compute_balances(rows, since, until)

    if command == "balances":
        names = sorted(bal)
        if fmt == "json":
            out = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            sys.stdout.write(json.dumps(out) + "\n")
        else:
            for n in names:
                sys.stdout.write("%s %s\n" % (n, fmt_signed(bal[n])))
    else:
        transfers = compute_transfers(bal)
        if fmt == "json":
            out = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
            sys.stdout.write(json.dumps(out) + "\n")
        elif not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            for d, c, a in transfers:
                sys.stdout.write("%s -> %s %s\n" % (d, c, fmt_plain(a)))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
