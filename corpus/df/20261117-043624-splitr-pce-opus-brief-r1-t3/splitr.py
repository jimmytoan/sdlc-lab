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

REQUIRED = ("date", "payer", "amount", "participants")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", re.ASCII)
AMOUNT_RE = re.compile(r"^([0-9]+)(?:\.([0-9]{1,2}))?$", re.ASCII)
WEIGHT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$", re.ASCII)


class UsageError(Exception):
    pass


def parse_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    positionals = []
    fmt = "text"
    since = None
    until = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and a != "-":
            if a.startswith("--") and "=" in a:
                name, value = a.split("=", 1)
            else:
                name, value = a, None
                if name in ("--format", "--since", "--until"):
                    if i + 1 >= len(argv):
                        raise UsageError("missing value for %s" % name)
                    i += 1
                    value = argv[i]
            if name == "--format":
                if value not in ("text", "json"):
                    raise UsageError("bad --format value")
                fmt = value
            elif name in ("--since", "--until"):
                d = parse_date(value)
                if d is None:
                    raise UsageError("bad date for %s" % name)
                if name == "--since":
                    since = d
                else:
                    until = d
            else:
                raise UsageError("unknown option %s" % a)
        else:
            positionals.append(a)
        i += 1
    if len(positionals) != 2:
        raise UsageError("expected <command> <ledger.csv>")
    command, path = positionals
    if command not in ("balances", "settle"):
        raise UsageError("unknown command %s" % command)
    return command, path, fmt, since, until


def read_ledger(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return f.read()


def iter_records(text):
    physical = text.splitlines(keepends=True)
    yielded = []

    def gen():
        for n, line in enumerate(physical, start=1):
            if line.startswith("#") or line.strip() == "":
                continue
            yielded.append(n)
            yield line

    reader = csv.reader(gen())
    records = []
    while True:
        count = len(yielded)
        try:
            fields = next(reader)
        except StopIteration:
            break
        except csv.Error:
            # Malformed CSV; treat as a record with no usable fields.
            fields = []
            start = yielded[count] if count < len(yielded) else (yielded[-1] if yielded else 1)
            records.append((start, fields))
            break
        start = yielded[count] if count < len(yielded) else (yielded[-1] if yielded else 1)
        records.append((start, fields))
    return records


def check_header(fields):
    names = [f.strip().lower() for f in fields]
    colmap = {}
    for idx, n in enumerate(names):
        if n not in colmap:
            colmap[n] = idx
    missing = [c for c in REQUIRED if c not in colmap]
    return colmap, missing


def parse_amount(s):
    m = AMOUNT_RE.match(s)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    if cents <= 0:
        return None
    return cents


def valid_name(name):
    return name != "" and ";" not in name and ":" not in name


def parse_participants(s):
    if s == "":
        return None
    parts = []
    seen = set()
    for entry in s.split(";"):
        entry = entry.strip()
        if entry == "":
            return None
        if entry.count(":") > 1:
            return None
        if ":" in entry:
            name, w = entry.split(":", 1)
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name = entry
            weight = Fraction(1)
        if not valid_name(name) or name in seen:
            return None
        seen.add(name)
        parts.append((name, weight))
    if not parts:
        return None
    return parts


def parse_row(fields, colmap):
    def get(col):
        idx = colmap[col]
        return fields[idx].strip() if idx < len(fields) else ""

    try:
        d = parse_date(get("date"))
        if d is None:
            return "bad-date"
        payer = get("payer")
        if not valid_name(payer):
            return "bad-payer"
        cents = parse_amount(get("amount"))
        if cents is None:
            return "bad-amount"
        parts = parse_participants(get("participants"))
        if parts is None:
            return "bad-participants"
    except Exception:  # defensive: never print a traceback
        return "bad-participants"
    return (d, payer, cents, parts)


def split(cents, parts):
    total_w = sum(w for _, w in parts)
    exact = [Fraction(cents) * w / total_w for _, w in parts]
    floors = [int(e.numerator // e.denominator) for e in exact]
    leftover = cents - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return [(parts[i][0], floors[i]) for i in range(len(parts))]


def compute_balances(expenses):
    bal = {}
    for _d, payer, cents, parts in expenses:
        bal[payer] = bal.get(payer, 0) + cents
        for name, share in split(cents, parts):
            bal[name] = bal.get(name, 0) - share
    return bal


def settle(balances):
    bal = {k: v for k, v in balances.items() if v != 0}
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
    return transfers


def fmt_plain(c):
    c = abs(c)
    return "%d.%02d" % (c // 100, c % 100)


def fmt_signed(c):
    if c == 0:
        return "0.00"
    return ("+" if c > 0 else "-") + fmt_plain(c)


def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s\n" % (e, USAGE))
        return 64

    try:
        text = read_ledger(path)
    except (OSError, UnicodeDecodeError, ValueError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    records = iter_records(text)
    if not records:
        colmap, missing = {}, list(REQUIRED)
    else:
        colmap, missing = check_header(records[0][1])
    if missing:
        for col in missing:
            sys.stderr.write("header: missing %s\n" % col)
        return 2

    errors = []
    expenses = []
    for lineno, fields in records[1:]:
        res = parse_row(fields, colmap)
        if isinstance(res, str):
            errors.append((lineno, res))
        else:
            expenses.append(res)
    if errors:
        for lineno, reason in sorted(errors, key=lambda e: e[0]):
            sys.stderr.write("line %d: %s\n" % (lineno, reason))
        return 2

    filtered = [e for e in expenses
                if (since is None or e[0] >= since) and (until is None or e[0] <= until)]
    balances = compute_balances(filtered)
    names = sorted(balances)

    out = []
    if command == "balances":
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": balances[n]} for n in names]},
                                  ensure_ascii=False))
        else:
            out.extend("%s %s" % (n, fmt_signed(balances[n])) for n in names)
    else:
        transfers = settle(balances)
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": f, "to": t, "cents": c}
                                                 for f, t, c in transfers]},
                                  ensure_ascii=False))
        elif not transfers:
            out.append("nothing to settle")
        else:
            out.extend("%s -> %s %s" % (f, t, fmt_plain(c)) for f, t, c in transfers)
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
