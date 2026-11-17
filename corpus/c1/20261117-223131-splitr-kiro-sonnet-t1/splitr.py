#!/usr/bin/env python3
"""splitr: split shared expenses from a CSV ledger."""
import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

OK, DATA, IO, USAGE = 0, 2, 3, 64
REQUIRED = ("date", "payer", "amount", "participants")
USAGE_LINE = ("usage: splitr.py {balances|settle} <ledger.csv> [--format text|json] "
              "[--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class UsageError(Exception):
    pass


class LedgerReadError(Exception):
    pass


@dataclass
class Options:
    command: str = None
    path: str = None
    fmt: str = "text"
    since: date = None
    until: date = None


@dataclass
class Expense:
    date: date
    payer: str
    cents: int
    participants: list


def parse_date(s):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    m = re.fullmatch(r"([0-9]+)(?:\.([0-9]{1,2}))?", s)
    if not m:
        return None
    c = int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0"))
    return c or None


def parse_participants(s):
    out, seen = [], set()
    for piece in s.split(";"):
        piece = piece.strip()
        if not piece:
            return None
        if ":" in piece:
            name, w = piece.split(":", 1)
            name, w = name.strip(), w.strip()
            if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name, weight = piece, Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        out.append((name, weight))
    return out


def split_expense(cents, participants):
    total = sum(w for _, w in participants)
    shares, rems = {}, []
    for name, w in participants:
        exact = Fraction(cents) * w / total
        fl = exact.numerator // exact.denominator
        shares[name] = fl
        rems.append((-(exact - fl), name))
    left = cents - sum(shares.values())
    rems.sort()
    for _, name in rems[:left]:
        shares[name] += 1
    return shares


def compute_balances(expenses):
    bal = {}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.cents
        for n, s in split_expense(e.cents, e.participants).items():
            bal[n] = bal.get(n, 0) - s
    return bal


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError, ValueError):
        raise LedgerReadError(path)


def validate_row(fields, idx):
    def get(c):
        i = idx[c]
        return fields[i].strip() if i < len(fields) else ""
    d = parse_date(get("date"))
    if d is None:
        return "bad-date", None
    payer = get("payer")
    if not payer:
        return "bad-payer", None
    cents = parse_amount(get("amount"))
    if cents is None:
        return "bad-amount", None
    parts = parse_participants(get("participants"))
    if parts is None:
        return "bad-participants", None
    return None, Expense(d, payer, cents, parts)


def parse_ledger(text):
    csv.field_size_limit(sys.maxsize if sys.maxsize < 2**31 else 2**31 - 1)
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+", text)
    state = {"pos": 0}

    def gen():
        while state["pos"] < len(lines):
            ln = lines[state["pos"]]
            state["pos"] += 1
            yield ln

    reader = csv.reader(gen())
    records = []  # (line, fields)
    while state["pos"] < len(lines):
        ln = lines[state["pos"]]
        if ln.startswith("#") or not ln.strip():
            state["pos"] += 1
            continue
        start = state["pos"] + 1
        try:
            rec = next(reader)
        except StopIteration:
            break
        except csv.Error:
            rec = []
            reader = csv.reader(gen())
        if rec:
            records.append((start, rec))
    if not records:
        return [f"header: missing {c}" for c in REQUIRED], [], []
    header = [h.strip() for h in records[0][1]]
    idx = {}
    for i, h in enumerate(header):
        idx.setdefault(h, i)
    herr = [f"header: missing {c}" for c in REQUIRED if c not in idx]
    if herr:
        return herr, [], []
    rerr, exps = [], []
    for line, fields in records[1:]:
        reason, e = validate_row(fields, idx)
        if reason:
            rerr.append(f"line {line}: {reason}")
        else:
            exps.append(e)
    return [], rerr, exps


def parse_args(argv):
    o = Options()
    pos = []
    vals = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--format", "--since", "--until"):
            if i + 1 >= len(argv):
                raise UsageError(a)
            vals[a] = argv[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            raise UsageError(a)
        pos.append(a)
        if len(pos) > 2:
            raise UsageError(a)
        i += 1
    if not pos or pos[0] not in ("balances", "settle"):
        raise UsageError("command")
    o.command = pos[0]
    if len(pos) < 2:
        raise UsageError("path")
    o.path = pos[1]
    o.fmt = vals.get("--format", "text")
    if o.fmt not in ("text", "json"):
        raise UsageError("format")
    for k, attr in (("--since", "since"), ("--until", "until")):
        if k in vals:
            d = parse_date(vals[k])
            if d is None:
                raise UsageError(k)
            setattr(o, attr, d)
    return o


def filter_range(expenses, since, until):
    return [e for e in expenses
            if (since is None or e.date >= since) and (until is None or e.date <= until)]


def settle(balances):
    bal = {k: v for k, v in balances.items() if v}
    out = []
    while bal:
        debtor = min((n for n in bal if bal[n] < 0), key=lambda n: (bal[n], n), default=None)
        creditor = min((n for n in bal if bal[n] > 0), key=lambda n: (-bal[n], n), default=None)
        if debtor is None or creditor is None:
            break
        amt = min(-bal[debtor], bal[creditor])
        out.append((debtor, creditor, amt))
        bal[debtor] += amt
        bal[creditor] -= amt
        for n in (debtor, creditor):
            if bal[n] == 0:
                del bal[n]
    return out


def fmt_unsigned(c):
    return f"{c // 100}.{c % 100:02d}"


def fmt_signed(c):
    return ("-" if c < 0 else "+") + fmt_unsigned(abs(c))


def run_cli(argv):
    try:
        o = parse_args(argv)
    except UsageError:
        sys.stderr.write(USAGE_LINE + "\n")
        return USAGE
    try:
        text = read_ledger(o.path)
    except LedgerReadError:
        sys.stderr.write(f"error: cannot read {o.path}\n")
        return IO
    herr, rerr, exps = parse_ledger(text)
    if herr or rerr:
        sys.stderr.write("".join(m + "\n" for m in (herr or rerr)))
        return DATA
    bal = compute_balances(filter_range(exps, o.since, o.until))
    people = sorted(bal)
    if o.command == "balances":
        if o.fmt == "json":
            out = json.dumps({"balances": [{"name": n, "cents": bal[n]} for n in people]}) + "\n"
        else:
            out = "".join(f"{n} {fmt_signed(bal[n])}\n" for n in people)
    else:
        tr = settle(bal)
        if o.fmt == "json":
            out = json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in tr]}) + "\n"
        elif tr:
            out = "".join(f"{a} -> {b} {fmt_unsigned(c)}\n" for a, b, c in tr)
        else:
            out = "nothing to settle\n"
    sys.stdout.write(out)
    sys.stdout.flush()
    return OK


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        return run_cli(argv)
    except BrokenPipeError:
        return OK
    except Exception as e:  # never show a traceback
        try:
            sys.stderr.write(f"error: {e}\n")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
