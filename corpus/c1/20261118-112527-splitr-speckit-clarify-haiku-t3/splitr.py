#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py balances <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]
    python splitr.py settle   <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Ledger: CSV with header date,payer,amount,participants,note. Participants are
separated by ';' and may carry a weight as name:weight (default 1). Lines starting
with '#' and blank lines are ignored. All money is integer cents.

Exit codes: 0 success, 2 invalid header or rows, 3 unreadable file, 64 usage error.

SC-002 check (T027): 10.00 split among Ann:1, Bob:2, Cy:4 gives shares 143, 286, 571
(sum 1000 exactly); a ledger of that expense paid by Zed gives balances summing to 0.
"""
import argparse
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_DATA = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ("date", "payer", "amount", "participants", "note")

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageParser(argparse.ArgumentParser):
    """Argument parser that reports usage errors with exit code 64."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(EXIT_USAGE)


def build_parser():
    parser = UsageParser(prog="splitr.py", allow_abbrev=False)
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger")
    parser.add_argument("--format", dest="fmt", choices=["text", "json"], default="text")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    return parser


# ---------------------------------------------------------------- helpers

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount_cents(text):
    """Return a positive integer number of cents, or None if invalid."""
    m = _AMOUNT_RE.fullmatch(text)
    if not m:
        return None
    cents = int(m.group(1)) * 100
    if m.group(2):
        cents += int(m.group(2).ljust(2, "0"))
    return cents if cents > 0 else None


def name_key(text):
    return text.strip().casefold()


def format_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def format_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def parse_participants(text):
    """Return a list of (display_name, key, weight), or None if invalid."""
    if text.strip() == "":
        return None
    result = []
    seen = set()
    for part in text.split(";"):
        name_part, sep, weight_part = part.partition(":")
        name = name_part.strip()
        if not name:
            return None
        if sep:
            w = weight_part.strip()
            if not _WEIGHT_RE.fullmatch(w) or int(w) <= 0:
                return None
            weight = int(w)
        else:
            weight = 1
        key = name_key(name)
        if key in seen:
            return None
        seen.add(key)
        result.append((name, key, weight))
    return result


def split_cents(amount_cents, participants):
    """Split amount_cents by weight; participants is an ordered list of (key, weight).

    Base share is floor(A*w/W). Leftover cents go one at a time to the largest
    fractional remainder (A*w mod W), ties by listed order. Shares sum to A exactly.
    """
    total_weight = sum(w for _, w in participants)
    shares = [amount_cents * w // total_weight for _, w in participants]
    leftover = amount_cents - sum(shares)
    order = sorted(
        range(len(participants)),
        key=lambda i: (-((amount_cents * participants[i][1]) % total_weight), i),
    )
    for i in order[:leftover]:
        shares[i] += 1
    return shares


# ---------------------------------------------------------------- reading

def read_ledger_text(path):
    """Return the file text, or raise OSError / UnicodeDecodeError."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return f.read()


def split_fields(line):
    """Split one physical line into CSV fields; None if the line is malformed."""
    if line.count('"') % 2:
        return None  # unterminated quote: cannot be a single-line record
    try:
        rows = list(csv.reader([line]))
    except csv.Error:
        return None
    return rows[0] if rows else []


class Expense:
    __slots__ = ("line", "date", "payer_name", "payer_key", "amount", "participants")

    def __init__(self, line, date, payer_name, amount, participants):
        self.line = line
        self.date = date
        self.payer_name = payer_name
        self.payer_key = name_key(payer_name)
        self.amount = amount
        self.participants = participants  # list of (display_name, key, weight)


def validate_row(fields, line_no, col, width):
    """Return (Expense, None) for a valid row, or (None, reason)."""
    if fields is None or len(fields) != width:
        return None, "bad-row"
    date_text = fields[col["date"]].strip()
    date = parse_date(date_text)
    if date is None:
        return None, "bad-date"
    payer = fields[col["payer"]].strip()
    if not payer:
        return None, "bad-payer"
    amount = parse_amount_cents(fields[col["amount"]].strip())
    if amount is None:
        return None, "bad-amount"
    participants = parse_participants(fields[col["participants"]])
    if participants is None:
        return None, "bad-participants"
    return Expense(line_no, date, payer, amount, participants), None


def load_ledger(path):
    """Return (header_ok, expenses, display, errors, exit_code) pieces as a dict.

    Writes diagnostics to stderr itself. On failure, 'code' is the exit code.
    """
    try:
        text = read_ledger_text(path)
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        return {"code": EXIT_READ}

    records = []
    for line_no, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        if line.startswith("#") or line.strip() == "":
            continue
        records.append((line_no, line))

    header = split_fields(records[0][1]) if records else []
    header = header or []
    header_names = [h.strip().lower() for h in header]
    col = {}
    missing = []
    for name in REQUIRED_COLUMNS:
        if name in header_names:
            col[name] = header_names.index(name)
        else:
            missing.append(name)
    if missing:
        for name in missing:
            sys.stderr.write(f"header: missing {name}\n")
        return {"code": EXIT_DATA}

    width = len(header)
    expenses = []
    errors = []
    for line_no, line in records[1:]:
        expense, reason = validate_row(split_fields(line), line_no, col, width)
        if reason is not None:
            errors.append(f"line {line_no}: {reason}")
        else:
            expenses.append(expense)

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return {"code": EXIT_DATA}

    # Display spelling: first occurrence in file order, over all valid rows.
    display = {}
    for e in expenses:
        display.setdefault(e.payer_key, e.payer_name)
        for name, key, _ in e.participants:
            display.setdefault(key, name)

    return {"code": EXIT_OK, "expenses": expenses, "display": display}


# ---------------------------------------------------------------- computing

def in_range(expense, since, until):
    if since is not None and expense.date < since:
        return False
    if until is not None and expense.date > until:
        return False
    return True


def compute_balances(expenses):
    """Return {key: cents}; paid minus share for each person."""
    bal = {}
    for e in expenses:
        bal[e.payer_key] = bal.get(e.payer_key, 0) + e.amount
        shares = split_cents(e.amount, [(key, w) for _, key, w in e.participants])
        for (_, key, _), share in zip(e.participants, shares):
            bal[key] = bal.get(key, 0) - share
    return bal


def compute_transfers(bal, display):
    """Greedy settlement; returns list of (from_name, to_name, cents)."""
    debtors = {k: -v for k, v in bal.items() if v < 0}
    creditors = {k: v for k, v in bal.items() if v > 0}
    transfers = []
    while debtors and creditors:
        d = min(debtors, key=lambda k: (-debtors[k], k))
        c = min(creditors, key=lambda k: (-creditors[k], k))
        amount = min(debtors[d], creditors[c])
        transfers.append((display[d], display[c], amount))
        debtors[d] -= amount
        creditors[c] -= amount
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return transfers


# ---------------------------------------------------------------- rendering

def render_balances_text(bal, display):
    return [f"{display[k]} {format_signed(bal[k])}" for k in sorted(bal)]


def render_balances_json(bal, display):
    items = [{"name": display[k], "cents": bal[k]} for k in sorted(bal)]
    return json.dumps({"balances": items})


def render_settle_text(transfers):
    if not transfers:
        return ["nothing to settle"]
    return [f"{f} -> {t} {format_plain(c)}" for f, t, c in transfers]


def render_settle_json(transfers):
    items = [{"from": f, "to": t, "cents": c} for f, t, c in transfers]
    return json.dumps({"transfers": items})


# ---------------------------------------------------------------- main

def main(argv):
    parser = build_parser()
    args = parser.parse_args(argv)

    since = until = None
    if args.since is not None:
        since = parse_date(args.since)
        if since is None:
            parser.error(f"invalid --since date: {args.since!r} (expected YYYY-MM-DD)")
    if args.until is not None:
        until = parse_date(args.until)
        if until is None:
            parser.error(f"invalid --until date: {args.until!r} (expected YYYY-MM-DD)")

    loaded = load_ledger(args.ledger)
    if loaded["code"] != EXIT_OK:
        return loaded["code"]

    display = loaded["display"]
    included = [e for e in loaded["expenses"] if in_range(e, since, until)]
    bal = compute_balances(included)

    if args.command == "balances":
        if args.fmt == "json":
            out = [render_balances_json(bal, display)]
        else:
            out = render_balances_text(bal, display)
    else:
        transfers = compute_transfers(bal, display)
        if args.fmt == "json":
            out = [render_settle_json(transfers)]
        else:
            out = render_settle_text(transfers)

    for line in out:
        sys.stdout.write(line + "\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
