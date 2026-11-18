#!/usr/bin/env python3
"""splitr: answer "who owes whom?" for shared expenses.

Usage: python splitr.py <balances|settle> <ledger.csv> [options]

Standard library only. Money is handled as integer cents throughout.
"""
from __future__ import annotations

import csv
import io
import itertools
import json
import re
import sys
from dataclasses import dataclass
from datetime import date

USAGE = (
    "usage: splitr.py <balances|settle> <ledger.csv> "
    "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"
)
HELP = (
    USAGE + "\n"
    "\n"
    "Commands:\n"
    "  balances   print each person's net position (paid minus share)\n"
    "  settle     print the transfers that clear every debt\n"
    "\n"
    "Options:\n"
    "  --format text|json     output format (default: text)\n"
    "  --since YYYY-MM-DD     include expenses on or after this date\n"
    "  --until YYYY-MM-DD     include expenses on or before this date\n"
    "  -h, --help             show this help and exit\n"
)

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
OPTIONS = ("--since", "--until", "--format")
REQUIRED = ("date", "payer", "amount", "participants")

_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
_WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    """Bad command line: exit 64."""


class HelpRequested(Exception):
    """-h or --help was given: usage to stdout, exit 0."""


class ReadError(Exception):
    """The ledger cannot be read: exit 3."""

    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


class RowError(Exception):
    """A data row failed validation."""

    def __init__(self, line: int, reason: str):
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


@dataclass(frozen=True)
class Options:
    command: str
    path: str
    fmt: str
    since: date | None
    until: date | None


@dataclass(frozen=True)
class Record:
    line: int                      # physical line where the record starts
    fields: list[str] | None       # None means the CSV itself was malformed


@dataclass(frozen=True)
class Expense:
    line: int
    date: date
    payer: str
    amount: int                                  # cents, > 0
    participants: tuple[tuple[str, int], ...]    # (name, weight), listed order


@dataclass(frozen=True)
class Transfer:
    frm: str
    to: str
    cents: int


# ---------------------------------------------------------------- dates


def parse_date(text: str) -> date | None:
    """Return the calendar date for a strict YYYY-MM-DD string, else None."""
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


# ---------------------------------------------------------- command line


def parse_args(argv: list[str]) -> Options:
    if any(a in ("-h", "--help") for a in argv):
        raise HelpRequested()

    positionals: list[str] = []
    values: dict[str, str] = {}
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if not tok.startswith("-"):
            positionals.append(tok)
            continue
        if not tok.startswith("--"):
            raise UsageError(f"unknown option: {tok}")
        name, eq, inline = tok.partition("=")
        if name not in OPTIONS:
            raise UsageError(f"unknown option: {name}")
        if name in values:
            raise UsageError(f"option given more than once: {name}")
        if eq:
            value = inline
        else:
            if i >= len(argv):
                raise UsageError(f"option needs a value: {name}")
            value = argv[i]
            i += 1
        values[name] = value

    if len(positionals) < 2:
        raise UsageError("missing command or ledger path")
    if len(positionals) > 2:
        raise UsageError("too many positional arguments")
    command, path = positionals
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}")

    fmt = values.get("--format", "text")
    if fmt not in FORMATS:
        raise UsageError(f"--format must be text or json, not {fmt!r}")

    since = until = None
    if "--since" in values:
        since = parse_date(values["--since"])
        if since is None:
            raise UsageError("--since must be a YYYY-MM-DD date")
    if "--until" in values:
        until = parse_date(values["--until"])
        if until is None:
            raise UsageError("--until must be a YYYY-MM-DD date")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")

    return Options(command, path, fmt, since, until)


# ---------------------------------------------------------------- reading


def read_ledger_text(path: str) -> str:
    """Read the whole ledger. Never writes the file."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReadError(path) from exc


def iter_records(text: str):
    """Yield CSV records with the physical line where each one starts.

    Blank, whitespace-only and '#'-first lines are skipped between records
    but still counted in the line numbering.
    """
    counter = [0]

    def physical():
        for ln in io.StringIO(text, newline=""):
            counter[0] += 1
            yield ln

    src = physical()
    for first in src:
        if first.strip() == "" or first.startswith("#"):
            continue
        start = counter[0]
        reader = csv.reader(itertools.chain([first], src))
        try:
            fields = next(reader)
        except csv.Error:
            yield Record(start, None)
            continue
        yield Record(start, fields)


# ------------------------------------------------------------- validation


def validate_header(header: list[str]) -> tuple[dict[str, int], list[str]]:
    """Return (column name -> index of first occurrence, error lines)."""
    cols: dict[str, int] = {}
    for idx, raw in enumerate(header):
        name = raw.strip()
        if name not in cols:
            cols[name] = idx
    errors = [f"header: missing {c}" for c in REQUIRED if c not in cols]
    return cols, errors


def parse_amount(text: str) -> int | None:
    """Return the amount in cents, or None if invalid or not positive."""
    m = _AMOUNT_RE.fullmatch(text)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0"))
    if cents <= 0:
        return None
    return cents


def parse_participants(text: str) -> list[tuple[str, int]] | None:
    """Return [(name, weight)] in listed order, or None if invalid."""
    parts: list[tuple[str, int]] = []
    seen: set[str] = set()
    for entry in text.split(";"):
        e = entry.strip()
        if e == "":
            return None
        pieces = e.split(":")
        if len(pieces) > 2:
            return None
        name = pieces[0].strip()
        if name == "":
            return None
        if len(pieces) == 2:
            w = pieces[1].strip()
            if not _WEIGHT_RE.fullmatch(w):
                return None
            weight = int(w)
            if weight == 0:
                return None
        else:
            weight = 1
        if name in seen:
            return None
        seen.add(name)
        parts.append((name, weight))
    return parts


def validate_row(rec: Record, cols: dict[str, int], width: int) -> Expense:
    """Validate one data row. Raises RowError with the first failing check."""
    line = rec.line
    f = rec.fields
    if f is None or len(f) != width:
        raise RowError(line, "bad-row")

    def get(name: str) -> str:
        return f[cols[name]].strip()

    dt = parse_date(get("date"))
    if dt is None:
        raise RowError(line, "bad-date")

    payer = get("payer")
    if payer == "" or ";" in payer or ":" in payer:
        raise RowError(line, "bad-payer")

    cents = parse_amount(get("amount"))
    if cents is None:
        raise RowError(line, "bad-amount")

    parts = parse_participants(get("participants"))
    if parts is None:
        raise RowError(line, "bad-participants")

    return Expense(line, dt, payer, cents, tuple(parts))


def in_range(e: Expense, since: date | None, until: date | None) -> bool:
    if since is not None and e.date < since:
        return False
    if until is not None and e.date > until:
        return False
    return True


# ------------------------------------------------------------ arithmetic


def split_cents(amount: int, weights: list[int]) -> list[int]:
    """Largest-remainder split of amount (cents) by weights, integers only."""
    total = sum(weights)
    bases = []
    rems = []
    for w in weights:
        q, r = divmod(amount * w, total)
        bases.append(q)
        rems.append(r)
    extra = amount - sum(bases)
    order = sorted(range(len(weights)), key=lambda i: (-rems[i], i))
    for i in order[:extra]:
        bases[i] += 1
    return bases


def compute_balances(expenses) -> dict[str, int]:
    bal: dict[str, int] = {}
    for e in expenses:
        bal[e.payer] = bal.get(e.payer, 0) + e.amount
        shares = split_cents(e.amount, [w for _, w in e.participants])
        for (name, _), share in zip(e.participants, shares):
            bal[name] = bal.get(name, 0) - share
    return bal


def plan_transfers(balances: dict[str, int]) -> list[Transfer]:
    owe = {n: -b for n, b in balances.items() if b < 0}
    owed = {n: b for n, b in balances.items() if b > 0}
    out: list[Transfer] = []
    while owe and owed:
        d = min(owe, key=lambda n: (-owe[n], n))
        c = min(owed, key=lambda n: (-owed[n], n))
        amt = min(owe[d], owed[c])
        out.append(Transfer(d, c, amt))
        owe[d] -= amt
        owed[c] -= amt
        if owe[d] == 0:
            del owe[d]
        if owed[c] == 0:
            del owed[c]
    return out


# ------------------------------------------------------------- formatting


def format_cents(c: int) -> str:
    """Unsigned amount with two decimals, for c >= 0."""
    return f"{c // 100}.{c % 100:02d}"


def format_signed(c: int) -> str:
    if c == 0:
        return "0.00"
    return ("+" if c > 0 else "-") + format_cents(abs(c))


def render_balances(bal: dict[str, int], fmt: str) -> str:
    names = sorted(bal)
    if fmt == "json":
        obj = {"balances": [{"name": n, "cents": bal[n]} for n in names]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    return "".join(f"{n} {format_signed(bal[n])}\n" for n in names)


def render_transfers(ts: list[Transfer], fmt: str) -> str:
    if fmt == "json":
        obj = {"transfers": [{"from": t.frm, "to": t.to, "cents": t.cents} for t in ts]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not ts:
        return "nothing to settle\n"
    return "".join(f"{t.frm} -> {t.to} {format_cents(t.cents)}\n" for t in ts)


# ------------------------------------------------------------------- main


def _execute(argv: list[str]) -> tuple[int, str, list[str]]:
    """Run the pipeline. Returns (exit code, stdout text, stderr lines)."""
    try:
        opts = parse_args(argv)
    except HelpRequested:
        return 0, HELP, []
    except UsageError as exc:
        return 64, "", [f"splitr.py: error: {exc}", USAGE]

    try:
        text = read_ledger_text(opts.path)
    except ReadError as exc:
        return 3, "", [f"error: cannot read {exc.path}"]

    records = iter_records(text)
    header = next(records, None)
    hfields = header.fields if (header is not None and header.fields is not None) else []
    cols, errors = validate_header(hfields)
    if errors:
        return 2, "", errors

    expenses: list[Expense] = []
    row_errors: list[str] = []
    for rec in records:
        try:
            expenses.append(validate_row(rec, cols, len(hfields)))
        except RowError as exc:
            row_errors.append(f"line {exc.line}: {exc.reason}")
    if row_errors:
        return 2, "", row_errors

    expenses = [e for e in expenses if in_range(e, opts.since, opts.until)]
    bal = compute_balances(expenses)
    if opts.command == "balances":
        return 0, render_balances(bal, opts.fmt), []
    return 0, render_transfers(plan_transfers(bal), opts.fmt), []


def main(argv: list[str]) -> int:
    sys.set_int_max_str_digits(0)  # exact arithmetic for very long amounts
    try:
        code, out, err = _execute(argv)
    except Exception:  # noqa: BLE001 - any unexpected failure maps to exit 1
        code, out, err = 1, "", ["error: internal"]

    if out:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    for line in err:
        sys.stderr.buffer.write((line + "\n").encode("utf-8", "surrogateescape"))
    sys.stderr.buffer.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
