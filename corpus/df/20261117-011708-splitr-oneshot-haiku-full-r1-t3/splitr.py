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

USAGE = ("usage: splitr.py {balances,settle} LEDGER "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


def err(msg):
    sys.stderr.write(msg + "\n")


def usage_error(msg):
    err(f"error: {msg}")
    err(USAGE)
    return 64


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_amount(s):
    """Return cents (> 0) or None if invalid."""
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    whole = int(m.group(1))
    frac = (m.group(2) or "").ljust(2, "0")
    cents = whole * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(s):
    """Return list of (name, weight) or None if invalid."""
    out = []
    seen = set()
    for tok in s.split(";"):
        tok = tok.strip()
        if ":" in tok:
            name, _, w = tok.rpartition(":")
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.fullmatch(w) or int(w) == 0:
                return None
            weight = int(w)
        else:
            name = tok
            weight = 1
        if not name:
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        out.append((name, weight))
    return out


def split_shares(cents, plist):
    """plist: list of (key, weight). Returns list of (key, share_cents)."""
    total_w = sum(w for _, w in plist)
    base = []
    rems = []
    for _, w in plist:
        base.append(cents * w // total_w)
        rems.append(cents * w % total_w)
    left = cents - sum(base)
    order = sorted(range(len(plist)), key=lambda i: (-rems[i], plist[i][0]))
    for i in order[:left]:
        base[i] += 1
    return [(plist[i][0], base[i]) for i in range(len(plist))]


def fmt_signed(c):
    if c == 0:
        sign = ""
    elif c > 0:
        sign = "+"
    else:
        sign = "-"
    a = abs(c)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(c):
    a = abs(c)
    return f"{a // 100}.{a % 100:02d}"


def main(argv):
    opts = {}
    positional = []
    only_pos = False
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if only_pos or a == "-" or not a.startswith("-"):
            positional.append(a)
            continue
        if a == "--":
            only_pos = True
            continue
        if not a.startswith("--"):
            return usage_error(f"unknown option: {a}")
        name, eq, val = a.partition("=")
        if name not in ("--format", "--since", "--until"):
            return usage_error(f"unknown option: {name}")
        if not eq:
            if i >= len(argv):
                return usage_error(f"option {name} requires a value")
            val = argv[i]
            i += 1
        opts[name] = val

    if len(positional) != 2:
        return usage_error("expected a command and a ledger file")
    command, path = positional
    if command not in ("balances", "settle"):
        return usage_error(f"unknown command: {command}")
    fmt = opts.get("--format", "text")
    if fmt not in ("text", "json"):
        return usage_error(f"bad --format value: {fmt}")
    since = until = None
    if "--since" in opts:
        since = parse_date(opts["--since"])
        if since is None:
            return usage_error(f"bad --since date: {opts['--since']}")
    if "--until" in opts:
        until = parse_date(opts["--until"])
        if until is None:
            return usage_error(f"bad --until date: {opts['--until']}")

    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        err(f"error: cannot read {path}")
        return 3

    header = None
    idx = {}
    rows = []     # (date, payer, cents, [(name, weight)])
    errors = []   # (lineno, reason)
    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            fields = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            fields = line.split(",")
        if header is None:
            header = [f.strip().lower() for f in fields]
            for col in REQUIRED:
                if col not in header:
                    err(f"header: missing {col}")
                    return 2
            for j, h in enumerate(header):
                idx.setdefault(h, j)
            continue

        def get(col):
            j = idx[col]
            return fields[j].strip() if j < len(fields) else ""

        d = parse_date(get("date"))
        if d is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = get("payer")
        if not payer:
            errors.append((lineno, "bad-payer"))
            continue
        cents = parse_amount(get("amount"))
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        rows.append((d, payer, cents, parts))

    if header is None:
        err("header: missing date")
        return 2

    if errors:
        for lineno, reason in errors:
            err(f"line {lineno}: {reason}")
        return 2

    # Display spelling: first appearance in the whole file.
    display = {}
    for _, payer, _, parts in rows:
        display.setdefault(payer.casefold(), payer)
        for name, _ in parts:
            display.setdefault(name.casefold(), name)

    bal = {}
    for d, payer, cents, parts in rows:
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        pk = payer.casefold()
        bal[pk] = bal.get(pk, 0) + cents
        plist = [(name.casefold(), w) for name, w in parts]
        for k, _ in plist:
            bal.setdefault(k, 0)
        for k, share in split_shares(cents, plist):
            bal[k] -= share

    keys = sorted(bal)

    if command == "balances":
        if fmt == "json":
            doc = {"balances": [{"name": display[k], "cents": bal[k]} for k in keys]}
            sys.stdout.write(json.dumps(doc) + "\n")
        else:
            for k in keys:
                sys.stdout.write(f"{display[k]} {fmt_signed(bal[k])}\n")
        return 0

    # settle
    work = dict(bal)
    transfers = []
    while True:
        nz = [k for k in keys if work[k] != 0]
        if not nz:
            break
        debtor = min(nz, key=lambda k: (work[k], k))
        creditor = min(nz, key=lambda k: (-work[k], k))
        if work[debtor] >= 0 or work[creditor] <= 0:
            break
        amt = min(-work[debtor], work[creditor])
        work[debtor] += amt
        work[creditor] -= amt
        transfers.append((debtor, creditor, amt))

    if fmt == "json":
        doc = {"transfers": [{"from": display[d], "to": display[c], "cents": amt}
                             for d, c, amt in transfers]}
        sys.stdout.write(json.dumps(doc) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        for d, c, amt in transfers:
            sys.stdout.write(f"{display[d]} -> {display[c]} {fmt_plain(amt)}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
