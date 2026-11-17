#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Money is handled as integer cents throughout.
"""
import argparse
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
AMOUNT_RE = re.compile(r"\d+(\.\d{1,2})?", re.ASCII)
WEIGHT_RE = re.compile(r"\d+", re.ASCII)


class UsageParser(argparse.ArgumentParser):
    """Argument parser whose usage errors exit with 64 instead of 2."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def parse_date_arg(value):
    if not DATE_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid date: {value!r}")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date: {value!r}")


def build_parser():
    p = UsageParser(description="Shared-expense settlement.")
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--since", type=parse_date_arg, default=None)
    p.add_argument("--until", type=parse_date_arg, default=None)
    return p


class CannotRead(Exception):
    pass


def read_text(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise CannotRead()
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def read_records(text):
    """Yield (start_line, fields) for each CSV record.

    Blank lines and comment lines (first non-space char '#') are skipped before
    CSV parsing. Line numbers are 1-based physical lines where the record starts.
    """
    src = io.StringIO(text, newline="")
    consumed = []  # physical line numbers handed to the csv reader

    def gen():
        for no, line in enumerate(src, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            consumed.append(no)
            yield line

    reader = csv.reader(gen())
    while True:
        before = len(consumed)
        try:
            rec = next(reader)
        except StopIteration:
            return
        yield consumed[before], rec


def parse_amount(s):
    """Return cents (>0) or None if invalid."""
    if not AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    frac = frac.ljust(2, "0")
    cents = int(whole) * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(s):
    """Return list of (name, weight) or None if invalid."""
    entries = []
    for seg in s.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" in seg:
            name, _, wpart = seg.rpartition(":")
            name = name.strip()
            wpart = wpart.strip()
            if not name or not WEIGHT_RE.fullmatch(wpart):
                return None
            weight = int(wpart)
            if weight < 1:
                return None
        else:
            name = seg
            weight = 1
        entries.append((name, weight))
    if not entries:
        return None
    names = [n for n, _ in entries]
    if len(set(names)) != len(names):
        return None
    return entries


def validate_row(date_s, payer, amount_s, parts_s):
    """Return (date, payer, cents, participants) or a reason string."""
    if not DATE_RE.fullmatch(date_s):
        return "bad-date"
    try:
        d = datetime.date.fromisoformat(date_s)
    except ValueError:
        return "bad-date"
    if not payer:
        return "bad-payer"
    cents = parse_amount(amount_s)
    if cents is None:
        return "bad-amount"
    parts = parse_participants(parts_s)
    if parts is None:
        return "bad-participants"
    return d, payer, cents, parts


def split_expense(cents, parts):
    """Split cents among (name, weight) pairs; return {name: share_cents}."""
    total_w = sum(w for _, w in parts)
    shares = {}
    remainders = []
    allotted = 0
    for name, w in parts:
        q, r = divmod(cents * w, total_w)
        shares[name] = q
        allotted += q
        remainders.append((r, name))
    leftover = cents - allotted
    assert 0 <= leftover < len(parts)
    remainders.sort(key=lambda t: (-t[0], t[1]))
    for _, name in remainders[:leftover]:
        shares[name] += 1
    assert sum(shares.values()) == cents
    return shares


def compute_balances(rows):
    bal = {}
    for _, payer, cents, parts in rows:
        bal.setdefault(payer, 0)
        bal[payer] += cents
        shares = split_expense(cents, parts)
        for name, share in shares.items():
            bal.setdefault(name, 0)
            bal[name] -= share
    assert sum(bal.values()) == 0
    return bal


def compute_transfers(bal):
    creditors = sorted(((n, b) for n, b in bal.items() if b > 0), key=lambda t: (-t[1], t[0]))
    debtors = sorted(((n, -b) for n, b in bal.items() if b < 0), key=lambda t: (-t[1], t[0]))
    transfers = []
    ci = di = 0
    creditors = [list(t) for t in creditors]
    debtors = [list(t) for t in debtors]
    while ci < len(creditors) and di < len(debtors):
        cname, cred = creditors[ci]
        dname, owed = debtors[di]
        amt = min(owed, cred)
        transfers.append((dname, cname, amt))
        creditors[ci][1] -= amt
        debtors[di][1] -= amt
        if creditors[ci][1] == 0:
            ci += 1
        if debtors[di][1] == 0:
            di += 1
    return transfers


def fmt_money(cents, signed):
    if cents == 0:
        return "0.00"
    sign = ""
    if signed:
        sign = "+" if cents > 0 else "-"
    elif cents < 0:
        sign = "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def emit(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def err(msg):
    sys.stderr.write(msg + "\n")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    try:
        text = read_text(args.ledger)
    except CannotRead:
        err(f"error: cannot read {args.ledger}")
        return EXIT_CANNOT_READ

    records = read_records(text)
    try:
        header_rec = next(records)
    except StopIteration:
        header_rec = None
    except csv.Error:
        err(f"error: cannot read {args.ledger}")
        return EXIT_CANNOT_READ

    if header_rec is None:
        header = []
    else:
        header = header_rec[1]
    col_index = {}
    for i, h in enumerate(header):
        col_index.setdefault(h.strip().lower(), i)

    missing = [c for c in REQUIRED_COLUMNS if c not in col_index]
    if missing:
        for c in missing:
            err(f"header: missing {c}")
        return EXIT_BAD_ROWS

    rows = []
    row_errors = []
    try:
        for lineno, rec in records:
            def field(name):
                i = col_index[name]
                return rec[i].strip() if i < len(rec) else ""

            result = validate_row(field("date"), field("payer"),
                                  field("amount"), field("participants"))
            if isinstance(result, str):
                row_errors.append(f"line {lineno}: {result}")
            else:
                rows.append(result)
    except csv.Error:
        err(f"error: cannot read {args.ledger}")
        return EXIT_CANNOT_READ

    if row_errors:
        for e in row_errors:
            err(e)
        return EXIT_BAD_ROWS

    if args.since is not None or args.until is not None:
        rows = [r for r in rows
                if (args.since is None or r[0] >= args.since)
                and (args.until is None or r[0] <= args.until)]

    bal = compute_balances(rows)

    if args.command == "balances":
        names = sorted(bal)
        if args.format == "json":
            obj = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            emit(json.dumps(obj, ensure_ascii=False) + "\n")
        else:
            out = "".join(f"{n} {fmt_money(bal[n], True)}\n" for n in names)
            emit(out)
    else:
        transfers = compute_transfers(bal)
        if args.format == "json":
            obj = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
            emit(json.dumps(obj, ensure_ascii=False) + "\n")
        elif not transfers:
            emit("nothing to settle\n")
        else:
            emit("".join(f"{d} -> {c} {fmt_money(a, False)}\n" for d, c, a in transfers))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
