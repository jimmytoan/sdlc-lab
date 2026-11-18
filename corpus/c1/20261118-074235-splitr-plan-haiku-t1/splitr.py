#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: splitr.py <balances|settle> <ledger.csv> [--format text|json]
                 [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_CANNOT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_WEIGHT_RE = re.compile(r"[1-9][0-9]*")


class UsageError(Exception):
    pass


class UnreadableError(Exception):
    pass


# ---------------------------------------------------------------- arguments

def parse_date(text):
    """Strict YYYY-MM-DD real calendar date, or None."""
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    """Return a dict with command, ledger, format, since, until. Raises UsageError.

    Returns None when help was requested."""
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    value_opts = {"--format", "--since", "--until"}
    i = 0
    only_positional = False
    while i < len(argv):
        arg = argv[i]
        i += 1
        if only_positional or arg == "-" or not arg.startswith("-"):
            positional.append(arg)
            continue
        if arg == "--":
            only_positional = True
            continue
        if arg in ("-h", "--help"):
            return None
        name, eq, inline = arg.partition("=")
        if name not in value_opts:
            raise UsageError(f"unknown option: {arg}")
        if eq:
            value = inline
        else:
            if i >= len(argv):
                raise UsageError(f"option {name} requires a value")
            value = argv[i]
            i += 1
        key = name[2:]
        if key == "format":
            if value not in FORMATS:
                raise UsageError(f"invalid --format value: {value!r}")
            opts["format"] = value
        else:
            d = parse_date(value)
            if d is None:
                raise UsageError(f"invalid --{key} date: {value!r}")
            opts[key] = d
    if len(positional) < 1:
        raise UsageError("missing command")
    if positional[0] not in COMMANDS:
        raise UsageError(f"unknown command: {positional[0]}")
    if len(positional) < 2:
        raise UsageError("missing ledger file")
    if len(positional) > 2:
        raise UsageError("unexpected extra argument: " + positional[2])
    opts["command"] = positional[0]
    opts["ledger"] = positional[1]
    return opts


# ---------------------------------------------------------------- ledger reading

def read_text(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        raise UnreadableError()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UnreadableError()


def iter_records(text):
    """Yield (line_number, [fields]) for each non-comment, non-blank CSV record.

    Comment and blank lines are skipped only when no record is in progress.
    A record continues over physical lines while a quote is open."""
    buf = ""
    start = 0
    for lineno, line in enumerate(io.StringIO(text, newline=""), start=1):
        if not buf:
            stripped = line.strip()
            if stripped == "" or line.lstrip().startswith("#"):
                continue
            start = lineno
        buf += line
        if buf.count('"') % 2 == 0:
            yield start, _parse_fields(buf)
            buf = ""
    if buf:
        yield start, _parse_fields(buf)


def _parse_fields(buf):
    try:
        rows = list(csv.reader(io.StringIO(buf, newline="")))
    except csv.Error:
        return [""]
    return rows[0] if rows else [""]


# ---------------------------------------------------------------- validation

def parse_participants(field):
    """Return list of (name, weight) or None if invalid."""
    entries = field.split(";")
    result = []
    seen = set()
    for entry in entries:
        parts = entry.split(":")
        if len(parts) > 2:
            return None
        name = parts[0].strip()
        if name == "":
            return None
        if len(parts) == 2:
            w = parts[1].strip()
            if not _WEIGHT_RE.fullmatch(w):
                return None
            weight = int(w)
        else:
            weight = 1
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    if not result:
        return None
    return result


def parse_amount(text):
    """Return cents (>0) or None."""
    if not _AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")
    if cents <= 0:
        return None
    return cents


def validate_row(fields):
    """Return (error_reason, record) where record is a dict or None."""
    fields = list(fields) + [""] * (4 - len(fields)) if len(fields) < 4 else list(fields)
    date_s, payer_s, amount_s, parts_s = (f for f in fields[:4])
    date = parse_date(date_s.strip())
    if date is None:
        return "bad-date", None
    payer = payer_s.strip()
    if payer == "":
        return "bad-payer", None
    cents = parse_amount(amount_s.strip())
    if cents is None:
        return "bad-amount", None
    parts = parse_participants(parts_s)
    if parts is None:
        return "bad-participants", None
    return None, {"date": date, "payer": payer, "cents": cents, "participants": parts}


# ---------------------------------------------------------------- splitting

def split_amount(cents, participants):
    """Largest-remainder split by weight. Returns dict name -> share cents."""
    total_w = sum(w for _, w in participants)
    shares = {}
    rems = []
    for name, w in participants:
        num = cents * w
        shares[name] = num // total_w
        rems.append((-(num % total_w), name))
    leftover = cents - sum(shares.values())
    rems.sort()  # largest remainder first, ties by name ascending
    for k in range(leftover):
        shares[rems[k][1]] += 1
    assert sum(shares.values()) == cents
    return shares


def compute_balances(records):
    bal = {}
    for rec in records:
        bal[rec["payer"]] = bal.get(rec["payer"], 0) + rec["cents"]
        for name, share in split_amount(rec["cents"], rec["participants"]).items():
            bal[name] = bal.get(name, 0) - share
        # ensure every participant appears even with zero balance
        for name, _ in rec["participants"]:
            bal.setdefault(name, 0)
    assert sum(bal.values()) == 0
    return bal


def settle(bal):
    """Greedy settlement. Returns list of (from, to, cents)."""
    cur = {n: c for n, c in bal.items() if c != 0}
    transfers = []
    while True:
        creditors = [n for n, c in cur.items() if c > 0]
        debtors = [n for n, c in cur.items() if c < 0]
        if not creditors or not debtors:
            break
        c = min(creditors, key=lambda n: (-cur[n], n))
        d = min(debtors, key=lambda n: (cur[n], n))
        amt = min(cur[c], -cur[d])
        transfers.append((d, c, amt))
        cur[c] -= amt
        cur[d] += amt
        cur = {n: v for n, v in cur.items() if v != 0}
    return transfers


# ---------------------------------------------------------------- formatting

def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


# ---------------------------------------------------------------- main logic

def load_ledger(path):
    """Return (records, errors). Raises UnreadableError, or HeaderError."""
    text = read_text(path)
    records_iter = iter_records(text)
    try:
        hline, header = next(records_iter)
    except StopIteration:
        raise HeaderError("date")
    cols = {}
    for idx, name in enumerate(header):
        key = name.strip().lower()
        if key and key not in cols:
            cols[key] = idx
    for req in REQUIRED_COLUMNS:
        if req not in cols:
            raise HeaderError(req)

    def pick(fields, col):
        i = cols[col]
        return fields[i] if i < len(fields) else ""

    records = []
    errors = []
    for lineno, fields in records_iter:
        row = [pick(fields, c) for c in REQUIRED_COLUMNS]
        reason, rec = validate_row(row)
        if reason is not None:
            errors.append(f"line {lineno}: {reason}")
        else:
            records.append(rec)
    return records, errors


class HeaderError(Exception):
    def __init__(self, column):
        super().__init__(column)
        self.column = column


def in_range(rec, since, until):
    if since is not None and rec["date"] < since:
        return False
    if until is not None and rec["date"] > until:
        return False
    return True


def run(argv, out, err):
    try:
        opts = parse_args(argv)
    except UsageError as e:
        err.write(f"error: {e}\n{USAGE}\n")
        return EXIT_USAGE
    if opts is None:
        out.write(USAGE + "\n")
        return EXIT_OK

    try:
        records, errors = load_ledger(opts["ledger"])
    except UnreadableError:
        err.write(f"error: cannot read {opts['ledger']}\n")
        return EXIT_CANNOT_READ
    except HeaderError as e:
        err.write(f"header: missing {e.column}\n")
        return EXIT_BAD_ROWS
    if errors:
        for line in errors:
            err.write(line + "\n")
        return EXIT_BAD_ROWS

    included = [r for r in records if in_range(r, opts["since"], opts["until"])]
    bal = compute_balances(included)
    json_out = opts["format"] == "json"

    if opts["command"] == "balances":
        names = sorted(bal)
        if json_out:
            payload = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            out.write(json.dumps(payload) + "\n")
        else:
            for n in names:
                out.write(f"{n} {fmt_signed(bal[n])}\n")
    else:
        transfers = settle(bal)
        if json_out:
            payload = {"transfers": [{"from": d, "to": c, "cents": a}
                                     for d, c, a in transfers]}
            out.write(json.dumps(payload) + "\n")
        elif not transfers:
            out.write("nothing to settle\n")
        else:
            for d, c, a in transfers:
                out.write(f"{d} -> {c} {fmt_plain(a)}\n")
    return EXIT_OK


def main(argv):
    out = io.StringIO()
    err = io.StringIO()
    code = run(argv, out, err)
    # Output is only produced after validation succeeds.
    if err.getvalue():
        sys.stderr.write(err.getvalue())
    if out.getvalue():
        sys.stdout.write(out.getvalue())
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
