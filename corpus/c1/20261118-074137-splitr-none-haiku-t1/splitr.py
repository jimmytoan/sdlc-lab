#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py {balances,settle} <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import argparse
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"usage error: {message}\n")
        sys.exit(EXIT_USAGE)


def parse_date(text):
    """Return a date for a strict YYYY-MM-DD string, else None."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or ""):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return amount in integer cents (positive), or None."""
    text = (text or "").strip()
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
    """Return list of (name, weight Fraction) or None when invalid."""
    if text is None or not text.strip():
        return None
    result = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            return None
        if ":" in part:
            name, _, wtext = part.partition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not name or not re.fullmatch(r"\d+(\.\d+)?", wtext):
                return None
            try:
                weight = Fraction(wtext)
            except (ValueError, ZeroDivisionError):
                return None
            if weight <= 0:
                return None
        else:
            name = part
            weight = Fraction(1)
        if not name:
            return None
        result.append((name, weight))
    names = [n for n, _ in result]
    if len(set(names)) != len(names):
        return None
    return result


def split_cents(total, parts):
    """Split total cents by weights; remainder cents go to largest fractions,
    ties broken by listing order. Returns list of ints summing to total."""
    weight_sum = sum(w for _, w in parts)
    exact = [Fraction(total) * w / weight_sum for _, w in parts]
    floors = [int(x) for x in exact]
    remainder = total - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def load_ledger(path):
    """Return (rows, errors, header_error).

    rows: list of dicts with date, payer, cents, parts (list of (name, weight)).
    errors: list of (line_no, reason).
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        text = f.read()

    lines = text.splitlines(keepends=False)
    numbered = []
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        numbered.append((idx, line))

    if not numbered:
        return None, None, "header: missing date"

    records = _with_line_numbers(numbered)

    _, header = records[0]
    header = [h.strip().lower() for h in header]
    for col in REQUIRED_COLUMNS:
        if col not in header:
            return None, None, f"header: missing {col}"
    index = {name: header.index(name) for name in header}

    rows = []
    errors = []
    for line_no, row in records[1:]:
        def field(name):
            i = index[name]
            return row[i] if i < len(row) else ""

        date_text = field("date").strip()
        date = parse_date(date_text)
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
        rows.append({"date": date, "payer": payer, "cents": cents, "parts": parts})
    return rows, errors, None


def _with_line_numbers(numbered):
    """Parse CSV records, returning (physical start line, fields) for each."""
    records = []
    pos = 0  # index into numbered of the first line of the next record

    def source():
        for _, line in numbered:
            yield line

    reader = csv.reader(source())
    for fields in reader:
        records.append((numbered[pos][0], fields))
        pos = reader.line_num
    return records


def compute_balances(rows):
    balances = {}
    for r in rows:
        balances.setdefault(r["payer"], 0)
        balances[r["payer"]] += r["cents"]
        names = [n for n, _ in r["parts"]]
        shares = split_cents(r["cents"], r["parts"])
        for name, share in zip(names, shares):
            balances.setdefault(name, 0)
            balances[name] -= share
    return balances


def compute_settlement(balances):
    """Greedy: largest debtor pays largest creditor, until all are zero."""
    transfers = []
    while True:
        creditors = sorted((-b, n) for n, b in balances.items() if b > 0)
        debtors = sorted((b, n) for n, b in balances.items() if b < 0)
        if not creditors or not debtors:
            break
        c_amt, c_name = creditors[0][0] * -1, creditors[0][1]
        d_amt, d_name = debtors[0][0] * -1, debtors[0][1]
        amt = min(c_amt, d_amt)
        transfers.append({"from": d_name, "to": c_name, "cents": amt})
        balances[c_name] -= amt
        balances[d_name] += amt
    return transfers


def format_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def format_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def main(argv=None):
    parser = _Parser(prog="splitr.py", add_help=True)
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger")
    parser.add_argument("--format", dest="fmt", choices=["text", "json"], default="text")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    args = parser.parse_args(argv)

    since = until = None
    if args.since is not None:
        since = parse_date(args.since)
        if since is None:
            parser.error(f"invalid --since date: {args.since}")
    if args.until is not None:
        until = parse_date(args.until)
        if until is None:
            parser.error(f"invalid --until date: {args.until}")

    try:
        rows, errors, header_error = load_ledger(args.ledger)
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_UNREADABLE

    if header_error is not None:
        sys.stderr.write(header_error + "\n")
        return EXIT_BAD_ROWS

    if errors:
        for line_no, reason in errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        return EXIT_BAD_ROWS

    if since is not None:
        rows = [r for r in rows if r["date"] >= since]
    if until is not None:
        rows = [r for r in rows if r["date"] <= until]

    balances = compute_balances(rows)

    if args.command == "balances":
        names = sorted(balances)
        if args.fmt == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {format_signed(balances[n])}")
    else:
        transfers = compute_settlement(balances)
        if args.fmt == "json":
            out = {"transfers": [
                {"from": t["from"], "to": t["to"], "cents": t["cents"]} for t in transfers]}
            print(json.dumps(out))
        else:
            if not transfers:
                print("nothing to settle")
            for t in transfers:
                print(f"{t['from']} -> {t['to']} {format_plain(t['cents'])}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
