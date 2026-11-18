"""splitr: shared-expense balances and settlement from a CSV ledger."""
import csv
import datetime
import json
import re
import sys
from dataclasses import dataclass

REQUIRED = ("date", "payer", "amount", "participants", "note")
REASONS = ("bad-date", "bad-payer", "bad-amount", "bad-participants")
USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class LedgerError(Exception):
    def __init__(self, lines, code):
        super().__init__("\n".join(lines))
        self.lines = lines
        self.code = code


@dataclass(frozen=True)
class Expense:
    line: int
    date: datetime.date
    payer: str
    cents: int
    participants: tuple


@dataclass
class Args:
    command: str
    path: str
    fmt: str = "text"
    since: datetime.date | None = None
    until: datetime.date | None = None


@dataclass(frozen=True)
class Transfer:
    payer: str
    payee: str
    cents: int


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def fmt_unsigned(cents):
    q, r = divmod(abs(cents), 100)
    return f"{q}.{r:02d}"


def fmt_signed(cents):
    return ("-" if cents < 0 else "+") + fmt_unsigned(cents)


def split_amount(cents, participants):
    total = sum(w for _, w in participants)
    shares = [cents * w // total for _, w in participants]
    rems = [cents * w % total for _, w in participants]
    left = cents - sum(shares)
    order = sorted(range(len(participants)), key=lambda i: -rems[i])
    for i in order[:left]:
        shares[i] += 1
    return [(n, s) for (n, _), s in zip(participants, shares)]


def parse_args(argv):
    command = path = None
    positional = []
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name not in ("--format", "--since", "--until"):
                raise UsageError(f"unknown option {a}")
            if not eq:
                i += 1
                if i >= len(argv):
                    raise UsageError(f"option {name} needs a value")
                val = argv[i]
            opts[name] = val
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option {a}")
        else:
            positional.append(a)
        i += 1
    if not positional:
        raise UsageError("missing command")
    command = positional[0]
    if command not in ("balances", "settle"):
        raise UsageError(f"unknown command {command}")
    if len(positional) < 2:
        raise UsageError("missing ledger path")
    if len(positional) > 2:
        raise UsageError("too many arguments")
    path = positional[1]
    fmt = opts.get("--format", "text")
    if fmt not in ("text", "json"):
        raise UsageError(f"bad --format {fmt}")
    dates = {}
    for k in ("--since", "--until"):
        if k in opts:
            d = parse_date(opts[k])
            if d is None:
                raise UsageError(f"bad {k} date {opts[k]}")
            dates[k] = d
    return Args(command, path, fmt, dates.get("--since"), dates.get("--until"))


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise LedgerError([f"error: cannot read {path}"], 3)


def validate_row(f):
    """f: dict of stripped fields. Returns Expense fields tuple or a reason."""
    d = parse_date(f["date"])
    if d is None:
        return "bad-date"
    if not f["payer"]:
        return "bad-payer"
    m = AMOUNT_RE.fullmatch(f["amount"])
    if not m:
        return "bad-amount"
    frac = (m.group(2) or "").ljust(2, "0")
    cents = int(m.group(1)) * 100 + int(frac)
    if cents == 0:
        return "bad-amount"
    raw = f["participants"]
    if not raw:
        return "bad-participants"
    parts = []
    seen = set()
    for item in raw.split(";"):
        name, colon, w = item.partition(":")
        name = name.strip()
        if not name or name in seen:
            return "bad-participants"
        if colon:
            w = w.strip()
            if not WEIGHT_RE.fullmatch(w) or int(w) <= 0:
                return "bad-participants"
            weight = int(w)
        else:
            weight = 1
        seen.add(name)
        parts.append((name, weight))
    return d, f["payer"], cents, tuple(parts)


def parse_ledger(text):
    header = None
    index = {}
    expenses = []
    errors = []
    for n, line in enumerate(text.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip() or line.startswith("#"):
            continue
        try:
            fields = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            fields = None
        if header is None:
            header = [c.strip() for c in fields] if fields else []
            missing = [c for c in REQUIRED if c not in header]
            if missing:
                raise LedgerError([f"header: missing {c}" for c in missing], 2)
            index = {}
            for pos, c in enumerate(header):
                index.setdefault(c, pos)
            continue
        if fields is None:
            errors.append(f"line {n}: bad-date")
            continue
        row = {}
        for c in REQUIRED:
            pos = index[c]
            row[c] = fields[pos].strip() if pos < len(fields) else ""
        res = validate_row(row)
        if isinstance(res, str):
            errors.append(f"line {n}: {res}")
        else:
            expenses.append(Expense(n, *res))
    if header is None:
        raise LedgerError([f"header: missing {c}" for c in REQUIRED], 2)
    if errors:
        raise LedgerError(errors, 2)
    return expenses


def filter_by_date(expenses, since, until):
    out = []
    for e in expenses:
        if since is not None and e.date < since:
            continue
        if until is not None and e.date > until:
            continue
        out.append(e)
    return out


def compute_balances(expenses):
    bal = {}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.cents
        for name, share in split_amount(e.cents, e.participants):
            bal[name] = bal.get(name, 0) - share
    return bal


def settle(balances):
    bal = dict(balances)
    out = []
    while True:
        debtors = [(v, k) for k, v in bal.items() if v < 0]
        creditors = [(v, k) for k, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        dv, dn = min(debtors)
        cv, cn = min((-v, k) for v, k in creditors)
        cv = -cv
        amt = min(-dv, cv)
        out.append(Transfer(dn, cn, amt))
        bal[dn] += amt
        bal[cn] -= amt
    return out


def render(command, fmt, data):
    if command == "balances":
        items = sorted(data.items())
        if fmt == "json":
            return json.dumps({"balances": [{"name": k, "cents": v} for k, v in items]},
                              ensure_ascii=False) + "\n"
        return "".join(f"{k} {fmt_signed(v)}\n" for k, v in items)
    if fmt == "json":
        return json.dumps({"transfers": [{"from": t.payer, "to": t.payee, "cents": t.cents}
                                         for t in data]}, ensure_ascii=False) + "\n"
    if not data:
        return "nothing to settle\n"
    return "".join(f"{t.payer} -> {t.payee} {fmt_unsigned(t.cents)}\n" for t in data)


def _err(msg):
    sys.stderr.buffer.write((msg + "\n").encode("utf-8", "replace"))
    sys.stderr.flush()


def main(argv):
    try:
        args = parse_args(argv)
    except UsageError as e:
        _err(f"{USAGE}\nerror: {e}")
        return 64
    try:
        text = read_ledger(args.path)
        expenses = parse_ledger(text)
    except LedgerError as e:
        _err("\n".join(e.lines))
        return e.code
    expenses = filter_by_date(expenses, args.since, args.until)
    bal = compute_balances(expenses)
    data = bal if args.command == "balances" else settle(bal)
    out = render(args.command, args.fmt, data)
    try:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
