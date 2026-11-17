#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys
from fractions import Fraction

USAGE = ("usage: python splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")

REQUIRED = ["date", "payer", "amount", "participants"]
OPTIONAL = ["kind"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$|^\.\d+$")


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
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        if a.startswith("--") and len(a) > 2:
            if "=" in a:
                name, val = a[2:].split("=", 1)
            else:
                name = a[2:]
                if name not in opts:
                    raise UsageError("unknown option " + a)
                if i + 1 >= len(argv):
                    raise UsageError("option --%s needs a value" % name)
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
                if d is None:
                    raise UsageError("bad date for --%s: %s" % (name, val))
                opts[name] = d
        elif a.startswith("-") and a != "-":
            raise UsageError("unknown option " + a)
        else:
            pos.append(a)
        i += 1
    if len(pos) != 2:
        raise UsageError("expected command and ledger path")
    if pos[0] not in ("balances", "settle"):
        raise UsageError("unknown command " + pos[0])
    return pos[0], pos[1], opts


def parse_cents(s):
    s = s.strip()
    if not AMOUNT_RE.match(s):
        return None
    if "." in s:
        whole, frac = s.split(".")
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    c = int(whole) * 100 + int(frac)
    if c <= 0:
        return None
    return c


def parse_participants(s):
    s = s.strip()
    if not s:
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        if not part:
            return None
        if ":" in part:
            name, w = part.split(":", 1)
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name, weight = part, Fraction(1)
        if not name or ":" in name or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    return result


def parse_recipient(s, payer):
    name = s.strip()
    if not name or ";" in name or ":" in name or name == payer:
        return None
    return name


def split_shares(total, parts, payer=None):
    wsum = sum(w for _, w in parts)
    shares = []
    for idx, (name, w) in enumerate(parts):
        exact = Fraction(total) * w / wsum
        fl = exact.numerator // exact.denominator
        shares.append([name, fl, exact - fl, idx])
    left = total - sum(s[1] for s in shares)
    payer_idx = None
    if payer is not None:
        for s in shares:
            if s[0] == payer:
                payer_idx = s[3]
                break
    if payer_idx is not None and left > 0:
        shares[payer_idx][1] += 1
        left -= 1
    order = sorted(range(len(shares)),
                   key=lambda i: (-shares[i][2], shares[i][0].lower(), shares[i][0], i))
    if payer_idx is not None:
        order = [i for i in order if i != payer_idx]
    if order:
        for k in range(left):
            shares[order[k % len(order)]][1] += 1
    return [(s[0], s[1]) for s in shares]


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)
    return text


def load_rows(text):
    """Returns rows: list of (lineno, dict)."""
    physical = text.splitlines(keepends=True)
    state = {"start": None}

    def gen():
        for n, line in enumerate(physical, 1):
            if line.lstrip().startswith("#"):
                continue
            if state["start"] is None:
                state["start"] = n
            yield line

    reader = csv.reader(gen())
    header = None
    records = []
    while True:
        state["start"] = None
        try:
            rec = next(reader)
        except StopIteration:
            break
        except csv.Error:
            sys.stderr.write("error: cannot read ledger\n")
            sys.exit(3)
        lineno = state["start"]
        if not rec or all(not x.strip() for x in rec):
            if header is None and not rec:
                continue
            if not rec:
                continue
            if header is not None and len(rec) <= 1:
                continue
        if header is None:
            header = [h.strip().lower() for h in rec]
            continue
        records.append((lineno, rec))
    if header is None:
        header = []
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for c in missing:
            sys.stderr.write("header: missing %s\n" % c)
        sys.exit(2)
    idx = {c: header.index(c) for c in REQUIRED}
    for c in OPTIONAL:
        if c in header:
            idx[c] = header.index(c)
    out = []
    for lineno, rec in records:
        row = {c: (rec[i] if i < len(rec) else "") for c, i in idx.items()}
        out.append((lineno, row))
    return out


def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s\n" % (e, USAGE))
        return 64

    text = read_ledger(path)
    rows = load_rows(text)

    errors = []
    entries = []
    for lineno, row in rows:
        d = parse_date(row["date"])
        if d is None:
            errors.append((lineno, "bad-date"))
            continue
        kind = row.get("kind", "").strip()
        if kind == "":
            kind = "expense"
        if kind not in ("expense", "payment"):
            errors.append((lineno, "bad-kind"))
            continue
        payer = row["payer"].strip()
        if not payer or ";" in payer or ":" in payer:
            errors.append((lineno, "bad-payer"))
            continue
        cents = parse_cents(row["amount"])
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        if kind == "payment":
            rcpt = parse_recipient(row["participants"], payer)
            if rcpt is None:
                errors.append((lineno, "bad-participants"))
                continue
            entries.append((d, kind, payer, cents, rcpt))
        else:
            parts = parse_participants(row["participants"])
            if parts is None:
                errors.append((lineno, "bad-participants"))
                continue
            entries.append((d, kind, payer, cents, parts))

    if errors:
        for lineno, reason in errors:
            sys.stderr.write("line %d: %s\n" % (lineno, reason))
        return 2

    balances = {}
    for d, kind, payer, cents, parts in entries:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        balances[payer] = balances.get(payer, 0) + cents
        if kind == "payment":
            balances[parts] = balances.get(parts, 0) - cents
        else:
            for name, share in split_shares(cents, parts, payer):
                balances[name] = balances.get(name, 0) - share

    if command == "balances":
        names = sorted(balances)
        if opts["format"] == "json":
            print(json.dumps({"balances": [{"name": n, "cents": balances[n]} for n in names]}))
        else:
            for n in names:
                print("%s %s" % (n, fmt_signed(balances[n])))
        return 0

    transfers = settle(balances)
    if opts["format"] == "json":
        print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
    else:
        if not transfers:
            print("nothing to settle")
        for a, b, c in transfers:
            print("%s -> %s %s" % (a, b, fmt_abs(c)))
    return 0


def fmt_abs(c):
    c = abs(c)
    return "%d.%02d" % (c // 100, c % 100)


def fmt_signed(c):
    if c > 0:
        return "+" + fmt_abs(c)
    if c < 0:
        return "-" + fmt_abs(c)
    return "0.00"


def settle(balances):
    bal = {n: v for n, v in balances.items() if v != 0}
    transfers = []
    while True:
        debtors = [(v, n) for n, v in bal.items() if v < 0]
        creditors = [(-v, n) for n, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        dv, dn = min(debtors, key=lambda x: (x[0], x[1]))
        cv, cn = min(creditors, key=lambda x: (x[0], x[1]))
        amt = min(-dv, -cv)
        transfers.append((dn, cn, amt))
        bal[dn] += amt
        bal[cn] -= amt
        if bal[dn] == 0:
            del bal[dn]
        if bal[cn] == 0:
            del bal[cn]
    return transfers


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
