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
from collections import defaultdict

EXIT_OK = 0
EXIT_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED = ("date", "payer", "amount", "participants", "note")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")

HELP = """usage: splitr.py <balances|settle> <ledger.csv> [--format text|json]
                   [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- output

def out(text):
    sys.stdout.buffer.write(text.encode("utf-8", "surrogateescape"))
    sys.stdout.flush()


def err(text):
    sys.stderr.buffer.write((text + "\n").encode("utf-8", "surrogateescape"))
    sys.stderr.flush()


# ---------------------------------------------------------------- arguments

def parse_date(s):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    only_positional = False
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if only_positional or a == "-" or not a.startswith("-"):
            positional.append(a)
            continue
        if a == "--":
            only_positional = True
            continue
        if a in ("-h", "--help"):
            raise SystemExit_Help()
        name, eq, value = a.partition("=")
        if name not in ("--format", "--since", "--until"):
            raise UsageError(f"unknown option: {name}")
        if not eq:
            if i >= len(argv):
                raise UsageError(f"option {name} requires a value")
            value = argv[i]
            i += 1
        key = name[2:]
        if key == "format":
            if value not in FORMATS:
                raise UsageError(f"bad --format value: {value}")
            opts["format"] = value
        else:
            d = parse_date(value)
            if d is None:
                raise UsageError(f"bad --{key} date: {value}")
            opts[key] = d
    if len(positional) != 2:
        raise UsageError("expected <command> <ledger.csv>")
    command, path = positional
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    opts["command"] = command
    opts["path"] = path
    return opts


class SystemExit_Help(Exception):
    pass


# ---------------------------------------------------------------- file reading

def read_ledger(path):
    """Return a list of (line_no, text) with 1-based physical numbers, or None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    lines = []
    for n, p in enumerate(parts, 1):
        if p.endswith("\r"):
            p = p[:-1]
        lines.append((n, p))
    return lines


def is_ignored(text):
    s = text.strip()
    return s == "" or s.startswith("#")


def parse_fields(text):
    try:
        rows = list(csv.reader([text]))
    except csv.Error:
        return text.split(",")
    return rows[0] if rows else []


# ---------------------------------------------------------------- rows

def get(fields, idx, col):
    i = idx[col]
    return fields[i] if i < len(fields) else ""


def parse_participants(raw):
    """Return list of (display_name, weight, key) or None if invalid."""
    if raw.strip() == "":
        return None
    result = []
    seen = set()
    for part in raw.split(";"):
        part = part.strip()
        if ":" in part:
            name, _, w = part.rpartition(":")
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.fullmatch(w):
                return None
            weight = int(w)
            if weight <= 0:
                return None
        else:
            name = part
            weight = 1
        if name == "":
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, weight, key))
    return result


def parse_payment_recipient(raw, payer_key):
    """Return (display, 1, key) for a payment recipient, or None if invalid.

    A payment names exactly one person, with no weight and not the payer."""
    s = raw.strip()
    if s == "" or ":" in s or ";" in s:
        return None
    key = s.casefold()
    if key == payer_key:
        return None
    return (s, 1, key)


def parse_cents(s):
    """Return cents for a valid amount string, or None."""
    if not AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


KINDS = ("expense", "payment")


def validate_row(fields, idx):
    """Return (reason, None) on failure or (None, expense) on success.

    expense = (kind, date, payer_display, payer_key, cents, [(display, weight, key)])
    A payment has exactly one entry in the parts list, with weight 1.
    """
    d_raw = get(fields, idx, "date").strip()
    d = parse_date(d_raw)
    if d is None:
        return "bad-date", None
    kind = get(fields, idx, "kind").strip() if "kind" in idx else ""
    if kind == "":
        kind = "expense"
    if kind not in KINDS:
        return "bad-kind", None
    payer = get(fields, idx, "payer").strip()
    if payer == "":
        return "bad-payer", None
    cents = parse_cents(get(fields, idx, "amount").strip())
    if cents is None:
        return "bad-amount", None
    pk = payer.casefold()
    if kind == "payment":
        recipient = parse_payment_recipient(get(fields, idx, "participants"), pk)
        if recipient is None:
            return "bad-participants", None
        parts = [recipient]
    else:
        parts = parse_participants(get(fields, idx, "participants").strip())
        if parts is None:
            return "bad-participants", None
    return None, (kind, d, payer, pk, cents, parts)


# ---------------------------------------------------------------- splitting

def split(cents, weighted, payer_key=None):
    """weighted: list of (key, weight). Return {key: share_cents}.

    If payer_key is a participant and there is leftover, the payer gets the
    first leftover cent; the rest go by largest remainder, skipping the payer.
    """
    total = sum(w for _, w in weighted)
    shares = {}
    rems = []
    for key, w in weighted:
        num = cents * w
        shares[key] = num // total
        rems.append((num % total, key))
    leftover = cents - sum(shares.values())
    rems.sort(key=lambda t: (-t[0], t[1]))
    if payer_key is not None and leftover >= 1 and payer_key in shares:
        shares[payer_key] += 1
        leftover -= 1
        rems = [t for t in rems if t[1] != payer_key]
    for _, key in rems[:leftover]:
        shares[key] += 1
    assert sum(shares.values()) == cents
    return shares


def compute_balances(expenses):
    bal = defaultdict(int)
    for kind, _d, _pd, pk, cents, parts in expenses:
        if kind == "payment":
            _, _, rk = parts[0]
            bal[pk] += cents
            bal[rk] -= cents
            bal.setdefault(pk, 0)
            bal.setdefault(rk, 0)
            continue
        bal[pk] += cents
        shares = split(cents, [(k, w) for _, w, k in parts], pk)
        for k, s in shares.items():
            bal[k] -= s
        for _, _, k in parts:
            bal.setdefault(k, bal[k])
    return dict(bal)


def settle_transfers(bal):
    b = {k: v for k, v in bal.items() if v != 0}
    transfers = []
    while b:
        debtors = [k for k, v in b.items() if v < 0]
        creditors = [k for k, v in b.items() if v > 0]
        assert debtors and creditors, "balances do not sum to zero"
        d = min(debtors, key=lambda k: (b[k], k))
        c = min(creditors, key=lambda k: (-b[k], k))
        amt = min(-b[d], b[c])
        transfers.append((d, c, amt))
        b[d] += amt
        b[c] -= amt
        b = {k: v for k, v in b.items() if v != 0}
    return transfers


# ---------------------------------------------------------------- formatting

def format_signed(c):
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


def format_unsigned(c):
    return f"{abs(c) // 100}.{abs(c) % 100:02d}"


# ---------------------------------------------------------------- main

def run_pipeline(opts):
    lines = read_ledger(opts["path"])
    if lines is None:
        err(f"error: cannot read {opts['path']}")
        return EXIT_READ

    header_pos = None
    for pos, (_n, text) in enumerate(lines):
        if not is_ignored(text):
            header_pos = pos
            break
    header_fields = parse_fields(lines[header_pos][1]) if header_pos is not None else []
    idx = {}
    for i, name in enumerate(header_fields):
        key = name.strip()
        if key not in idx:
            idx[key] = i
    for col in REQUIRED:
        if col not in idx:
            err(f"header: missing {col}")
            return EXIT_ROWS
    if header_pos is None:
        return EXIT_ROWS  # unreachable: missing 'date' handled above

    rows_errors = []
    expenses_all = []
    for n, text in lines[header_pos + 1:]:
        if is_ignored(text):
            continue
        fields = parse_fields(text)
        reason, exp = validate_row(fields, idx)
        if reason is not None:
            rows_errors.append(f"line {n}: {reason}")
        else:
            expenses_all.append((n, exp))
    if rows_errors:
        for e in rows_errors:
            err(e)
        return EXIT_ROWS

    # Display names: first appearance across the whole file (payer, then participants).
    display = {}
    for _n, (_kind, _d, payer, pk, _c, parts) in expenses_all:
        display.setdefault(pk, payer)
        for name, _w, k in parts:
            display.setdefault(k, name)

    since, until = opts["since"], opts["until"]
    considered = []
    for _n, exp in expenses_all:
        d = exp[1]
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        considered.append(exp)

    bal = compute_balances(considered)
    keys = sorted(bal, key=lambda k: k)

    if opts["command"] == "balances":
        if opts["format"] == "json":
            obj = {"balances": [{"name": display[k], "cents": bal[k]} for k in keys]}
            out(json.dumps(obj) + "\n")
        else:
            text = "".join(f"{display[k]} {format_signed(bal[k])}\n" for k in keys)
            out(text)
        return EXIT_OK

    transfers = settle_transfers(bal)
    if opts["format"] == "json":
        obj = {"transfers": [{"from": display[d], "to": display[c], "cents": amt}
                             for d, c, amt in transfers]}
        out(json.dumps(obj) + "\n")
    else:
        if not transfers:
            out("nothing to settle\n")
        else:
            out("".join(f"{display[d]} -> {display[c]} {format_unsigned(amt)}\n"
                        for d, c, amt in transfers))
    return EXIT_OK


def main(argv):
    try:
        opts = parse_args(argv)
    except SystemExit_Help:
        out(HELP)
        return EXIT_OK
    except UsageError as e:
        err(f"usage error: {e}")
        err(HELP.rstrip("\n"))
        return EXIT_USAGE
    return run_pipeline(opts)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
