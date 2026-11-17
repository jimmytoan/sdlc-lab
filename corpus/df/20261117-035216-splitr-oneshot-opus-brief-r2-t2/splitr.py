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
WEIGHT_RE = re.compile(r"^\d+(?:\.\d+)?$|^\.\d+$")


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
    cmd = argv[0]
    if cmd not in ("balances", "settle"):
        raise UsageError("unknown command: %s" % cmd)
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and a != "--":
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name = a
                if i + 1 >= len(argv):
                    raise UsageError("option %s requires a value" % name)
                i += 1
                val = argv[i]
            if name == "--format":
                if val not in ("text", "json"):
                    raise UsageError("bad format: %s" % val)
                opts["format"] = val
            elif name in ("--since", "--until"):
                d = parse_date(val)
                if d is None:
                    raise UsageError("bad date for %s: %s" % (name, val))
                opts[name[2:]] = d
            else:
                raise UsageError("unknown option: %s" % name)
        elif a == "--":
            positional.extend(argv[i + 1:])
            break
        elif a.startswith("-") and a != "-":
            raise UsageError("unknown option: %s" % a)
        else:
            positional.append(a)
        i += 1
    if len(positional) != 1:
        raise UsageError("expected exactly one ledger path")
    return cmd, positional[0], opts


def parse_amount(s):
    s = s.strip()
    m = AMOUNT_RE.match(s)
    if not m:
        return None
    whole = int(m.group(1))
    frac = m.group(2) or ""
    frac = (frac + "00")[:2]
    cents = whole * 100 + int(frac)
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
        if not name or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def split_cents(total, participants, payer=None):
    W = sum(w for _, w in participants)
    shares = []
    rems = []
    payer_idx = None
    for idx, (name, w) in enumerate(participants):
        exact = Fraction(total) * w / W
        base = exact.numerator // exact.denominator
        shares.append(base)
        if name == payer:
            payer_idx = idx
        rems.append((-(exact - base), name.casefold(), name, idx))
    leftover = total - sum(shares)
    rems.sort()
    if payer_idx is not None and leftover > 0:
        shares[payer_idx] += 1
        leftover -= 1
        rems = [r for r in rems if r[3] != payer_idx]
    for k in range(leftover):
        shares[rems[k][3]] += 1
    return [(participants[i][0], shares[i]) for i in range(len(participants))]


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    return text


def load(text):
    """Returns (header_errors, row_errors, entries)."""
    physical = text.splitlines(keepends=True)
    numbers = []

    def gen():
        for n, line in enumerate(physical, start=1):
            if line.startswith("#"):
                continue
            numbers.append(n)
            yield line

    reader = csv.reader(gen())
    header = None
    rows = []
    while True:
        start = len(numbers)
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            ln = numbers[start] if start < len(numbers) else len(physical)
            rows.append((ln, None))
            continue
        lineno = numbers[start] if start < len(numbers) else len(physical)
        if not row or all(not c.strip() for c in row):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            continue
        rows.append((lineno, row))

    if header is None:
        header = []
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        return ["header: missing %s" % c for c in missing], [], []

    idx = {c: header.index(c) for c in REQUIRED}
    if "kind" in header:
        idx["kind"] = header.index("kind")
    errors = []
    entries = []
    for lineno, row in rows:
        if row is None:
            errors.append("line %d: bad-date" % lineno)
            continue

        def get(col):
            if col not in idx:
                return ""
            i = idx[col]
            return row[i] if i < len(row) else ""

        d = parse_date(get("date"))
        if d is None:
            errors.append("line %d: bad-date" % lineno)
            continue
        kind = get("kind").strip()
        if kind == "":
            kind = "expense"
        if kind not in ("expense", "payment"):
            errors.append("line %d: bad-kind" % lineno)
            continue
        payer = get("payer").strip()
        if not payer or ":" in payer or ";" in payer:
            errors.append("line %d: bad-payer" % lineno)
            continue
        cents = parse_amount(get("amount"))
        if cents is None:
            errors.append("line %d: bad-amount" % lineno)
            continue
        raw_parts = get("participants")
        parts = parse_participants(raw_parts)
        if parts is None:
            errors.append("line %d: bad-participants" % lineno)
            continue
        if kind == "payment":
            if len(parts) != 1 or ":" in raw_parts or parts[0][0] == payer:
                errors.append("line %d: bad-participants" % lineno)
                continue
        entries.append((d, kind, payer, cents, parts))
    return [], errors, entries


def fmt_cents(c, signed):
    sign = ""
    if c < 0:
        sign = "-"
    elif c > 0 and signed:
        sign = "+"
    a = abs(c)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def compute_balances(entries):
    bal = {}
    for _, kind, payer, cents, parts in entries:
        bal[payer] = bal.get(payer, 0) + cents
        if kind == "payment":
            rec = parts[0][0]
            bal[rec] = bal.get(rec, 0) - cents
            continue
        for name, share in split_cents(cents, parts, payer):
            bal[name] = bal.get(name, 0) - share
    return bal


def settle(bal):
    debtors = {n: -c for n, c in bal.items() if c < 0}
    creditors = {n: c for n, c in bal.items() if c > 0}
    transfers = []
    while debtors and creditors:
        d = min(debtors, key=lambda n: (-debtors[n], n))
        c = min(creditors, key=lambda n: (-creditors[n], n))
        amt = min(debtors[d], creditors[c])
        transfers.append((d, c, amt))
        debtors[d] -= amt
        creditors[c] -= amt
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return transfers


def main(argv):
    try:
        cmd, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s\n" % (e, USAGE))
        return 64

    text = read_ledger(path)
    if text is None:
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    header_errors, row_errors, entries = load(text)
    if header_errors:
        sys.stderr.write("\n".join(header_errors) + "\n")
        return 2
    if row_errors:
        sys.stderr.write("\n".join(row_errors) + "\n")
        return 2

    since, until = opts["since"], opts["until"]
    entries = [e for e in entries
               if (since is None or e[0] >= since) and (until is None or e[0] <= until)]

    bal = compute_balances(entries)
    if cmd == "balances":
        names = sorted(bal)
        if opts["format"] == "json":
            out = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            sys.stdout.write(json.dumps(out) + "\n")
        else:
            for n in names:
                sys.stdout.write("%s %s\n" % (n, fmt_cents(bal[n], True)))
    else:
        transfers = settle(bal)
        if opts["format"] == "json":
            out = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
            sys.stdout.write(json.dumps(out) + "\n")
        elif not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            for f, t, c in transfers:
                sys.stdout.write("%s -> %s %s\n" % (f, t, fmt_cents(c, False)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
