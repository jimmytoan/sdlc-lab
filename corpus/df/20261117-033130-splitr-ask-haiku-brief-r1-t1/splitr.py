#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
AMOUNT_RE = re.compile(r"^(?:[0-9]+(?:\.[0-9]{0,2})?|\.[0-9]{1,2})$")
WEIGHT_RE = re.compile(r"^[0-9]+$")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)


class UsageError(Exception):
    pass


class ReadError(Exception):
    def __init__(self, path):
        super().__init__(path)
        self.path = path


# ---------------------------------------------------------------- arguments

def parse_date(value):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        y, m, d = (int(p) for p in value.split("-"))
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            sys.exit(EXIT_OK)
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name not in ("--format", "--since", "--until"):
                raise UsageError("unknown option " + name)
            if not eq:
                i += 1
                if i >= len(argv):
                    raise UsageError("missing value for " + name)
                value = argv[i]
            opts[name[2:]] = value
        elif arg.startswith("-") and arg != "-":
            raise UsageError("unknown option " + arg)
        else:
            positional.append(arg)
        i += 1

    if len(positional) != 2:
        raise UsageError("expected a command and a ledger path")
    command, path = positional
    if command not in COMMANDS:
        raise UsageError("unknown command " + command)
    if opts["format"] not in FORMATS:
        raise UsageError("bad --format value")
    for key in ("since", "until"):
        if opts[key] is not None:
            parsed = parse_date(opts[key])
            if parsed is None:
                raise UsageError("bad --" + key + " date")
            opts[key] = parsed
    return command, path, opts


# ------------------------------------------------------------------- parsing

def read_records(path):
    """Yield (line_number, fields) for every non-blank, non-comment record.

    Physical line numbers are 1-based. A quoted field may span lines; the
    record is reported under the line where it starts."""
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8-sig")
    except (OSError, ValueError):
        raise ReadError(path)

    physical = text.splitlines()
    idx = 0
    while idx < len(physical):
        start = idx
        buf = physical[idx]
        idx += 1
        stripped = buf.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        # Continue gathering physical lines while a quoted field is open.
        while buf.count('"') % 2 == 1 and idx < len(physical):
            buf += "\n" + physical[idx]
            idx += 1
        try:
            rows = list(csv.reader([buf]))
            fields = rows[0] if rows else []
        except csv.Error:
            fields = buf.split(",")
        yield start + 1, fields


def parse_amount(text):
    """Return integer cents for a valid amount string, else None."""
    if not AMOUNT_RE.match(text):
        return None
    if "." in text:
        whole, frac = text.split(".")
    else:
        whole, frac = text, ""
    cents = int(whole or "0") * 100 + int(frac.ljust(2, "0") or "0")
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return a list of (name, weight) or None if invalid."""
    if text.strip() == "":
        return None
    entries = []
    seen = set()
    for piece in text.split(";"):
        piece = piece.strip()
        if ":" in piece:
            name, _, weight_text = piece.partition(":")
            if ":" in weight_text:
                return None
            name = name.strip()
            weight_text = weight_text.strip()
            if not WEIGHT_RE.match(weight_text):
                return None
            weight = int(weight_text)
            if weight <= 0:
                return None
        else:
            name = piece
            weight = 1
        if name == "":
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        entries.append((name, weight))
    return entries


def load_ledger(path):
    """Return (rows, errors). rows: list of dicts; errors: list of strings."""
    records = read_records(path)
    try:
        first = next(records)
    except StopIteration:
        first = None

    header = first[1] if first else []
    column_index = {}
    for i, name in enumerate(header):
        column_index.setdefault(name.strip().lower(), i)
    missing = [c for c in REQUIRED_COLUMNS if c not in column_index]
    if missing:
        return None, ["header: missing " + c for c in missing]

    def field(fields, column):
        i = column_index[column]
        return fields[i].strip() if i < len(fields) else ""

    rows = []
    errors = []
    for lineno, fields in records:
        date_text = field(fields, "date")
        date = parse_date(date_text)
        payer = field(fields, "payer")
        amount = parse_amount(field(fields, "amount"))
        participants = parse_participants(field(fields, "participants"))
        note = field(fields, "note")

        reason = None
        if date is None:
            reason = "bad-date"
        elif payer == "":
            reason = "bad-payer"
        elif amount is None:
            reason = "bad-amount"
        elif participants is None:
            reason = "bad-participants"

        if reason:
            errors.append((lineno, reason))
            continue
        rows.append({
            "line": lineno,
            "date": date,
            "payer": payer,
            "cents": amount,
            "participants": participants,
            "note": note,
        })
    return rows, errors


# ---------------------------------------------------------------- arithmetic

def split_cents(total, participants):
    """Split total cents by weight. participants: list of (name, weight).

    Returns dict name -> share. Leftover cents go to the largest weighted
    remainders; ties broken by name ascending ignoring case."""
    weight_sum = sum(w for _, w in participants)
    shares = {}
    remainders = []
    for name, w in participants:
        product = total * w
        base = product // weight_sum
        shares[name] = base
        remainders.append((-(product % weight_sum), name.casefold(), name))
    leftover = total - sum(shares.values())
    remainders.sort()
    for _, _, name in remainders[:leftover]:
        shares[name] += 1
    return shares


def compute_balances(rows):
    """Return (balances, display) keyed by casefolded name."""
    balances = {}
    display = {}

    def register(name):
        key = name.casefold()
        if key not in balances:
            balances[key] = 0
            display[key] = name
        return key

    for row in rows:
        payer_key = register(row["payer"])
        balances[payer_key] += row["cents"]
        keyed = [(register(name), w) for name, w in row["participants"]]
        shares = split_cents(row["cents"], [(k, w) for k, w in keyed])
        for key, share in shares.items():
            balances[key] -= share
    return balances, display


def settle_transfers(balances):
    """Greedy settlement. Returns list of (debtor_key, creditor_key, cents)."""
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [k for k, v in bal.items() if v < 0]
        creditors = [k for k, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda k: (bal[k], k))
        c = min(creditors, key=lambda k: (-bal[k], k))
        amount = min(-bal[d], bal[c])
        transfers.append((d, c, amount))
        bal[d] += amount
        bal[c] -= amount
    return transfers


# ------------------------------------------------------------------- output

def fmt_plain(cents):
    return "%d.%02d" % (cents // 100, cents % 100)


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt_plain(cents)
    if cents < 0:
        return "-" + fmt_plain(-cents)
    return fmt_plain(0)


def render_balances(balances, display, fmt):
    keys = sorted(balances, key=lambda k: (k, display[k]))
    if fmt == "json":
        obj = {"balances": [{"name": display[k], "cents": balances[k]} for k in keys]}
        return json.dumps(obj) + "\n"
    return "".join("%s %s\n" % (display[k], fmt_signed(balances[k])) for k in keys)


def render_settle(transfers, display, fmt):
    if fmt == "json":
        obj = {"transfers": [
            {"from": display[d], "to": display[c], "cents": amt}
            for d, c, amt in transfers]}
        return json.dumps(obj) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (display[d], display[c], fmt_plain(amt))
                   for d, c, amt in transfers)


# --------------------------------------------------------------------- main

def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as exc:
        sys.stderr.write("error: %s\n%s\n" % (exc, USAGE))
        return EXIT_USAGE

    try:
        rows, errors = load_ledger(path)
    except ReadError as exc:
        sys.stderr.write("error: cannot read %s\n" % exc.path)
        return EXIT_READ

    if rows is None:
        for message in errors:
            sys.stderr.write(message + "\n")
        return EXIT_ROWS
    if errors:
        for lineno, reason in sorted(errors):
            sys.stderr.write("line %d: %s\n" % (lineno, reason))
        return EXIT_ROWS

    if opts["since"] is not None:
        rows = [r for r in rows if r["date"] >= opts["since"]]
    if opts["until"] is not None:
        rows = [r for r in rows if r["date"] <= opts["until"]]

    balances, display = compute_balances(rows)
    if command == "balances":
        out = render_balances(balances, display, opts["format"])
    else:
        out = render_settle(settle_transfers(balances), display, opts["format"])
    sys.stdout.write(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
