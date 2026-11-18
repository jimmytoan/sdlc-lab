"""splitr: shared-expense settlement CLI."""
import sys
import csv
import re
import json
import datetime
import argparse
from fractions import Fraction

COLUMNS = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")
WEIGHT_RE = re.compile(r"^\d+(?:\.\d+)?$")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("splitr: error: %s\n" % message)
        raise SystemExit(64)


def _date_arg(s):
    d = _parse_date(s)
    if d is None:
        raise argparse.ArgumentTypeError("invalid date: %r" % s)
    return d


def _parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    p = _Parser(prog="splitr.py")
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", dest="fmt", choices=["text", "json"], default="text")
    p.add_argument("--since", type=_date_arg, default=None)
    p.add_argument("--until", type=_date_arg, default=None)
    return p.parse_args(argv)


def _parse_amount(s):
    m = AMOUNT_RE.match(s)
    if not m:
        return None
    frac = (m.group(2) or "").ljust(2, "0")
    cents = int(m.group(1)) * 100 + int(frac or "0")
    return cents if cents > 0 else None


def _parse_participants(s):
    out = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            return None
        name, weight = part, Fraction(1)
        if ":" in part:
            name, w = part.rsplit(":", 1)
            name, w = name.strip(), w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        if not name:
            return None
        out.append((name, weight))
    return out or None


def load_ledger(path):
    """Return (expenses, errors). Raises OSError/UnicodeDecodeError/csv.Error
    for unreadable files; raises ValueError('missing col') for header issues."""
    expenses, errors = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            header = []
        header = [h.strip() for h in header]
        for col in COLUMNS:
            if col not in header:
                raise ValueError(col)
        idx = {c: header.index(c) for c in COLUMNS}
        for rec in reader:
            lineno = reader.line_num
            if not rec or all(not x.strip() for x in rec):
                continue
            if rec[0].startswith("#"):
                continue

            def get(c):
                i = idx[c]
                return rec[i] if i < len(rec) else ""
            d = _parse_date(get("date").strip())
            if d is None:
                errors.append((lineno, "bad-date"))
                continue
            payer = get("payer").strip()
            if not payer:
                errors.append((lineno, "bad-payer"))
                continue
            amt = _parse_amount(get("amount").strip())
            if amt is None:
                errors.append((lineno, "bad-amount"))
                continue
            parts = _parse_participants(get("participants"))
            if parts is None:
                errors.append((lineno, "bad-participants"))
                continue
            expenses.append((d, payer, amt, parts))
    return expenses, errors


def split_cents(amount_cents, weights):
    total = sum(weights)
    shares = [int(amount_cents * w / total) for w in weights]
    rem = amount_cents - sum(shares)
    i = 0
    while rem > 0:
        shares[i % len(shares)] += 1
        rem -= 1
        i += 1
    return shares


def compute_balances(expenses):
    bal = {}
    for _d, payer, amt, parts in expenses:
        bal[payer] = bal.get(payer, 0) + amt
        shares = split_cents(amt, [w for _n, w in parts])
        for (n, _w), s in zip(parts, shares):
            bal[n] = bal.get(n, 0) - s
    return bal


def settle(balances):
    bal = dict(balances)
    out = []
    while True:
        debtors = [(v, n) for n, v in bal.items() if v < 0]
        creditors = [(v, n) for n, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        dv, dn = min(debtors, key=lambda x: (x[0], x[1]))
        cv, cn = min(creditors, key=lambda x: (-x[0], x[1]))
        amt = min(-dv, cv)
        out.append((dn, cn, amt))
        bal[dn] += amt
        bal[cn] -= amt
    return out


def _money(c, sign=False):
    s = "-" if c < 0 else ("+" if sign else "")
    c = abs(c)
    return "%s%d.%02d" % (s, c // 100, c % 100)


def render(command, fmt, balances):
    if command == "balances":
        items = sorted(balances.items())
        if fmt == "json":
            return json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}) + "\n"
        return "".join("%s %s\n" % (n, _money(c, True)) for n, c in items)
    tr = settle(balances)
    if fmt == "json":
        return json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in tr]}) + "\n"
    if not tr:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (a, b, _money(c)) for a, b, c in tr)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        expenses, errors = load_ledger(args.ledger)
    except (OSError, UnicodeDecodeError, csv.Error):
        sys.stderr.write("error: cannot read %s\n" % args.ledger)
        return 3
    except ValueError as e:
        sys.stderr.write("header: missing %s\n" % e.args[0])
        return 2
    if errors:
        for n, r in errors:
            sys.stderr.write("line %d: %s\n" % (n, r))
        return 2
    if args.since:
        expenses = [e for e in expenses if e[0] >= args.since]
    if args.until:
        expenses = [e for e in expenses if e[0] <= args.until]
    out = render(args.command, args.fmt, compute_balances(expenses))
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
