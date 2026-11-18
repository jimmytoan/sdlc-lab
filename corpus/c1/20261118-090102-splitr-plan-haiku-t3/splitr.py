#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] [--since DATE] [--until DATE]

All money is handled as integer cents.
Exit codes: 0 success, 2 bad rows/header, 3 unreadable file, 64 usage error.
"""
import csv
import datetime
import io
import json
import re
import sys

EXIT_OK = 0
EXIT_DATA = 2
EXIT_IO = 3
EXIT_USAGE = 64

PROG = "splitr.py"
USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)
COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class ReadError(Exception):
    pass


def usage_error(msg):
    sys.stderr.write(f"{PROG}: error: {msg}\n{USAGE}\n")
    sys.exit(EXIT_USAGE)


def help_text():
    return (
        f"{USAGE}\n\n"
        "Commands:\n"
        "  balances    print each person's net balance\n"
        "  settle      print the payments that clear all balances\n\n"
        "Options:\n"
        "  --format text|json   output format (default: text)\n"
        "  --since DATE         only rows on or after DATE (YYYY-MM-DD)\n"
        "  --until DATE         only rows on or before DATE (YYYY-MM-DD)\n"
        "  -h, --help           show this help and exit\n"
    )


def valid_date_string(value):
    if not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positionals = []
    after_dd = False
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if after_dd:
            positionals.append(a)
            continue
        if a == "--":
            after_dd = True
            continue
        if a in ("-h", "--help"):
            sys.stdout.write(help_text())
            sys.exit(EXIT_OK)
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name not in ("--format", "--since", "--until"):
                usage_error(f"unknown option: {name}")
            if not eq:
                if i >= len(argv):
                    usage_error(f"option {name} requires a value")
                val = argv[i]
                i += 1
            key = name[2:]
            if key == "format":
                if val not in FORMATS:
                    usage_error(f"invalid --format value: {val!r} (expected text or json)")
                opts["format"] = val
            else:
                if not valid_date_string(val):
                    usage_error(f"invalid --{key} date: {val!r} (expected YYYY-MM-DD)")
                opts[key] = val
        elif a.startswith("-") and a != "-":
            usage_error(f"unknown option: {a}")
        else:
            positionals.append(a)
    if len(positionals) > 2:
        usage_error("too many positional arguments")
    if len(positionals) < 2:
        usage_error("missing command or ledger path")
    command, path = positionals
    if command not in COMMANDS:
        usage_error(f"unknown command: {command}")
    return command, path, opts


def read_text(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise ReadError()


def split_records(text):
    """Split CSV text into (start_line, record_text) pairs.

    Blank lines and lines starting with '#' (at a record start) are skipped.
    Quoted fields may span several physical lines.
    """
    records = []
    n = len(text)
    i = 0
    line = 1
    while i < n:
        j = text.find("\n", i)
        eol = n if j == -1 else j
        raw = text[i:eol]
        if raw.strip() == "" or raw.startswith("#"):
            line += 1
            i = eol + 1
            continue
        start = line
        k = i
        in_quotes = False
        field_start = True
        while k < n:
            c = text[k]
            if in_quotes:
                if c == '"':
                    if k + 1 < n and text[k + 1] == '"':
                        k += 2
                        continue
                    in_quotes = False
            else:
                if c == "\n":
                    break
                if c == '"' and field_start:
                    in_quotes = True
                elif c == ",":
                    field_start = True
                    k += 1
                    continue
            field_start = False
            k += 1
        rec = text[i:k]
        records.append((start, rec))
        line += rec.count("\n") + (1 if k < n else 0)
        i = k + 1 if k < n else n
    return records


def parse_record(rec):
    reader = csv.reader(io.StringIO(rec, newline=""))
    for row in reader:
        return row
    return []


def amount_to_cents(value):
    whole, _, frac = value.partition(".")
    frac = (frac + "00")[:2]
    return int(whole) * 100 + int(frac)


def parse_participants(value):
    """Return list of (name, weight) or None if invalid."""
    result = []
    seen = set()
    for seg in value.split(";"):
        seg = seg.strip()
        if seg == "":
            return None
        if ":" in seg:
            parts = seg.split(":")
            if len(parts) != 2:
                return None
            name = parts[0].strip()
            wtext = parts[1].strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight < 1:
                return None
        else:
            name = seg
            weight = 1
        if name == "":
            return None
        if name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    if not result:
        return None
    return result


def validate_row(lineno, row, idx):
    """Return (errors, record) where record is None if any error."""
    def field(key):
        j = idx[key]
        return row[j].strip() if j < len(row) else ""

    errors = []
    date_s = field("date")
    payer = field("payer")
    amount_s = field("amount")
    parts_s = field("participants")

    date_ok = bool(DATE_RE.fullmatch(date_s))
    if date_ok:
        try:
            datetime.date.fromisoformat(date_s)
        except ValueError:
            date_ok = False
    if not date_ok:
        errors.append((lineno, "bad-date"))

    payer_ok = payer != "" and ";" not in payer and ":" not in payer
    if not payer_ok:
        errors.append((lineno, "bad-payer"))

    cents = None
    if AMOUNT_RE.fullmatch(amount_s):
        cents = amount_to_cents(amount_s)
        if cents <= 0:
            cents = None
    if cents is None:
        errors.append((lineno, "bad-amount"))

    parts = parse_participants(parts_s)
    if parts is None:
        errors.append((lineno, "bad-participants"))

    if errors:
        return errors, None
    return [], {
        "date": datetime.date.fromisoformat(date_s),
        "payer": payer,
        "cents": cents,
        "participants": parts,
    }


def split_shares(total, parts):
    """Integer shares summing exactly to total, by weight.

    Leftover cents go to the largest fractional remainders; ties go to earlier listed.
    """
    weights = [w for _, w in parts]
    W = sum(weights)
    base = [(total * w) // W for w in weights]
    rem = total - sum(base)
    fracs = [(total * w) % W for w in weights]
    order = sorted(range(len(parts)), key=lambda k: (-fracs[k], k))
    shares = list(base)
    for k in order[:rem]:
        shares[k] += 1
    assert sum(shares) == total
    return shares


def compute_balances(rows):
    bal = {}
    for r in rows:
        bal.setdefault(r["payer"], 0)
        bal[r["payer"]] += r["cents"]
        shares = split_shares(r["cents"], r["participants"])
        for (name, _), sh in zip(r["participants"], shares):
            bal.setdefault(name, 0)
            bal[name] -= sh
    assert sum(bal.values()) == 0
    return bal


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(cents):
    a = abs(cents)
    return f"{a // 100}.{a % 100:02d}"


def settle(bal):
    creditors = {n: c for n, c in bal.items() if c > 0}
    debtors = {n: -c for n, c in bal.items() if c < 0}
    transfers = []
    while debtors and creditors:
        d = min(debtors, key=lambda n: (-debtors[n], n))
        c = min(creditors, key=lambda n: (-creditors[n], n))
        amt = min(debtors[d], creditors[c])
        transfers.append((d, c, amt))
        debtors[d] -= amt
        creditors[c] -= amt
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return transfers


def render(command, bal, fmt):
    names = sorted(bal)
    if command == "balances":
        if fmt == "json":
            obj = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
            return json.dumps(obj, ensure_ascii=False) + "\n"
        return "".join(f"{n} {fmt_signed(bal[n])}\n" for n in names)

    nonzero = {n: bal[n] for n in names if bal[n] != 0}
    transfers = settle(nonzero)
    if fmt == "json":
        obj = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{d} -> {c} {fmt_unsigned(a)}\n" for d, c, a in transfers)


def main(argv):
    command, path, opts = parse_args(argv)

    try:
        text = read_text(path)
    except ReadError:
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_IO

    try:
        records = split_records(text)
    except csv.Error:
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_IO

    if not records:
        sys.stderr.write("header: missing date\n")
        return EXIT_DATA

    try:
        header = parse_record(records[0][1])
    except csv.Error:
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_IO
    cols = [h.strip().lower() for h in header]
    idx = {}
    for name in REQUIRED_COLUMNS:
        if name not in cols:
            sys.stderr.write(f"header: missing {name}\n")
            return EXIT_DATA
        idx[name] = cols.index(name)

    errors = []
    rows = []
    for lineno, rec in records[1:]:
        try:
            row = parse_record(rec)
        except csv.Error:
            sys.stderr.write(f"error: cannot read {path}\n")
            return EXIT_IO
        errs, parsed = validate_row(lineno, row, idx)
        errors.extend(errs)
        if parsed is not None:
            rows.append(parsed)

    if errors:
        for lineno, reason in errors:
            sys.stderr.write(f"line {lineno}: {reason}\n")
        return EXIT_DATA

    since = datetime.date.fromisoformat(opts["since"]) if opts["since"] else None
    until = datetime.date.fromisoformat(opts["until"]) if opts["until"] else None
    included = [
        r for r in rows
        if (since is None or r["date"] >= since) and (until is None or r["date"] <= until)
    ]

    bal = compute_balances(included)
    out = render(command, bal, opts["format"])
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
