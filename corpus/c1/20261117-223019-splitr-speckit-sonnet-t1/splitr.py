#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import json
import re
import sys
from datetime import date
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants"]
HEADER_COLS = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


def usage_error():
    sys.exit(64)


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    if len(argv) < 2:
        usage_error()
    cmd, path = argv[0], argv[1]
    if cmd not in ("balances", "settle") or path.startswith("--"):
        usage_error()
    fmt, since, until = "text", None, None
    rest = argv[2:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if "=" in a and a.startswith("--"):
            k, v = a.split("=", 1)
        else:
            k, v = a, None
        if k not in ("--format", "--since", "--until"):
            usage_error()
        if v is None:
            i += 1
            if i >= len(rest):
                usage_error()
            v = rest[i]
        i += 1
        if k == "--format":
            if v not in ("text", "json"):
                usage_error()
            fmt = v
        else:
            d = parse_date(v)
            if d is None:
                usage_error()
            if k == "--since":
                since = d
            else:
                until = d
    return cmd, path, fmt, since, until


def read_records(path):
    """Return list of (orig_line_no, fields) for non-comment, non-blank records."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        text = f.read()
    orig = []

    def gen():
        for n, line in enumerate(text.splitlines(True), 1):
            if line.startswith("#"):
                continue
            orig.append(n)
            yield line

    reader = csv.reader(gen())
    out = []
    while True:
        before = len(orig)
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            out.append((orig[before] if before < len(orig) else 0, None))
            continue
        if not row or all(c.strip() == "" for c in row):
            continue
        out.append((orig[before], row))
    return out


def split_cents(total, weighted):
    """weighted: list of (name, Fraction). Largest remainder, ties by order."""
    W = sum(w for _, w in weighted)
    exact = [total * w / W for _, w in weighted]
    base = [int(x) for x in exact]
    left = total - sum(base)
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i] - base[i]), i))
    for i in order[:left]:
        base[i] += 1
    return [(weighted[i][0], base[i]) for i in range(len(weighted))]


def parse_row(fields, idx):
    def get(c):
        j = idx.get(c)
        return fields[j].strip() if j is not None and j < len(fields) else ""

    d = parse_date(get("date"))
    if d is None:
        return "bad-date"
    payer = get("payer")
    if not payer:
        return "bad-payer"
    amt = get("amount")
    if not AMOUNT_RE.match(amt):
        return "bad-amount"
    if "." in amt:
        a, b = amt.split(".")
        cents = int(a) * 100 + int(b.ljust(2, "0"))
    else:
        cents = int(amt) * 100
    parts = []
    seen = set()
    raw = get("participants")
    if not raw:
        return "bad-participants"
    for p in raw.split(";"):
        p = p.strip()
        if not p:
            return "bad-participants"
        if ":" in p:
            name, w = (x.strip() for x in p.split(":", 1))
            if not WEIGHT_RE.match(w) or Fraction(w) <= 0:
                return "bad-participants"
            w = Fraction(w)
        else:
            name, w = p, Fraction(1)
        if not name or ":" in name or name in seen:
            return "bad-participants"
        seen.add(name)
        parts.append((name, w))
    return (d, payer, cents, parts)


def main(argv):
    cmd, path, fmt, since, until = parse_args(argv)
    try:
        records = read_records(path)
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    if not records:
        header = []
    else:
        header = [c.strip().lower() for c in (records[0][1] or [])]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for c in missing:
            sys.stderr.write("header: missing %s\n" % c)
        return 2
    idx = {}
    for j, c in enumerate(header):
        idx.setdefault(c, j)

    errors = []
    expenses = []
    for n, fields in records[1:]:
        if fields is None:
            errors.append("line %d: bad-participants" % n)
            continue
        r = parse_row(fields, idx)
        if isinstance(r, str):
            errors.append("line %d: %s" % (n, r))
        else:
            expenses.append(r)
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2

    bal = {}
    for d, payer, cents, parts in expenses:
        if since and d < since:
            continue
        if until and d > until:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        bal.setdefault(payer, 0)
        for name, share in split_cents(cents, parts):
            bal[name] = bal.get(name, 0) - share

    def money(c):
        return "%s%d.%02d" % ("-" if c < 0 else "+", abs(c) // 100, abs(c) % 100)

    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            print(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                print("%s %s" % (n, money(c)))
        return 0

    debt = {n: -c for n, c in bal.items() if c < 0}
    cred = {n: c for n, c in bal.items() if c > 0}
    transfers = []
    while debt and cred:
        dn = min(debt, key=lambda n: (-debt[n], n))
        cn = min(cred, key=lambda n: (-cred[n], n))
        amt = min(debt[dn], cred[cn])
        transfers.append((dn, cn, amt))
        debt[dn] -= amt
        cred[cn] -= amt
        if debt[dn] == 0:
            del debt[dn]
        if cred[cn] == 0:
            del cred[cn]
    if fmt == "json":
        print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
    elif not transfers:
        print("nothing to settle")
    else:
        for a, b, c in transfers:
            print("%s -> %s %d.%02d" % (a, b, c // 100, c % 100))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
