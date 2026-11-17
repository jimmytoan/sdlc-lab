#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import argparse
import csv
import datetime
import io
import json
import re
import sys
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


class UsageError(Exception):
    pass


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, or None."""
    if not DATE_RE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return cents (positive int) or None."""
    text = text.strip()
    if not AMOUNT_RE.match(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (name, weight Fraction) or None if invalid."""
    parts = text.split(";")
    result = []
    seen = set()
    for raw in parts:
        raw = raw.strip()
        if not raw:
            return None
        if ":" in raw:
            name, _, wtext = raw.partition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not name or not WEIGHT_RE.match(wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            name = raw
            weight = Fraction(1)
        if not name or ":" in name:
            return None
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    if not result:
        return None
    return result


def split_shares(cents, participants):
    """Split cents among (name, weight) pairs; sums exactly to cents.

    Largest-remainder method; leftover cents go to the largest remainders,
    ties broken by listing order."""
    total_w = sum(w for _, w in participants)
    exact = [Fraction(cents) * w / total_w for _, w in participants]
    floors = [int(q) for q in exact]
    leftover = cents - sum(floors)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - floors[i]), i))
    for k in range(leftover):
        floors[order[k]] += 1
    return [(participants[i][0], floors[i]) for i in range(len(participants))]


def read_ledger(path):
    """Return (header_error_list, rows, row_errors).

    rows: list of dict(line, date, payer, cents, shares)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None

    reader = csv.reader(io.StringIO(text, newline=""))
    header = None
    header_line = None
    records = []
    try:
        for row in reader:
            lineno = reader.line_num
            if not row or all(not c.strip() for c in row):
                continue
            if row[0].lstrip().startswith("#"):
                continue
            if header is None:
                header = [c.strip().lower() for c in row]
                header_line = lineno
                continue
            records.append((lineno, row))
    except csv.Error:
        records_ok = False
    else:
        records_ok = True

    if header is None:
        return ("missing", REQUIRED_COLUMNS[0]), [], []

    index = {}
    for i, name in enumerate(header):
        if name and name not in index:
            index[name] = i
    for col in REQUIRED_COLUMNS:
        if col not in index:
            return ("missing", col), [], []

    rows = []
    errors = []
    for lineno, row in records:
        def field(col):
            i = index[col]
            return row[i] if i < len(row) else ""

        date = parse_date(field("date").strip())
        if date is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = field("payer").strip()
        if not payer:
            errors.append((lineno, "bad-payer"))
            continue
        cents = parse_amount(field("amount"))
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        participants = parse_participants(field("participants"))
        if participants is None:
            errors.append((lineno, "bad-participants"))
            continue
        rows.append({
            "line": lineno,
            "date": date,
            "payer": payer,
            "cents": cents,
            "participants": participants,
        })
    if not records_ok:
        errors.append((0, "bad-csv"))
    return None, rows, errors


def compute_balances(rows):
    balances = {}
    for r in rows:
        balances[r["payer"]] = balances.get(r["payer"], 0) + r["cents"]
        for name, share in split_shares(r["cents"], r["participants"]):
            balances[name] = balances.get(name, 0) - share
    return balances


def compute_transfers(balances):
    owe = sorted(((n, -c) for n, c in balances.items() if c < 0), key=lambda x: (-x[1], x[0]))
    get = sorted(((n, c) for n, c in balances.items() if c > 0), key=lambda x: (-x[1], x[0]))
    owe = [list(x) for x in owe]
    get = [list(x) for x in get]
    transfers = []
    i = j = 0
    while i < len(owe) and j < len(get):
        amt = min(owe[i][1], get[j][1])
        if amt > 0:
            transfers.append({"from": owe[i][0], "to": get[j][0], "cents": amt})
        owe[i][1] -= amt
        get[j][1] -= amt
        if owe[i][1] == 0:
            i += 1
        if get[j][1] == 0:
            j += 1
    return transfers


def fmt_unsigned(cents):
    return "%d.%02d" % (abs(cents) // 100, abs(cents) % 100)


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt_unsigned(cents)
    if cents < 0:
        return "-" + fmt_unsigned(cents)
    return fmt_unsigned(cents)


def build_parser():
    p = UsageParser(prog="splitr.py", add_help=True)
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        sys.stderr.write("usage: splitr.py {balances,settle} <ledger.csv> [--format text|json] "
                         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
        sys.stderr.write("error: %s\n" % exc)
        return EXIT_USAGE

    since = until = None
    if args.since is not None:
        since = parse_date(args.since)
        if since is None:
            sys.stderr.write("error: invalid --since date: %s\n" % args.since)
            return EXIT_USAGE
    if args.until is not None:
        until = parse_date(args.until)
        if until is None:
            sys.stderr.write("error: invalid --until date: %s\n" % args.until)
            return EXIT_USAGE

    result = read_ledger(args.ledger)
    if result is None:
        sys.stderr.write("error: cannot read %s\n" % args.ledger)
        return EXIT_CANNOT_READ
    header_err, rows, errors = result
    if header_err is not None:
        sys.stderr.write("header: missing %s\n" % header_err[1])
        return EXIT_BAD_ROWS
    if errors:
        for lineno, reason in errors:
            if lineno:
                sys.stderr.write("line %d: %s\n" % (lineno, reason))
            else:
                sys.stderr.write("error: %s\n" % reason)
        return EXIT_BAD_ROWS

    if since is not None:
        rows = [r for r in rows if r["date"] >= since]
    if until is not None:
        rows = [r for r in rows if r["date"] <= until]

    balances = compute_balances(rows)

    if args.command == "balances":
        names = sorted(balances)
        if args.format == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print("%s %s" % (n, fmt_signed(balances[n])))
    else:
        transfers = compute_transfers(balances)
        if args.format == "json":
            out = {"transfers": [{"from": t["from"], "to": t["to"], "cents": t["cents"]}
                                 for t in transfers]}
            print(json.dumps(out))
        elif not transfers:
            print("nothing to settle")
        else:
            for t in transfers:
                print("%s -> %s %s" % (t["from"], t["to"], fmt_unsigned(t["cents"])))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
