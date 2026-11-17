#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")
WEIGHT_RE = re.compile(r"^\d+$")

USAGE = ("usage: python splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class UsageError(Exception):
    pass


def parse_date(s):
    s = s.strip()
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    command = None
    path = None
    fmt = "text"
    since = until = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                key, val = a.split("=", 1)
            else:
                key = a
                if key not in ("--format", "--since", "--until"):
                    raise UsageError(f"unknown option: {a}")
                if i + 1 >= len(argv):
                    raise UsageError(f"option {a} requires a value")
                i += 1
                val = argv[i]
            if key == "--format":
                if val not in ("text", "json"):
                    raise UsageError(f"invalid --format value: {val}")
                fmt = val
            elif key in ("--since", "--until"):
                d = parse_date(val)
                if d is None or val != val.strip():
                    raise UsageError(f"invalid {key} date: {val}")
                if key == "--since":
                    since = d
                else:
                    until = d
            else:
                raise UsageError(f"unknown option: {key}")
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1
    if not positional:
        raise UsageError("missing command")
    command = positional[0]
    if command not in ("balances", "settle"):
        raise UsageError(f"unknown command: {command}")
    if len(positional) < 2:
        raise UsageError("missing ledger argument")
    if len(positional) > 2:
        raise UsageError("too many arguments")
    path = positional[1]
    return command, path, fmt, since, until


def parse_amount(s):
    s = s.strip()
    m = AMOUNT_RE.match(s)
    if not m:
        return None
    frac = (m.group(2) or "").ljust(2, "0")
    cents = int(m.group(1)) * 100 + int(frac or "0")
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
        if ":" in part:
            name, w = part.split(":", 1)
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = int(w)
            if weight <= 0:
                return None
        else:
            name, weight = part, 1
        name = name.strip()
        if not name:
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, weight))
    return result


def split_cents(cents, parts):
    total_w = sum(w for _, w in parts)
    shares = []
    for name, w in parts:
        q, r = divmod(cents * w, total_w)
        shares.append([name, q, r])
    leftover = cents - sum(s[1] for s in shares)
    order = sorted(range(len(shares)),
                   key=lambda i: (-shares[i][2], shares[i][0].casefold(), shares[i][0]))
    for i in order[:leftover]:
        shares[i][1] += 1
    return [(s[0], s[1]) for s in shares]


def name_key(n):
    return (n.casefold(), n)


def fmt_cents(c, signed):
    sign = ""
    if c < 0:
        sign = "-"
    elif c > 0 and signed:
        sign = "+"
    a = abs(c)
    return f"{sign}{a // 100}.{a % 100:02d}"


def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
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

    raw_lines = text.splitlines()
    header = None
    rows = []  # (lineno, fields)
    for idx, line in enumerate(raw_lines, start=1):
        st = line.strip()
        if not st or st.startswith("#"):
            continue
        fields = next(csv.reader([line]), [])
        if header is None:
            header = [h.strip().lower() for h in fields]
            continue
        rows.append((idx, fields))

    if header is None:
        header = []
    for col in REQUIRED:
        if col not in header:
            print(f"header: missing {col}", file=sys.stderr)
            return 2
    col_idx = {c: header.index(c) for c in REQUIRED}

    errors = []
    expenses = []
    for lineno, fields in rows:
        def get(c):
            i = col_idx[c]
            return fields[i] if i < len(fields) else ""
        d = parse_date(get("date"))
        if d is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = get("payer").strip()
        if not payer:
            errors.append((lineno, "bad-payer"))
            continue
        amount = parse_amount(get("amount"))
        if amount is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        expenses.append((d, payer, amount, parts))

    if errors:
        for lineno, reason in errors:
            print(f"line {lineno}: {reason}", file=sys.stderr)
        return 2

    display = {}
    balances = {}

    def person(n):
        k = n.casefold()
        if k not in display:
            display[k] = n
            balances[k] = 0
        return k

    for d, payer, amount, parts in expenses:
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        balances[person(payer)] += amount
        for name, share in split_cents(amount, parts):
            balances[person(name)] -= share

    people = sorted(balances, key=lambda k: name_key(display[k]))

    if command == "balances":
        if fmt == "json":
            out = {"balances": [{"name": display[k], "cents": balances[k]} for k in people]}
            print(json.dumps(out))
        else:
            for k in people:
                print(f"{display[k]} {fmt_cents(balances[k], True)}")
        return 0

    # settle
    rem = dict(balances)
    transfers = []
    while True:
        debtors = [k for k in rem if rem[k] < 0]
        creditors = [k for k in rem if rem[k] > 0]
        if not debtors or not creditors:
            break
        dk = min(debtors, key=lambda k: (rem[k], name_key(display[k])))
        ck = min(creditors, key=lambda k: (-rem[k], name_key(display[k])))
        amt = min(-rem[dk], rem[ck])
        rem[dk] += amt
        rem[ck] -= amt
        transfers.append((display[dk], display[ck], amt))

    if fmt == "json":
        print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
    elif not transfers:
        print("nothing to settle")
    else:
        for a, b, c in transfers:
            print(f"{a} -> {b} {fmt_cents(c, False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
