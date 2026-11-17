#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 success, 2 invalid ledger rows or missing header column,
3 ledger cannot be read, 64 usage error.

Decisions where the spec is silent:
- Header names are matched after trimming and ignoring case. If a column
  appears twice, the first occurrence is used.
- Ignored lines before the header are skipped; the first non-ignored line is
  the header.
- Rows with fewer fields than the header get empty values for the missing
  fields (so they fail validation). Extra fields are ignored.
- A row that cannot be parsed as CSV is reported as bad-date (the first check
  fails because no usable fields are present).
- Names are trimmed (spec). Amounts and dates must match exactly, with no
  surrounding whitespace. Weights are trimmed after the colon.
- Weights may have leading zeros (`01` is weight 1); the value must be > 0.
- Amounts may have leading zeros (`01` is 1.00); the value must be > 0.
- --since after --until gives an empty result with exit 0.
- Optional `kind` column (change request 1). Absent column, empty cell, or a
  short row (missing field) means `expense`. The value is trimmed and matched
  exactly and case-sensitively: `expense` and `payment` are accepted, anything
  else (including `Payment`) gives bad-kind. bad-kind is checked after
  bad-date and before bad-payer.
- A `payment` row has exactly one recipient in `participants`: no `;`, no
  weight (a colon is rejected, so `Ann:1` is bad-participants), not empty, and
  not equal to the payer ignoring case. Payer gains the amount, recipient loses
  it; no splitting.
- Payer-first rounding (change request 1): if the payer is a participant of an
  expense and leftover cents exist, the payer takes the first leftover cent,
  then the rest go to the other participants by largest remainder.
- -h/--help is not a defined option, so it is an unknown option (exit 64).
"""
import csv
import json
import re
import sys
from datetime import date

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")

COMMANDS = ("balances", "settle")
REQUIRED = ("date", "payer", "amount", "participants", "note")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class ReadError(Exception):
    pass


def _write(stream, text):
    stream.buffer.write(text.encode("utf-8"))
    stream.flush()


def _err(text):
    _write(sys.stderr, text)


# ---------------------------------------------------------------- arguments

def parse_date_arg(value):
    if value is None or not DATE_RE.fullmatch(value):
        raise UsageError("bad date: %r" % (value,))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise UsageError("bad date: %r" % (value,))


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
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
        name, eq, value = arg.partition("=")
        if name not in ("--format", "--since", "--until"):
            raise UsageError("unknown option: %s" % arg)
        if not eq:
            if i >= len(argv):
                raise UsageError("missing value for %s" % name)
            value = argv[i]
            i += 1
        key = name[2:]
        if key == "format":
            if value not in ("text", "json"):
                raise UsageError("bad format: %r" % (value,))
            opts["format"] = value
        else:
            opts[key] = parse_date_arg(value)
    if len(positional) != 2:
        raise UsageError("expected <command> <ledger.csv>")
    command, path = positional
    if command not in COMMANDS:
        raise UsageError("unknown command: %s" % command)
    return command, path, opts


# ---------------------------------------------------------------- reading

def read_lines(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise ReadError()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    result = []
    for ln in lines:
        if ln.endswith("\r"):
            ln = ln[:-1]
        result.append(ln)
    return result


def is_ignored(line):
    s = line.strip()
    return s == "" or s.startswith("#")


def split_csv_line(line):
    """Parse one physical line as CSV.

    Strict parsing is tried first. If quoting is malformed, a lenient parse is
    used so that a bad quote in one field (for example `note`) does not turn
    into a reason about a different field. Returns None only if even the
    lenient parse fails.
    """
    try:
        rows = list(csv.reader([line], strict=True))
    except csv.Error:
        try:
            rows = list(csv.reader([line], strict=False))
        except csv.Error:
            return None
    return rows[0] if rows else []


def parse_header(fields):
    index = {}
    for i, name in enumerate(fields or []):
        key = name.strip().casefold()
        if key and key not in index:
            index[key] = i
    for col in REQUIRED:
        if col not in index:
            return None, col
    return index, None


# ---------------------------------------------------------------- rows

def parse_amount(text):
    m = AMOUNT_RE.fullmatch(text)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (display_name, key, weight) or None if invalid."""
    if text.strip() == "":
        return None
    parts = []
    seen = set()
    for entry in text.split(";"):
        entry = entry.strip()
        if entry == "":
            return None
        if ":" in entry:
            name, _, wtext = entry.rpartition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight <= 0:
                return None
        else:
            name = entry
            weight = 1
        if name == "":
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        parts.append((name, key, weight))
    return parts


def parse_payment_recipient(text, payer):
    """A payment has exactly one recipient, no weight, not the payer.
    Returns (display_name, key, 1) or None if invalid."""
    if ";" in text or ":" in text:
        return None
    name = text.strip()
    if name == "":
        return None
    if name.casefold() == payer.casefold():
        return None
    return (name, name.casefold(), 1)


def parse_row(lineno, fields, index):
    """Returns (record, None) or (None, reason)."""
    def get(col):
        i = index.get(col)
        if i is None:
            return ""
        return fields[i] if i < len(fields) else ""

    raw_date = get("date")
    if not DATE_RE.fullmatch(raw_date):
        return None, "bad-date"
    try:
        d = date.fromisoformat(raw_date)
    except ValueError:
        return None, "bad-date"

    kind = get("kind").strip()
    if kind in ("", "expense"):
        kind = "expense"
    elif kind != "payment":
        return None, "bad-kind"

    payer = get("payer").strip()
    if payer == "":
        return None, "bad-payer"

    cents = parse_amount(get("amount"))
    if cents is None:
        return None, "bad-amount"

    if kind == "payment":
        recipient = parse_payment_recipient(get("participants"), payer)
        if recipient is None:
            return None, "bad-participants"
        parts = [recipient]
    else:
        parts = parse_participants(get("participants"))
        if parts is None:
            return None, "bad-participants"

    return {"date": d, "payer": payer, "amount": cents, "kind": kind,
            "parts": parts, "line": lineno}, None


def split_expense(cents, weights):
    total = sum(weights)
    bases = [cents * w // total for w in weights]
    rems = [cents * w % total for w in weights]
    leftover = cents - sum(bases)
    return bases, rems, leftover


# ---------------------------------------------------------------- compute

def load(path, opts):
    """Returns (records, registry, error_lines). Raises ReadError."""
    lines = read_lines(path)
    header_idx = None
    body = []
    for lineno, line in enumerate(lines, start=1):
        if is_ignored(line):
            continue
        if header_idx is None:
            fields = split_csv_line(line) or []
            header_idx, missing = parse_header(fields)
            if header_idx is None:
                return None, None, ["header: missing %s" % missing]
            continue
        body.append((lineno, line))
    if header_idx is None:
        return None, None, ["header: missing %s" % REQUIRED[0]]

    records = []
    errors = []
    for lineno, line in body:
        fields = split_csv_line(line)
        if fields is None:
            errors.append((lineno, "bad-date"))
            continue
        rec, reason = parse_row(lineno, fields, header_idx)
        if reason is not None:
            errors.append((lineno, reason))
        else:
            records.append(rec)
    if errors:
        return None, None, ["line %d: %s" % e for e in errors]

    registry = {}
    for rec in records:
        for name, key in [(rec["payer"], rec["payer"].casefold())] + \
                [(p[0], p[1]) for p in rec["parts"]]:
            registry.setdefault(key, name)

    since, until = opts["since"], opts["until"]
    considered = [r for r in records
                  if (since is None or r["date"] >= since)
                  and (until is None or r["date"] <= until)]
    return considered, registry, []


def expense_shares(rec, pkey):
    """Shares per participant (same order as rec["parts"]).

    Payer-first rounding: if the payer is a participant and there is at least
    one leftover cent, the payer takes the first leftover cent; the remaining
    leftover cents go to the other participants by largest remainder, ties by
    casefolded name ascending.
    """
    parts = rec["parts"]
    bases, rems, leftover = split_expense(
        rec["amount"], [p[2] for p in parts])
    order = sorted(range(len(parts)),
                   key=lambda i: (-rems[i], parts[i][1]))
    shares = list(bases)
    payer_idx = None
    for i, p in enumerate(parts):
        if p[1] == pkey:
            payer_idx = i
            break
    if payer_idx is not None and leftover > 0:
        shares[payer_idx] += 1
        leftover -= 1
        order = [i for i in order if i != payer_idx]
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def compute_balances(considered):
    """Returns dict key -> cents for every person in considered rows."""
    bal = {}
    for rec in considered:
        pkey = rec["payer"].casefold()
        bal.setdefault(pkey, 0)
        parts = rec["parts"]
        for _, key, _ in parts:
            bal.setdefault(key, 0)
        if rec["kind"] == "payment":
            # Payments are never split: payer gains, recipient loses.
            bal[pkey] += rec["amount"]
            bal[parts[0][1]] -= rec["amount"]
            continue
        shares = expense_shares(rec, pkey)
        bal[pkey] += rec["amount"]
        for (name, key, _), share in zip(parts, shares):
            bal[key] -= share
    return bal


def settle(bal):
    """Returns list of (debtor_key, creditor_key, cents)."""
    bal = dict(bal)
    transfers = []
    while True:
        debtors = [k for k, v in bal.items() if v < 0]
        creditors = [k for k, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda k: (bal[k], k))
        c = min(creditors, key=lambda k: (-bal[k], k))
        amt = min(-bal[d], bal[c])
        bal[d] += amt
        bal[c] -= amt
        transfers.append((d, c, amt))
    return transfers


# ---------------------------------------------------------------- format

def format_signed(c):
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    a = abs(c)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def format_unsigned(c):
    a = abs(c)
    return "%d.%02d" % (a // 100, a % 100)


def render(command, fmt, bal, registry, transfers):
    if command == "balances":
        keys = sorted(bal, key=lambda k: k)
        if fmt == "json":
            obj = {"balances": [{"name": registry[k], "cents": bal[k]}
                                for k in keys]}
            return json.dumps(obj) + "\n"
        return "".join("%s %s\n" % (registry[k], format_signed(bal[k]))
                       for k in keys)
    if fmt == "json":
        obj = {"transfers": [{"from": registry[d], "to": registry[c],
                              "cents": amt} for d, c, amt in transfers]}
        return json.dumps(obj) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (registry[d], registry[c],
                                      format_unsigned(amt))
                   for d, c, amt in transfers)


# ---------------------------------------------------------------- main

def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as e:
        _err("usage error: %s\n%s" % (e, USAGE))
        return 64

    try:
        considered, registry, errors = load(path, opts)
    except ReadError:
        _err("error: cannot read %s\n" % path)
        return 3
    if errors:
        for line in errors:
            _err(line + "\n")
        return 2

    bal = compute_balances(considered)
    transfers = settle(bal) if command == "settle" else []
    out = render(command, opts["format"], bal, registry, transfers)
    _write(sys.stdout, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
