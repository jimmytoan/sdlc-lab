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
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")
WEIGHT_RE = re.compile(r"^\d+(?:\.\d+)?$")


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
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        if a.startswith("--") and len(a) > 2:
            if "=" in a:
                name, val = a[2:].split("=", 1)
            else:
                name = a[2:]
                if i + 1 >= len(argv):
                    raise UsageError(f"option --{name} needs a value")
                val = argv[i + 1]
                i += 1
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
                raise UsageError(f"unknown option --{name}")
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option {a}")
        else:
            positional.append(a)
        i += 1
    if len(positional) != 2:
        raise UsageError("expected <command> <ledger.csv>")
    cmd, path = positional
    if cmd not in ("balances", "settle"):
        raise UsageError(f"unknown command: {cmd}")
    return cmd, path, opts


def split_lines(text):
    out = []
    start = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append(text[start:i + 1])
            start = i + 1
        elif ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                i += 1
            out.append(text[start:i + 1])
            start = i + 1
        i += 1
    if start < n:
        out.append(text[start:])
    return out


def parse_amount(s):
    m = AMOUNT_RE.match(s.strip())
    if not m:
        return None
    whole = int(m.group(1))
    frac = m.group(2) or ""
    cents = whole * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None
    return cents


def parse_name(s):
    s = s.strip()
    if not s or ";" in s or ":" in s:
        return None
    return s


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
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name = part
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


def allocate(cents, parts):
    total_w = sum(w for _, w in parts)
    shares = []
    rems = []
    for idx, (_, w) in enumerate(parts):
        exact = Fraction(cents) * w / total_w
        q = exact.numerator // exact.denominator
        shares.append(q)
        rems.append((-(exact - q), idx))
    leftover = cents - sum(shares)
    for _, idx in sorted(rems)[:leftover]:
        shares[idx] += 1
    return shares


def load(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(3)
    return text


def main(argv):
    try:
        cmd, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write(f"error: {e}\n{USAGE}\n")
        return 64

    text = load(path)
    raw_lines = split_lines(text)
    yielded = []

    def gen():
        for no, line in enumerate(raw_lines, 1):
            if line.lstrip().startswith("#"):
                continue
            yielded.append(no)
            yield line

    reader = csv.reader(gen())
    records = []
    try:
        while True:
            k = len(yielded)
            try:
                row = next(reader)
            except StopIteration:
                break
            lineno = yielded[k] if k < len(yielded) else len(raw_lines)
            if not row or all(c.strip() == "" for c in row):
                continue
            records.append((lineno, row))
    except csv.Error:
        sys.stderr.write(f"error: cannot read {path}\n")
        return 3

    if not records:
        header = []
        rows = []
    else:
        header = [h.strip().lower() for h in records[0][1]]
        rows = records[1:]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for c in missing:
            sys.stderr.write(f"header: missing {c}\n")
        return 2
    col = {}
    for i, h in enumerate(header):
        col.setdefault(h, i)

    def get(row, name):
        i = col[name]
        return row[i] if i < len(row) else ""

    errors = []
    expenses = []
    for lineno, row in rows:
        d = parse_date(get(row, "date"))
        if d is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = parse_name(get(row, "payer"))
        if payer is None:
            errors.append((lineno, "bad-payer"))
            continue
        amount = parse_amount(get(row, "amount"))
        if amount is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(get(row, "participants"))
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        expenses.append((d, payer, amount, parts))

    if errors:
        for lineno, reason in errors:
            sys.stderr.write(f"line {lineno}: {reason}\n")
        return 2

    balances = {}
    for d, payer, amount, parts in expenses:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        balances[payer] = balances.get(payer, 0) + amount
        for (name, _), share in zip(parts, allocate(amount, parts)):
            balances[name] = balances.get(name, 0) - share

    if cmd == "balances":
        names = sorted(balances)
        if opts["format"] == "json":
            print(json.dumps({"balances": [{"name": n, "cents": balances[n]} for n in names]}))
        else:
            for n in names:
                print(f"{n} {fmt_signed(balances[n])}")
        return 0

    # settle
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
        if bal[d] == 0:
            del bal[d]
        if bal[c] == 0:
            del bal[c]
    if opts["format"] == "json":
        print(json.dumps({"transfers": [{"from": f, "to": t, "cents": a} for f, t, a in transfers]}))
    elif not transfers:
        print("nothing to settle")
    else:
        for f, t, a in transfers:
            print(f"{f} -> {t} {fmt_abs(a)}")
    return 0


def fmt_abs(cents):
    cents = abs(cents)
    return f"{cents // 100}.{cents % 100:02d}"


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt_abs(cents)
    if cents < 0:
        return "-" + fmt_abs(cents)
    return "0.00"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
