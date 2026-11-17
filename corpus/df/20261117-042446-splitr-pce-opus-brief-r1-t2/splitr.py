#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys
from collections import namedtuple
from fractions import Fraction

Row = namedtuple("Row", "line date payer cents parts")

REQUIRED = ("date", "payer", "amount", "participants")
COMMANDS = ("balances", "settle")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")

USAGE = ("usage: splitr.py balances|settle <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class UsageError(Exception):
    pass


class ReadError(Exception):
    pass


class HeaderError(Exception):
    def __init__(self, missing):
        super().__init__(missing)
        self.missing = missing


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    if not argv:
        raise UsageError("missing command")
    command = argv[0]
    if command not in COMMANDS:
        raise UsageError("unknown command: %s" % command)
    fmt = "text"
    since = until = None
    paths = []
    i = 1
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg.startswith("-") and arg != "-":
            if arg == "--":
                paths.extend(argv[i + 1:])
                break
            if "=" in arg and arg.startswith("--"):
                name, value = arg.split("=", 1)
                has_value = True
            else:
                name, value, has_value = arg, None, False
            if name not in ("--format", "--since", "--until"):
                raise UsageError("unknown option: %s" % name)
            if not has_value:
                if i + 1 >= n:
                    raise UsageError("missing value for %s" % name)
                value = argv[i + 1]
                i += 1
            if name == "--format":
                if value not in ("text", "json"):
                    raise UsageError("bad format: %s" % value)
                fmt = value
            else:
                d = parse_date(value)
                if d is None:
                    raise UsageError("bad date for %s: %s" % (name, value))
                if name == "--since":
                    since = d
                else:
                    until = d
        else:
            paths.append(arg)
        i += 1
    if len(paths) != 1:
        raise UsageError("expected exactly one ledger path")
    return command, paths[0], fmt, since, until


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError, ValueError):
        raise ReadError(path)


def parse_amount(s):
    m = AMOUNT_RE.fullmatch(s)
    if not m:
        return None
    whole, frac = m.group(1), m.group(2) or ""
    cents = int(whole) * 100 + int(frac.ljust(2, "0") or "0")
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    if not s:
        return None
    parts = []
    seen = set()
    for entry in s.split(";"):
        entry = entry.strip()
        if not entry:
            return None
        if entry.count(":") > 1:
            return None
        if ":" in entry:
            name, w = entry.split(":", 1)
            name, w = name.strip(), w.strip()
            if not WEIGHT_RE.fullmatch(w):
                return None
            weight = Fraction(w)
            if weight <= 0:
                return None
        else:
            name, weight = entry, Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        parts.append((name, weight))
    return parts


def parse_row(fields, colmap, line):
    def get(col):
        idx = colmap[col]
        return fields[idx].strip() if idx < len(fields) else ""

    d = parse_date(get("date"))
    if d is None:
        return "bad-date"
    payer = get("payer")
    if not payer or ";" in payer or ":" in payer:
        return "bad-payer"
    cents = parse_amount(get("amount"))
    if cents is None:
        return "bad-amount"
    parts = parse_participants(get("participants"))
    if parts is None:
        return "bad-participants"
    return Row(line, d, payer, cents, parts)


def _ends_in_quote(line, in_quote):
    """Scan one physical line with csv-module quoting rules (default dialect,
    non-strict). Return True if the line ends inside a quoted field."""
    FIELD_START, UNQUOTED, QUOTED, QUOTE_IN_QUOTED = 0, 1, 2, 3
    state = QUOTED if in_quote else FIELD_START
    for ch in line:
        if ch in "\r\n" and state != QUOTED:
            break
        if state == FIELD_START:
            if ch == '"':
                state = QUOTED
            elif ch != ",":
                state = UNQUOTED
        elif state == UNQUOTED:
            if ch == ",":
                state = FIELD_START
        elif state == QUOTED:
            if ch == '"':
                state = QUOTE_IN_QUOTED
        else:  # QUOTE_IN_QUOTED
            if ch == '"':
                state = QUOTED
            elif ch == ",":
                state = FIELD_START
            else:
                state = UNQUOTED
    return state == QUOTED


def iter_records(text):
    """Yield (start_line, [physical lines]) for each CSV record.

    Comment ('#') and blank lines are skipped only where a record would start.
    A quoted field may span several lines; if a quote is never closed before
    end of file, the record is confined to its first physical line and
    parsing resumes on the next line, so later rows are not swallowed."""
    physical = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text)
    n = len(physical)
    i = 0
    while i < n:
        raw = physical[i]
        if not raw.strip() or raw.startswith("#"):
            i += 1
            continue
        j = i
        in_quote = _ends_in_quote(raw, False)
        while in_quote and j + 1 < n:
            j += 1
            in_quote = _ends_in_quote(physical[j], True)
        if in_quote:  # unterminated quote at end of file
            j = i
        yield i + 1, physical[i:j + 1]
        i = j + 1


def load_ledger(text):
    rows, errors = [], []
    colmap = None
    for lineno, lines in iter_records(text):
        try:
            fields = next(csv.reader(lines), [])
        except csv.Error:
            fields = None
        if colmap is None:
            colmap = {}
            for idx, name in enumerate(fields or []):
                key = name.strip().lower()
                if key not in colmap:
                    colmap[key] = idx
            missing = [c for c in REQUIRED if c not in colmap]
            if missing:
                raise HeaderError(missing)
            continue
        if fields is None:
            errors.append((lineno, "bad-date"))
            continue
        result = parse_row(fields, colmap, lineno)
        if isinstance(result, str):
            errors.append((lineno, result))
        else:
            rows.append(result)
    if colmap is None:
        raise HeaderError(list(REQUIRED))
    return rows, errors


def filter_rows(rows, since, until):
    return [r for r in rows
            if (since is None or r.date >= since)
            and (until is None or r.date <= until)]


def split_cents(cents, parts):
    total = sum(w for _, w in parts)
    exact = [Fraction(cents) * w / total for _, w in parts]
    floors = [e.numerator // e.denominator for e in exact]
    leftover = cents - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[:leftover]:
        floors[i] += 1
    return floors


def compute_balances(rows):
    bal = {}
    for r in rows:
        bal[r.payer] = bal.get(r.payer, 0) + r.cents
        for (name, _), share in zip(r.parts, split_cents(r.cents, r.parts)):
            bal[name] = bal.get(name, 0) - share
    return bal


def settle(balances):
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [(v, k) for k, v in bal.items() if v < 0]
        creditors = [(-v, k) for k, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        dv, dname = min(debtors)
        cv, cname = min(creditors)
        amount = min(-dv, -cv)
        transfers.append((dname, cname, amount))
        bal[dname] += amount
        bal[cname] -= amount
    return transfers


def fmt_cents(c, signed=True):
    a = abs(c)
    body = "%d.%02d" % (a // 100, a % 100)
    if not signed or c == 0:
        return body
    return ("+" if c > 0 else "-") + body


def render_balances(bal, fmt):
    names = sorted(bal)
    if fmt == "json":
        return json.dumps({"balances": [{"name": n, "cents": bal[n]} for n in names]}) + "\n"
    return "".join("%s %s\n" % (n, fmt_cents(bal[n])) for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        return json.dumps({"transfers": [
            {"from": f, "to": t, "cents": c} for f, t, c in transfers]}) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (f, t, fmt_cents(c, False)) for f, t, c in transfers)


def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("splitr: %s\n%s\n" % (e, USAGE))
        return 64
    try:
        text = read_text(path)
    except ReadError:
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3
    try:
        rows, errors = load_ledger(text)
    except HeaderError as e:
        sys.stderr.write("".join("header: missing %s\n" % c for c in e.missing))
        return 2
    if errors:
        sys.stderr.write("".join("line %d: %s\n" % le for le in errors))
        return 2
    bal = compute_balances(filter_rows(rows, since, until))
    if command == "balances":
        out = render_balances(bal, fmt)
    else:
        out = render_settle(settle(bal), fmt)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
