#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import argparse
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f"usage error: {message}\n")
        sys.exit(EXIT_USAGE)


def _build_parser():
    p = _Parser(prog="splitr.py", allow_abbrev=False)
    p.add_argument("command", choices=["balances", "settle"])
    p.add_argument("ledger")
    p.add_argument("--format", dest="fmt", default="text", choices=["text", "json"])
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    return p


# ---------------------------------------------------------------- parsing helpers

def parse_date(s):
    """Strict YYYY-MM-DD and real calendar date, or None."""
    if s is None or not _DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_amount(s):
    """Positive decimal with at most two fraction digits -> integer cents, or None."""
    if not _AMOUNT_RE.fullmatch(s):
        return None
    whole, _, frac = s.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    """Return list of (display_name, key, weight), or None if invalid."""
    if s.strip() == "":
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        if ":" in part:
            name, _, wtext = part.partition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not _WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight < 1:
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
        result.append((name, key, weight))
    return result


def split_cents(cents, people):
    """people: list of (key, weight). Returns dict key -> share in cents."""
    total_w = sum(w for _, w in people)
    shares = {}
    rems = []
    base_sum = 0
    for key, w in people:
        num = cents * w
        base = num // total_w
        rem = num % total_w
        shares[key] = base
        base_sum += base
        rems.append((-rem, key))
    leftover = cents - base_sum
    rems.sort()
    for i in range(leftover):
        _, key = rems[i]
        shares[key] += 1
    return shares


def format_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole}.{frac:02d}"


def format_unsigned(cents):
    whole, frac = divmod(abs(cents), 100)
    return f"{whole}.{frac:02d}"


# ---------------------------------------------------------------- ledger reading

def read_ledger(path):
    """Return list of (lineno, text) for non-ignored physical lines.

    Raises OSError / UnicodeDecodeError on read failure."""
    with open(path, "rb") as f:
        data = f.read()
    text = data.decode("utf-8-sig")
    result = []
    for idx, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        result.append((idx, line))
    return result


def _csv_fields(line):
    try:
        rows = list(csv.reader([line]))
    except csv.Error:
        return []
    return rows[0] if rows else []


def run_ledger(path, command, fmt, since, until):
    """Returns (exit_code, stdout_text, stderr_text)."""
    try:
        lines = read_ledger(path)
    except (OSError, UnicodeDecodeError):
        return EXIT_READ, "", f"error: cannot read {path}\n"

    if not lines:
        return EXIT_INVALID, "", "header: missing date\n"

    header_line_no, header_line = lines[0]
    header_fields = [h.strip() for h in _csv_fields(header_line)]
    col_index = {}
    for i, h in enumerate(header_fields):
        if h not in col_index:
            col_index[h] = i
    for col in REQUIRED_COLUMNS:
        if col not in col_index:
            return EXIT_INVALID, "", f"header: missing {col}\n"

    def field(fields, col):
        i = col_index[col]
        return fields[i] if i < len(fields) else ""

    errors = []
    rows = []  # (lineno, date, payer_name, payer_key, cents, participants, note)
    for lineno, line in lines[1:]:
        fields = _csv_fields(line)
        d = parse_date(field(fields, "date"))
        if d is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = field(fields, "payer").strip()
        if payer == "":
            errors.append((lineno, "bad-payer"))
            continue
        cents = parse_amount(field(fields, "amount").strip())
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(field(fields, "participants"))
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        note = field(fields, "note")
        rows.append((lineno, d, payer, payer.casefold(), cents, parts, note))

    if errors:
        errors.sort(key=lambda e: e[0])
        err = "".join(f"line {n}: {reason}\n" for n, reason in errors)
        return EXIT_INVALID, "", err

    # First-appearance spelling over all valid rows.
    spelling = {}
    for _, _, payer, pkey, _, parts, _ in rows:
        spelling.setdefault(pkey, payer)
        for name, key, _ in parts:
            spelling.setdefault(key, name)

    # Filter by date.
    considered = [r for r in rows
                  if (since is None or r[1] >= since) and (until is None or r[1] <= until)]

    balances = {}
    for _, _, _, pkey, cents, parts, _ in considered:
        balances.setdefault(pkey, 0)
        for _, key, _ in parts:
            balances.setdefault(key, 0)
        balances[pkey] += cents
        shares = split_cents(cents, [(key, w) for _, key, w in parts])
        for key, share in shares.items():
            balances[key] -= share

    keys_sorted = sorted(balances, key=lambda k: k)
    if command == "balances":
        return EXIT_OK, _render_balances(keys_sorted, balances, spelling, fmt), ""
    transfers = _settle(balances)
    return EXIT_OK, _render_settle(transfers, spelling, fmt), ""


def _render_balances(keys_sorted, balances, spelling, fmt):
    if fmt == "json":
        obj = {"balances": [{"name": spelling[k], "cents": balances[k]} for k in keys_sorted]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not keys_sorted:
        return ""
    return "".join(f"{spelling[k]} {format_signed(balances[k])}\n" for k in keys_sorted)


def _settle(balances):
    bal = {k: v for k, v in balances.items() if v != 0}
    transfers = []
    while bal:
        debtor = min((k for k in bal if bal[k] < 0), key=lambda k: (bal[k], k))
        creditor = min((k for k in bal if bal[k] > 0), key=lambda k: (-bal[k], k))
        amount = min(-bal[debtor], bal[creditor])
        transfers.append((debtor, creditor, amount))
        bal[debtor] += amount
        bal[creditor] -= amount
        bal = {k: v for k, v in bal.items() if v != 0}
    return transfers


def _render_settle(transfers, spelling, fmt):
    if fmt == "json":
        obj = {"transfers": [{"from": spelling[d], "to": spelling[c], "cents": a}
                             for d, c, a in transfers]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{spelling[d]} -> {spelling[c]} {format_unsigned(a)}\n"
                   for d, c, a in transfers)


# ---------------------------------------------------------------- main

def _parse_cli_date(value):
    d = parse_date(value)
    if d is None:
        raise UsageError(f"invalid date: {value!r}")
    return d


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except AttributeError:
        pass

    args = _build_parser().parse_args(argv)
    try:
        since = _parse_cli_date(args.since) if args.since is not None else None
        until = _parse_cli_date(args.until) if args.until is not None else None
    except UsageError as e:
        sys.stderr.write(f"usage error: {e}\n")
        return EXIT_USAGE

    code, out, err = run_ledger(args.ledger, args.command, args.fmt, since, until)
    if err:
        sys.stderr.write(err)
    if out:
        sys.stdout.write(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
