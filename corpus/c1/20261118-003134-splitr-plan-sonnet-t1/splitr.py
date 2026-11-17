import csv
import datetime
import io
import json
import re
import sys
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


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
    if not argv:
        raise Usage("missing command")
    cmd = argv[0]
    if cmd not in ("balances", "settle"):
        raise Usage("unknown command")
    fmt, since, until, path = "text", None, None, None
    rest = argv[1:]
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
            else:
                k, v = a, None
            if k not in ("--format", "--since", "--until"):
                raise Usage("unknown option " + k)
            if v is None:
                i += 1
                if i >= len(rest):
                    raise Usage("option needs a value")
                v = rest[i]
            if k == "--format":
                if v not in ("text", "json"):
                    raise Usage("bad format")
                fmt = v
            else:
                d = parse_date(v)
                if d is None:
                    raise Usage("bad date for " + k)
                if k == "--since":
                    since = d
                else:
                    until = d
        elif a.startswith("-") and a != "-":
            raise Usage("unknown option " + a)
        else:
            if path is not None:
                raise Usage("extra argument")
            path = a
        i += 1
    if path is None:
        raise Usage("missing path")
    if since and until and since > until:
        raise Usage("since after until")
    return cmd, path, fmt, since, until


class Counter:
    def __init__(self, text):
        self.it = io.StringIO(text, newline="")
        self.count = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self.it)
        self.count += 1
        return line


def check_row(row):
    def f(i):
        return row[i].strip() if i < len(row) else ""
    d = parse_date(f(0))
    if d is None:
        return "bad-date"
    if not f(1):
        return "bad-payer"
    amt = f(2)
    if not AMOUNT_RE.match(amt):
        return "bad-amount"
    if "." in amt:
        w, fr = amt.split(".")
        cents = int(w) * 100 + int((fr + "0")[:2])
    else:
        cents = int(amt) * 100
    if cents <= 0:
        return "bad-amount"
    parts = []
    seen = set()
    plist = f(3)
    if not plist:
        return "bad-participants"
    for p in plist.split(";"):
        p = p.strip()
        if not p:
            return "bad-participants"
        if ":" in p:
            name, w = p.rsplit(":", 1)
            name, w = name.strip(), w.strip()
            if not WEIGHT_RE.match(w) or Fraction(w) <= 0:
                return "bad-participants"
            weight = Fraction(w)
        else:
            name, weight = p, Fraction(1)
        if not name or name in seen:
            return "bad-participants"
        seen.add(name)
        parts.append((name, weight))
    return (d, f(1), cents, parts)


def read_ledger(text):
    """Returns (expenses, errors, header_errors)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    src = Counter(text)
    reader = csv.reader(src)
    header = None
    expenses, errors = [], []
    while True:
        start = src.count + 1
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            if header is not None:
                errors.append("line %d: bad-date" % start)
            continue
        if not row:
            continue
        if row[0].startswith("#"):
            continue
        if header is None:
            header = {}
            for idx, name in enumerate(row):
                header.setdefault(name.strip(), idx)
            missing = [c for c in REQUIRED if c not in header]
            if missing:
                return None, None, ["header: missing " + c for c in missing]
            continue
        order = [header[c] for c in REQUIRED]
        r = [row[i] if i < len(row) else "" for i in order]
        res = check_row(r)
        if isinstance(res, str):
            errors.append("line %d: %s" % (start, res))
        else:
            expenses.append(res)
    if header is None:
        return None, None, ["header: missing " + c for c in REQUIRED]
    return expenses, errors, []


def split(cents, parts):
    total = sum(w for _, w in parts)
    shares = []
    for n, w in parts:
        exact = Fraction(cents) * w / total
        fl = exact.numerator // exact.denominator
        shares.append([fl, exact - fl])
    left = cents - sum(s[0] for s in shares)
    order = sorted(range(len(shares)), key=lambda i: (-shares[i][1], i))
    for i in order[:left]:
        shares[i][0] += 1
    return [s[0] for s in shares]


def compute_balances(expenses):
    bal = {}
    for _, payer, cents, parts in expenses:
        bal[payer] = bal.get(payer, 0) + cents
        for (n, _), s in zip(parts, split(cents, parts)):
            bal[n] = bal.get(n, 0) - s
    return bal


def settle(bal):
    debt = {n: -v for n, v in bal.items() if v < 0}
    cred = {n: v for n, v in bal.items() if v > 0}
    out = []
    while debt and cred:
        d = min(debt, key=lambda n: (-debt[n], n))
        c = min(cred, key=lambda n: (-cred[n], n))
        amt = min(debt[d], cred[c])
        out.append((d, c, amt))
        debt[d] -= amt
        cred[c] -= amt
        if debt[d] == 0:
            del debt[d]
        if cred[c] == 0:
            del cred[c]
    return out


def money(c):
    sign = "-" if c < 0 else "+"
    c = abs(c)
    return "%s%d.%02d" % (sign, c // 100, c % 100)


def main(argv):
    try:
        cmd, path, fmt, since, until = parse_args(argv)
    except Usage as e:
        sys.stderr.write("usage: splitr.py balances|settle <ledger.csv> "
                         "[--format text|json] [--since D] [--until D] (%s)\n" % e)
        return 64
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3
    expenses, errors, herr = read_ledger(text)
    if herr:
        sys.stderr.write("\n".join(herr) + "\n")
        return 2
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2
    sel = [e for e in expenses
           if (since is None or e[0] >= since) and (until is None or e[0] <= until)]
    bal = compute_balances(sel)
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            print(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                print("%s %s" % (n, money(c)))
    else:
        tr = settle(bal)
        if fmt == "json":
            print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in tr]}))
        elif not tr:
            print("nothing to settle")
        else:
            for a, b, c in tr:
                print("%s -> %s %d.%02d" % (a, b, c // 100, c % 100))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
