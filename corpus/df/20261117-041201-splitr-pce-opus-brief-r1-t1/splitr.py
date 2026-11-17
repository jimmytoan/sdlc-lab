#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import json
import re
import sys
from datetime import date
from fractions import Fraction

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")
COMMANDS = ("balances", "settle")
REQUIRED = ("date", "payer", "amount", "participants")
OPTIONS = ("--format", "--since", "--until")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class UsageError(Exception):
    pass


class RowError(Exception):
    pass


class ReadError(Exception):
    pass


class HeaderError(Exception):
    def __init__(self, missing):
        super().__init__(missing)
        self.missing = missing


class RowsError(Exception):
    def __init__(self, errors):
        super().__init__(errors)
        self.errors = errors


class Row:
    __slots__ = ("line", "date", "payer", "cents", "participants")

    def __init__(self, line, date_, payer, cents, participants):
        self.line = line
        self.date = date_
        self.payer = payer
        self.cents = cents
        self.participants = participants


# ---------------------------------------------------------------- parsing

def strict_date(s):
    """Return a date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    if not argv:
        raise UsageError("missing command")
    command = argv[0]
    if command not in COMMANDS:
        raise UsageError("unknown command: %s" % command)
    opts = {"--format": "text", "--since": None, "--until": None}
    positionals = []
    i = 1
    only_pos = False
    while i < len(argv):
        a = argv[i]
        i += 1
        if only_pos:
            positionals.append(a)
            continue
        if a == "--":
            only_pos = True
            continue
        if a.startswith("-") and a != "-":
            if "=" in a:
                name, value = a.split("=", 1)
            else:
                name, value = a, None
            if name not in OPTIONS:
                raise UsageError("unknown option: %s" % name)
            if value is None:
                if i >= len(argv):
                    raise UsageError("missing value for %s" % name)
                value = argv[i]
                i += 1
            opts[name] = value
        else:
            positionals.append(a)
    if len(positionals) != 1:
        raise UsageError("expected exactly one ledger path")
    fmt = opts["--format"]
    if fmt not in ("text", "json"):
        raise UsageError("bad format: %s" % fmt)
    since = until = None
    if opts["--since"] is not None:
        since = strict_date(opts["--since"])
        if since is None:
            raise UsageError("bad --since date")
    if opts["--until"] is not None:
        until = strict_date(opts["--until"])
        if until is None:
            raise UsageError("bad --until date")
    return command, positionals[0], fmt, since, until


def parse_date(s):
    d = strict_date(s)
    if d is None:
        raise RowError("bad-date")
    return d


def parse_payer(s):
    if not s or ";" in s or ":" in s:
        raise RowError("bad-payer")
    return s


def parse_amount(s):
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        raise RowError("bad-amount")
    whole, frac = m.group(1), m.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")
    if cents <= 0:
        raise RowError("bad-amount")
    return cents


def parse_participants(s):
    if not s:
        raise RowError("bad-participants")
    result = []
    seen = set()
    for item in s.split(";"):
        item = item.strip()
        if not item:
            raise RowError("bad-participants")
        parts = item.split(":")
        if len(parts) > 2:
            raise RowError("bad-participants")
        name = parts[0].strip()
        if not name:
            raise RowError("bad-participants")
        if len(parts) == 2:
            w = parts[1].strip()
            if not WEIGHT_RE.fullmatch(w):
                raise RowError("bad-participants")
            weight = Fraction(w)
            if weight <= 0:
                raise RowError("bad-participants")
        else:
            weight = Fraction(1)
        if name in seen:
            raise RowError("bad-participants")
        seen.add(name)
        result.append((name, weight))
    return result


def read_ledger(path):
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeError, ValueError):
        raise ReadError()

    physical = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text)

    def feed():
        for ln in physical:
            if ln.startswith("#"):
                yield "\n"
            else:
                yield ln

    csv.field_size_limit(max(len(text), 131072) + 1)
    reader = csv.reader(feed())
    header = None
    rows = []
    errors = []
    try:
        while True:
            start = reader.line_num + 1
            try:
                rec = next(reader)
            except StopIteration:
                break
            if not rec or all(not x.strip() for x in rec) and len(rec) == 1:
                continue
            if header is None:
                header = {}
                for idx, col in enumerate(rec):
                    header.setdefault(col.strip(), idx)
                missing = [c for c in REQUIRED if c not in header]
                if missing:
                    raise HeaderError(missing)
                continue

            def field(col):
                idx = header[col]
                return rec[idx].strip() if idx < len(rec) else ""

            try:
                d = parse_date(field("date"))
                payer = parse_payer(field("payer"))
                cents = parse_amount(field("amount"))
                parts = parse_participants(field("participants"))
            except RowError as e:
                errors.append((start, str(e)))
                continue
            rows.append(Row(start, d, payer, cents, parts))
    except csv.Error:
        raise ReadError()
    if header is None:
        raise HeaderError(list(REQUIRED))
    if errors:
        raise RowsError(errors)
    return rows


# ---------------------------------------------------------------- money

def split(cents, participants):
    total_w = sum(w for _, w in participants)
    shares = []
    for idx, (name, w) in enumerate(participants):
        exact = Fraction(cents) * w / total_w
        base = exact.numerator // exact.denominator
        shares.append([name, base, exact - base, idx])
    leftover = cents - sum(s[1] for s in shares)
    order = sorted(shares, key=lambda s: (-s[2], s[3]))
    for k in range(leftover):
        order[k][1] += 1
    result = {}
    for name, base, _, _ in shares:
        result[name] = base
    assert sum(result.values()) == cents
    return result


def compute_balances(rows):
    bal = {}
    for r in rows:
        bal[r.payer] = bal.get(r.payer, 0) + r.cents
        for name, share in split(r.cents, r.participants).items():
            bal[name] = bal.get(name, 0) - share
    assert sum(bal.values()) == 0
    return bal


def settle(balances):
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [(v, n) for n, v in bal.items() if v < 0]
        creditors = [(-v, n) for n, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        dv, dn = min(debtors)            # most negative, then name
        cv, cn = min(creditors)          # most positive, then name
        amount = min(-dv, -cv)
        transfers.append((dn, cn, amount))
        bal[dn] += amount
        bal[cn] -= amount
    return transfers


def fmt_plain(c):
    c = abs(c)
    return "%d.%02d" % (c // 100, c % 100)


def fmt_signed(c):
    if c > 0:
        return "+" + fmt_plain(c)
    if c < 0:
        return "-" + fmt_plain(c)
    return "0.00"


# ---------------------------------------------------------------- main

def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s\n" % (e, USAGE))
        return 64
    try:
        rows = read_ledger(path)
    except ReadError:
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3
    except HeaderError as e:
        for col in e.missing:
            sys.stderr.write("header: missing %s\n" % col)
        return 2
    except RowsError as e:
        for line, reason in e.errors:
            sys.stderr.write("line %d: %s\n" % (line, reason))
        return 2

    kept = [r for r in rows
            if (since is None or r.date >= since)
            and (until is None or r.date <= until)]
    balances = compute_balances(kept)
    names = sorted(balances)

    out = []
    if command == "balances":
        if fmt == "json":
            out.append(json.dumps({"balances": [
                {"name": n, "cents": balances[n]} for n in names]}))
        else:
            for n in names:
                out.append("%s %s" % (n, fmt_signed(balances[n])))
    else:
        transfers = settle(balances)
        if fmt == "json":
            out.append(json.dumps({"transfers": [
                {"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for a, b, c in transfers:
                out.append("%s -> %s %s" % (a, b, fmt_plain(c)))
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
