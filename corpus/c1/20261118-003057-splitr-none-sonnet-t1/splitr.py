import sys
import csv
import io
import json
import re
import datetime
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


def usage():
    sys.exit(64)


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def split(cents, weights):
    """weights: list of (name, Fraction). Returns {name: share}, sums exactly."""
    total = sum(w for _, w in weights)
    raw = [cents * w / total for _, w in weights]
    shares = [int(r) for r in raw]  # floor (non-negative)
    left = cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - shares[i]), i))
    for i in order[:left]:
        shares[i] += 1
    return {weights[i][0]: shares[i] for i in range(len(weights))}


def parse_row(row):
    def get(k):
        i = idx[k]
        return row[i].strip() if i < len(row) else ""
    d = parse_date(get("date"))
    if d is None:
        return "bad-date"
    payer = get("payer")
    if not payer:
        return "bad-payer"
    a = get("amount")
    if not AMOUNT_RE.match(a):
        return "bad-amount"
    whole, _, frac = a.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return "bad-amount"
    parts = {}
    order = []
    ptxt = get("participants")
    for p in ptxt.split(";"):
        p = p.strip()
        if not p:
            if ptxt.strip() == "":
                break
            return "bad-participants"
        name, sep, w = p.partition(":")
        name = name.strip()
        w = w.strip()
        if not name:
            return "bad-participants"
        if sep:
            if not WEIGHT_RE.match(w) or Fraction(w) <= 0:
                return "bad-participants"
            wt = Fraction(w)
        else:
            wt = Fraction(1)
        if name not in parts:
            order.append(name)
            parts[name] = Fraction(0)
        parts[name] += wt
    if not order:
        return "bad-participants"
    return (d, payer, cents, [(n, parts[n]) for n in order])


def main(argv):
    if len(argv) < 2:
        usage()
    cmd, path = argv[0], argv[1]
    if cmd not in ("balances", "settle"):
        usage()
    fmt = "text"
    since = until = None
    i = 2
    while i < len(argv):
        o = argv[i]
        if o in ("--format", "--since", "--until") and i + 1 < len(argv):
            v = argv[i + 1]
            i += 2
            if o == "--format":
                if v not in ("text", "json"):
                    usage()
                fmt = v
            else:
                d = parse_date(v)
                if d is None:
                    usage()
                if o == "--since":
                    since = d
                else:
                    until = d
        else:
            usage()

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        print("error: cannot read " + path, file=sys.stderr)
        return 3

    global idx
    reader = csv.reader(io.StringIO(text, newline=''))
    header = None
    errors = []
    expenses = []
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            errors.append("line %d: bad-participants" % reader.line_num)
            continue
        if not row or all(c.strip() == "" for c in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            missing = [c for c in REQUIRED if c not in header]
            if missing:
                for c in missing:
                    errors.append("header: missing " + c)
                break
            idx = {c: header.index(c) for c in REQUIRED}
            continue
        # line number of record start
        r = parse_row(row)
        if isinstance(r, str):
            errors.append("line %d: %s" % (start_line(text, reader.line_num, row), r))
        else:
            expenses.append(r)
    if header is None:
        errors.append("header: missing " + REQUIRED[0])
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 2

    bal = {}
    for d, payer, cents, parts in expenses:
        if since and d < since:
            continue
        if until and d > until:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        bal.setdefault(payer, 0)
        for n, s in split(cents, parts).items():
            bal[n] = bal.get(n, 0) - s

    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            print(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                print("%s %s" % (n, money(c, True)))
        return 0

    debt = {n: -c for n, c in bal.items() if c < 0}
    cred = {n: c for n, c in bal.items() if c > 0}
    transfers = []
    while debt and cred:
        dn = min(debt, key=lambda k: (-debt[k], k))
        cn = min(cred, key=lambda k: (-cred[k], k))
        amt = min(debt[dn], cred[cn])
        transfers.append((dn, cn, amt))
        debt[dn] -= amt
        cred[cn] -= amt
        if debt[dn] == 0:
            del debt[dn]
        if cred[cn] == 0:
            del cred[cn]
    if fmt == "json":
        print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
    elif not transfers:
        print("nothing to settle")
    else:
        for a, b, c in transfers:
            print("%s -> %s %s" % (a, b, money(c, False)))
    return 0


def money(c, sign):
    s = "%d.%02d" % (abs(c) // 100, abs(c) % 100)
    if sign:
        return ("-" if c < 0 else "+") + s
    return s


def start_line(text, end_line, row):
    # records may span lines when quoted fields contain newlines
    extra = sum(c.count("\n") + c.count("\r") for c in row)
    return end_line - extra


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
