#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

EXIT_USAGE = 64
REQUIRED = ["date", "payer", "amount", "participants"]
USAGE = ("usage: python splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(?:\.\d+)?$")
BAD_NAME_CHARS = set(";:")


class UsageError(Exception):
    pass


def parse_date(s):
    s = s.strip()
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def valid_name(n):
    return bool(n) and not (set(n) & BAD_NAME_CHARS)


def parse_amount(s):
    s = s.strip()
    if not AMOUNT_RE.match(s):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    s = s.strip()
    if not s:
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        if not part:
            return None
        if ":" in part:
            name, _, w = part.partition(":")
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name, weight = part, Fraction(1)
        if not valid_name(name) or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def split_cents(cents, participants):
    total_w = sum(w for _, w in participants)
    exact = [Fraction(cents) * w / total_w for _, w in participants]
    floors = [int(e) for e in exact]  # non-negative, so int() floors
    leftover = cents - sum(floors)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [(participants[i][0], floors[i]) for i in range(len(participants))]


def parse_args(argv):
    if len(argv) < 1:
        raise UsageError("missing command")
    command = argv[0]
    if command not in ("balances", "settle"):
        raise UsageError(f"unknown command: {command}")
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                key, val = a[2:].split("=", 1)
            else:
                key = a[2:]
                if i + 1 >= len(argv):
                    raise UsageError(f"option --{key} needs a value")
                i += 1
                val = argv[i]
            if key == "format":
                if val not in ("text", "json"):
                    raise UsageError(f"bad format: {val}")
                opts["format"] = val
            elif key in ("since", "until"):
                d = parse_date(val)
                if d is None or val.strip() != val:
                    raise UsageError(f"bad date for --{key}: {val}")
                opts[key] = d
            else:
                raise UsageError(f"unknown option: {a}")
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1
    if len(positional) != 1:
        raise UsageError("expected exactly one ledger file")
    return command, positional[0], opts


def read_rows(text):
    """Yield (line_number, fields) skipping comments and blank lines."""
    physical = text.splitlines(keepends=True)
    # Build list of non-comment lines with their numbers, preserving quoted
    # multi-line records by feeding csv the lines in order.
    kept = []
    for n, line in enumerate(physical, start=1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped == "":
            # Only treat as comment/blank if we are not inside a quoted field;
            # handled approximately below by tracking quote parity.
            kept.append((n, line, True))
        else:
            kept.append((n, line, False))

    records = []
    buf = []
    start = None
    in_quotes = False
    for n, line, skippable in kept:
        if not in_quotes and skippable:
            continue
        if not buf:
            start = n
        buf.append(line)
        if line.count('"') % 2 == 1:
            in_quotes = not in_quotes
        if not in_quotes:
            records.append((start, "".join(buf)))
            buf = []
    if buf:
        records.append((start, "".join(buf)))

    for start, rec in records:
        try:
            fields = next(csv.reader([rec]), [])
        except csv.Error:
            fields = None
        yield start, fields


def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE

    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {path}", file=sys.stderr)
        return 3

    rows = list(read_rows(text))
    if not rows:
        header = []
        body = []
    else:
        header = [h.strip().lower() for h in (rows[0][1] or [])]
        body = rows[1:]

    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for c in missing:
            print(f"header: missing {c}", file=sys.stderr)
        return 2
    idx = {c: header.index(c) for c in REQUIRED}

    errors = []
    expenses = []
    for line_no, fields in body:
        if fields is None:
            errors.append((line_no, "bad-participants"))
            continue

        def get(col):
            i = idx[col]
            return fields[i] if i < len(fields) else ""

        date = parse_date(get("date"))
        if date is None:
            errors.append((line_no, "bad-date"))
            continue
        payer = get("payer").strip()
        if not valid_name(payer):
            errors.append((line_no, "bad-payer"))
            continue
        cents = parse_amount(get("amount"))
        if cents is None:
            errors.append((line_no, "bad-amount"))
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((line_no, "bad-participants"))
            continue
        expenses.append((date, payer, cents, parts))

    if errors:
        for line_no, reason in errors:
            print(f"line {line_no}: {reason}", file=sys.stderr)
        return 2

    balances = {}
    for date, payer, cents, parts in expenses:
        if opts["since"] is not None and date < opts["since"]:
            continue
        if opts["until"] is not None and date > opts["until"]:
            continue
        balances[payer] = balances.get(payer, 0) + cents
        for name, share in split_cents(cents, parts):
            balances[name] = balances.get(name, 0) - share

    if command == "balances":
        names = sorted(balances)
        if opts["format"] == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {fmt_signed(balances[n])}")
        return 0

    transfers = settle(balances)
    if opts["format"] == "json":
        print(json.dumps({"transfers": [
            {"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
    else:
        if not transfers:
            print("nothing to settle")
        for f, t, c in transfers:
            print(f"{f} -> {t} {fmt_cents(c)}")
    return 0


def fmt_cents(c):
    return f"{c // 100}.{c % 100:02d}"


def fmt_signed(c):
    if c > 0:
        return "+" + fmt_cents(c)
    if c < 0:
        return "-" + fmt_cents(-c)
    return "0.00"


def settle(balances):
    bal = {n: v for n, v in balances.items() if v != 0}
    transfers = []
    while True:
        debtors = [n for n, v in bal.items() if v < 0]
        creditors = [n for n, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda n: (bal[n], n))
        c = min(creditors, key=lambda n: (-bal[n], n))
        amt = min(-bal[d], bal[c])
        transfers.append((d, c, amt))
        bal[d] += amt
        bal[c] -= amt
    return transfers


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
