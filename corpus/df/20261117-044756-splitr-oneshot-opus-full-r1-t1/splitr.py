#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
AMOUNT_RE = re.compile(r"^([0-9]+)(?:\.([0-9]{1,2}))?$")
WEIGHT_RE = re.compile(r"^[0-9]+$")


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
    if s.strip() == "":
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        if ":" in part:
            name, _, w = part.rpartition(":")
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = int(w)
            if weight <= 0:
                return None
        else:
            name = part
            weight = 1
        if name == "":
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, weight))
    return result


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and len(a) > 1:
            if a.startswith("--") and "=" in a:
                name, _, val = a.partition("=")
                has_val = True
            else:
                name, val, has_val = a, None, False
            if name not in ("--format", "--since", "--until"):
                raise UsageError("unknown option: %s" % a)
            if not has_val:
                if i + 1 >= len(argv):
                    raise UsageError("missing value for %s" % name)
                val = argv[i + 1]
                i += 1
            key = name[2:]
            if key == "format":
                if val not in ("text", "json"):
                    raise UsageError("bad --format value: %s" % val)
                opts["format"] = val
            else:
                if not DATE_RE.match(val):
                    raise UsageError("bad date for %s: %s" % (name, val))
                try:
                    d = datetime.date.fromisoformat(val)
                except ValueError:
                    raise UsageError("bad date for %s: %s" % (name, val))
                opts[key] = d
        else:
            positional.append(a)
        i += 1
    if len(positional) < 1:
        raise UsageError("missing command")
    cmd = positional[0]
    if cmd not in ("balances", "settle"):
        raise UsageError("unknown command: %s" % cmd)
    if len(positional) < 2:
        raise UsageError("missing ledger path")
    if len(positional) > 2:
        raise UsageError("unexpected argument: %s" % positional[2])
    return cmd, positional[1], opts


def is_ignored(line):
    s = line.strip()
    return s == "" or s.startswith("#")


def parse_csv_line(line):
    try:
        rows = list(csv.reader([line]))
    except csv.Error:
        return None
    if not rows:
        return []
    return rows[0]


def fmt_signed(c):
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    a = abs(c)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def fmt_unsigned(c):
    return "%d.%02d" % (c // 100, c % 100)


def main(argv):
    try:
        cmd, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("usage error: %s\n" % e)
        sys.stderr.write("usage: python splitr.py balances|settle <ledger.csv> "
                         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
        return 64

    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError, ValueError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    raw_lines = text.split("\n")
    lines = [l[:-1] if l.endswith("\r") else l for l in raw_lines]

    # Find header: first non-ignored line.
    header = None
    idx = 0
    while idx < len(lines):
        if not is_ignored(lines[idx]):
            header = parse_csv_line(lines[idx]) or []
            idx += 1
            break
        idx += 1
    if header is None:
        header = []
    colmap = {}
    for i, h in enumerate(header):
        k = h.strip().lower()
        if k not in colmap:
            colmap[k] = i
    for col in REQUIRED:
        if col not in colmap:
            sys.stderr.write("header: missing %s\n" % col)
            return 2

    errors = []
    expenses = []  # (date, payer, cents, participants)
    for li in range(idx, len(lines)):
        line = lines[li]
        if is_ignored(line):
            continue
        lineno = li + 1
        fields = parse_csv_line(line)
        if fields is None:
            errors.append("line %d: bad-date" % lineno)
            continue

        def get(col):
            i = colmap[col]
            return fields[i] if i < len(fields) else ""

        d = parse_date(get("date"))
        if d is None:
            errors.append("line %d: bad-date" % lineno)
            continue
        payer = get("payer").strip()
        if payer == "":
            errors.append("line %d: bad-payer" % lineno)
            continue
        cents = parse_amount(get("amount"))
        if cents is None:
            errors.append("line %d: bad-amount" % lineno)
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append("line %d: bad-participants" % lineno)
            continue
        expenses.append((d, payer, cents, parts))

    if errors:
        sys.stderr.write("".join(e + "\n" for e in errors))
        return 2

    # Display names: first appearance in file.
    display = {}
    for d, payer, cents, parts in expenses:
        for name in [payer] + [n for n, _ in parts]:
            k = name.casefold()
            if k not in display:
                display[k] = name

    balances = {}
    since, until = opts["since"], opts["until"]
    for d, payer, cents, parts in expenses:
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        pk = payer.casefold()
        balances[pk] = balances.get(pk, 0) + cents
        W = sum(w for _, w in parts)
        shares = []
        for name, w in parts:
            k = name.casefold()
            base = (cents * w) // W
            rem = (cents * w) % W
            shares.append([k, base, rem])
        leftover = cents - sum(s[1] for s in shares)
        order = sorted(range(len(shares)),
                       key=lambda i: (-shares[i][2], display[shares[i][0]].casefold(), shares[i][0]))
        for j in range(leftover):
            shares[order[j % len(order)]][1] += 1
        for k, base, _ in shares:
            balances[k] = balances.get(k, 0) - base

    def sort_key(k):
        return (display[k].casefold(), display[k])

    fmt = opts["format"]
    out = []
    if cmd == "balances":
        keys = sorted(balances, key=sort_key)
        if fmt == "json":
            doc = {"balances": [{"name": display[k], "cents": balances[k]} for k in keys]}
            out.append(json.dumps(doc, ensure_ascii=False))
        else:
            for k in keys:
                out.append("%s %s" % (display[k], fmt_signed(balances[k])))
    else:
        bal = dict(balances)
        transfers = []
        while True:
            debtors = [k for k, v in bal.items() if v < 0]
            creditors = [k for k, v in bal.items() if v > 0]
            if not debtors or not creditors:
                break
            dk = min(debtors, key=lambda k: (bal[k], sort_key(k)))
            ck = min(creditors, key=lambda k: (-bal[k], sort_key(k)))
            amt = min(-bal[dk], bal[ck])
            bal[dk] += amt
            bal[ck] -= amt
            transfers.append((dk, ck, amt))
        if fmt == "json":
            doc = {"transfers": [{"from": display[a], "to": display[b], "cents": c}
                                 for a, b, c in transfers]}
            out.append(json.dumps(doc, ensure_ascii=False))
        else:
            if not transfers:
                out.append("nothing to settle")
            for a, b, c in transfers:
                out.append("%s -> %s %s" % (display[a], display[b], fmt_unsigned(c)))

    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
