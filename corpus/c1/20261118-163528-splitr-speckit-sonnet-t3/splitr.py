"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

COLUMNS = ["date", "payer", "amount", "participants", "note"]
USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class Expense:
    def __init__(self, line, date, payer, cents, participants):
        self.line, self.date, self.payer = line, date, payer
        self.cents, self.participants = cents, participants


def usage_error(msg=None):
    if msg:
        sys.stderr.write(msg + "\n")
    sys.stderr.write(USAGE + "\n")
    return 64


def valid_date(s):
    if not DATE_RE.match(s):
        return False
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def parse_args(argv):
    if any(a in ("-h", "--help") for a in argv):
        return "help"
    pos = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name, eq, val = a[2:].partition("=")
            if name not in opts:
                return "unknown option " + a
            if not eq:
                i += 1
                if i >= len(argv):
                    return "missing value for " + a
                val = argv[i]
            opts[name] = val
        else:
            pos.append(a)
        i += 1
    if len(pos) != 2:
        return "expected a command and a ledger file"
    if pos[0] not in ("balances", "settle"):
        return "unknown command " + pos[0]
    if opts["format"] not in ("text", "json"):
        return "bad --format value"
    for k in ("since", "until"):
        if opts[k] is not None and not valid_date(opts[k]):
            return "bad --%s date" % k
    return pos[0], pos[1], opts


def load_ledger(path):
    """Returns (rows, header_error) where rows = list of (line_no, fields dict)."""
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8")
    except (OSError, UnicodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)
    if text.startswith("\ufeff"):
        text = text[1:]
    physical = text.splitlines(keepends=True)
    state = {"n": 0, "start": None, "mark": True}

    def gen():
        in_record = False
        for ln in physical:
            state["n"] += 1
            if not in_record and state["n"] > 1:
                s = ln.strip()
                if not s or ln.startswith("#"):
                    continue
            if state["mark"]:
                state["start"] = state["n"]
                state["mark"] = False
            yield ln

    reader = csv.reader(gen())
    header = None
    rows = []
    while True:
        state["mark"] = True
        try:
            rec = next(reader)
        except StopIteration:
            break
        except csv.Error:
            rec = None
        if header is None:
            header = [h.strip().lower() for h in (rec or [])]
            missing = [c for c in COLUMNS if c not in header]
            if missing:
                for c in missing:
                    sys.stderr.write("header: missing %s\n" % c)
                sys.exit(2)
            continue
        rows.append((state["start"], rec, header))
    if header is None:
        for c in COLUMNS:
            sys.stderr.write("header: missing %s\n" % c)
        sys.exit(2)
    return rows


def parse_row(line, rec, header):
    """Returns (Expense|None, [reasons])."""
    if rec is None:
        return None, ["bad-date", "bad-payer", "bad-amount", "bad-participants"]
    f = {}
    for i, h in enumerate(header):
        if h in COLUMNS and h not in f:
            f[h] = rec[i].strip() if i < len(rec) else ""
    errs = []
    date = f["date"]
    if not valid_date(date):
        errs.append("bad-date")
    payer = f["payer"]
    if not payer:
        errs.append("bad-payer")
    amt = f["amount"]
    cents = 0
    if AMOUNT_RE.match(amt):
        whole, _, frac = amt.partition(".")
        cents = int(whole) * 100 + int((frac + "00")[:2]) if frac else int(whole) * 100
        if cents <= 0:
            errs.append("bad-amount")
    else:
        errs.append("bad-amount")
    parts = []
    perr = False
    raw = f["participants"]
    if not raw.strip():
        perr = True
    else:
        order = {}
        for item in raw.split(";"):
            item = item.strip()
            name, sep, w = item.partition(":")
            name = name.strip()
            if not name:
                perr = True
                break
            if sep:
                try:
                    wt = Fraction(w.strip())
                except (ValueError, ZeroDivisionError):
                    perr = True
                    break
                if wt <= 0:
                    perr = True
                    break
            else:
                wt = Fraction(1)
            if name in order:
                parts[order[name]] = (name, parts[order[name]][1] + wt)
            else:
                order[name] = len(parts)
                parts.append((name, wt))
    if perr:
        errs.append("bad-participants")
    if errs:
        return None, errs
    return Expense(line, date, payer, cents, parts), []


def split(cents, participants):
    total = sum(w for _, w in participants)
    shares = [int(Fraction(cents) * w / total) for _, w in participants]
    left = cents - sum(shares)
    i = 0
    while left > 0:
        shares[i % len(shares)] += 1
        left -= 1
        i += 1
    return shares


def compute_balances(expenses):
    bal = {}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.cents
        for (name, _), s in zip(e.participants, split(e.cents, e.participants)):
            bal[name] = bal.get(name, 0) - s
    return bal


def settle(bal):
    debtors = sorted(([-c, n] for n, c in bal.items() if c < 0), key=lambda x: (-x[0], x[1]))
    creditors = sorted(([c, n] for n, c in bal.items() if c > 0), key=lambda x: (-x[0], x[1]))
    out = []
    while debtors and creditors:
        debtors.sort(key=lambda x: (-x[0], x[1]))
        creditors.sort(key=lambda x: (-x[0], x[1]))
        d, c = debtors[0], creditors[0]
        amt = min(d[0], c[0])
        out.append((d[1], c[1], amt))
        d[0] -= amt
        c[0] -= amt
        if d[0] == 0:
            debtors.pop(0)
        if c[0] == 0:
            creditors.pop(0)
    return out


def money(c, sign=True):
    s = "%d.%02d" % (abs(c) // 100, abs(c) % 100)
    if sign:
        return ("-" if c < 0 else "+") + s
    return s


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parsed = parse_args(argv)
    if parsed == "help":
        print(USAGE)
        return 0
    if isinstance(parsed, str):
        return usage_error(parsed)
    cmd, path, opts = parsed
    rows = load_ledger(path)
    expenses = []
    errors = []
    for line, rec, header in rows:
        e, errs = parse_row(line, rec, header)
        if errs:
            errors.extend((line, r) for r in errs)
        else:
            expenses.append(e)
    if errors:
        for line, r in sorted(errors, key=lambda x: x[0]):
            sys.stderr.write("line %d: %s\n" % (line, r))
        return 2
    if opts["since"]:
        expenses = [e for e in expenses if e.date >= opts["since"]]
    if opts["until"]:
        expenses = [e for e in expenses if e.date <= opts["until"]]
    bal = compute_balances(expenses)
    js = opts["format"] == "json"
    if cmd == "balances":
        items = sorted(bal.items())
        if js:
            print(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                print("%s %s" % (n, money(c)))
    else:
        tr = settle(bal)
        if js:
            print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in tr]}))
        elif not tr:
            print("nothing to settle")
        else:
            for a, b, c in tr:
                print("%s -> %s %s" % (a, b, money(c, False)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
