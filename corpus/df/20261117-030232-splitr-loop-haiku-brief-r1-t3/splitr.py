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
from fractions import Fraction

EXIT_OK = 0
EXIT_BAD_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED_COLUMNS = ("date", "payer", "amount", "participants")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


def usage_fail(msg):
    sys.stderr.write("usage: splitr.py <balances|settle> <ledger.csv> "
                     "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
    if msg:
        sys.stderr.write(f"error: {msg}\n")
    sys.exit(EXIT_USAGE)


def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            sys.stdout.write("usage: splitr.py <balances|settle> <ledger.csv> "
                             "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
            sys.exit(EXIT_OK)
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name not in ("--format", "--since", "--until"):
                usage_fail(f"unknown option {name}")
            if not eq:
                i += 1
                if i >= len(argv):
                    usage_fail(f"option {name} needs a value")
                value = argv[i]
            key = name[2:]
            if key == "format":
                if value not in FORMATS:
                    usage_fail(f"bad format {value!r}")
            else:
                if parse_date(value) is None:
                    usage_fail(f"bad date for --{key}: {value!r}")
            opts[key] = value
        elif arg.startswith("-") and arg != "-":
            usage_fail(f"unknown option {arg}")
        else:
            positional.append(arg)
        i += 1
    if len(positional) != 2:
        usage_fail("expected a command and a ledger file")
    command, path = positional
    if command not in COMMANDS:
        usage_fail(f"unknown command {command!r}")
    return command, path, opts


def read_records(path):
    """Return (header, rows) where rows is a list of (line_no, fields).

    Blank lines and lines starting with '#' are skipped. Line numbers are
    physical line numbers in the file (header is line 1)."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(EXIT_READ)

    phys_numbers = []  # physical line number of each line handed to csv

    def gen():
        for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            phys_numbers.append(lineno)
            yield line

    reader = csv.reader(gen())
    records = []
    prev_end = 0
    for fields in reader:
        end = reader.line_num
        if fields:
            records.append((phys_numbers[prev_end], fields))
        prev_end = end
    if not records:
        return None, []
    _, header = records[0]
    return header, records[1:]


def check_header(header):
    names = [h.strip().lower() for h in header]
    for col in REQUIRED_COLUMNS:
        if col not in names:
            sys.stderr.write(f"header: missing {col}\n")
            sys.exit(EXIT_BAD_ROWS)
    return {n: names.index(n) for n in reversed(names)}


def parse_participants(text):
    """Return list of (name, weight Fraction) or None if invalid."""
    result = []
    seen = set()
    for seg in text.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if ":" in seg:
            name, _, wtext = seg.partition(":")
            name = name.strip()
            wtext = wtext.strip()
            if not WEIGHT_RE.match(wtext):
                return None
            weight = Fraction(wtext)
            if weight <= 0:
                return None
        else:
            name = seg
            weight = Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        result.append((name, weight))
    if not result:
        return None
    return result


def parse_row(lineno, fields, cols):
    """Return (record, None) on success or (None, reason) on failure."""
    def get(col):
        idx = cols[col]
        return fields[idx].strip() if idx < len(fields) else ""

    date_text = get("date")
    date = parse_date(date_text)
    if date is None:
        return None, "bad-date"

    payer = get("payer")
    if not payer or ":" in payer:
        return None, "bad-payer"

    amount_text = get("amount")
    if not AMOUNT_RE.match(amount_text):
        return None, "bad-amount"
    whole, _, frac = amount_text.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return None, "bad-amount"

    parts = parse_participants(get("participants"))
    if parts is None:
        return None, "bad-participants"

    note_idx = cols.get("note")
    note = ""
    if note_idx is not None and note_idx < len(fields):
        note = ",".join(fields[note_idx:]).strip()

    return {"date": date, "payer": payer, "cents": cents,
            "participants": parts, "note": note}, None


def split_cents(total, parts):
    """Split `total` cents by weights exactly (largest remainder; ties by order)."""
    wsum = sum(w for _, w in parts)
    quotas = [Fraction(total) * w / wsum for _, w in parts]
    floors = [int(q) for q in quotas]
    leftover = total - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(quotas[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return {name: floors[i] for i, (name, _) in enumerate(parts)}


def compute_balances(records):
    balances = {}
    for rec in records:
        balances[rec["payer"]] = balances.get(rec["payer"], 0) + rec["cents"]
        shares = split_cents(rec["cents"], rec["participants"])
        for name, share in shares.items():
            balances[name] = balances.get(name, 0) - share
    return balances


def settle(balances):
    owed = {n: c for n, c in balances.items() if c != 0}
    transfers = []
    while True:
        debtors = [n for n, c in owed.items() if c < 0]
        creditors = [n for n, c in owed.items() if c > 0]
        if not debtors or not creditors:
            break
        # largest debtor; ties broken by name
        d = min(debtors, key=lambda n: (owed[n], n))
        # largest creditor; ties broken by name
        c = min(creditors, key=lambda n: (-owed[n], n))
        amt = min(-owed[d], owed[c])
        transfers.append((d, c, amt))
        owed[d] += amt
        owed[c] -= amt
        owed = {n: v for n, v in owed.items() if v != 0}
    return transfers


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def main(argv):
    command, path, opts = parse_args(argv)

    header, rows = read_records(path)
    if header is None:
        header = []
    cols = check_header(header)

    records = []
    errors = []
    for lineno, fields in rows:
        rec, reason = parse_row(lineno, fields, cols)
        if reason is not None:
            errors.append(f"line {lineno}: {reason}")
        else:
            records.append((lineno, rec))

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_BAD_ROWS

    if opts["since"]:
        since = parse_date(opts["since"])
        records = [(n, r) for n, r in records if r["date"] >= since]
    if opts["until"]:
        until = parse_date(opts["until"])
        records = [(n, r) for n, r in records if r["date"] <= until]

    balances = compute_balances([r for _, r in records])

    if command == "balances":
        names = sorted(balances)
        if opts["format"] == "json":
            out = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
            print(json.dumps(out))
        else:
            for n in names:
                print(f"{n} {fmt_signed(balances[n])}")
    else:
        transfers = settle(balances)
        if opts["format"] == "json":
            out = {"transfers": [{"from": d, "to": c, "cents": a} for d, c, a in transfers]}
            print(json.dumps(out))
        elif not transfers:
            print("nothing to settle")
        else:
            for d, c, a in transfers:
                print(f"{d} -> {c} {fmt_plain(a)}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
