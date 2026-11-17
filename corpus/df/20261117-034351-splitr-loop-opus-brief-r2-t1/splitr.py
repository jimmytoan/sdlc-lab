#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

USAGE = ("usage: python splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
REQUIRED = ["date", "payer", "amount", "participants"]
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def parse_args(argv):
    if not argv:
        raise UsageError("missing command")
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)
    command = argv[0]
    if command not in ("balances", "settle"):
        raise UsageError(f"unknown command: {command}")
    opts = {"format": "text", "since": None, "until": None}
    path = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and len(a) > 2:
            if "=" in a:
                name, val = a[2:].split("=", 1)
            else:
                name = a[2:]
                if name not in ("format", "since", "until"):
                    raise UsageError(f"unknown option: {a}")
                if i + 1 >= len(argv):
                    raise UsageError(f"option --{name} needs a value")
                i += 1
                val = argv[i]
            if name == "format":
                if val not in ("text", "json"):
                    raise UsageError(f"bad format: {val}")
                opts["format"] = val
            elif name in ("since", "until"):
                d = parse_date(val)
                if d is None:
                    raise UsageError(f"bad date for --{name}: {val}")
                opts[name] = d
            else:
                raise UsageError(f"unknown option: {a}")
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option: {a}")
        else:
            if path is not None:
                raise UsageError(f"unexpected argument: {a}")
            path = a
        i += 1
    if path is None:
        raise UsageError("missing ledger path")
    return command, path, opts


class LineFeeder:
    """Feeds physical lines to csv.reader, skipping comments, tracking line numbers."""

    def __init__(self, lines):
        self.lines = lines
        self.idx = 0
        self.first = None

    def __iter__(self):
        return self

    def __next__(self):
        while self.idx < len(self.lines):
            line = self.lines[self.idx]
            self.idx += 1
            if self.first is None and line.lstrip().startswith("#"):
                continue
            if self.first is None:
                self.first = self.idx
            return line
        raise StopIteration


def records(text):
    feeder = LineFeeder(text.splitlines(keepends=True))
    reader = csv.reader(feeder)
    while True:
        feeder.first = None
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:
            yield feeder.first or feeder.idx, None
            return
        if not row or all(not c.strip() for c in row):
            continue
        yield feeder.first, row


def parse_amount(s):
    s = s.strip()
    if not AMOUNT_RE.match(s):
        return None
    if "." in s:
        whole, frac = s.split(".")
        cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    else:
        cents = int(s) * 100
    if cents <= 0:
        return None
    return cents


def valid_name(n):
    return bool(n) and not any(c in n for c in ";:")


def parse_participants(s):
    s = s.strip()
    if not s:
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        if ":" in part:
            name, w = part.split(":", 1)
            name, w = name.strip(), w.strip()
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


def split(cents, parts, payer=None):
    total_w = sum(w for _, w in parts)
    exact = [Fraction(cents) * w / total_w for _, w in parts]
    floors = [e.numerator // e.denominator for e in exact]
    left = cents - sum(floors)
    order = sorted(range(len(parts)),
                   key=lambda i: (-(exact[i] - floors[i]), parts[i][0].lower(), parts[i][0], i))
    if left > 0:
        payer_idx = next((i for i, (n, _) in enumerate(parts) if n == payer), None)
        if payer_idx is not None:
            floors[payer_idx] += 1
            left -= 1
            order = [i for i in order if i != payer_idx]
    for i in order[:left]:
        floors[i] += 1
    return floors


def fmt(cents, sign=True):
    a = abs(cents)
    s = f"{a // 100}.{a % 100:02d}"
    if not sign or cents == 0:
        return s
    return ("+" if cents > 0 else "-") + s


def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 64

    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {path}", file=sys.stderr)
        return 3

    recs = records(text)
    header = None
    for _, row in recs:
        header = row
        break
    cols = {}
    if header:
        for i, h in enumerate(header):
            cols.setdefault(h.strip().lower(), i)
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        for c in missing:
            print(f"header: missing {c}", file=sys.stderr)
        return 2

    errors = []
    expenses = []
    for lineno, row in recs:
        if row is None:
            errors.append(f"line {lineno}: bad-participants")
            break

        def get(c):
            i = cols[c]
            return row[i] if i < len(row) else ""

        d = parse_date(get("date"))
        if d is None:
            errors.append(f"line {lineno}: bad-date")
            continue
        kind = "expense"
        if "kind" in cols:
            kind = get("kind").strip().lower() or "expense"
            if kind not in ("expense", "payment"):
                errors.append(f"line {lineno}: bad-kind")
                continue
        payer = get("payer").strip()
        if not valid_name(payer):
            errors.append(f"line {lineno}: bad-payer")
            continue
        amount = parse_amount(get("amount"))
        if amount is None:
            errors.append(f"line {lineno}: bad-amount")
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append(f"line {lineno}: bad-participants")
            continue
        if kind == "payment":
            raw = get("participants").strip()
            if (len(parts) != 1 or ":" in raw or ";" in raw
                    or parts[0][0] == payer):
                errors.append(f"line {lineno}: bad-participants")
                continue
        expenses.append((d, kind, payer, amount, parts))

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 2

    balances = {}
    for d, kind, payer, amount, parts in expenses:
        if opts["since"] and d < opts["since"]:
            continue
        if opts["until"] and d > opts["until"]:
            continue
        balances[payer] = balances.get(payer, 0) + amount
        if kind == "payment":
            recipient = parts[0][0]
            balances[recipient] = balances.get(recipient, 0) - amount
            continue
        for (name, _), share in zip(parts, split(amount, parts, payer)):
            balances[name] = balances.get(name, 0) - share

    if command == "balances":
        names = sorted(balances)
        if opts["format"] == "json":
            print(json.dumps({"balances": [{"name": n, "cents": balances[n]} for n in names]}))
        else:
            for n in names:
                print(f"{n} {fmt(balances[n])}")
        return 0

    bal = {n: c for n, c in balances.items() if c != 0}
    transfers = []
    while bal:
        debtor = min(bal, key=lambda n: (bal[n], n))
        # creditor: largest positive balance, ties broken by name ascending
        top = max(bal.values())
        creditor = sorted(n for n, c in bal.items() if c == top)[0]
        amt = min(-bal[debtor], bal[creditor])
        transfers.append((debtor, creditor, amt))
        bal[debtor] += amt
        bal[creditor] -= amt
        bal = {n: c for n, c in bal.items() if c != 0}

    if opts["format"] == "json":
        print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
    elif not transfers:
        print("nothing to settle")
    else:
        for a, b, c in transfers:
            print(f"{a} -> {b} {fmt(c, sign=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
