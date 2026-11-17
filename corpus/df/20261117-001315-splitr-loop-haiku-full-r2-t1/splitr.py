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

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
WEIGHT_RE = re.compile(r"[0-9]+")


class UsageError(Exception):
    pass


def usage_fail(msg):
    sys.stderr.write(f"usage error: {msg}\n")
    sys.stderr.write("usage: splitr.py <balances|settle> <ledger.csv> "
                     "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
    sys.exit(EXIT_USAGE)


def parse_date(s):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 0
    options_with_value = {"--format", "--since", "--until"}
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            sys.stdout.write("usage: splitr.py <balances|settle> <ledger.csv> "
                             "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
            sys.exit(EXIT_OK)
        if a.startswith("--") and "=" in a:
            name, value = a.split("=", 1)
            if name not in options_with_value:
                usage_fail(f"unknown option {name}")
        elif a in options_with_value:
            if i + 1 >= len(argv):
                usage_fail(f"option {a} requires a value")
            name, value = a, argv[i + 1]
            i += 1
        elif a.startswith("-") and a != "-":
            usage_fail(f"unknown option {a}")
        else:
            positional.append(a)
            i += 1
            continue
        i += 1
        if name == "--format":
            if value not in FORMATS:
                usage_fail(f"bad --format value {value!r}")
            opts["format"] = value
        else:
            d = parse_date(value)
            if d is None:
                usage_fail(f"bad {name} date {value!r}")
            opts["since" if name == "--since" else "until"] = d
    if len(positional) < 1:
        usage_fail("missing command")
    if positional[0] not in COMMANDS:
        usage_fail(f"unknown command {positional[0]!r}")
    if len(positional) < 2:
        usage_fail("missing ledger file argument")
    if len(positional) > 2:
        usage_fail("too many arguments")
    return positional[0], positional[1], opts


def fold(name):
    return name.casefold()


class Registry:
    """Maps case-insensitive names to the spelling of first appearance."""

    def __init__(self):
        self.display = {}

    def add(self, name):
        key = fold(name)
        if key not in self.display:
            self.display[key] = name
        return key


def split_shares(cents, weighted, payer=None):
    """weighted: list of (key, weight). Returns dict key -> share cents.

    If payer is one of the participants and there is a leftover cent, the
    payer receives the first leftover cent; the rest go by largest remainder.
    """
    total_w = sum(w for _, w in weighted)
    shares = {}
    rems = []
    base_sum = 0
    for key, w in weighted:
        prod = cents * w
        base = prod // total_w
        rem = prod % total_w
        shares[key] = base
        rems.append((key, rem))
        base_sum += base
    leftover = cents - base_sum
    if payer is not None and leftover > 0 and any(k == payer for k, _ in weighted):
        shares[payer] += 1
        leftover -= 1
        rems = [kr for kr in rems if kr[0] != payer]
    rems.sort(key=lambda kr: (-kr[1], kr[0]))
    for key, _ in rems[:leftover]:
        shares[key] += 1
    return shares


def parse_participant(part):
    """Return (name, weight) or None if invalid."""
    if ":" in part:
        name, wtext = part.rsplit(":", 1)
        name = name.strip()
        wtext = wtext.strip()
        if not WEIGHT_RE.fullmatch(wtext):
            return None
        weight = int(wtext)
        if weight <= 0:
            return None
    else:
        name = part.strip()
        weight = 1
    if not name:
        return None
    return name, weight


def read_ledger(path):
    """Return list of (lineno, fields-dict-or-None, reason) and header info.

    Raises OSError / UnicodeDecodeError on read failure."""
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8-sig")
    physical = text.split("\n")
    rows = []
    header = None
    for idx, line in enumerate(physical):
        lineno = idx + 1
        if line.endswith("\r"):
            line = line[:-1]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        fields = next(csv.reader([line]))
        if header is None:
            header = [h.strip().lower() for h in fields]
            continue
        rows.append((lineno, fields))
    return header, rows


def main(argv):
    command, path, opts = parse_args(argv)

    try:
        header, rows = read_ledger(path)
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        return EXIT_READ

    if header is None:
        header = []
    for col in REQUIRED_COLUMNS:
        if col not in header:
            sys.stderr.write(f"header: missing {col}\n")
            return EXIT_INVALID
    col_idx = {c: header.index(c) for c in REQUIRED_COLUMNS}
    has_kind = "kind" in header
    if has_kind:
        col_idx["kind"] = header.index("kind")

    names = Registry()
    invalid = []
    valid_rows = []  # (date, kind, payer_key, [(key, weight)], cents)

    for lineno, fields in rows:
        def get(col):
            i = col_idx[col]
            return fields[i] if i < len(fields) else ""

        date_s = get("date").strip()
        d = parse_date(date_s)
        if d is None:
            invalid.append((lineno, "bad-date"))
            continue

        kind = "expense"
        if has_kind:
            kind_s = get("kind").strip().casefold()
            if kind_s in ("", "expense"):
                kind = "expense"
            elif kind_s == "payment":
                kind = "payment"
            else:
                invalid.append((lineno, "bad-kind"))
                continue

        payer_raw = get("payer").strip()
        if not payer_raw:
            invalid.append((lineno, "bad-payer"))
            continue

        amount_s = get("amount").strip()
        if not AMOUNT_RE.fullmatch(amount_s):
            invalid.append((lineno, "bad-amount"))
            continue
        if "." in amount_s:
            whole, frac = amount_s.split(".")
            frac = (frac + "00")[:2]
        else:
            whole, frac = amount_s, "00"
        cents = int(whole) * 100 + int(frac)
        if cents <= 0:
            invalid.append((lineno, "bad-amount"))
            continue

        part_s = get("participants")
        parts = part_s.split(";")
        parsed = []
        bad = False
        if kind == "payment":
            recipient = part_s.strip()
            if (not recipient or ";" in part_s or ":" in part_s
                    or fold(recipient) == fold(payer_raw)):
                bad = True
            else:
                parsed.append((recipient, 1))
        elif part_s.strip() == "":
            bad = True
        else:
            seen = set()
            for p in parts:
                res = parse_participant(p)
                if res is None:
                    bad = True
                    break
                pname, weight = res
                pkey = fold(pname)
                if pkey in seen:
                    bad = True
                    break
                seen.add(pkey)
                parsed.append((pname, weight))
        if bad:
            invalid.append((lineno, "bad-participants"))
            continue

        # Valid row: register names in order payer, then participants.
        payer_key = names.add(payer_raw)
        weighted = []
        for pname, weight in parsed:
            weighted.append((names.add(pname), weight))
        valid_rows.append((d, kind, payer_key, weighted, cents))

    if invalid:
        for lineno, reason in sorted(invalid):
            sys.stderr.write(f"line {lineno}: {reason}\n")
        return EXIT_INVALID

    balances = {}
    for d, kind, payer_key, weighted, cents in valid_rows:
        if opts["since"] is not None and d < opts["since"]:
            continue
        if opts["until"] is not None and d > opts["until"]:
            continue
        balances.setdefault(payer_key, 0)
        balances[payer_key] += cents
        for key, _ in weighted:
            balances.setdefault(key, 0)
        if kind == "payment":
            # payer handed cents directly to the single recipient
            balances[weighted[0][0]] -= cents
            continue
        shares = split_shares(cents, weighted, payer_key)
        for key, share in shares.items():
            balances[key] -= share

    people = sorted(balances.keys())  # keys are casefolded; sorting by them ignores case

    if command == "balances":
        if opts["format"] == "json":
            obj = {"balances": [{"name": names.display[k], "cents": balances[k]} for k in people]}
            sys.stdout.write(json.dumps(obj) + "\n")
        else:
            out = []
            for k in people:
                out.append(f"{names.display[k]} {format_signed(balances[k])}")
            if out:
                sys.stdout.write("\n".join(out) + "\n")
        return EXIT_OK

    # settle
    transfers = settle(balances, names.display)
    if opts["format"] == "json":
        obj = {"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}
        sys.stdout.write(json.dumps(obj) + "\n")
    else:
        if not transfers:
            sys.stdout.write("nothing to settle\n")
        else:
            out = [f"{f} -> {t} {format_unsigned(c)}" for f, t, c in transfers]
            sys.stdout.write("\n".join(out) + "\n")
    return EXIT_OK


def settle(balances, display):
    bal = {k: v for k, v in balances.items() if v != 0}
    transfers = []
    while bal:
        debtor = min(bal, key=lambda k: (bal[k], k))  # most negative, then name
        creditor = min((k for k in bal if bal[k] > 0), key=lambda k: (-bal[k], k))
        amount = min(-bal[debtor], bal[creditor])
        transfers.append((display[debtor], display[creditor], amount))
        bal[debtor] += amount
        bal[creditor] -= amount
        bal = {k: v for k, v in bal.items() if v != 0}
    return transfers


def format_unsigned(cents):
    a = abs(cents)
    return f"{a // 100}.{a % 100:02d}"


def format_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    return sign + format_unsigned(cents)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
