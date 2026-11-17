#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
       [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


def usage_error(msg):
    sys.stderr.write("usage error: %s\n" % msg)
    sys.exit(EXIT_USAGE)


def parse_date(s):
    """Return a datetime.date for a strict YYYY-MM-DD string, or None."""
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    """Return cents (> 0) or None."""
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    frac = (m.group(2) or "").ljust(2, "0")
    cents = int(m.group(1)) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    """Return list of (name, key, weight) or None if invalid."""
    if s.strip() == "":
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        name, sep, wtext = part.partition(":")
        name = name.strip()
        if name == "":
            return None
        if sep:
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight <= 0:
                return None
        else:
            weight = 1
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, key, weight))
    return result


def split_expense(cents, parts):
    """parts: list of (name, key, weight). Returns dict key -> share cents."""
    total_w = sum(w for _, _, w in parts)
    bases = []
    rems = []
    for _, _, w in parts:
        num = cents * w
        bases.append(num // total_w)
        rems.append(num % total_w)
    leftover = cents - sum(bases)
    order = sorted(range(len(parts)), key=lambda i: (-rems[i], parts[i][1]))
    shares = [b for b in bases]
    for i in order[:leftover]:
        shares[i] += 1
    return {parts[i][1]: shares[i] for i in range(len(parts))}


def signed(c):
    if c == 0:
        sign = ""
    elif c > 0:
        sign = "+"
    else:
        sign = "-"
    a = abs(c)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def unsigned(c):
    a = abs(c)
    return "%d.%02d" % (a // 100, a % 100)


def fail_read(path):
    sys.stderr.write("error: cannot read %s\n" % path)
    sys.exit(EXIT_READ)


def main(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--format", "--since", "--until") or a.startswith(("--format=", "--since=", "--until=")):
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name = a
                i += 1
                if i >= len(argv):
                    usage_error("option %s requires a value" % name)
                val = argv[i]
            if name == "--format":
                if val not in FORMATS:
                    usage_error("bad --format value: %r" % val)
                opts["format"] = val
            else:
                if parse_date(val) is None:
                    usage_error("bad date for %s: %r" % (name, val))
                opts[name[2:]] = parse_date(val)
        elif a.startswith("-") and a != "-":
            usage_error("unknown option: %s" % a)
        else:
            positional.append(a)
        i += 1

    if len(positional) != 2:
        usage_error("expected <command> <ledger.csv>")
    command, path = positional
    if command not in COMMANDS:
        usage_error("unknown command: %s" % command)

    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        fail_read(path)

    header = None
    col = {}
    rows = []
    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        fields = next(csv.reader([line]))
        if header is None:
            header = [f.strip().lower() for f in fields]
            for name in REQUIRED_COLUMNS:
                if name not in header:
                    sys.stderr.write("header: missing %s\n" % name)
                    sys.exit(EXIT_INVALID)
            col = {name: header.index(name) for name in REQUIRED_COLUMNS}
            continue
        rows.append((lineno, fields))

    def field(fields, name):
        idx = col[name]
        return fields[idx] if idx < len(fields) else ""

    display = {}  # key -> first-appearance spelling
    errors = []
    valid = []  # (date, payer_name, payer_key, parts, cents)
    for lineno, fields in rows:
        date_s = field(fields, "date").strip()
        d = parse_date(date_s)
        if d is None:
            errors.append("line %d: bad-date" % lineno)
            continue
        payer = field(fields, "payer").strip()
        if payer == "":
            errors.append("line %d: bad-payer" % lineno)
            continue
        cents = parse_amount(field(fields, "amount").strip())
        if cents is None:
            errors.append("line %d: bad-amount" % lineno)
            continue
        parts = parse_participants(field(fields, "participants"))
        if parts is None:
            errors.append("line %d: bad-participants" % lineno)
            continue
        valid.append((lineno, d, payer, parts, cents))

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        sys.exit(EXIT_INVALID)

    # Display names: first appearance in file order, payer before participants.
    for _, _, payer, parts, _ in valid:
        display.setdefault(payer.casefold(), payer)
        for name, key, _ in parts:
            display.setdefault(key, name)

    balances = {}
    considered = set()
    for _, d, payer, parts, cents in valid:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        pkey = payer.casefold()
        considered.add(pkey)
        balances.setdefault(pkey, 0)
        balances[pkey] += cents
        for _, key, _ in parts:
            considered.add(key)
            balances.setdefault(key, 0)
        shares = split_expense(cents, parts)
        for key, share in shares.items():
            balances[key] -= share

    keys = sorted(considered)

    if command == "balances":
        if opts["format"] == "json":
            obj = {"balances": [{"name": display[k], "cents": balances[k]} for k in keys]}
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        else:
            out = "".join("%s %s\n" % (display[k], signed(balances[k])) for k in keys)
            sys.stdout.write(out)
        return EXIT_OK

    # settle
    bal = dict(balances)
    transfers = []
    while any(bal[k] != 0 for k in keys):
        debtor = min(keys, key=lambda k: (bal[k], k))
        creditor = min(keys, key=lambda k: (-bal[k], k))
        amt = min(-bal[debtor], bal[creditor])
        bal[debtor] += amt
        bal[creditor] -= amt
        transfers.append((debtor, creditor, amt))

    if opts["format"] == "json":
        obj = {"transfers": [
            {"from": display[d], "to": display[c], "cents": amt} for d, c, amt in transfers]}
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            for d, c, amt in transfers:
                sys.stdout.write("%s -> %s %s\n" % (display[d], display[c], unsigned(amt)))
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
