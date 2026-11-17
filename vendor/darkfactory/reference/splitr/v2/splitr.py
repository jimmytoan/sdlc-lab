"""Reference implementation of specs/splitr/SPEC.md. Never shown to a coding agent."""
import argparse
import csv
import datetime as dt
import json
import re
import sys

REQUIRED = ["date", "payer", "amount", "participants", "note"]
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
V2 = True


class Usage(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def to_cents(s):
    if not AMOUNT_RE.match(s):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    return cents if cents > 0 else None


def parse_participants(s):
    out, seen = [], set()
    if not s.strip():
        return None
    for part in s.split(";"):
        name, sep, weight = part.partition(":")
        name = name.strip()
        if not name:
            return None
        w = 1
        if sep:
            weight = weight.strip()
            if not re.match(r"^\d+$", weight) or int(weight) < 1:
                return None
            w = int(weight)
        if name.lower() in seen:
            return None
        seen.add(name.lower())
        out.append((name, w))
    return out


def split(cents, parts, payer=None):
    total = sum(w for _, w in parts)
    shares = {n.lower(): cents * w // total for n, w in parts}
    left = cents - sum(shares.values())
    order = sorted(parts, key=lambda p: (-((cents * p[1]) % total), p[0].lower()))
    if V2 and payer is not None:
        order = [p for p in order if p[0].lower() == payer.lower()] + [p for p in order if p[0].lower() != payer.lower()]
    for n, _ in order[:left]:
        shares[n.lower()] += 1
    return shares


def load(path):
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            lines = f.read().split("\n")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(3)
    rows = []
    for i, line in enumerate(lines, 1):
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rows.append((i, next(csv.reader([line]))))
    if not rows:
        sys.stderr.write("header: missing date\n")
        sys.exit(2)
    header = [h.strip() for h in rows[0][1]]
    for col in REQUIRED:
        if col not in header:
            sys.stderr.write(f"header: missing {col}\n")
            sys.exit(2)
    idx = {h: header.index(h) for h in header}
    expenses, errors = [], []
    for n, cells in rows[1:]:
        get = lambda c: cells[idx[c]].strip() if c in idx and idx[c] < len(cells) else ""
        date = parse_date(get("date"))
        if date is None:
            errors.append((n, "bad-date")); continue
        kind = "expense"
        if V2 and "kind" in idx:
            kind = get("kind") or "expense"
            if kind not in ("expense", "payment"):
                errors.append((n, "bad-kind")); continue
        payer = get("payer")
        if not payer:
            errors.append((n, "bad-payer")); continue
        cents = to_cents(get("amount"))
        if cents is None:
            errors.append((n, "bad-amount")); continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((n, "bad-participants")); continue
        if kind == "payment" and (len(parts) != 1 or ":" in get("participants") or parts[0][0].lower() == payer.lower()):
            errors.append((n, "bad-participants")); continue
        expenses.append((date, payer, cents, parts, kind))
    if errors:
        for n, reason in errors:
            sys.stderr.write(f"line {n}: {reason}\n")
        sys.exit(2)
    return expenses


def compute(expenses, since, until):
    names, bal = {}, {}

    def touch(name):
        k = name.lower()
        names.setdefault(k, name)
        bal.setdefault(k, 0)
        return k

    for date, payer, cents, parts, kind in expenses:
        if (since and date < since) or (until and date > until):
            continue
        p = touch(payer)
        keys = [touch(n) for n, _ in parts]
        bal[p] += cents
        if kind == "payment":
            bal[keys[0]] -= cents
        else:
            payer_in = payer if payer.lower() in keys else None
            for k, share in split(cents, parts, payer_in).items():
                bal[k] -= share
    return [(names[k], bal[k]) for k in sorted(bal)]


def fmt(cents, signed):
    sign = ""
    if signed and cents != 0:
        sign = "+" if cents > 0 else "-"
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def settle(balances):
    bal = {n: c for n, c in balances}
    out = []
    while any(bal.values()):
        debtor = min(bal, key=lambda n: (bal[n], n.lower()))
        creditor = min(bal, key=lambda n: (-bal[n], n.lower()))
        amt = min(-bal[debtor], bal[creditor])
        bal[debtor] += amt
        bal[creditor] -= amt
        out.append((debtor, creditor, amt))
    return out


def main(argv):
    ap = Parser(add_help=False)
    ap.add_argument("command")
    ap.add_argument("ledger")
    ap.add_argument("--format", default="text")
    ap.add_argument("--since")
    ap.add_argument("--until")
    try:
        a = ap.parse_args(argv)
        if a.command not in ("balances", "settle") or a.format not in ("text", "json"):
            raise Usage("bad command or format")
        since = until = None
        if a.since is not None:
            since = parse_date(a.since)
            if since is None:
                raise Usage("bad --since")
        if a.until is not None:
            until = parse_date(a.until)
            if until is None:
                raise Usage("bad --until")
    except Usage as e:
        sys.stderr.write(f"usage error: {e}\n")
        return 64
    balances = compute(load(a.ledger), since, until)
    if a.command == "balances":
        if a.format == "json":
            print(json.dumps({"balances": [{"name": n, "cents": c} for n, c in balances]}))
        else:
            for n, c in balances:
                print(f"{n} {fmt(c, True)}")
    else:
        transfers = settle(balances)
        if a.format == "json":
            print(json.dumps({"transfers": [{"from": d, "to": c, "cents": x} for d, c, x in transfers]}))
        elif not transfers:
            print("nothing to settle")
        else:
            for d, c, x in transfers:
                print(f"{d} -> {c} {fmt(x, False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
