"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import io
import json
import re
import sys

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+$")


class Exit(Exception):
    def __init__(self, code, msg=None):
        self.code = code
        self.msg = msg


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:]))
    except ValueError:
        return None


def parse_args(argv):
    fmt, since, until = "text", None, None
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name, val = a, None
                if name in ("--format", "--since", "--until"):
                    i += 1
                    if i >= len(argv):
                        raise Exit(64, "usage: missing value for " + name)
                    val = argv[i]
            if name == "--format":
                if val not in ("text", "json"):
                    raise Exit(64, "usage: bad --format")
                fmt = val
            elif name in ("--since", "--until"):
                d = parse_date(val)
                if d is None:
                    raise Exit(64, "usage: bad date for " + name)
                if name == "--since":
                    since = d
                else:
                    until = d
            else:
                raise Exit(64, "usage: unknown option " + name)
        else:
            pos.append(a)
        i += 1
    if len(pos) != 2 or pos[0] not in ("balances", "settle"):
        raise Exit(64, "usage: splitr.py balances|settle <ledger.csv> "
                       "[--format text|json] [--since D] [--until D]")
    return pos[0], pos[1], fmt, since, until


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise Exit(3, "error: cannot read " + path)
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def parse_row(rec, idx):
    def get(col):
        j = idx[col]
        return rec[j] if j < len(rec) else ""

    date = parse_date(get("date").strip())
    if date is None:
        return None, "bad-date"
    payer = get("payer").strip()
    if not payer:
        return None, "bad-payer"
    amt = get("amount").strip()
    if not AMOUNT_RE.match(amt):
        return None, "bad-amount"
    if "." in amt:
        w, f = amt.split(".")
        cents = int(w) * 100 + int(f.ljust(2, "0"))
    else:
        cents = int(amt) * 100
    if cents <= 0:
        return None, "bad-amount"
    parts = []
    for item in get("participants").split(";"):
        item = item.strip()
        if not item:
            return None, "bad-participants"
        weight = 1
        if ":" in item:
            name, ws = item.rsplit(":", 1)
            name, ws = name.strip(), ws.strip()
            if not WEIGHT_RE.match(ws) or int(ws) <= 0:
                return None, "bad-participants"
            weight = int(ws)
        else:
            name = item
        if not name:
            return None, "bad-participants"
        parts.append((name, weight))
    return (date, payer, cents, parts), None


def parse_ledger(text):
    """Returns list of (line, date, payer, cents, parts); raises Exit on errors."""
    lines = text.splitlines(keepends=True)
    kept = []  # (physical line no, text)
    # keep comment-stripped text but remember physical line numbers per record
    out = []
    linemap = []
    for n, ln in enumerate(lines, 1):
        if ln.startswith("#"):
            continue
        out.append(ln)
        linemap.append(n)
    # parse record by record, tracking physical line via counting newlines
    reader_lines = out
    # Compute start line for each record using csv reader's line_num
    reader = csv.reader(iter(reader_lines))
    records = []
    prev = 0
    for rec in reader:
        end = reader.line_num  # number of lines consumed (index into out)
        start_idx = prev
        prev = end
        records.append((linemap[start_idx] if start_idx < len(linemap) else 0, rec))
    records = [r for r in records if r[1] and any(c.strip() for c in r[1])]
    if not records:
        raise Exit(2, "header: missing " + REQUIRED[0])
    header = [h.strip().lower() for h in records[0][1]]
    idx = {}
    for col in REQUIRED:
        if col not in header:
            raise Exit(2, "header: missing " + col)
        idx[col] = header.index(col)
    expenses, errors = [], []
    for line, rec in records[1:]:
        row, err = parse_row(rec, idx)
        if err:
            errors.append("line %d: %s" % (line, err))
        else:
            expenses.append((line,) + row)
    if errors:
        raise Exit(2, "\n".join(errors))
    return expenses


def split_cents(cents, parts):
    total = sum(w for _, w in parts)
    shares = [cents * w // total for _, w in parts]
    rem = cents - sum(shares)
    i = 0
    while rem > 0:
        shares[i % len(shares)] += 1
        rem -= 1
        i += 1
    return shares


def compute_balances(expenses):
    bal = {}
    for _, _, payer, cents, parts in expenses:
        bal[payer] = bal.get(payer, 0) + cents
        for (name, _), s in zip(parts, split_cents(cents, parts)):
            bal[name] = bal.get(name, 0) - s
    return bal


def fmt_cents(c, sign=True):
    s = "%d.%02d" % (abs(c) // 100, abs(c) % 100)
    if sign:
        return ("-" if c < 0 else "+") + s
    return s


def settle(bal):
    debtors = {n: -c for n, c in bal.items() if c < 0}
    creditors = {n: c for n, c in bal.items() if c > 0}
    transfers = []
    while debtors and creditors:
        d = min(debtors, key=lambda n: (-debtors[n], n))
        c = min(creditors, key=lambda n: (-creditors[n], n))
        amt = min(debtors[d], creditors[c])
        transfers.append((d, c, amt))
        debtors[d] -= amt
        creditors[c] -= amt
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return transfers


def run(argv):
    cmd, path, fmt, since, until = parse_args(argv)
    text = read_ledger(path)
    expenses = parse_ledger(text)
    expenses = [e for e in expenses
                if (since is None or e[1] >= since) and (until is None or e[1] <= until)]
    bal = compute_balances(expenses)
    out = []
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]},
                                  ensure_ascii=False))
        else:
            out.extend("%s %s" % (n, fmt_cents(c)) for n, c in items)
    else:
        tr = settle(bal)
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": a, "to": b, "cents": c}
                                                 for a, b, c in tr]}, ensure_ascii=False))
        elif not tr:
            out.append("nothing to settle")
        else:
            out.extend("%s -> %s %s" % (a, b, fmt_cents(c, False)) for a, b, c in tr)
    return "".join(l + "\n" for l in out)


def main(argv):
    try:
        res = run(argv)
    except Exit as e:
        if e.msg:
            sys.stderr.write(e.msg + "\n")
        return e.code
    sys.stdout.write(res)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
