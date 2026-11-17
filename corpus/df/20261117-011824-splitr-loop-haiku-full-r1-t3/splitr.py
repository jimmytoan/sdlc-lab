#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import datetime
import json
import re
import sys

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_READ = 3
EXIT_USAGE = 64

REQUIRED = ["date", "payer", "amount", "participants", "note"]
COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")

USAGE = """usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]"""

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


def usage_fail(msg):
    sys.stderr.write("usage error: %s\n%s\n" % (msg, USAGE))
    sys.exit(EXIT_USAGE)


def parse_date(text):
    """Return a date or None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_args(argv):
    positional = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    only_positional = False
    while i < len(argv):
        arg = argv[i]
        i += 1
        if only_positional:
            positional.append(arg)
            continue
        if arg == "--":
            only_positional = True
            continue
        if arg in ("-h", "--help"):
            print(USAGE)
            sys.exit(EXIT_OK)
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name not in ("--format", "--since", "--until"):
                usage_fail("unknown option %s" % name)
            if not eq:
                if i >= len(argv):
                    usage_fail("option %s requires a value" % name)
                value = argv[i]
                i += 1
            key = name[2:]
            if key == "format":
                if value not in FORMATS:
                    usage_fail("bad --format value %r" % value)
                opts["format"] = value
            else:
                if parse_date(value) is None:
                    usage_fail("bad --%s date %r" % (key, value))
                opts[key] = parse_date(value)
        elif arg.startswith("-") and arg != "-":
            usage_fail("unknown option %s" % arg)
        else:
            positional.append(arg)
    if len(positional) < 1:
        usage_fail("missing command")
    if positional[0] not in COMMANDS:
        usage_fail("unknown command %r" % positional[0])
    if len(positional) < 2:
        usage_fail("missing ledger argument")
    if len(positional) > 2:
        usage_fail("unexpected extra argument %r" % positional[2])
    return positional[0], positional[1], opts


def split_physical_lines(text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def is_ignored(line):
    s = line.strip()
    return s == "" or s.startswith("#")


def parse_csv_line(line):
    import csv
    return next(csv.reader([line]), [])


def parse_participants(text):
    """Return list of (name, weight) or None if invalid."""
    if text.strip() == "":
        return None
    result = []
    for part in text.split(";"):
        part = part.strip()
        if part == "":
            return None
        name, colon, weight_text = part.partition(":")
        name = name.strip()
        if name == "":
            return None
        if colon:
            weight_text = weight_text.strip()
            if not WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight <= 0:
                return None
        else:
            weight = 1
        result.append((name, weight))
    keys = [n.casefold() for n, _ in result]
    if len(set(keys)) != len(keys):
        return None
    return result


def parse_amount_cents(text):
    text = text.strip()
    if not AMOUNT_RE.fullmatch(text):
        return None
    whole, _, frac = text.partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None
    return cents


def load_ledger(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(EXIT_READ)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(EXIT_READ)
    return split_physical_lines(text)


def main(argv):
    command, path, opts = parse_args(argv)
    physical = load_ledger(path)

    # Find header: first non-ignored line.
    header_idx = None
    for idx, line in enumerate(physical):
        if not is_ignored(line):
            header_idx = idx
            break
    if header_idx is None:
        header = []
    else:
        header = [h.strip().lower() for h in parse_csv_line(physical[header_idx])]
    for col in REQUIRED:
        if col not in header:
            sys.stderr.write("header: missing %s\n" % col)
            return EXIT_INVALID
    col_index = {}
    for col in REQUIRED:
        col_index[col] = header.index(col)

    def field(fields, col):
        i = col_index[col]
        return fields[i] if i < len(fields) else ""

    display = {}  # casefold key -> display spelling (first appearance file-wide)

    def register(name):
        key = name.casefold()
        if key not in display:
            display[key] = name
        return key

    rows = []  # (line_no, date, payer_key, cents, [(key, weight)])
    errors = []
    for idx in range(header_idx + 1, len(physical)):
        line = physical[idx]
        if is_ignored(line):
            continue
        lineno = idx + 1
        fields = parse_csv_line(line)
        d = parse_date(field(fields, "date").strip())
        payer = field(fields, "payer").strip()
        cents = parse_amount_cents(field(fields, "amount"))
        parts = parse_participants(field(fields, "participants"))
        if d is None:
            errors.append("line %d: bad-date" % lineno)
            continue
        if payer == "":
            errors.append("line %d: bad-payer" % lineno)
            continue
        if cents is None:
            errors.append("line %d: bad-amount" % lineno)
            continue
        if parts is None:
            errors.append("line %d: bad-participants" % lineno)
            continue
        payer_key = register(payer)
        part_keys = []
        for name, weight in parts:
            key = register(name)
            part_keys.append((key, weight))
        rows.append((lineno, d, payer_key, cents, part_keys))

    if errors:
        for e in errors:
            sys.stderr.write(e + "\n")
        return EXIT_INVALID

    balances = {}  # key -> cents
    for _, d, payer_key, cents, parts in rows:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        for key, _w in parts:
            balances.setdefault(key, 0)
        balances.setdefault(payer_key, 0)
        balances[payer_key] += cents
        total_w = sum(w for _, w in parts)
        shares = []
        for key, w in parts:
            num = cents * w
            base = num // total_w
            rem = num % total_w
            shares.append([key, base, rem])
        leftover = cents - sum(s[1] for s in shares)
        order = sorted(shares, key=lambda s: (-s[2], display[s[0]].casefold()))
        for s in order[:leftover]:
            s[1] += 1
        for key, base, _rem in shares:
            balances[key] -= base

    names_sorted = sorted(balances.keys(), key=lambda k: display[k].casefold())

    if command == "balances":
        if opts["format"] == "json":
            doc = {"balances": [{"name": display[k], "cents": balances[k]} for k in names_sorted]}
            sys.stdout.write(json.dumps(doc) + "\n")
        else:
            out = []
            for k in names_sorted:
                out.append("%s %s" % (display[k], fmt_signed(balances[k])))
            if out:
                sys.stdout.write("\n".join(out) + "\n")
        return EXIT_OK

    # settle
    bal = dict(balances)
    transfers = []
    while True:
        debtor = None
        creditor = None
        for k in names_sorted:
            v = bal[k]
            if v < 0 and (debtor is None or v < bal[debtor]):
                debtor = k
            if v > 0 and (creditor is None or v > bal[creditor]):
                creditor = k
        if debtor is None or creditor is None:
            break
        amt = min(-bal[debtor], bal[creditor])
        bal[debtor] += amt
        bal[creditor] -= amt
        transfers.append((debtor, creditor, amt))

    if opts["format"] == "json":
        doc = {"transfers": [{"from": display[d], "to": display[c], "cents": a}
                             for d, c, a in transfers]}
        sys.stdout.write(json.dumps(doc) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            for d, c, a in transfers:
                sys.stdout.write("%s -> %s %s\n" % (display[d], display[c], fmt_unsigned(a)))
    return EXIT_OK


def fmt_unsigned(cents):
    return "%d.%02d" % (abs(cents) // 100, abs(cents) % 100)


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    return sign + fmt_unsigned(cents)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
