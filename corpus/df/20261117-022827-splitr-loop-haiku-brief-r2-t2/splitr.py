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
    p = UsageParser(prog="splitr.py", add_help=True)
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", dest="fmt", choices=["text", "json"], default="text")
    p.add_argument("--since", type=_date_arg, default=None)
    p.add_argument("--until", type=_date_arg, default=None)
    return p.parse_args(argv)


def _date_arg(value):
    if not DATE_RE.match(value):
        raise argparse.ArgumentTypeError(f"invalid date: {value}")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date: {value}")


def parse_amount_cents(text):
    """Return integer cents for a strictly positive amount, else None."""
    if not AMOUNT_RE.match(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return dict name -> Fraction weight, or None if invalid."""
    weights = {}
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            return None
        if ":" in segment:
            name, _, wtext = segment.rpartition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not WEIGHT_RE.match(wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            name = segment
            weight = Fraction(1)
        if not name:
            return None
        weights[name] = weights.get(name, Fraction(0)) + weight
    if not weights:
        return None
    return weights


def split_cents(amount_cents, weights, payer=None):
    """Split amount_cents by weights (dict name->Fraction), exactly.

    Leftover cents: if the payer is a participant, the payer takes the first
    leftover cent; the rest go by largest remainder (ties by name, ignoring case).
    """
    total_w = sum(weights.values())
    exact = {n: Fraction(amount_cents) * w / total_w for n, w in weights.items()}
    base = {n: int(e) for n, e in exact.items()}  # floor, all non-negative
    leftover = amount_cents - sum(base.values())
    if leftover > 0 and payer is not None and payer in base:
        base[payer] += 1
        leftover -= 1
    order = sorted((n for n in exact if n != payer),
                   key=lambda n: (-(exact[n] - base[n]), n.casefold(), n))
    for n in order[:leftover]:
        base[n] += 1
    return base


def _payment_recipient(text, payer):
    """Return the single unweighted recipient name of a payment row, or None."""
    segments = text.split(";")
    if len(segments) != 1:
        return None
    name = segments[0].strip()
    if not name or ":" in name or name == payer:
        return None
    return name


def read_ledger(path):
    """Return (header_error, rows, row_errors).

    rows: list of dicts with keys date, payer, amount_cents, shares, line.
    """
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            raw_lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        raise OSError(path)

    # Feed csv.reader only non-comment lines, remembering original line numbers.
    fed_lines = []
    orig_numbers = []

    def feeder():
        for idx, line in enumerate(raw_lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            fed_lines.append(line)
            orig_numbers.append(idx)
            yield line

    reader = csv.reader(feeder())
    records = []  # (start_line, fields)
    prev = 0
    for fields in reader:
        end = reader.line_num
        if fields:
            records.append((orig_numbers[prev], fields))
        prev = end
    # Remaining fed lines not consumed (none expected) are ignored.

    if not records:
        return ["date"], [], []

    header = [h.strip().lower() for h in records[0][1]]
    for col in REQUIRED_COLUMNS:
        if col not in header:
            return col, [], []
    idx = {col: header.index(col) for col in REQUIRED_COLUMNS}
    kind_idx = header.index("kind") if "kind" in header else None

    rows = []
    errors = []
    for line_no, fields in records[1:]:
        if all(not f.strip() for f in fields):
            continue

        def field(col):
            i = idx[col]
            return fields[i] if i < len(fields) else ""

        kind = "expense"
        if kind_idx is not None:
            kind_text = fields[kind_idx].strip() if kind_idx < len(fields) else ""
            kind = kind_text if kind_text else "expense"

        date_text = field("date").strip()
        payer = field("payer").strip()
        amount_text = field("amount").strip()
        part_text = field("participants")

        reason = None
        date_val = None
        if not DATE_RE.match(date_text):
            reason = "bad-date"
        else:
            try:
                date_val = datetime.date.fromisoformat(date_text)
            except ValueError:
                reason = "bad-date"
        if reason is None and kind not in ("expense", "payment"):
            reason = "bad-kind"
        if reason is None and not payer:
            reason = "bad-payer"
        amount_cents = None
        if reason is None:
            amount_cents = parse_amount_cents(amount_text)
            if amount_cents is None:
                reason = "bad-amount"
        weights = None
        if reason is None:
            if kind == "payment":
                recipient = _payment_recipient(part_text, payer)
                if recipient is None:
                    reason = "bad-participants"
                else:
                    weights = {recipient: Fraction(1)}
            else:
                weights = parse_participants(part_text)
                if weights is None:
                    reason = "bad-participants"

        if reason is not None:
            errors.append((line_no, reason))
            continue
        rows.append({
            "date": date_val,
            "kind": kind,
            "payer": payer,
            "amount_cents": amount_cents,
            "weights": weights,
            "line": line_no,
        })
    return None, rows, errors


def compute_balances(rows, since, until):
    balances = {}
    for r in rows:
        if since and r["date"] < since:
            continue
        if until and r["date"] > until:
            continue
        balances.setdefault(r["payer"], 0)
        balances[r["payer"]] += r["amount_cents"]
        if r["kind"] == "payment":
            (recipient,) = r["weights"].keys()
            balances.setdefault(recipient, 0)
            balances[recipient] -= r["amount_cents"]
            continue
        shares = split_cents(r["amount_cents"], r["weights"], r["payer"])
        for name, cents in shares.items():
            balances.setdefault(name, 0)
            balances[name] -= cents
    return balances


def compute_transfers(balances):
    creds = {n: b for n, b in balances.items() if b > 0}
    debts = {n: -b for n, b in balances.items() if b < 0}
    transfers = []
    while creds and debts:
        d = min(debts, key=lambda n: (-debts[n], n))
        c = min(creds, key=lambda n: (-creds[n], n))
        pay = min(debts[d], creds[c])
        transfers.append((d, c, pay))
        debts[d] -= pay
        creds[c] -= pay
        if debts[d] == 0:
            del debts[d]
        if creds[c] == 0:
            del creds[c]
    return transfers


def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.since and args.until and args.since > args.until:
        pass  # empty range; not an error

    try:
        header_err, rows, row_errors = read_ledger(args.ledger)
    except OSError:
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_CANNOT_READ

    if header_err is not None:
        sys.stderr.write(f"header: missing {header_err}\n")
        return EXIT_BAD_ROWS

    if row_errors:
        for line_no, reason in row_errors:
            sys.stderr.write(f"line {line_no}: {reason}\n")
        return EXIT_BAD_ROWS

    balances = compute_balances(rows, args.since, args.until)

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
            out = {"transfers": [{"from": d, "to": c, "cents": amt} for d, c, amt in transfers]}
            print(json.dumps(out))
        else:
            if not transfers:
                print("nothing to settle")
            for d, c, amt in transfers:
                print(f"{d} -> {c} {fmt_plain(amt)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
