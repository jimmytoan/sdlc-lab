#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]
"""
import csv
import datetime
import io
import json
import re
import sys

EXIT_BAD_ROWS = 2
EXIT_UNREADABLE = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class Parser(object):
    """Minimal argument parser that reports every usage problem as exit 64."""

    def parse(self, argv):
        opts = {"format": "text", "since": None, "until": None}
        positional = []
        i = 0
        while i < len(argv):
            a = argv[i]
            if a in ("--format", "--since", "--until"):
                if i + 1 >= len(argv):
                    raise UsageError("option %s needs a value" % a)
                opts[a[2:]] = argv[i + 1]
                i += 2
                continue
            if a.startswith("--format=") or a.startswith("--since=") or a.startswith("--until="):
                key, _, val = a[2:].partition("=")
                opts[key] = val
                i += 1
                continue
            if a.startswith("-") and a != "-":
                raise UsageError("unknown option %s" % a)
            positional.append(a)
            i += 1
        if len(positional) != 2:
            raise UsageError("expected a command and a ledger path")
        command, path = positional
        if command not in ("balances", "settle"):
            raise UsageError("unknown command %s" % command)
        if opts["format"] not in ("text", "json"):
            raise UsageError("bad format %s" % opts["format"])
        for key in ("since", "until"):
            if opts[key] is not None:
                opts[key] = parse_iso_date(opts[key])
                if opts[key] is None:
                    raise UsageError("bad %s date" % key)
        if opts["since"] and opts["until"] and opts["since"] > opts["until"]:
            raise UsageError("--since is later than --until")
        opts["command"] = command
        opts["path"] = path
        return opts


def parse_iso_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return amount in cents, or None if invalid."""
    text = text.strip()
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def parse_participants(text):
    """Return list of (display_name, weight) or None if invalid."""
    if text.strip() == "":
        return None
    result = []
    seen = set()
    for token in text.split(";"):
        if ":" in token:
            name, _, wtext = token.partition(":")
            wtext = wtext.strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight <= 0:
                return None
        else:
            name = token
            weight = 1
        name = name.strip()
        if name == "":
            return None
        key = name.casefold()
        if key in seen:
            return None
        seen.add(key)
        result.append((name, weight))
    return result


def split_cents(total, parts):
    """Split total cents among [(name, weight)] by largest remainder.

    Returns {name: cents}. Leftover cents go to the largest remainders,
    ties broken by name ascending (ignoring case)."""
    wsum = sum(w for _, w in parts)
    base = {}
    rems = []
    for name, w in parts:
        num = total * w
        base[name] = num // wsum
        rems.append((num % wsum, name))
    leftover = total - sum(base.values())
    rems.sort(key=lambda r: (-r[0], r[1].casefold()))
    for _, name in rems[:leftover]:
        base[name] += 1
    return base


def read_text(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(EXIT_UNREADABLE)


def load_rows(text):
    """Return (rows, errors). rows: list of dict with parsed fields.

    Raises SystemExit(2) on header problems."""
    lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""))
    header = None
    rows = []
    errors = []
    while True:
        start = reader.line_num + 1
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error:
            # Unparseable quoting: the rest of the file cannot be trusted.
            errors.append((start, "bad-participants"))
            break
        raw_first = lines[start - 1] if start - 1 < len(lines) else ""
        stripped = raw_first.lstrip()
        if stripped.startswith("#"):
            continue
        if not record or all(c.strip() == "" for c in record):
            continue
        if header is None:
            header = [h.strip() for h in record]
            for col in REQUIRED_COLUMNS:
                if col not in header:
                    sys.stderr.write("header: missing %s\n" % col)
                    sys.exit(EXIT_BAD_ROWS)
            continue
        idx = {col: header.index(col) for col in REQUIRED_COLUMNS}

        def cell(col):
            i = idx[col]
            return record[i] if i < len(record) else ""

        date_text = cell("date").strip()
        d = parse_iso_date(date_text)
        if d is None:
            errors.append((start, "bad-date"))
            continue
        payer = cell("payer").strip()
        if payer == "":
            errors.append((start, "bad-payer"))
            continue
        cents = parse_amount(cell("amount"))
        if cents is None:
            errors.append((start, "bad-amount"))
            continue
        parts = parse_participants(cell("participants"))
        if parts is None:
            errors.append((start, "bad-participants"))
            continue
        rows.append({"line": start, "date": d, "payer": payer,
                     "cents": cents, "parts": parts})
    if header is None:
        sys.stderr.write("header: missing %s\n" % REQUIRED_COLUMNS[0])
        sys.exit(EXIT_BAD_ROWS)
    errors.sort()
    return rows, errors


def compute_balances(rows, since, until):
    """Return ordered dict-like list of (display_name, cents), sorted by name."""
    display = {}
    bal = {}

    def touch(name):
        key = name.casefold()
        if key not in display:
            display[key] = name
            bal[key] = 0
        return key

    for r in rows:
        if since and r["date"] < since:
            continue
        if until and r["date"] > until:
            continue
        pk = touch(r["payer"])
        bal[pk] += r["cents"]
        shares = split_cents(r["cents"], r["parts"])
        for name, share in shares.items():
            k = touch(name)
            bal[k] -= share
    keys = sorted(display, key=lambda k: (k, display[k]))
    return [(display[k], bal[k]) for k in keys]


def compute_transfers(balances):
    """Greedy settlement. Returns list of (from, to, cents)."""
    owe = {}
    owed = {}
    for name, c in balances:
        if c < 0:
            owe[name] = -c
        elif c > 0:
            owed[name] = c
    transfers = []
    while owe and owed:
        debtor = min(owe, key=lambda n: (-owe[n], n.casefold(), n))
        creditor = min(owed, key=lambda n: (-owed[n], n.casefold(), n))
        amt = min(owe[debtor], owed[creditor])
        transfers.append((debtor, creditor, amt))
        owe[debtor] -= amt
        owed[creditor] -= amt
        if owe[debtor] == 0:
            del owe[debtor]
        if owed[creditor] == 0:
            del owed[creditor]
    return transfers


def fmt_cents(c):
    sign = "-" if c < 0 else ""
    c = abs(c)
    return "%s%d.%02d" % (sign, c // 100, c % 100)


def fmt_signed(c):
    if c > 0:
        return "+" + fmt_cents(c)
    if c == 0:
        return "0.00"
    return fmt_cents(c)


def main(argv):
    try:
        opts = Parser().parse(argv)
    except UsageError as e:
        sys.stderr.write("usage error: %s\n" % e)
        return EXIT_USAGE

    text = read_text(opts["path"])
    rows, errors = load_rows(text)
    if errors:
        for line, reason in errors:
            sys.stderr.write("line %d: %s\n" % (line, reason))
        return EXIT_BAD_ROWS

    balances = compute_balances(rows, opts["since"], opts["until"])
    out = []
    if opts["command"] == "balances":
        if opts["format"] == "json":
            out.append(json.dumps({"balances": [
                {"name": n, "cents": c} for n, c in balances]}))
        else:
            for n, c in balances:
                out.append("%s %s" % (n, fmt_signed(c)))
    else:
        transfers = compute_transfers(balances)
        if opts["format"] == "json":
            out.append(json.dumps({"transfers": [
                {"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for f, t, c in transfers:
                out.append("%s -> %s %s" % (f, t, fmt_cents(c)))
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
