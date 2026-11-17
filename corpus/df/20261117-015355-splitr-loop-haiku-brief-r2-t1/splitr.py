#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import argparse
import csv
import datetime as _dt
import io
import json
import re
import sys
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def parse_args(argv):
    p = _Parser(prog="splitr.py", add_help=True)
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", dest="fmt", choices=["text", "json"], default="text")
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    args = p.parse_args(argv)
    for opt in ("since", "until"):
        val = getattr(args, opt)
        if val is not None:
            if not DATE_RE.match(val):
                raise UsageError("invalid %s date: %s" % (opt, val))
            try:
                _dt.date.fromisoformat(val)
            except ValueError:
                raise UsageError("invalid %s date: %s" % (opt, val))
    return args


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount_cents(s):
    if not AMOUNT_RE.match(s):
        return None
    if "." in s:
        whole, frac = s.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(s, single=False):
    """Return list of (name, weight Fraction) or None if invalid."""
    parts = s.split(";")
    result = []
    seen = set()
    for raw in parts:
        item = raw.strip()
        if ":" in item:
            name, _, wstr = item.partition(":")
            name = name.strip()
            wstr = wstr.strip()
            if not WEIGHT_RE.match(wstr):
                return None
            weight = Fraction(wstr)
        else:
            name = item
            weight = Fraction(1)
        if not name or ":" in name:
            return None
        if weight <= 0:
            return None
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    if not result:
        return None
    if single and (len(result) != 1 or ":" in s):
        return None
    return result


def split_cents(total, shares, payer=None):
    """Split total cents among (name, weight) by weight, exact to the cent.

    If the payer is a participant and there are leftover cents, the payer
    takes the first leftover cent. The rest go to largest fractional
    remainders (ties by name ascending, ignoring case), skipping the payer."""
    wsum = sum(w for _, w in shares)
    exact = [(Fraction(total) * w / wsum) for _, w in shares]
    floors = [int(e) for e in exact]  # all non-negative
    leftover = total - sum(floors)
    payer_idx = None
    for i, (name, _) in enumerate(shares):
        if payer is not None and name == payer:
            payer_idx = i
    if payer_idx is not None and leftover > 0:
        floors[payer_idx] += 1
        leftover -= 1
    candidates = [i for i in range(len(shares)) if i != payer_idx]
    order = sorted(candidates,
                   key=lambda i: (-(exact[i] - floors[i]), shares[i][0].casefold(), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [(shares[i][0], floors[i]) for i in range(len(shares))]


def load(path, since, until):
    """Returns (status, transfers-data). Prints errors itself."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return EXIT_READ, None

    reader = csv.reader(io.StringIO(text, newline=""))
    header = None
    header_line = None
    records = []  # (line_no, fields)
    try:
        for fields in reader:
            line_no = reader.line_num
            if header is None:
                if not fields or all(not x.strip() for x in fields):
                    continue
                header = [h.strip().lower() for h in fields]
                header_line = line_no
                continue
            if not fields or all(not x.strip() for x in fields):
                continue
            if fields[0].lstrip().startswith("#"):
                continue
            records.append((line_no, fields))
    except csv.Error:
        sys.stderr.write("error: cannot read %s\n" % path)
        return EXIT_READ, None

    if header is None:
        sys.stderr.write("header: missing %s\n" % REQUIRED_COLUMNS[0])
        return EXIT_BAD_ROWS, None
    for col in REQUIRED_COLUMNS:
        if col not in header:
            sys.stderr.write("header: missing %s\n" % col)
            return EXIT_BAD_ROWS, None
    idx = {c: header.index(c) for c in REQUIRED_COLUMNS}
    kind_idx = header.index("kind") if "kind" in header else None

    errors = []
    rows = []  # (date, payer, cents, shares)
    for line_no, fields in records:
        def field(i):
            return fields[i].strip() if i < len(fields) else ""

        d = parse_date(field(idx["date"]))
        if d is None:
            errors.append((line_no, "bad-date"))
            continue
        kind = field(kind_idx).lower() if kind_idx is not None else ""
        if kind == "":
            kind = "expense"
        if kind not in ("expense", "payment"):
            errors.append((line_no, "bad-kind"))
            continue
        payer = field(idx["payer"])
        if not payer or ":" in payer or ";" in payer:
            errors.append((line_no, "bad-payer"))
            continue
        cents = parse_amount_cents(field(idx["amount"]))
        if cents is None:
            errors.append((line_no, "bad-amount"))
            continue
        shares = parse_participants(field(idx["participants"]), single=(kind == "payment"))
        if shares is None or (kind == "payment" and shares[0][0] == payer):
            errors.append((line_no, "bad-participants"))
            continue
        rows.append((d, payer, cents, shares, kind))

    if errors:
        for line_no, reason in errors:
            sys.stderr.write("line %d: %s\n" % (line_no, reason))
        return EXIT_BAD_ROWS, None

    since_d = _dt.date.fromisoformat(since) if since else None
    until_d = _dt.date.fromisoformat(until) if until else None

    balances = {}
    for d, payer, cents, shares, kind in rows:
        if since_d and d < since_d:
            continue
        if until_d and d > until_d:
            continue
        balances[payer] = balances.get(payer, 0) + cents
        if kind == "payment":
            recipient = shares[0][0]
            balances[recipient] = balances.get(recipient, 0) - cents
            continue
        for name, share in split_cents(cents, shares, payer):
            balances[name] = balances.get(name, 0) - share
    return EXIT_OK, balances


def compute_transfers(balances):
    debtors = sorted(
        [[n, -c] for n, c in balances.items() if c < 0],
        key=lambda x: (-x[1], x[0]))
    creditors = sorted(
        [[n, c] for n, c in balances.items() if c > 0],
        key=lambda x: (-x[1], x[0]))
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        amt = min(debtors[i][1], creditors[j][1])
        if amt > 0:
            transfers.append((debtors[i][0], creditors[j][0], amt))
        debtors[i][1] -= amt
        creditors[j][1] -= amt
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers


def fmt_cents(c):
    sign = "+" if c > 0 else ("-" if c < 0 else "")
    a = abs(c)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def fmt_plain(c):
    a = abs(c)
    return "%d.%02d" % (a // 100, a % 100)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("usage: splitr.py <balances|settle> <ledger.csv> [options]\n")
        if str(e):
            sys.stderr.write("error: %s\n" % e)
        return EXIT_USAGE

    status, balances = load(args.ledger, args.since, args.until)
    if status != EXIT_OK:
        return status

    out = []
    if args.command == "balances":
        names = sorted(balances)
        if args.fmt == "json":
            out.append(json.dumps({"balances": [
                {"name": n, "cents": balances[n]} for n in names]}))
        else:
            for n in names:
                out.append("%s %s" % (n, fmt_cents(balances[n])))
    else:
        transfers = compute_transfers(balances)
        if args.fmt == "json":
            out.append(json.dumps({"transfers": [
                {"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for f, t, c in transfers:
                out.append("%s -> %s %s" % (f, t, fmt_plain(c)))

    for line in out:
        sys.stdout.write(line + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
