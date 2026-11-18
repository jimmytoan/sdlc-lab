#!/usr/bin/env python3
"""splitr - split shared trip expenses from a CSV ledger."""
import csv
import datetime
import json
import re
import sys
from typing import NamedTuple, Optional

EXIT_OK = 0
EXIT_LEDGER = 2
EXIT_READ = 3
EXIT_USAGE = 64

USAGE = ("usage: splitr.py {balances|settle} <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")

REQUIRED = ("date", "payer", "amount", "participants")


class UsageError(Exception):
    pass


class LedgerError(Exception):
    def __init__(self, lines):
        super().__init__("\n".join(lines))
        self.lines = list(lines)


class Options(NamedTuple):
    command: str
    path: str
    fmt: str = "text"
    since: Optional[datetime.date] = None
    until: Optional[datetime.date] = None


class Expense(NamedTuple):
    line: int
    date: datetime.date
    payer: str
    cents: int
    participants: tuple


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_AMOUNT_RE = re.compile(r"(\d+)(?:\.(\d{1,2}))?", re.ASCII)
_WEIGHT_RE = re.compile(r"[0-9]+", re.ASCII)


def parse_date(s):
    if not isinstance(s, str) or not _DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    m = _AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    frac = (m.group(2) or "").ljust(2, "0")
    cents = int(m.group(1)) * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(s):
    if s == "":
        return None
    out = []
    seen = set()
    for entry in s.split(";"):
        if ":" in entry:
            name, w = entry.split(":", 1)
            name = name.strip()
            w = w.strip()
            if not _WEIGHT_RE.fullmatch(w) or int(w) <= 0:
                return None
            weight = int(w)
        else:
            name = entry.strip()
            weight = 1
        if not name or name in seen:
            return None
        seen.add(name)
        out.append((name, weight))
    return out


def parse_args(argv):
    fmt = "text"
    since = until = None
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                opt, val = a.split("=", 1)
            else:
                opt, val = a, None
            if opt not in ("--format", "--since", "--until"):
                raise UsageError("unknown option: " + opt)
            if val is None:
                i += 1
                if i >= len(argv):
                    raise UsageError("option needs a value: " + opt)
                val = argv[i]
            if opt == "--format":
                if val not in ("text", "json"):
                    raise UsageError("bad --format: " + val)
                fmt = val
            else:
                d = parse_date(val)
                if d is None:
                    raise UsageError("bad date for %s: %s" % (opt, val))
                if opt == "--since":
                    since = d
                else:
                    until = d
        else:
            positional.append(a)
        i += 1
    if not positional:
        raise UsageError("no command")
    command = positional[0]
    if command not in ("balances", "settle"):
        raise UsageError("unknown command: " + command)
    if len(positional) < 2:
        raise UsageError("missing ledger path")
    if len(positional) > 2:
        raise UsageError("too many arguments")
    return Options(command, positional[1], fmt, since, until)


def read_ledger(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError, ValueError):
        raise LedgerError(["error: cannot read " + path])


def _records(text):
    """Yield (first_line_number, fields) for non-comment, non-blank records."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        if raw.endswith("\r"):
            raw = raw[:-1]
        if raw.startswith("#") or not raw.strip():
            i += 1
            continue
        start = i + 1
        buf = raw
        # continue while a quoted field is open
        while buf.count('"') % 2 == 1 and i + 1 < n:
            i += 1
            nxt = lines[i]
            if nxt.endswith("\r"):
                nxt = nxt[:-1]
            buf += "\n" + nxt
        i += 1
        try:
            rows = list(csv.reader([buf]))
            fields = rows[0] if rows else []
            ok = True
        except csv.Error:
            fields = []
            ok = False
        yield start, [f.strip() for f in fields], ok


def parse_ledger(text):
    recs = _records(text)
    header = None
    for rec in recs:
        header = rec
        break
    if header is None:
        raise LedgerError(["header: missing " + c for c in REQUIRED])
    names = header[1]
    missing = [c for c in REQUIRED if c not in names]
    if missing:
        raise LedgerError(["header: missing " + c for c in missing])
    idx = {c: names.index(c) for c in REQUIRED}

    expenses = []
    errors = []
    for line, fields, ok in recs:
        def get(c):
            j = idx[c]
            return fields[j] if ok and j < len(fields) else ""
        d = parse_date(get("date"))
        if d is None:
            errors.append("line %d: bad-date" % line)
            continue
        payer = get("payer")
        if not payer:
            errors.append("line %d: bad-payer" % line)
            continue
        cents = parse_amount(get("amount"))
        if cents is None:
            errors.append("line %d: bad-amount" % line)
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append("line %d: bad-participants" % line)
            continue
        expenses.append(Expense(line, d, payer, cents, tuple(parts)))
    if errors:
        raise LedgerError(errors)
    return expenses


def filter_expenses(expenses, since, until):
    out = []
    for e in expenses:
        if since is not None and e.date < since:
            continue
        if until is not None and e.date > until:
            continue
        out.append(e)
    return out


def split_amount(cents, weighted):
    total_w = sum(w for _, w in weighted)
    q = []
    rem = []
    for _, w in weighted:
        a, r = divmod(cents * w, total_w)
        q.append(a)
        rem.append(r)
    left = cents - sum(q)
    order = sorted(range(len(weighted)), key=lambda k: (-rem[k], k))
    for k in order[:left]:
        q[k] += 1
    return q


def compute_balances(expenses):
    bal = {}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.cents
        shares = split_amount(e.cents, e.participants)
        for (name, _), s in zip(e.participants, shares):
            bal[name] = bal.get(name, 0) - s
    return bal


def settle(balances):
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [(b, n) for n, b in bal.items() if b < 0]
        creditors = [(-b, n) for n, b in bal.items() if b > 0]
        if not debtors or not creditors:
            break
        db, dn = min(debtors)
        cb, cn = min(creditors)
        cb = -cb
        amt = min(-db, cb)
        bal[dn] += amt
        bal[cn] -= amt
        transfers.append((dn, cn, amt))
    return transfers


def fmt_plain(c):
    w, f = divmod(abs(c), 100)
    return "%d.%02d" % (w, f)


def fmt_signed(c):
    return ("-" if c < 0 else "+") + fmt_plain(c)


def render_balances(bal, fmt):
    items = sorted(bal.items())
    if fmt == "json":
        return json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]},
                          ensure_ascii=False) + "\n"
    return "".join("%s %s\n" % (n, fmt_signed(c)) for n, c in items)


def render_settle(transfers, fmt):
    if fmt == "json":
        return json.dumps({"transfers": [{"from": a, "to": b, "cents": c}
                                         for a, b, c in transfers]},
                          ensure_ascii=False) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (a, b, fmt_plain(c)) for a, b, c in transfers)


def _write(stream_name, s):
    stream = getattr(sys, stream_name)
    data = s.encode("utf-8", errors="backslashreplace")
    try:
        stream.buffer.write(data)
        stream.buffer.flush()
    except AttributeError:
        stream.write(s)
        stream.flush()


def _run(argv):
    try:
        opts = parse_args(argv)
    except UsageError as e:
        _write("stderr", "%s\n%s\n" % (e, USAGE))
        return EXIT_USAGE
    try:
        text = read_ledger(opts.path)
    except LedgerError as e:
        _write("stderr", "\n".join(e.lines) + "\n")
        return EXIT_READ
    try:
        expenses = parse_ledger(text)
    except LedgerError as e:
        _write("stderr", "\n".join(e.lines) + "\n")
        return EXIT_LEDGER
    expenses = filter_expenses(expenses, opts.since, opts.until)
    bal = compute_balances(expenses)
    if opts.command == "balances":
        out = render_balances(bal, opts.fmt)
    else:
        out = render_settle(settle(bal), opts.fmt)
    if out:
        _write("stdout", out)
    return EXIT_OK


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    try:
        return _run(argv)
    except Exception as e:  # last-resort guard: never show a traceback
        try:
            _write("stderr", "error: %s\n" % e)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
