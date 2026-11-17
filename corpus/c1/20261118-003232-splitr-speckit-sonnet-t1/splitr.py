import sys
import csv
import re
import json
import datetime
from fractions import Fraction

COLUMNS = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")
USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class Usage(Exception):
    pass


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    if not argv or argv[0] not in ("balances", "settle"):
        raise Usage()
    cmd = argv[0]
    path = None
    fmt = "text"
    since = until = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name = a
                if name not in ("--format", "--since", "--until"):
                    raise Usage()
                i += 1
                if i >= len(argv):
                    raise Usage()
                val = argv[i]
            if name == "--format":
                if val not in ("text", "json"):
                    raise Usage()
                fmt = val
            elif name in ("--since", "--until"):
                d = parse_date(val)
                if d is None:
                    raise Usage()
                if name == "--since":
                    since = d
                else:
                    until = d
            else:
                raise Usage()
        else:
            if path is not None:
                raise Usage()
            path = a
        i += 1
    if path is None:
        raise Usage()
    return cmd, path, fmt, since, until


def read_records(text):
    """Yield (line_number, fields) skipping comments and blank lines."""
    state = {"phys": 0, "start": None}

    def gen():
        for line in text.splitlines(True):
            state["phys"] += 1
            if state["start"] is None:
                if line.startswith("#"):
                    continue
                state["start"] = state["phys"]
            yield line

    reader = csv.reader(gen())
    while True:
        state["start"] = None
        try:
            rec = next(reader)
        except StopIteration:
            return
        except csv.Error:
            yield state["start"] or state["phys"], None
            continue
        if not rec:
            continue
        yield state["start"], rec


def parse_participants(s):
    out = []
    for item in s.split(";"):
        if ":" in item:
            name, w = item.rsplit(":", 1)
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name = item.strip()
            weight = Fraction(1)
        if not name:
            return None
        out.append((name, weight))
    return out


def parse_row(rec, idx):
    def get(c):
        j = idx[c]
        return rec[j] if j < len(rec) else ""

    d = parse_date(get("date").strip())
    if d is None:
        return "bad-date"
    payer = get("payer").strip()
    if not payer:
        return "bad-payer"
    amt = get("amount").strip()
    if not AMOUNT_RE.match(amt):
        return "bad-amount"
    if "." in amt:
        whole, frac = amt.split(".")
        cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    else:
        cents = int(amt) * 100
    if cents <= 0:
        return "bad-amount"
    parts = parse_participants(get("participants"))
    if not parts:
        return "bad-participants"
    return (d, payer, cents, parts)


def split(cents, weights):
    total = sum(weights)
    shares = [int(Fraction(cents) * w / total) for w in weights]
    left = cents - sum(shares)
    for i in range(left):
        shares[i] += 1
    return shares


def compute_balances(expenses):
    bal = {}
    for _d, payer, cents, parts in expenses:
        bal[payer] = bal.get(payer, 0) + cents
        shares = split(cents, [w for _n, w in parts])
        for (n, _w), s in zip(parts, shares):
            bal[n] = bal.get(n, 0) - s
    return bal


def settle(bal):
    b = {n: c for n, c in bal.items() if c != 0}
    out = []
    while b:
        debtor = min(b, key=lambda n: (b[n], n))
        creditor = min(b, key=lambda n: (-b[n], n))
        if b[debtor] >= 0 or b[creditor] <= 0:
            break
        amt = min(-b[debtor], b[creditor])
        out.append((debtor, creditor, amt))
        b[debtor] += amt
        b[creditor] -= amt
        for n in (debtor, creditor):
            if b[n] == 0:
                del b[n]
    return out


def money(c, sign=False):
    s = "-" if c < 0 else ("+" if sign else "")
    c = abs(c)
    return "%s%d.%02d" % (s, c // 100, c % 100)


def main(argv):
    try:
        cmd, path, fmt, since, until = parse_args(argv)
    except Usage:
        sys.stderr.write(USAGE + "\n")
        return 64
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    records = read_records(text)
    header = next(records, None)
    names = [h.strip() for h in header[1]] if header and header[1] else []
    missing = [c for c in COLUMNS if c not in names]
    if missing:
        sys.stderr.write("".join("header: missing %s\n" % c for c in missing))
        return 2
    idx = {c: names.index(c) for c in COLUMNS}

    expenses = []
    errors = []
    for ln, rec in records:
        r = parse_row(rec, idx) if rec is not None else "bad-date"
        if isinstance(r, str):
            errors.append("line %d: %s\n" % (ln, r))
        else:
            expenses.append(r)
    if errors:
        sys.stderr.write("".join(errors))
        return 2

    if since:
        expenses = [e for e in expenses if e[0] >= since]
    if until:
        expenses = [e for e in expenses if e[0] <= until]

    bal = compute_balances(expenses)
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out = json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]})
        else:
            out = "\n".join("%s %s" % (n, money(c, True)) for n, c in items)
    else:
        tr = settle(bal)
        if fmt == "json":
            out = json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in tr]})
        elif tr:
            out = "\n".join("%s -> %s %s" % (a, b, money(c)) for a, b, c in tr)
        else:
            out = "nothing to settle"
    if out:
        sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
