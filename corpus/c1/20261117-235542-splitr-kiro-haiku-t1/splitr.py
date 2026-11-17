#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI.

Usage: python splitr.py {balances,settle} LEDGER.csv [--format text|json]
                        [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Standard library only. All money is integer cents.
"""
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_ROWS = 2
EXIT_READ = 3
EXIT_USAGE = 64

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
REQUIRED = ("date", "payer", "amount", "participants")
REASON = {
    "date": "bad-date",
    "payer": "bad-payer",
    "amount": "bad-amount",
    "participants": "bad-participants",
}
TRIM = " \t"

USAGE = ("usage: splitr.py {balances,settle} LEDGER.csv [--format text|json] "
         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
HELP = (
    "usage: splitr.py {balances,settle} LEDGER.csv [--format text|json] "
    "[--since YYYY-MM-DD] [--until YYYY-MM-DD]\n"
    "\n"
    "commands:\n"
    "  balances            print each person's net position\n"
    "  settle              print the payments that clear all debts\n"
    "\n"
    "options:\n"
    "  --format text|json  output format (default: text)\n"
    "  --since YYYY-MM-DD  only rows on or after this date\n"
    "  --until YYYY-MM-DD  only rows on or before this date\n"
    "  --help              show this help and exit\n"
)

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


class HelpRequested(Exception):
    pass


class ReadError(Exception):
    def __init__(self, path):
        super().__init__(path)
        self.path = path


class LedgerError(Exception):
    def __init__(self, messages):
        super().__init__("ledger errors")
        self.messages = messages


class RowFail(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Options:
    def __init__(self, command, path, fmt, since, until):
        self.command = command
        self.path = path
        self.fmt = fmt
        self.since = since
        self.until = until


class Header:
    def __init__(self, cols, ncols, missing):
        self.cols = cols        # {name: first index}
        self.ncols = ncols      # number of header columns
        self.missing = missing  # required columns absent, in REQUIRED order


class Row:
    def __init__(self, line, date, payer, cents, participants):
        self.line = line
        self.date = date
        self.payer = payer
        self.cents = cents
        self.participants = participants  # tuple of (name, weight), entry order


def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


# ---------------------------------------------------------------- CLI

def parse_args(argv):
    positionals = []
    fmt = "text"
    since = None
    until = None
    want_help = False
    error = None
    options_done = False

    def fail(msg):
        nonlocal error
        if error is None:
            error = msg

    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if options_done or a == "-" or not a.startswith("-"):
            positionals.append(a)
            continue
        if a == "--":
            options_done = True
            continue
        if a == "--help":
            want_help = True
            continue
        name, eq, value = a.partition("=")
        if name not in ("--format", "--since", "--until"):
            fail(f"unknown option {name}")
            continue
        if not eq:
            if i < len(argv):
                value = argv[i]
                i += 1
            else:
                fail(f"option {name} needs a value")
                continue
        if name == "--format":
            if value in FORMATS:
                fmt = value
            else:
                fail("--format must be text or json")
        elif name == "--since":
            d = parse_date(value)
            if d is None:
                fail("--since must be a date YYYY-MM-DD")
            else:
                since = d
        else:
            d = parse_date(value)
            if d is None:
                fail("--until must be a date YYYY-MM-DD")
            else:
                until = d

    if want_help:
        raise HelpRequested()
    if error is not None:
        raise UsageError(error)
    if len(positionals) < 1:
        raise UsageError("missing command")
    command = positionals[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command {command!r}")
    if len(positionals) < 2:
        raise UsageError("missing ledger path")
    if len(positionals) > 2:
        raise UsageError("too many arguments")
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until")
    return Options(command, positionals[1], fmt, since, until)


# ---------------------------------------------------------------- reading

def read_text(path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except (OSError, ValueError):
        raise ReadError(path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReadError(path)
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def tokenise(text):
    """Return a list of (start_line, fields) records.

    Comment lines (a '#' in column 1 at the start of a record) and records
    that are only whitespace are dropped. Line numbers count every physical
    line.
    """
    records = []
    n = len(text)
    i = 0
    line = 1
    while i < n:
        start_line = line
        if text[i] == "#":
            j = text.find("\n", i)
            if j == -1:
                break
            i = j + 1
            line += 1
            continue
        rec_start = i
        fields = []
        while True:
            j = i
            while j < n and text[j] == " ":
                j += 1
            if j < n and text[j] == '"':
                i = j + 1
                buf = []
                while i < n:
                    c = text[i]
                    if c == '"':
                        if i + 1 < n and text[i + 1] == '"':
                            buf.append('"')
                            i += 2
                            continue
                        i += 1
                        break
                    if c == "\n":
                        line += 1
                    buf.append(c)
                    i += 1
                # Characters after the closing quote are kept literally.
                while i < n and text[i] not in ",\n":
                    buf.append(text[i])
                    i += 1
                fields.append("".join(buf))
            else:
                j = i
                while j < n and text[j] not in ",\n":
                    j += 1
                fields.append(text[i:j])
                i = j
            if i < n and text[i] == ",":
                i += 1
                continue
            rec_end = i
            if i < n:
                i += 1  # consume the record terminator
                line += 1
            break
        if text[rec_start:rec_end].strip() == "":
            continue
        records.append((start_line, fields))
    return records


def parse_header(records):
    if not records:
        return Header({}, 0, list(REQUIRED))
    names = [f.strip(TRIM) for f in records[0][1]]
    cols = {}
    for idx, name in enumerate(names):
        cols.setdefault(name, idx)
    missing = [c for c in REQUIRED if c not in cols]
    return Header(cols, len(names), missing)


# ---------------------------------------------------------------- rows

def parse_participants(raw):
    if raw == "":
        raise RowFail("bad-participants")
    entries = []
    names = set()
    for part in raw.split(";"):
        bits = part.split(":")
        if len(bits) > 2:
            raise RowFail("bad-participants")
        name = bits[0].strip(TRIM)
        if name == "":
            raise RowFail("bad-participants")
        if len(bits) == 2:
            wt = bits[1].strip(TRIM)
            if not WEIGHT_RE.fullmatch(wt):
                raise RowFail("bad-participants")
            weight = int(wt)
            if weight < 1:
                raise RowFail("bad-participants")
        else:
            weight = 1
        if name in names:
            raise RowFail("bad-participants")
        names.add(name)
        entries.append((name, weight))
    return tuple(entries)


def validate_row(record, header):
    """Return a Row, or raise RowFail with the reason for the first problem."""
    line, fields = record
    if len(fields) > header.ncols:
        raise RowFail("bad-participants")
    for col in REQUIRED:
        if header.cols[col] >= len(fields):
            raise RowFail(REASON[col])

    def get(col):
        return fields[header.cols[col]].strip(TRIM)

    d = parse_date(get("date"))
    if d is None:
        raise RowFail("bad-date")

    payer = get("payer")
    if payer == "" or ";" in payer or ":" in payer:
        raise RowFail("bad-payer")

    m = AMOUNT_RE.fullmatch(get("amount"))
    if m is None:
        raise RowFail("bad-amount")
    whole = int(m.group(1))
    frac = m.group(2) or ""
    cents = whole * 100 + int(frac.ljust(2, "0"))
    if cents == 0:
        raise RowFail("bad-amount")

    participants = parse_participants(get("participants"))
    return Row(line, d, payer, cents, participants)


def in_range(row, since, until):
    if since is not None and row.date < since:
        return False
    if until is not None and row.date > until:
        return False
    return True


# ---------------------------------------------------------------- money

def split(cents, participants):
    """Split cents among (name, weight) entries; returns shares in entry order."""
    total = sum(w for _, w in participants)
    floors = []
    rems = []
    for _, w in participants:
        q, r = divmod(cents * w, total)
        floors.append(q)
        rems.append(r)
    left = cents - sum(floors)
    shares = list(floors)
    order = sorted(range(len(participants)), key=lambda i: (-rems[i], i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def accumulate(rows):
    balances = {}
    for row in rows:
        balances[row.payer] = balances.get(row.payer, 0) + row.cents
        shares = split(row.cents, row.participants)
        for (name, _), share in zip(row.participants, shares):
            balances[name] = balances.get(name, 0) - share
    return balances


def settle(balances):
    """Greedy settlement. Returns a list of (from, to, cents) in order made."""
    bal = dict(balances)
    transfers = []
    while True:
        debtors = [n for n, v in bal.items() if v < 0]
        creditors = [n for n, v in bal.items() if v > 0]
        if not debtors or not creditors:
            break
        d = min(debtors, key=lambda n: (bal[n], n))
        c = min(creditors, key=lambda n: (-bal[n], n))
        amt = min(-bal[d], bal[c])
        transfers.append((d, c, amt))
        bal[d] += amt
        bal[c] -= amt
    return transfers


# ---------------------------------------------------------------- output

def fmt_magnitude(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def fmt_signed(cents):
    if cents > 0:
        return "+" + fmt_magnitude(cents)
    if cents < 0:
        return "-" + fmt_magnitude(-cents)
    return fmt_magnitude(0)


def render_balances(balances, fmt):
    names = sorted(balances)
    if fmt == "json":
        obj = {"balances": [{"name": n, "cents": balances[n]} for n in names]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    return "".join(f"{n} {fmt_signed(balances[n])}\n" for n in names)


def render_transfers(transfers, fmt):
    if fmt == "json":
        obj = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join(f"{f} -> {t} {fmt_magnitude(c)}\n" for f, t, c in transfers)


# ---------------------------------------------------------------- pipeline

def run_pipeline(argv):
    """Return (exit_code, stdout_bytes, stderr_bytes)."""
    try:
        opts = parse_args(argv)
        text = read_text(opts.path)
        records = tokenise(text)
        header = parse_header(records)
        if header.missing:
            raise LedgerError([f"header: missing {c}" for c in header.missing])

        rows = []
        errors = []
        for rec in records[1:]:
            try:
                rows.append(validate_row(rec, header))
            except RowFail as e:
                errors.append(f"line {rec[0]}: {e.reason}")
        if errors:
            raise LedgerError(errors)

        rows = [r for r in rows if in_range(r, opts.since, opts.until)]
        balances = accumulate(rows)
        if opts.command == "balances":
            out = render_balances(balances, opts.fmt)
        else:
            out = render_transfers(settle(balances), opts.fmt)
        return EXIT_OK, out.encode("utf-8"), b""
    except HelpRequested:
        return EXIT_OK, HELP.encode("utf-8"), b""
    except UsageError as e:
        return EXIT_USAGE, b"", (f"splitr: {e}\n" + USAGE).encode("utf-8")
    except ReadError as e:
        msg = f"error: cannot read {e.path}\n"
        return EXIT_READ, b"", msg.encode("utf-8", "surrogateescape")
    except LedgerError as e:
        msg = "".join(m + "\n" for m in e.messages)
        return EXIT_ROWS, b"", msg.encode("utf-8")


def main(argv, stdout=None, stderr=None):
    """Run splitr. stdout/stderr are binary streams (default: sys.std*.buffer)."""
    out_stream = stdout if stdout is not None else sys.stdout.buffer
    err_stream = stderr if stderr is not None else sys.stderr.buffer
    if hasattr(sys, "set_int_max_str_digits"):
        sys.set_int_max_str_digits(0)
    try:
        code, out, err = run_pipeline(list(argv))
    except Exception:
        code, out, err = EXIT_READ, b"", b"error: internal\n"
    if out:
        out_stream.write(out)
        out_stream.flush()
    if err:
        err_stream.write(err)
        err_stream.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
