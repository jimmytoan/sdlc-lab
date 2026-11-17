#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import io
import json
import re
import sys
from fractions import Fraction

USAGE = ("usage: splitr.py {balances,settle} <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED = ["date", "payer", "amount", "participants", "note"]

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AMOUNT_RE = re.compile(r"(\d+)(?:\.(\d{1,2}))?")
WEIGHT_RE = re.compile(r"\d+(?:\.\d+)?")


class UsageError(Exception):
    pass


def usage_exit():
    print(USAGE, file=sys.stderr)
    sys.exit(64)


# ---------------------------------------------------------------- parsing

def parse_date(text):
    s = text.strip()
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(text):
    """Return amount in cents (positive int) or None."""
    s = text.strip()
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    whole = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    cents = whole * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(text):
    """Return list of (name, weight Fraction) or None if invalid."""
    entries = []
    seen = set()
    for part in text.split(";"):
        part = part.strip()
        if part == "":
            continue
        name, sep, wtext = part.partition(":")
        name = name.strip()
        if not name:
            return None
        if sep:
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            weight = Fraction(1)
        if name in seen:
            return None
        seen.add(name)
        entries.append((name, weight))
    return entries or None


def split_cents(total, entries, payer=None):
    """Split total cents among (name, weight) entries; cents sum exactly.

    If the payer is among the entries and there are leftover cents, the payer
    receives the first leftover cent. The remaining leftover cents go to the
    largest fractional remainders (ties by name ascending, ignoring case),
    skipping the payer.
    """
    wsum = sum(w for _, w in entries)
    exacts = [Fraction(total) * w / wsum for _, w in entries]
    bases = [int(x) for x in exacts]  # non-negative, so int() == floor
    leftover = total - sum(bases)
    payer_idx = None
    for i, (name, _) in enumerate(entries):
        if payer is not None and name == payer:
            payer_idx = i
            break
    if payer_idx is not None and leftover > 0:
        bases[payer_idx] += 1
        leftover -= 1
    others = [i for i in range(len(entries)) if i != payer_idx]
    order = sorted(others,
                   key=lambda i: (-(exacts[i] - bases[i]),
                                  entries[i][0].lower(), entries[i][0]))
    for i in order[:leftover]:
        bases[i] += 1
    return [(entries[i][0], bases[i]) for i in range(len(entries))]


def read_rows(text):
    """Yield (end_line_number, row) for each CSV record.

    Blank lines and lines starting with '#' are skipped entirely.
    """
    counter = [0]

    def physical_lines():
        for line in io.StringIO(text, newline=""):
            counter[0] += 1
            s = line.strip()
            if s == "" or s.startswith("#"):
                continue
            yield line

    reader = csv.reader(physical_lines())
    for row in reader:
        yield counter[0], row


# ---------------------------------------------------------------- logic

def compute_balances(valid_rows):
    bal = {}
    for _d, payer, cents, parts, _ln, kind in valid_rows:
        if kind == "payment":
            bal[payer] = bal.get(payer, 0) + cents
            recipient = parts[0][0]
            bal[recipient] = bal.get(recipient, 0) - cents
            continue
        bal[payer] = bal.get(payer, 0) + cents
        for name, share in split_cents(cents, parts, payer):
            bal[name] = bal.get(name, 0) - share
    return bal


def compute_transfers(bal):
    debtors = sorted([[n, -b] for n, b in bal.items() if b < 0],
                     key=lambda x: (-x[1], x[0]))
    creditors = sorted([[n, b] for n, b in bal.items() if b > 0],
                       key=lambda x: (-x[1], x[0]))
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d = debtors[i]
        c = creditors[j]
        amt = min(d[1], c[1])
        transfers.append((d[0], c[0], amt))
        d[1] -= amt
        c[1] -= amt
        if d[1] == 0:
            i += 1
        if c[1] == 0:
            j += 1
    return transfers


def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


# ---------------------------------------------------------------- main

def main(argv):
    if not argv or argv[0] not in COMMANDS:
        usage_exit()
    command = argv[0]

    fmt = "text"
    since = None
    until = None
    positional = []

    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            opt, _, val = a.partition("=")
            i += 1
        elif a in ("--format", "--since", "--until"):
            if i + 1 >= len(argv):
                usage_exit()
            opt, val = a, argv[i + 1]
            i += 2
        elif a.startswith("-") and a != "-":
            usage_exit()
        else:
            positional.append(a)
            i += 1
            continue

        if opt == "--format":
            if val not in FORMATS:
                usage_exit()
            fmt = val
        elif opt in ("--since", "--until"):
            d = parse_date(val)
            if d is None:
                usage_exit()
            if opt == "--since":
                since = d
            else:
                until = d
        else:
            usage_exit()

    if len(positional) != 1:
        usage_exit()
    path = positional[0]

    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        print(f"error: cannot read {path}", file=sys.stderr)
        return 3

    header = None
    index = {}
    valid_rows = []
    errors = []

    try:
        for lineno, row in read_rows(text):
            if header is None:
                header = [c.strip().lower() for c in row]
                missing = [c for c in REQUIRED if c not in header]
                if missing:
                    print(f"header: missing {missing[0]}", file=sys.stderr)
                    return 2
                index = {c: header.index(c) for c in REQUIRED}
                if "kind" in header:
                    index["kind"] = header.index("kind")
                continue

            def get(name):
                if name not in index:
                    return ""
                k = index[name]
                return row[k] if k < len(row) else ""

            reasons = []
            d = parse_date(get("date"))
            if d is None:
                reasons.append("bad-date")
            kind_raw = get("kind").strip().lower()
            if kind_raw in ("", "expense"):
                kind = "expense"
            elif kind_raw == "payment":
                kind = "payment"
            else:
                kind = None
                reasons.append("bad-kind")
            payer = get("payer").strip()
            if not payer:
                reasons.append("bad-payer")
            cents = parse_amount(get("amount"))
            if cents is None:
                reasons.append("bad-amount")

            parts_raw = get("participants")
            parts = parse_participants(parts_raw)
            if kind == "payment":
                ok = (parts is not None and len(parts) == 1
                      and ":" not in parts_raw
                      and parts[0][0] != payer)
                if not ok:
                    parts = None
                    reasons.append("bad-participants")
            elif parts is None:
                reasons.append("bad-participants")

            if reasons:
                for r in reasons:
                    errors.append((lineno, r))
            else:
                valid_rows.append((d, payer, cents, parts, lineno, kind))
    except csv.Error:
        print(f"error: cannot read {path}", file=sys.stderr)
        return 3

    if header is None:
        print("header: missing date", file=sys.stderr)
        return 2

    if errors:
        for lineno, reason in errors:
            print(f"line {lineno}: {reason}", file=sys.stderr)
        return 2

    rows = [r for r in valid_rows
            if (since is None or r[0] >= since)
            and (until is None or r[0] <= until)]

    bal = compute_balances(rows)

    if command == "balances":
        names = sorted(bal)
        if fmt == "json":
            out = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {fmt_signed(bal[n])}")
    else:
        transfers = compute_transfers(bal)
        if fmt == "json":
            out = {"transfers": [{"from": f, "to": t, "cents": c}
                                 for f, t, c in transfers]}
            print(json.dumps(out))
        else:
            if not transfers:
                print("nothing to settle")
            for f, t, c in transfers:
                print(f"{f} -> {t} {fmt_plain(c)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
