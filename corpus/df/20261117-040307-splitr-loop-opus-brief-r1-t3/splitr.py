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
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^(\d+(\.\d+)?|\.\d+)$")
BAD_NAME_CHARS = set(";:")


class UsageError(Exception):
    pass


def parse_date(s):
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
        raise UsageError("unknown command: %s" % command)
    opts = {"format": "text", "since": None, "until": None}
    path = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                key, val = a[2:].split("=", 1)
            else:
                key = a[2:]
                if key not in opts:
                    raise UsageError("unknown option: %s" % a)
                if i + 1 >= len(argv):
                    raise UsageError("option %s needs a value" % a)
                val = argv[i + 1]
                i += 1
            if key not in opts:
                raise UsageError("unknown option: --%s" % key)
            if key == "format":
                if val not in ("text", "json"):
                    raise UsageError("bad format: %s" % val)
                opts["format"] = val
            else:
                d = parse_date(val)
                if d is None:
                    raise UsageError("bad date for --%s: %s" % (key, val))
                opts[key] = d
        elif a.startswith("-") and a != "-":
            raise UsageError("unknown option: %s" % a)
        else:
            if path is not None:
                raise UsageError("unexpected argument: %s" % a)
            path = a
        i += 1
    if path is None:
        raise UsageError("missing ledger path")
    return command, path, opts


def valid_name(n):
    return n != "" and not (set(n) & BAD_NAME_CHARS)


def parse_amount(s):
    if not AMOUNT_RE.match(s):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    if s.strip() == "":
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
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
            name = part
            weight = Fraction(1)
        if not valid_name(name) or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def split(cents, parts):
    total_w = sum(w for _, w in parts)
    exact = [Fraction(cents) * w / total_w for _, w in parts]
    floors = [int(e) for e in exact]  # non-negative, so int() == floor
    left = cents - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:left]:
        floors[i] += 1
    return [(parts[i][0], floors[i]) for i in range(len(parts))]


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)
    return text


def logical_lines(text):
    """Yield (line_number, physical_line) for non-comment, non-blank lines."""
    for n, line in enumerate(text.splitlines(keepends=True), start=1):
        stripped = line.strip()
        if stripped == "" or line.lstrip().startswith("#"):
            continue
        yield n, line


def parse_records(text):
    """Return list of (start_line, fields or None)."""
    lines = list(logical_lines(text))
    records = []
    idx = 0
    while idx < len(lines):
        start, buf = lines[idx]
        idx += 1
        while not _is_complete(buf) and idx < len(lines):
            buf += lines[idx][1]
            idx += 1
        try:
            rows = list(csv.reader(buf.splitlines(keepends=True), strict=True))
            fields = rows[0] if len(rows) == 1 else None
        except csv.Error:
            fields = None
        records.append((start, fields))
    return records


def _is_complete(buf):
    """True if quotes are balanced (record does not continue on next line)."""
    return buf.count('"') % 2 == 0


def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s\n" % (e, USAGE))
        return 64

    text = read_ledger(path)
    records = parse_records(text)

    if not records:
        for col in REQUIRED:
            sys.stderr.write("header: missing %s\n" % col)
        return 2

    _, header = records[0]
    header = [h.strip().lower() for h in (header or [])]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for col in missing:
            sys.stderr.write("header: missing %s\n" % col)
        return 2
    pos = {c: header.index(c) for c in REQUIRED}

    errors = []
    expenses = []
    for line_no, fields in records[1:]:
        if fields is None:
            errors.append("line %d: bad-participants" % line_no)
            continue

        def get(col):
            i = pos[col]
            return fields[i].strip() if i < len(fields) else ""

        d = parse_date(get("date"))
        if d is None:
            errors.append("line %d: bad-date" % line_no)
            continue
        payer = get("payer")
        if not valid_name(payer):
            errors.append("line %d: bad-payer" % line_no)
            continue
        amount = parse_amount(get("amount"))
        if amount is None:
            errors.append("line %d: bad-amount" % line_no)
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append("line %d: bad-participants" % line_no)
            continue
        expenses.append((d, payer, amount, parts))

    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2

    balances = {}
    for d, payer, amount, parts in expenses:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        balances[payer] = balances.get(payer, 0) + amount
        for name, share in split(amount, parts):
            balances[name] = balances.get(name, 0) - share

    if command == "balances":
        names = sorted(balances)
        if opts["format"] == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print("%s %s" % (n, fmt_signed(balances[n])))
        return 0

    transfers = settle(balances)
    if opts["format"] == "json":
        print(json.dumps({"transfers": [
            {"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
    else:
        if not transfers:
            print("nothing to settle")
        for f, t, c in transfers:
            print("%s -> %s %s" % (f, t, fmt(c)))
    return 0


def fmt(cents):
    return "%d.%02d" % (cents // 100, cents % 100)


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt(cents)
    if cents < 0:
        return "-" + fmt(-cents)
    return "0.00"


def settle(balances):
    bal = {n: c for n, c in balances.items() if c != 0}
    transfers = []
    while bal:
        debtor = min(bal, key=lambda n: (bal[n], n))
        top = max(bal.values())
        creditor = sorted(n for n in bal if bal[n] == top)[0]
        if bal[debtor] >= 0 or top <= 0:
            break
        amt = min(-bal[debtor], top)
        transfers.append((debtor, creditor, amt))
        bal[debtor] += amt
        bal[creditor] -= amt
        for n in (debtor, creditor):
            if bal[n] == 0:
                del bal[n]
    return transfers


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
