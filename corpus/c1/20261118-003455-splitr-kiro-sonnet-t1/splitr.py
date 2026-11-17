#!/usr/bin/env python3
"""splitr: shared-expense balances and settlement."""
import csv
import datetime
import io
import json
import re
import sys
from dataclasses import dataclass

COLUMNS = ["date", "payer", "amount", "participants", "note"]
USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class UsageError(Exception):
    pass


class ReadError(Exception):
    pass


class LedgerError(Exception):
    def __init__(self, lines):
        super().__init__("\n".join(lines))
        self.lines = lines


@dataclass(frozen=True)
class Expense:
    date: str
    payer: str
    cents: int
    participants: tuple  # of (name, weight)


@dataclass
class Options:
    command: str = ""
    path: str = ""
    format: str = "text"
    since: str = None
    until: str = None


def format_cents(c, signed):
    q, r = divmod(abs(c), 100)
    s = f"{q}.{r:02d}"
    if signed:
        return ("-" if c < 0 else "+") + s
    return s


def parse_date(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s, re.ASCII):
        return None
    try:
        datetime.date(int(s[:4]), int(s[5:7]), int(s[8:]))
    except ValueError:
        return None
    return s


def parse_amount(s):
    s = s.strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s, re.ASCII):
        return None
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    return cents if cents > 0 else None


def parse_participants(s):
    out = []
    seen = set()
    for entry in s.split(";"):
        entry = entry.strip()
        if not entry:
            return None
        name, weight = entry, 1
        if ":" in entry:
            name, _, w = entry.rpartition(":")
            name = name.strip()
            w = w.strip()
            if not re.fullmatch(r"\d+", w, re.ASCII) or int(w) <= 0:
                return None
            weight = int(w)
        if not name or name in seen:
            return None
        seen.add(name)
        out.append((name, weight))
    return tuple(out)


def validate_row(fields):
    """Return Expense or a reason string."""
    date = parse_date(fields.get("date", "").strip())
    if date is None:
        return "bad-date"
    payer = fields.get("payer", "").strip()
    if not payer:
        return "bad-payer"
    cents = parse_amount(fields.get("amount", ""))
    if cents is None:
        return "bad-amount"
    parts = parse_participants(fields.get("participants", ""))
    if parts is None:
        return "bad-participants"
    return Expense(date, payer, cents, parts)


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        raise ReadError(path)
    if text.startswith("\ufeff"):
        text = text[1:]
    return text


def records(text):
    """Yield (line_number, fields) skipping comments and blank lines."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    n = len(lines)
    while i < n:
        start = i + 1
        line = lines[i].rstrip("\r") if lines[i].endswith("\r") else lines[i]
        i += 1
        if line.startswith("#") or not line.strip():
            continue
        rec = line
        while rec.count('"') % 2 == 1 and i < n:
            rec += "\n" + lines[i].rstrip("\r")
            i += 1
        try:
            rows = list(csv.reader(io.StringIO(rec, newline="")))
        except csv.Error:
            rows = [[rec]]
        fields = rows[0] if rows else []
        yield start, fields


def parse_ledger(text):
    recs = records(text)
    header = None
    for _, fields in recs:
        header = [h.strip() for h in fields]
        break
    if header is None:
        header = []
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise LedgerError([f"header: missing {c}" for c in missing])
    idx = {c: header.index(c) for c in COLUMNS}
    expenses, errors = [], []
    for ln, fields in recs:
        row = {c: (fields[i] if i < len(fields) else "") for c, i in idx.items()}
        r = validate_row(row)
        if isinstance(r, str):
            errors.append(f"line {ln}: {r}")
        else:
            expenses.append(r)
    if errors:
        raise LedgerError(errors)
    return expenses


def filter_range(expenses, since, until):
    return [e for e in expenses
            if (since is None or e.date >= since) and (until is None or e.date <= until)]


def split_expense(cents, parts):
    total = sum(w for _, w in parts)
    shares = [cents * w // total for _, w in parts]
    rems = [cents * w % total for _, w in parts]
    left = cents - sum(shares)
    order = sorted(range(len(parts)), key=lambda i: (-rems[i], i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def compute_balances(expenses):
    bal = {}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.cents
        for (name, _), sh in zip(e.participants, split_expense(e.cents, e.participants)):
            bal[name] = bal.get(name, 0) - sh
    return bal


def compute_transfers(balances):
    bal = {k: v for k, v in balances.items() if v != 0}
    out = []
    while True:
        debtors = [k for k, v in bal.items() if v < 0]
        creditors = [k for k, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda k: (bal[k], k))
        c = min(creditors, key=lambda k: (-bal[k], k))
        amt = min(-bal[d], bal[c])
        out.append((d, c, amt))
        bal[d] += amt
        bal[c] -= amt
        if bal[d] == 0:
            del bal[d]
        if bal[c] == 0:
            del bal[c]
    return out


def render(opts, balances, transfers):
    items = sorted(balances.items())
    if opts.command == "balances":
        if opts.format == "json":
            return json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}) + "\n"
        return "".join(f"{n} {format_cents(c, True)}\n" for n, c in items)
    if opts.format == "json":
        return json.dumps({"transfers": [{"from": a, "to": b, "cents": c}
                                         for a, b, c in transfers]}) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{a} -> {b} {format_cents(c, False)}\n" for a, b, c in transfers)


def parse_args(argv):
    o = Options()
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--format", "--since", "--until"):
            if i + 1 >= len(argv):
                raise UsageError(f"option {a} needs a value")
            setattr(o, a[2:], argv[i + 1])
            i += 2
            continue
        if a.startswith("--") and len(a) > 2:
            if "=" in a and a.split("=", 1)[0] in ("--format", "--since", "--until"):
                k, v = a.split("=", 1)
                setattr(o, k[2:], v)
                i += 1
                continue
            raise UsageError(f"unknown option {a}")
        pos.append(a)
        i += 1
    if not pos:
        raise UsageError("missing command")
    if pos[0] not in ("balances", "settle"):
        raise UsageError(f"unknown command {pos[0]}")
    if len(pos) < 2:
        raise UsageError("missing ledger file")
    if len(pos) > 2:
        raise UsageError("too many arguments")
    o.command, o.path = pos
    if o.format not in ("text", "json"):
        raise UsageError("bad --format")
    for k in ("since", "until"):
        v = getattr(o, k)
        if v is not None and parse_date(v) is None:
            raise UsageError(f"bad --{k} date")
    if o.since and o.until and o.since > o.until:
        raise UsageError("--since is later than --until")
    return o


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        try:
            opts = parse_args(argv)
            text = read_ledger(opts.path)
            exps = filter_range(parse_ledger(text), opts.since, opts.until)
            bal = compute_balances(exps)
            tr = compute_transfers(bal) if opts.command == "settle" else []
            out = render(opts, bal, tr)
        except UsageError as e:
            sys.stderr.write(f"error: {e}\n{USAGE}\n")
            return 64
        except LedgerError as e:
            sys.stderr.write("\n".join(e.lines) + "\n")
            return 2
        except ReadError as e:
            sys.stderr.write(f"error: cannot read {e}\n")
            return 3
        sys.stdout.write(out)
        sys.stdout.flush()
        return 0
    except BrokenPipeError:
        return 0
    except Exception as e:  # no tracebacks
        try:
            sys.stderr.write(f"error: {type(e).__name__}: {e}\n")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
