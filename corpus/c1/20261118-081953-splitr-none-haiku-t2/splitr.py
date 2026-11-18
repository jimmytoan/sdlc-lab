#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
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
        sys.stderr.write(f"usage error: {message}\n")
        sys.exit(EXIT_USAGE)


def parse_args(argv):
    parser = UsageParser(prog="splitr.py", add_help=True)
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger")
    parser.add_argument("--format", dest="fmt", default="text", choices=["text", "json"])
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    args = parser.parse_args(argv)
    for opt in ("since", "until"):
        value = getattr(args, opt)
        if value is not None:
            if not DATE_RE.match(value):
                sys.stderr.write(f"usage error: invalid --{opt} date: {value}\n")
                sys.exit(EXIT_USAGE)
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                sys.stderr.write(f"usage error: invalid --{opt} date: {value}\n")
                sys.exit(EXIT_USAGE)
    return args


def parse_date(text):
    if not DATE_RE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount_cents(text):
    if not AMOUNT_RE.match(text):
        return None
    value = Fraction(text)
    return int(value * 100)


def parse_participants(text):
    """Return list of (name, weight Fraction) or None if invalid."""
    result = []
    seen = set()
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        if segment.count(":") > 1:
            return None
        if ":" in segment:
            name, weight_text = segment.split(":", 1)
            name = name.strip()
            weight_text = weight_text.strip()
            if not WEIGHT_RE.match(weight_text):
                return None
            weight = Fraction(weight_text)
            if weight <= 0:
                return None
        else:
            name = segment
            weight = Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    if not result:
        return None
    return result


def split_cents(total, parts):
    """Split total cents by weights; largest remainder, ties by listed order."""
    weight_sum = sum(w for _, w in parts)
    exact = [Fraction(total) * w / weight_sum for _, w in parts]
    floors = [int(e) for e in exact]
    leftover = total - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [(parts[i][0], floors[i]) for i in range(len(parts))]


def read_ledger(path):
    """Return (expenses, errors). Raises OSError/UnicodeError on unreadable file."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        text = f.read()
    reader = csv.reader(io.StringIO(text, newline=""))

    header = None
    header_line = 0
    errors = []
    expenses = []

    prev_line = 0
    for row in reader:
        start = prev_line + 1
        prev_line = reader.line_num
        if not row or all(not cell.strip() for cell in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            header_line = start
            missing = [c for c in REQUIRED_COLUMNS if c not in header]
            if missing:
                for c in missing:
                    sys.stderr.write(f"header: missing {c}\n")
                return None, None
            continue

        idx = {name: header.index(name) for name in header if name}
        fields = list(row)

        def get(col):
            i = idx.get(col)
            if i is None or i >= len(fields):
                return ""
            return fields[i].strip()

        date_text = get("date")
        payer = get("payer")
        amount_text = get("amount")
        participants_text = get("participants")
        note = get("note") if "note" in idx else ""

        date = parse_date(date_text)
        if date is None:
            errors.append((start, "bad-date"))
            continue
        if not payer or ":" in payer or ";" in payer:
            errors.append((start, "bad-payer"))
            continue
        cents = parse_amount_cents(amount_text)
        if cents is None:
            errors.append((start, "bad-amount"))
            continue
        parts = parse_participants(participants_text)
        if parts is None:
            errors.append((start, "bad-participants"))
            continue
        expenses.append({
            "line": start,
            "date": date,
            "payer": payer,
            "cents": cents,
            "participants": parts,
            "note": note,
        })

    if header is None:
        for c in REQUIRED_COLUMNS:
            sys.stderr.write(f"header: missing {c}\n")
        return None, None
    return expenses, errors


def compute_balances(expenses):
    balances = {}
    for e in expenses:
        payer = e["payer"]
        balances.setdefault(payer, 0)
        balances[payer] += e["cents"]
        for name, cents in split_cents(e["cents"], e["participants"]):
            balances.setdefault(name, 0)
            balances[name] -= cents
    return balances


def compute_transfers(balances):
    creditors = sorted(((n, b) for n, b in balances.items() if b > 0), key=lambda x: (-x[1], x[0]))
    debtors = sorted(((n, -b) for n, b in balances.items() if b < 0), key=lambda x: (-x[1], x[0]))
    transfers = []
    ci = di = 0
    creditors = [list(c) for c in creditors]
    debtors = [list(d) for d in debtors]
    while ci < len(creditors) and di < len(debtors):
        amount = min(creditors[ci][1], debtors[di][1])
        if amount > 0:
            transfers.append((debtors[di][0], creditors[ci][0], amount))
        creditors[ci][1] -= amount
        debtors[di][1] -= amount
        if creditors[ci][1] == 0:
            ci += 1
        if debtors[di][1] == 0:
            di += 1
    return transfers


def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        expenses, errors = read_ledger(args.ledger)
    except (OSError, UnicodeError):
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_CANNOT_READ

    if expenses is None:
        return EXIT_BAD_ROWS
    if errors:
        for line, reason in errors:
            sys.stderr.write(f"line {line}: {reason}\n")
        return EXIT_BAD_ROWS

    since = datetime.date.fromisoformat(args.since) if args.since else None
    until = datetime.date.fromisoformat(args.until) if args.until else None
    if since is not None or until is not None:
        expenses = [e for e in expenses
                    if (since is None or e["date"] >= since)
                    and (until is None or e["date"] <= until)]

    balances = compute_balances(expenses)

    if args.command == "balances":
        names = sorted(balances)
        if args.fmt == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {fmt_signed(balances[n])}")
    else:
        transfers = compute_transfers(balances)
        if args.fmt == "json":
            out = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
            print(json.dumps(out))
        else:
            if not transfers:
                print("nothing to settle")
            for f, t, c in transfers:
                print(f"{f} -> {t} {fmt_plain(c)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
