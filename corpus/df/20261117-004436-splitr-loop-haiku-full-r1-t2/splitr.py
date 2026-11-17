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
EXIT_INVALID = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]
COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return cents (positive int) or None."""
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (name, weight) or None if invalid."""
    if text.strip() == "":
        return None
    result = []
    seen = set()
    for part in text.split(";"):
        part = part.strip()
        name, sep, weight_text = part.partition(":")
        name = name.strip()
        if name == "":
            return None
        if sep:
            weight_text = weight_text.strip()
            if not WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight <= 0:
                return None
        else:
            weight = 1
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, weight))
    return result


def split_cents(cents, parts):
    """parts: list of (key, sort_name, weight). Returns list of shares (ints)."""
    total_w = sum(w for _, _, w in parts)
    bases = []
    rems = []
    for _, _, w in parts:
        num = cents * w
        bases.append(num // total_w)
        rems.append(num % total_w)
    leftover = cents - sum(bases)
    order = sorted(range(len(parts)), key=lambda i: (-rems[i], parts[i][1].casefold(), parts[i][1]))
    shares = list(bases)
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(cents):
    a = abs(cents)
    return f"{a // 100}.{a % 100:02d}"


# ---------------------------------------------------------------- parsing

def parse_ledger(text):
    """Return (rows, errors, header_error).

    rows: list of dicts with keys lineno, date, payer, parts, amount (valid rows, file order)
    errors: list of (lineno, reason) for invalid rows
    header_error: string or None
    """
    entries = []  # (lineno, fields)
    for idx, raw in enumerate(text.split("\n")):
        line = raw[:-1] if raw.endswith("\r") else raw
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        fields = next(csv.reader([line]), [])
        entries.append((idx + 1, fields))

    if not entries:
        return [], [], "header: missing date"

    _, header = entries[0]
    names = [h.strip().casefold() for h in header]
    colmap = {}
    for col in REQUIRED_COLUMNS:
        if col not in names:
            return [], [], f"header: missing {col}"
        colmap[col] = names.index(col)

    rows = []
    errors = []
    for lineno, fields in entries[1:]:
        def get(col):
            i = colmap[col]
            return fields[i] if i < len(fields) else ""

        date_text = get("date").strip()
        date = parse_date(date_text)
        if date is None:
            errors.append((lineno, "bad-date"))
            continue
        payer = get("payer").strip()
        if payer == "":
            errors.append((lineno, "bad-payer"))
            continue
        amount_text = get("amount").strip()
        cents = parse_amount(amount_text)
        if cents is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        rows.append({
            "lineno": lineno,
            "date": date,
            "payer": payer,
            "cents": cents,
            "parts": parts,
        })
    return rows, errors, None


# ---------------------------------------------------------------- computation

def compute(rows, since, until):
    """Return (canon, balances, present) where
    canon: key -> first-appearance spelling (over all valid rows),
    balances: key -> cents for considered rows,
    present: set of keys appearing in considered rows.
    """
    canon = {}

    def register(name):
        key = name.casefold()
        if key not in canon:
            canon[key] = name
        return key

    balances = {}
    present = set()
    for row in rows:
        payer_key = register(row["payer"])
        part_keys = [register(n) for n, _ in row["parts"]]

        if since is not None and row["date"] < since:
            continue
        if until is not None and row["date"] > until:
            continue

        cents = row["cents"]
        balances[payer_key] = balances.get(payer_key, 0) + cents
        present.add(payer_key)
        weights = [(k, canon[k], w) for k, (_, w) in zip(part_keys, row["parts"])]
        shares = split_cents(cents, weights)
        for (k, _, _), share in zip(weights, shares):
            balances[k] = balances.get(k, 0) - share
            present.add(k)
    return canon, balances, present


def settle_transfers(balances, canon):
    """Return list of (debtor_key, creditor_key, cents)."""
    bal = {k: v for k, v in balances.items() if v != 0}
    transfers = []
    while bal:
        debtor = min((k for k in bal if bal[k] < 0),
                     key=lambda k: (bal[k], canon[k].casefold(), canon[k]))
        creditor = min((k for k in bal if bal[k] > 0),
                       key=lambda k: (-bal[k], canon[k].casefold(), canon[k]))
        amount = min(-bal[debtor], bal[creditor])
        transfers.append((debtor, creditor, amount))
        bal[debtor] += amount
        bal[creditor] -= amount
        bal = {k: v for k, v in bal.items() if v != 0}
    return transfers


# ---------------------------------------------------------------- CLI

def parse_args(argv):
    positional = []
    opts = {"--format": "text", "--since": None, "--until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            positional.extend(argv[i + 1:])
            break
        if a in ("-h", "--help"):
            raise SystemExit_Help()
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name not in opts:
                raise UsageError(f"unknown option: {name}")
            if not eq:
                i += 1
                if i >= len(argv):
                    raise UsageError(f"option {name} requires a value")
                val = argv[i]
            opts[name] = val
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1

    if len(positional) < 2:
        raise UsageError("missing argument")
    if len(positional) > 2:
        raise UsageError("too many arguments")
    command, path = positional
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")
    if opts["--format"] not in FORMATS:
        raise UsageError("bad --format value (use text or json)")
    since = None
    until = None
    if opts["--since"] is not None:
        since = parse_date(opts["--since"])
        if since is None:
            raise UsageError("bad --since date (use YYYY-MM-DD)")
    if opts["--until"] is not None:
        until = parse_date(opts["--until"])
        if until is None:
            raise UsageError("bad --until date (use YYYY-MM-DD)")
    return command, path, opts["--format"], since, until


class SystemExit_Help(Exception):
    pass


def out(text):
    sys.stdout.write(text)


def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except SystemExit_Help:
        out(USAGE + "\n")
        return EXIT_OK
    except UsageError as e:
        sys.stderr.write(f"error: {e}\n{USAGE}\n")
        return EXIT_USAGE

    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_READ

    rows, errors, header_error = parse_ledger(text)
    if header_error is not None:
        sys.stderr.write(header_error + "\n")
        return EXIT_INVALID
    if errors:
        for lineno, reason in sorted(errors):
            sys.stderr.write(f"line {lineno}: {reason}\n")
        return EXIT_INVALID

    canon, balances, present = compute(rows, since, until)

    people = sorted(present, key=lambda k: (k, canon[k]))

    if command == "balances":
        if fmt == "json":
            doc = {"balances": [{"name": canon[k], "cents": balances[k]} for k in people]}
            out(json.dumps(doc) + "\n")
        else:
            for k in people:
                out(f"{canon[k]} {fmt_signed(balances[k])}\n")
        return EXIT_OK

    # settle
    transfers = settle_transfers({k: balances[k] for k in people}, canon)
    if fmt == "json":
        doc = {"transfers": [
            {"from": canon[d], "to": canon[c], "cents": amt} for d, c, amt in transfers
        ]}
        out(json.dumps(doc) + "\n")
    else:
        if not transfers:
            out("nothing to settle\n")
        for d, c, amt in transfers:
            out(f"{canon[d]} -> {canon[c]} {fmt_unsigned(amt)}\n")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv[1:]))
