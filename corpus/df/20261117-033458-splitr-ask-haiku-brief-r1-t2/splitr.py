#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py {balances,settle} <ledger.csv> [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Exit codes: 0 ok, 2 bad ledger rows / header, 3 cannot read file, 64 bad usage.
"""
import argparse
import csv
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_BAD_LEDGER = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED_COLUMNS = ["date", "payer", "amount", "participants"]
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+$")


class UsageParser(argparse.ArgumentParser):
    def error(self, message):  # bad usage -> exit 64
        sys.stderr.write(f"usage error: {message}\n")
        sys.exit(EXIT_USAGE)


def parse_date(text):
    """Strict YYYY-MM-DD; returns a date or None."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_amount(text):
    """Return cents (positive int) or None."""
    text = text.strip()
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    return cents if cents > 0 else None


def parse_participants(text):
    """Return list of (display_name, weight) with duplicates merged, or None."""
    if text is None or not text.strip():
        return None
    result = []  # list of [display, key, weight]
    index = {}
    for seg in text.split(";"):
        seg = seg.strip()
        if not seg:
            return None
        if ":" in seg:
            parts = seg.split(":")
            if len(parts) != 2:
                return None
            name, wtext = parts[0].strip(), parts[1].strip()
            if not WEIGHT_RE.fullmatch(wtext):
                return None
            weight = int(wtext)
            if weight <= 0:
                return None
        else:
            name, weight = seg, 1
        if not name:
            return None
        key = name.casefold()
        if key in index:
            result[index[key]][2] += weight
        else:
            index[key] = len(result)
            result.append([name, key, weight])
    return [(display, key, weight) for display, key, weight in result]


def split_cents(total, shares):
    """Split total cents by weights; leftover cents go by largest remainder,
    ties broken by name ascending (case-insensitive). shares: list of (key, weight)."""
    wsum = sum(w for _, w in shares)
    base = []
    rems = []
    for key, w in shares:
        q, r = divmod(total * w, wsum)
        base.append(q)
        rems.append((r, key))
    leftover = total - sum(base)
    order = sorted(range(len(shares)), key=lambda i: (-rems[i][0], rems[i][1]))
    for i in order[:leftover]:
        base[i] += 1
    return base


def read_ledger(path):
    """Return (rows, errors). rows: list of dicts; errors: list of strings."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise OSError(path)

    physical = text.splitlines()
    header = None
    rows = []
    errors = []
    col = {}
    for lineno, line in enumerate(physical, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = next(csv.reader([line]))
        if header is None:
            header = [f.strip().lower() for f in fields]
            for name in REQUIRED_COLUMNS:
                if name not in header:
                    sys.stderr.write(f"header: missing {name}\n")
                    sys.exit(EXIT_BAD_LEDGER)
            col = {name: header.index(name) for name in header}
            continue

        def get(name):
            i = col.get(name)
            if i is None or i >= len(fields):
                return ""
            return fields[i]

        date_s = get("date").strip()
        payer = get("payer").strip()
        amount_s = get("amount")
        parts_s = get("participants")
        note = get("note") if "note" in col else ""

        date = parse_date(date_s)
        if date is None:
            errors.append(f"line {lineno}: bad-date")
            continue
        if not payer:
            errors.append(f"line {lineno}: bad-payer")
            continue
        cents = parse_amount(amount_s)
        if cents is None:
            errors.append(f"line {lineno}: bad-amount")
            continue
        parts = parse_participants(parts_s)
        if parts is None:
            errors.append(f"line {lineno}: bad-participants")
            continue
        rows.append({
            "line": lineno,
            "date": date,
            "payer": payer,
            "cents": cents,
            "participants": parts,
            "note": note,
        })

    if header is None:
        sys.stderr.write(f"header: missing {REQUIRED_COLUMNS[0]}\n")
        sys.exit(EXIT_BAD_LEDGER)
    return rows, errors


def compute_balances(rows, since, until):
    """Return ordered list of (display_name, cents) sorted by name."""
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
        pkey = touch(r["payer"])
        shares = [(k, w) for _, k, w in r["participants"]]
        for disp, k, _ in r["participants"]:
            if k not in display:
                display[k] = disp
                bal[k] = 0
        amounts = split_cents(r["cents"], shares)
        bal[pkey] += r["cents"]
        for (k, _), amt in zip(shares, amounts):
            bal[k] -= amt
    people = sorted(((display[k], bal[k]) for k in bal),
                    key=lambda t: (t[0].casefold(), t[0]))
    return people


def compute_transfers(balances):
    creditors = [[n, c] for n, c in balances if c > 0]
    debtors = [[n, -c] for n, c in balances if c < 0]
    key = lambda t: (-t[1], t[0].casefold())
    creditors.sort(key=key)
    debtors.sort(key=key)
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d_name, d_amt = debtors[i]
        c_name, c_amt = creditors[j]
        pay = min(d_amt, c_amt)
        if pay > 0:
            transfers.append({"from": d_name, "to": c_name, "cents": pay})
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transfers


def fmt_signed(cents):
    sign = "+" if cents > 0 else ("-" if cents < 0 else "")
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_plain(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def main(argv=None):
    parser = UsageParser(prog="splitr.py", description="shared-expense settlement")
    parser.add_argument("command", choices=["balances", "settle"])
    parser.add_argument("ledger")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    args = parser.parse_args(argv)

    since = until = None
    if args.since is not None:
        since = parse_date(args.since)
        if since is None:
            parser.error(f"invalid --since date: {args.since}")
    if args.until is not None:
        until = parse_date(args.until)
        if until is None:
            parser.error(f"invalid --until date: {args.until}")

    try:
        rows, errors = read_ledger(args.ledger)
    except OSError:
        sys.stderr.write(f"error: cannot read {args.ledger}\n")
        return EXIT_READ

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_BAD_LEDGER

    balances = compute_balances(rows, since, until)

    if args.command == "balances":
        if args.format == "json":
            out = {"balances": [{"name": n, "cents": c} for n, c in balances]}
            print(json.dumps(out))
        else:
            for n, c in balances:
                print(f"{n} {fmt_signed(c)}")
    else:
        transfers = compute_transfers(balances)
        if args.format == "json":
            print(json.dumps({"transfers": [
                {"from": t["from"], "to": t["to"], "cents": t["cents"]} for t in transfers]}))
        else:
            if not transfers:
                print("nothing to settle")
            for t in transfers:
                print(f"{t['from']} -> {t['to']} {fmt_plain(t['cents'])}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
