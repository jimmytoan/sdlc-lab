#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys

USAGE = ("usage: python splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]{1,2})?|\.[0-9]{1,2})")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


def parse_date(s):
    s = s.strip()
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    s = s.strip()
    if not AMOUNT_RE.fullmatch(s):
        return None
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    cents = int(whole or "0") * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    if s is None or not s.strip():
        return None
    result = []
    seen = set()
    for entry in s.split(";"):
        if ":" in entry:
            name, weight = entry.split(":", 1)
            weight = weight.strip()
            if not WEIGHT_RE.fullmatch(weight):
                return None
            w = int(weight)
            if w <= 0:
                return None
        else:
            name, w = entry, 1
        name = name.strip()
        if not name:
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, w))
    return result


def parse_args(argv):
    if len(argv) < 1:
        raise UsageError("missing command")
    opts = {"format": "text", "since": None, "until": None}
    positionals = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and len(a) > 2:
            if "=" in a:
                name, val = a[2:].split("=", 1)
            else:
                name = a[2:]
                if name not in opts:
                    raise UsageError("unknown option " + a)
                if i + 1 >= len(argv):
                    raise UsageError("missing value for " + a)
                i += 1
                val = argv[i]
            if name not in opts:
                raise UsageError("unknown option --" + name)
            if name == "format":
                if val not in ("text", "json"):
                    raise UsageError("bad format " + val)
                opts["format"] = val
            else:
                d = parse_date(val)
                if d is None or val != val.strip():
                    raise UsageError("bad date for --" + name)
                opts[name] = d
        elif a.startswith("-") and a != "-":
            raise UsageError("unknown option " + a)
        else:
            positionals.append(a)
        i += 1
    if len(positionals) != 2:
        raise UsageError("expected <command> <ledger.csv>")
    cmd, path = positionals
    if cmd not in ("balances", "settle"):
        raise UsageError("unknown command " + cmd)
    return cmd, path, opts


def is_ignored(line):
    s = line.strip()
    return s == "" or line.lstrip().startswith("#")


def load(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError, ValueError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)
    return text


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    try:
        cmd, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s\n" % (e, USAGE))
        return 64

    text = load(path)
    lines = text.splitlines()

    header = None
    errors = []
    rows = []  # (date, payer, cents, participants)
    for idx, line in enumerate(lines, start=1):
        if is_ignored(line):
            continue
        fields = next(csv.reader([line]))
        if header is None:
            header = {}
            for pos, name in enumerate(fields):
                key = name.strip().lower()
                if key not in header:
                    header[key] = pos
            for col in REQUIRED:
                if col not in header:
                    sys.stderr.write("header: missing %s\n" % col)
                    return 2
            continue

        def get(col):
            pos = header[col]
            return fields[pos] if pos < len(fields) else None

        raw_date = get("date")
        d = parse_date(raw_date) if raw_date is not None else None
        if d is None:
            errors.append((idx, "bad-date"))
            continue
        payer = (get("payer") or "").strip()
        if not payer:
            errors.append((idx, "bad-payer"))
            continue
        raw_amount = get("amount")
        cents = parse_amount(raw_amount) if raw_amount is not None else None
        if cents is None:
            errors.append((idx, "bad-amount"))
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((idx, "bad-participants"))
            continue
        rows.append((d, payer, cents, parts))

    if header is None:
        sys.stderr.write("header: missing date\n")
        return 2
    if errors:
        for n, reason in errors:
            sys.stderr.write("line %d: %s\n" % (n, reason))
        return 2

    balances = {}
    display = {}

    def touch(name):
        key = name.casefold()
        if key not in display:
            display[key] = name
            balances[key] = 0
        return key

    for d, payer, cents, parts in rows:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        pk = touch(payer)
        balances[pk] += cents
        total_w = sum(w for _, w in parts)
        shares = []
        for name, w in parts:
            k = touch(name)
            base, rem = divmod(cents * w, total_w)
            shares.append([k, base, rem])
        leftover = cents - sum(s[1] for s in shares)
        order = sorted(shares, key=lambda s: (-s[2], s[0], display[s[0]]))
        for s in order[:leftover]:
            s[1] += 1
        for k, share, _ in shares:
            balances[k] -= share

    def sort_key(k):
        return (k, display[k])

    keys = sorted(balances, key=sort_key)

    if cmd == "balances":
        if opts["format"] == "json":
            out = {"balances": [{"name": display[k], "cents": balances[k]} for k in keys]}
            print(json.dumps(out))
        else:
            for k in keys:
                print("%s %s" % (display[k], fmt_signed(balances[k])))
        return 0

    # settle
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [k for k in bal if bal[k] < 0]
        creditors = [k for k in bal if bal[k] > 0]
        if not debtors or not creditors:
            break
        dk = min(debtors, key=lambda k: (bal[k], sort_key(k)))
        ck = min(creditors, key=lambda k: (-bal[k], sort_key(k)))
        amt = min(-bal[dk], bal[ck])
        bal[dk] += amt
        bal[ck] -= amt
        transfers.append((dk, ck, amt))
    if opts["format"] == "json":
        out = {"transfers": [{"from": display[a], "to": display[b], "cents": c}
                             for a, b, c in transfers]}
        print(json.dumps(out))
    else:
        if not transfers:
            print("nothing to settle")
        for a, b, c in transfers:
            print("%s -> %s %s" % (display[a], display[b], fmt_abs(c)))
    return 0


def fmt_abs(c):
    return "%d.%02d" % (c // 100, c % 100)


def fmt_signed(c):
    if c == 0:
        return "0.00"
    return ("+" if c > 0 else "-") + fmt_abs(abs(c))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
