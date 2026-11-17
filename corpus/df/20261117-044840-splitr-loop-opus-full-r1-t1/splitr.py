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
INT_RE = re.compile(r"^[0-9]+$")


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
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    only_pos = False
    while i < len(argv):
        a = argv[i]
        if only_pos:
            positional.append(a)
        elif a == "--":
            only_pos = True
        elif a.startswith("--"):
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name, val = a, None
            key = name[2:]
            if key not in opts:
                raise UsageError("unknown option: %s" % name)
            if val is None:
                i += 1
                if i >= len(argv):
                    raise UsageError("missing value for %s" % name)
                val = argv[i]
            if key == "format":
                if val not in ("text", "json"):
                    raise UsageError("bad --format value: %s" % val)
                opts["format"] = val
            else:
                d = parse_date(val) if val.strip() == val else None
                if d is None:
                    raise UsageError("bad --%s date: %s" % (key, val))
                opts[key] = d
        elif a.startswith("-") and a != "-":
            raise UsageError("unknown option: %s" % a)
        else:
            positional.append(a)
        i += 1
    if not positional:
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
    return rows[0] if rows else []


def validate_row(fields, idx):
    def get(col):
        j = idx[col]
        return fields[j] if j < len(fields) else ""

    date = parse_date(get("date"))
    if date is None:
        return "bad-date", None
    payer = get("payer").strip()
    if not payer:
        return "bad-payer", None
    m = AMOUNT_RE.match(get("amount").strip())
    if not m:
        return "bad-amount", None
    whole, frac = m.group(1), m.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")
    if cents <= 0:
        return "bad-amount", None
    raw = get("participants")
    if raw.strip() == "":
        return "bad-participants", None
    parts = []
    seen = set()
    for item in raw.split(";"):
        item = item.strip()
        if ":" in item:
            name, _, w = item.rpartition(":")
            name = name.strip()
            w = w.strip()
            if not INT_RE.match(w) or int(w) <= 0:
                return "bad-participants", None
            weight = int(w)
        else:
            name, weight = item, 1
        if not name:
            return "bad-participants", None
        key = name.casefold()
        if key in seen:
            return "bad-participants", None
        seen.add(key)
        parts.append((name, weight))
    return None, (date, payer, cents, parts)


def split(cents, parts):
    """parts: list of (key, weight). Returns dict key->share."""
    total_w = sum(w for _, w in parts)
    shares = {}
    rems = []
    for key, w in parts:
        shares[key] = cents * w // total_w
        rems.append(((cents * w) % total_w, key))
    left = cents - sum(shares.values())
    rems.sort(key=lambda t: (-t[0], t[1]))
    for i in range(left):
        shares[rems[i][1]] += 1
    return shares


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
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
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

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [l[:-1] if l.endswith("\r") else l for l in lines]

    # header
    header = None
    pos = 0
    while pos < len(lines):
        if not is_ignored(lines[pos]):
            header = parse_csv_line(lines[pos]) or []
            pos += 1
            break
        pos += 1
    if header is None:
        header = []
    names = [h.strip().lower() for h in header]
    idx = {}
    for col in REQUIRED:
        if col not in names:
            sys.stderr.write("header: missing %s\n" % col)
            return 2
        idx[col] = names.index(col)

    errors = []
    rows = []
    for n in range(pos, len(lines)):
        line = lines[n]
        if is_ignored(line):
            continue
        fields = parse_csv_line(line)
        if fields is None:
            errors.append("line %d: bad-date" % (n + 1))
            continue
        reason, row = validate_row(fields, idx)
        if reason:
            errors.append("line %d: %s" % (n + 1, reason))
        else:
            rows.append(row)
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2

    display = {}

    def note(name):
        k = name.casefold()
        if k not in display:
            display[k] = name
        return k

    for date, payer, cents, parts in rows:
        note(payer)
        for name, _ in parts:
            note(name)

    balances = {}
    since, until = opts["since"], opts["until"]
    for date, payer, cents, parts in rows:
        if since is not None and date < since:
            continue
        if until is not None and date > until:
            continue
        pk = payer.casefold()
        balances[pk] = balances.get(pk, 0) + cents
        shares = split(cents, [(n.casefold(), w) for n, w in parts])
        for k, s in shares.items():
            balances[k] = balances.get(k, 0) - s

    order = sorted(balances, key=lambda k: (k, display[k]))

    if cmd == "balances":
        if opts["format"] == "json":
            doc = {"balances": [{"name": display[k], "cents": balances[k]} for k in order]}
            sys.stdout.write(json.dumps(doc, ensure_ascii=False) + "\n")
        else:
            for k in order:
                sys.stdout.write("%s %s\n" % (display[k], fmt_signed(balances[k])))
        return 0

    bal = dict(balances)
    transfers = []
    while True:
        debtors = [k for k in bal if bal[k] < 0]
        creditors = [k for k in bal if bal[k] > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda k: (bal[k], k))
        c = min(creditors, key=lambda k: (-bal[k], k))
        amt = min(-bal[d], bal[c])
        bal[d] += amt
        bal[c] -= amt
        transfers.append((d, c, amt))
    if opts["format"] == "json":
        doc = {"transfers": [{"from": display[d], "to": display[c], "cents": a}
                             for d, c, a in transfers]}
        sys.stdout.write(json.dumps(doc, ensure_ascii=False) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        for d, c, a in transfers:
            sys.stdout.write("%s -> %s %s\n" % (display[d], display[c], fmt_unsigned(a)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
