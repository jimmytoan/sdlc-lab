#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys

USAGE = ("usage: splitr.py {balances,settle} <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED = ("date", "payer", "amount", "participants", "note")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


def usage_fail(msg=None):
    if msg:
        sys.stderr.write(f"error: {msg}\n")
    sys.stderr.write(USAGE + "\n")
    sys.exit(64)


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    whole = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    cents = whole * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    if s.strip() == "":
        return None
    result = []
    seen = set()
    for piece in s.split(";"):
        parts = piece.split(":")
        if len(parts) > 2:
            return None
        name = parts[0].strip()
        if not name:
            return None
        if len(parts) == 2:
            w = parts[1].strip()
            if not WEIGHT_RE.fullmatch(w):
                return None
            weight = int(w)
            if weight <= 0:
                return None
        else:
            weight = 1
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, weight))
    return result


def is_ignored(line):
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def fmt_signed(c):
    if c > 0:
        sign = "+"
    elif c < 0:
        sign = "-"
    else:
        sign = ""
    a = abs(c)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(c):
    a = abs(c)
    return f"{a // 100}.{a % 100:02d}"


def split_shares(cents, parts):
    """parts: list of (key, weight). Returns list of shares in same order."""
    total_w = sum(w for _, w in parts)
    bases = [cents * w // total_w for _, w in parts]
    rems = [(cents * w) % total_w for _, w in parts]
    leftover = cents - sum(bases)
    order = sorted(range(len(parts)), key=lambda i: (-rems[i], parts[i][0]))
    for i in order[:leftover]:
        bases[i] += 1
    return bases


def main(argv):
    positional = []
    fmt = "text"
    since = None
    until = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            positional.extend(argv[i + 1:])
            break
        if a in ("-h", "--help"):
            sys.stdout.write(USAGE + "\n")
            return 0
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name not in ("--format", "--since", "--until"):
                usage_fail(f"unknown option {name}")
            if not eq:
                if i + 1 >= len(argv):
                    usage_fail(f"option {name} requires a value")
                val = argv[i + 1]
                i += 1
            if name == "--format":
                fmt = val
            elif name == "--since":
                since = parse_date(val)
                if since is None:
                    usage_fail(f"bad --since date: {val}")
            else:
                until = parse_date(val)
                if until is None:
                    usage_fail(f"bad --until date: {val}")
        elif a.startswith("-") and a != "-":
            usage_fail(f"unknown option {a}")
        else:
            positional.append(a)
        i += 1

    if len(positional) != 2:
        usage_fail("expected a command and a ledger file")
    command, path = positional
    if command not in COMMANDS:
        usage_fail(f"unknown command {command}")
    if fmt not in FORMATS:
        usage_fail(f"bad --format value {fmt}")

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        return 3

    header = None
    cols = {}
    rows = []
    for lineno, raw_line in enumerate(text.split("\n"), 1):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if is_ignored(line):
            continue
        try:
            fields = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            fields = []
        if header is None:
            header = fields
            names = [h.strip().lower() for h in header]
            for col in REQUIRED:
                if col not in names:
                    sys.stderr.write(f"header: missing {col}\n")
                    return 2
            for col in REQUIRED:
                cols[col] = names.index(col)
            continue
        rows.append((lineno, fields))

    if header is None:
        sys.stderr.write("header: missing date\n")
        return 2

    display = {}

    def register(name):
        k = name.casefold()
        if k not in display:
            display[k] = name
        return k

    errors = []
    parsed = []
    for lineno, f in rows:
        def g(col):
            idx = cols[col]
            return f[idx] if idx < len(f) else ""

        date_s = g("date").strip()
        d = parse_date(date_s)
        if d is None:
            errors.append(f"line {lineno}: bad-date")
            continue
        payer = g("payer").strip()
        if not payer:
            errors.append(f"line {lineno}: bad-payer")
            continue
        cents = parse_amount(g("amount").strip())
        if cents is None:
            errors.append(f"line {lineno}: bad-amount")
            continue
        parts = parse_participants(g("participants"))
        if parts is None:
            errors.append(f"line {lineno}: bad-participants")
            continue
        payer_key = register(payer)
        part_keys = []
        for name, weight in parts:
            part_keys.append((register(name), weight))
        parsed.append((d, payer_key, cents, part_keys))

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return 2

    bal = {}
    present = set()
    for d, payer_key, cents, part_keys in parsed:
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        present.add(payer_key)
        for k, _ in part_keys:
            present.add(k)
        shares = split_shares(cents, part_keys)
        bal[payer_key] = bal.get(payer_key, 0) + cents
        for (k, _), share in zip(part_keys, shares):
            bal[k] = bal.get(k, 0) - share

    ordered = sorted(present)

    if command == "balances":
        out_rows = [(display[k], bal.get(k, 0)) for k in ordered]
        if fmt == "json":
            doc = {"balances": [{"name": n, "cents": c} for n, c in out_rows]}
            sys.stdout.write(json.dumps(doc) + "\n")
        else:
            out = "".join(f"{n} {fmt_signed(c)}\n" for n, c in out_rows)
            sys.stdout.write(out)
        return 0

    # settle
    nonzero = {k: bal[k] for k in ordered if bal.get(k, 0) != 0}
    transfers = []
    while nonzero:
        debtor = min(nonzero, key=lambda k: (nonzero[k], k))
        creditor = min(nonzero, key=lambda k: (-nonzero[k], k))
        amt = min(-nonzero[debtor], nonzero[creditor])
        transfers.append((display[debtor], display[creditor], amt))
        nonzero[debtor] += amt
        nonzero[creditor] -= amt
        nonzero = {k: v for k, v in nonzero.items() if v != 0}

    if fmt == "json":
        doc = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        sys.stdout.write(json.dumps(doc) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            out = "".join(f"{f} -> {t} {fmt_unsigned(c)}\n" for f, t, c in transfers)
            sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
