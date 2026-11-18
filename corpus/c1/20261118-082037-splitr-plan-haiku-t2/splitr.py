#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: splitr.py <balances|settle> <ledger.csv> [--format text|json]
                 [--since YYYY-MM-DD] [--until YYYY-MM-DD]

All money is integer cents. Exit codes: 0 ok, 2 bad ledger, 3 cannot read
file, 64 usage error.
"""
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_LEDGER = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)


def usage_exit(msg=None):
    if msg:
        sys.stderr.write(f"splitr: {msg}\n")
    sys.stderr.write(USAGE + "\n")
    sys.exit(EXIT_USAGE)


def parse_date_arg(value):
    """Return a date for a valid YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(value):
        return None
    try:
        return datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    positionals = []
    opts = {"format": "text", "since": None, "until": None}
    value_opts = {"--format": "format", "--since": "since", "--until": "until"}
    i = 0
    only_positional = False
    while i < len(argv):
        arg = argv[i]
        i += 1
        if only_positional or arg == "-" or not arg.startswith("-"):
            positionals.append(arg)
            continue
        if arg == "--":
            only_positional = True
            continue
        if arg in ("-h", "--help"):
            sys.stdout.write(USAGE + "\n")
            sys.exit(EXIT_OK)
        name, eq, inline = arg.partition("=")
        if name not in value_opts:
            usage_exit(f"unknown option: {arg}")
        if eq:
            value = inline
        else:
            if i >= len(argv):
                usage_exit(f"option {name} needs a value")
            value = argv[i]
            i += 1
        key = value_opts[name]
        if key == "format":
            if value not in FORMATS:
                usage_exit(f"bad --format value: {value}")
        else:
            if parse_date_arg(value) is None:
                usage_exit(f"bad {name} date: {value}")
            value = parse_date_arg(value)
        opts[key] = value
    if len(positionals) < 1:
        usage_exit("missing command")
    if len(positionals) < 2:
        usage_exit("missing ledger path")
    if len(positionals) > 2:
        usage_exit(f"unexpected argument: {positionals[2]}")
    command, path = positionals
    if command not in COMMANDS:
        usage_exit(f"unknown command: {command}")
    return command, path, opts


def read_text(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_CANNOT_READ)
    # universal newlines
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_records(text):
    """Return list of (line_number, fields) for each non-comment, non-blank record."""
    phys = text.split("\n")
    n = len(phys)
    records = []
    i = 0
    while i < n:
        line = phys[i]
        if line.startswith("#") or line.strip() == "":
            i += 1
            continue
        start = i + 1
        buf = line
        j = i
        # a quoted field may span physical lines; keep joining while quotes are open
        while buf.count('"') % 2 == 1 and j + 1 < n:
            j += 1
            buf += "\n" + phys[j]
        i = j + 1
        try:
            rows = list(csv.reader(io.StringIO(buf)))
            fields = rows[0] if rows else []
        except csv.Error:
            fields = buf.split(",")
        records.append((start, fields))
    return records


def check_row(fields, idx, header_len):
    """Validate one row. Return (reason or None, parsed) where parsed is
    (date_obj, payer, cents, [(name, weight), ...])."""
    def get(col):
        i = idx[col]
        return fields[i] if i < len(fields) else ""

    date_txt = get("date").strip()
    if not DATE_RE.fullmatch(date_txt):
        return "bad-date", None
    date_obj = parse_date_arg(date_txt)
    if date_obj is None:
        return "bad-date", None

    payer = get("payer").strip()
    if payer == "":
        return "bad-payer", None

    amt_txt = get("amount").strip()
    m = AMOUNT_RE.fullmatch(amt_txt)
    if not m:
        return "bad-amount", None
    whole = int(m.group(1))
    frac = m.group(2) or ""
    frac_cents = int(frac.ljust(2, "0")) if frac else 0
    cents = whole * 100 + frac_cents
    if cents <= 0:
        return "bad-amount", None

    if len(fields) > header_len:
        return "bad-participants", None
    parts = parse_participants(get("participants"))
    if parts is None:
        return "bad-participants", None

    return None, (date_obj, payer, cents, parts)



def parse_participants(text):
    if text.strip(" ;\t") == "":
        return None
    parts = []
    seen = set()
    for seg in text.split(";"):
        seg = seg.strip()
        if seg == "":
            return None
        if ":" in seg:
            name, _, wtxt = seg.partition(":")
            name = name.strip()
            wtxt = wtxt.strip()
            if name == "" or not WEIGHT_RE.fullmatch(wtxt):
                return None
            weight = int(wtxt)
            if weight < 1:
                return None
        else:
            name = seg
            weight = 1
        if name == "" or name in seen:
            return None
        seen.add(name)
        parts.append((name, weight))
    return parts


def split_expense(amount, parts):
    """Largest-remainder split of `amount` cents by weights. Returns {name: cents}."""
    total_w = sum(w for _, w in parts)
    base = {}
    rem = {}
    for name, w in parts:
        num = amount * w
        base[name] = num // total_w
        rem[name] = num % total_w
    leftover = amount - sum(base.values())
    assert 0 <= leftover < len(parts), "internal error: bad leftover"
    order = sorted((name for name, _ in parts), key=lambda nm: (-rem[nm], nm))
    for name in order[:leftover]:
        base[name] += 1
    assert sum(base.values()) == amount, "internal error: shares do not sum to amount"
    return base


def compute_balances(expenses):
    bal = {}
    for _date, payer, amount, parts in expenses:
        bal.setdefault(payer, 0)
        for name, _ in parts:
            bal.setdefault(name, 0)
        bal[payer] += amount
        shares = split_expense(amount, parts)
        for name, share in shares.items():
            bal[name] -= share
    assert sum(bal.values()) == 0, "internal error: balances do not sum to zero"
    return bal


def compute_settle(bal):
    cur = {n: b for n, b in bal.items() if b != 0}
    transfers = []
    while True:
        debtors = [(b, n) for n, b in cur.items() if b < 0]
        creditors = [(-b, n) for n, b in cur.items() if b > 0]
        if not debtors or not creditors:
            break
        db, dn = min(debtors)  # most negative; ties -> name first
        neg_cb, cn = min(creditors)  # most positive; ties -> name first
        cb = -neg_cb
        amt = min(-db, cb)
        cur[dn] += amt
        cur[cn] -= amt
        cur = {n: b for n, b in cur.items() if b != 0}
        transfers.append((dn, cn, amt))
    assert not cur, "internal error: settle did not clear balances"
    return transfers


def fmt_signed(c):
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    a = abs(c)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(c):
    return f"{c // 100}.{c % 100:02d}"


def main(argv):
    command, path, opts = parse_args(argv)
    text = read_text(path)
    records = split_records(text)

    if not records:
        sys.stderr.write("header: missing date\n")
        return EXIT_BAD_LEDGER

    _, header = records[0]
    cols = {}
    for i, name in enumerate(header):
        cols.setdefault(name.strip().lower(), i)
    for req in REQUIRED_COLUMNS:
        if req not in cols:
            sys.stderr.write(f"header: missing {req}\n")
            return EXIT_BAD_LEDGER
    idx = {k: cols[k] for k in REQUIRED_COLUMNS}

    errors = []
    expenses = []
    for line_no, fields in records[1:]:
        reason, parsed = check_row(fields, idx, len(header))
        if reason:
            errors.append(f"line {line_no}: {reason}")
        else:
            expenses.append(parsed)

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_BAD_LEDGER

    since, until = opts["since"], opts["until"]
    expenses = [
        e for e in expenses
        if (since is None or e[0] >= since) and (until is None or e[0] <= until)
    ]

    bal = compute_balances(expenses)
    as_json = opts["format"] == "json"

    if command == "balances":
        names = sorted(bal)
        if as_json:
            obj = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        else:
            out = "".join(f"{n} {fmt_signed(bal[n])}\n" for n in names)
            sys.stdout.write(out)
    else:
        transfers = compute_settle(bal)
        if as_json:
            obj = {"transfers": [
                {"from": f, "to": t, "cents": a} for f, t, a in transfers]}
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        elif not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            out = "".join(f"{f} -> {t} {fmt_plain(a)}\n" for f, t, a in transfers)
            sys.stdout.write(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
