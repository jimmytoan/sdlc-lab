#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py {balances,settle} LEDGER.csv [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 success, 1 unexpected internal error, 2 bad ledger rows or
header, 3 ledger cannot be read, 64 usage error.
"""
import argparse
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_BAD_LEDGER = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(self.format_usage())
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(EXIT_USAGE)


def parse_date(text):
    """Return datetime.date for a strict YYYY-MM-DD string, or None."""
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def _date_arg(text):
    d = parse_date(text)
    if d is None:
        raise argparse.ArgumentTypeError(f"invalid date: {text!r} (expected YYYY-MM-DD)")
    return d


def build_parser():
    p = UsageParser(prog="splitr.py", description="Shared-expense settlement.")
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", dest="format", choices=["text", "json"], default="text")
    p.add_argument("--since", type=_date_arg, default=None)
    p.add_argument("--until", type=_date_arg, default=None)
    return p


def parse_amount(text):
    """Return cents (>0) or None if invalid."""
    if not _AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return dict name -> weight, or None if invalid."""
    if text.strip() == "":
        return None
    result = {}
    for entry in text.split(";"):
        entry = entry.strip()
        if entry == "":
            return None
        if entry.count(":") > 1:
            return None
        if ":" in entry:
            name, _, weight = entry.partition(":")
            name = name.strip()
            weight = weight.strip()
            if name == "" or not _WEIGHT_RE.fullmatch(weight):
                return None
            w = int(weight)
            if w < 1:
                return None
        else:
            name = entry
            w = 1
        if name in result:
            return None
        result[name] = w
    return result


def split_cents(total, weights):
    """Split total cents among {name: weight} with largest-remainder allocation."""
    names = list(weights)
    W = sum(weights.values())
    base = {n: total * weights[n] // W for n in names}
    rem = {n: (total * weights[n]) % W for n in names}
    leftover = total - sum(base.values())
    order = sorted(names, key=lambda n: (-rem[n], n))
    shares = dict(base)
    for n in order[:leftover]:
        shares[n] += 1
    return shares


class _LineSource:
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
    """Return list of (line_number, fields) for non-blank, non-comment records."""
    lines = list(io.StringIO(text, newline=""))
    src = _LineSource(lines)
    reader = csv.reader(src)
    records = []
    while src.pos < len(lines):
        line = lines[src.pos]
        if line.strip() == "" or line.startswith("#"):
            src.pos += 1
            continue
        start = src.pos + 1
        try:
            fields = next(reader)
        except csv.Error:
            fields = None
        except StopIteration:
            break
        records.append((start, fields))
    return records


class LedgerError(Exception):
    """Header problem: message printed as-is, exit 2."""


def load_expenses(text):
    """Return (list of row dicts, list of (line, reason) errors)."""
    records = read_records(text)
    if not records:
        raise LedgerError("header: missing date")
    _, header_fields = records[0]
    if header_fields is None:
        raise LedgerError("header: missing date")
    cols = {}
    for i, name in enumerate(header_fields):
        key = name.strip().lower()
        if key and key not in cols:
            cols[key] = i
    for required in REQUIRED_COLUMNS:
        if required not in cols:
            raise LedgerError(f"header: missing {required}")

    expenses = []
    errors = []
    for lineno, fields in records[1:]:
        if fields is None:
            errors.append((lineno, "bad-row"))
            continue
        if len(fields) > len(header_fields):
            errors.append((lineno, "bad-row"))
            continue

        def get(col):
            i = cols[col]
            return fields[i] if i < len(fields) else ""

        date = parse_date(get("date").strip())
        if date is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = get("payer").strip()
        if payer == "":
            errors.append((lineno, "bad-payer"))
            continue
        cents = parse_amount(get("amount").strip())
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        expenses.append({"date": date, "payer": payer, "cents": cents, "parts": parts})
    return expenses, errors


def compute_balances(expenses):
    bal = {}
    for e in expenses:
        bal.setdefault(e["payer"], 0)
        bal[e["payer"]] += e["cents"]
        shares = split_cents(e["cents"], e["parts"])
        for name, share in shares.items():
            bal.setdefault(name, 0)
            bal[name] -= share
    return bal


def compute_settlement(bal):
    debtors = {n: -v for n, v in bal.items() if v < 0}
    creditors = {n: v for n, v in bal.items() if v > 0}
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
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def run(argv):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.since is not None and args.until is not None and args.since > args.until:
        parser.error("--since is later than --until")

    try:
        with open(args.ledger, "rb") as f:
            raw = f.read()
    except OSError:
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_UNREADABLE
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_UNREADABLE

    try:
        expenses, errors = load_expenses(text)
    except LedgerError as e:
        sys.stderr.write(f"{e}\n")
        return EXIT_BAD_LEDGER
    if errors:
        for lineno, reason in sorted(errors):
            sys.stderr.write(f"line {lineno}: {reason}\n")
        return EXIT_BAD_LEDGER

    if args.since is not None:
        expenses = [e for e in expenses if e["date"] >= args.since]
    if args.until is not None:
        expenses = [e for e in expenses if e["date"] <= args.until]

    bal = compute_balances(expenses)
    out = []
    if args.command == "balances":
        names = sorted(bal)
        if args.format == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": bal[n]} for n in names]}))
        else:
            for n in names:
                out.append(f"{n} {fmt_signed(bal[n])}")
    else:
        transfers = compute_settlement(bal)
        if args.format == "json":
            out.append(json.dumps({"transfers": [
                {"from": d, "to": c, "cents": amt} for d, c, amt in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for d, c, amt in transfers:
                out.append(f"{d} -> {c} {fmt_unsigned(amt)}")
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return EXIT_OK


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        return run(argv)
    except SystemExit:
        raise
    except Exception as e:  # never show a traceback
        sys.stderr.write(f"error: {e}\n")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
