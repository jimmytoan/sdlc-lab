"""splitr: shared-expense settlement CLI.

Usage:
    python splitr.py <balances|settle> <ledger.csv> [--format text|json]
                     [--since YYYY-MM-DD] [--until YYYY-MM-DD]

Money is handled as integer cents throughout. The whole ledger is validated
before anything is printed, so stdout is never partially written.

Exit codes: 0 success, 2 invalid ledger (header or rows), 3 unreadable ledger,
64 usage error.
"""
import csv
import datetime
import io
import json
import re
import sys

COMMANDS = ("balances", "settle")
FORMATS = ("text", "json")
OPTIONS = ("--format", "--since", "--until")
REQUIRED_COLUMNS = ["date", "payer", "amount", "participants", "note"]
ROW_FIELDS = ["date", "payer", "amount", "participants"]

DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
AMOUNT_RE = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")
WEIGHT_RE = re.compile(r"[0-9]+")

USAGE = ("usage: splitr.py <balances|settle> <ledger.csv> [--format text|json] "
         "[--since YYYY-MM-DD] [--until YYYY-MM-DD]")


class UsageError(Exception):
    pass


def parse_date(text):
    """Return a datetime.date for a strict YYYY-MM-DD string, else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_args(argv):
    """Return (command, path, fmt, since, until). Raises UsageError."""
    if len(argv) < 2:
        raise UsageError(USAGE)
    command = argv[0]
    if command not in COMMANDS:
        raise UsageError(f"unknown command: {command}\n{USAGE}")
    path = argv[1]
    if path.startswith("--"):
        raise UsageError(f"missing ledger path\n{USAGE}")

    opts = {"--format": "text", "--since": None, "--until": None}
    i = 2
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            raise UsageError(f"unexpected argument: {arg}\n{USAGE}")
        name, eq, value = arg.partition("=")
        if name not in OPTIONS:
            raise UsageError(f"unknown option: {name}\n{USAGE}")
        if eq:
            i += 1
        else:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                raise UsageError(f"missing value for {name}\n{USAGE}")
            value = argv[i + 1]
            i += 2
        if name == "--format":
            if value not in FORMATS:
                raise UsageError(f"invalid --format value: {value}\n{USAGE}")
            opts[name] = value
        else:
            parsed = parse_date(value)
            if parsed is None:
                raise UsageError(f"invalid date for {name}: {value}\n{USAGE}")
            opts[name] = parsed

    since, until = opts["--since"], opts["--until"]
    if since is not None and until is not None and since > until:
        raise UsageError("--since is later than --until\n" + USAGE)
    return command, path, opts["--format"], since, until


def read_lines(path):
    """Return the physical lines of the ledger, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    return io.StringIO(text, newline="").readlines()


def iter_records(lines):
    """Yield (start_line_number, fields) for each CSV record.

    Blank lines and lines starting with '#' are skipped between records.
    fields is None if the CSV layer could not parse the record.
    """
    n = len(lines)
    idx = 0
    while idx < n:
        line = lines[idx]
        if line.startswith("#") or line.strip() == "":
            idx += 1
            continue
        start = idx + 1
        pos = [idx]

        def feed():
            while pos[0] < n:
                current = lines[pos[0]]
                pos[0] += 1
                yield current

        reader = csv.reader(feed())
        try:
            fields = next(reader)
        except StopIteration:
            fields = None
        except csv.Error:
            fields = None
        idx = pos[0]
        yield start, fields


def parse_participants(text):
    """Return an ordered list of [key, display, weight], or None if invalid.

    Duplicate people within one row are merged: the weights are summed and the
    position of the first occurrence is kept.
    """
    if text is None:
        return None
    merged = {}
    order = []
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            return None
        name, colon, weight_text = segment.partition(":")
        weight = 1
        if colon:
            weight_text = weight_text.strip()
            if not WEIGHT_RE.fullmatch(weight_text):
                return None
            weight = int(weight_text)
            if weight < 1:
                return None
        name = name.strip()
        if not name:
            return None
        key = name.casefold()
        if key in merged:
            merged[key][2] += weight
        else:
            entry = [key, name, weight]
            merged[key] = entry
            order.append(entry)
    return order


def parse_amount(text):
    """Return integer cents for a positive amount with at most two decimals."""
    if text is None:
        return None
    match = AMOUNT_RE.fullmatch(text.strip())
    if not match:
        return None
    whole = int(match.group(1))
    fraction = (match.group(2) or "0").ljust(2, "0")
    cents = whole * 100 + int(fraction)
    return cents if cents > 0 else None


def validate_row(fields, columns, width):
    """Validate one record. Return (expense, None) or (None, reason).

    Only the first error in the row is reported. columns maps each required
    column name to its index in the header.
    """

    def get(column):
        index = columns[column]
        return fields[index] if index < len(fields) else None

    date_text = get("date")
    date = parse_date(date_text.strip()) if date_text is not None else None
    if date is None:
        return None, "bad-date"

    payer_text = get("payer")
    if payer_text is None or not payer_text.strip():
        return None, "bad-payer"

    cents = parse_amount(get("amount"))
    if cents is None:
        return None, "bad-amount"

    participants = parse_participants(get("participants"))
    if participants is None:
        return None, "bad-participants"

    # Four required fields are valid. A record with a different number of
    # fields than the header is still refused.
    if len(fields) != width:
        return None, "bad-participants"

    payer_name = payer_text.strip()
    return {
        "date": date,
        "payer_key": payer_name.casefold(),
        "payer_name": payer_name,
        "cents": cents,
        "participants": participants,
    }, None


def split_cents(cents, weights):
    """Split cents by weight using the largest-remainder rule. Sums exactly."""
    total = sum(weights)
    bases = []
    remainders = []
    for weight in weights:
        product = cents * weight
        bases.append(product // total)
        remainders.append(product % total)
    leftover = cents - sum(bases)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        bases[i] += 1
    return bases


def compute_balances(expenses):
    """Return {key: cents} with paid minus shares for each counted person."""
    balances = {}
    for expense in expenses:
        payer = expense["payer_key"]
        balances[payer] = balances.get(payer, 0) + expense["cents"]
        keys = [p[0] for p in expense["participants"]]
        shares = split_cents(expense["cents"], [p[2] for p in expense["participants"]])
        for key, share in zip(keys, shares):
            balances[key] = balances.get(key, 0) - share
    return balances


def settle(balances):
    """Greedy settlement. Return [(debtor_key, creditor_key, cents)]."""
    remaining = dict(balances)
    transfers = []
    while True:
        debtors = [k for k, v in remaining.items() if v < 0]
        creditors = [k for k, v in remaining.items() if v > 0]
        if not debtors or not creditors:
            break
        debtor = min(debtors, key=lambda k: (remaining[k], k))
        creditor = min(creditors, key=lambda k: (-remaining[k], k))
        amount = min(-remaining[debtor], remaining[creditor])
        transfers.append((debtor, creditor, amount))
        remaining[debtor] += amount
        remaining[creditor] -= amount
    return transfers


def format_signed(cents):
    sign = "+" if cents > 0 else "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def format_plain(cents):
    return f"{abs(cents) // 100}.{abs(cents) % 100:02d}"


def main(argv):
    try:
        command, path, fmt, since, until = parse_args(argv)
    except UsageError as exc:
        sys.stderr.write(f"{exc}\n")
        return 64

    lines = read_lines(path)
    if lines is None:
        sys.stderr.write(f"error: cannot read {path}\n")
        return 3

    records = iter_records(lines)
    header_record = next(records, None)
    header = None
    if header_record is not None and header_record[1] is not None:
        header = [name.strip() for name in header_record[1]]
    if header is None:
        header = []
    for column in REQUIRED_COLUMNS:
        if column not in header:
            sys.stderr.write(f"header: missing {column}\n")
            return 2
    columns = {column: header.index(column) for column in REQUIRED_COLUMNS}

    expenses = []
    errors = []
    for line_no, fields in records:
        if fields is None:
            errors.append(f"line {line_no}: bad-participants")
            continue
        expense, reason = validate_row(fields, columns, len(header))
        if reason is not None:
            errors.append(f"line {line_no}: {reason}")
        else:
            expense["line"] = line_no
            expenses.append(expense)

    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2

    # Display spelling: first appearance in the whole file, payer before
    # participants, regardless of any date filter.
    display = {}
    for expense in expenses:
        display.setdefault(expense["payer_key"], expense["payer_name"])
        for key, name, _ in expense["participants"]:
            display.setdefault(key, name)

    counted = [
        e for e in expenses
        if (since is None or e["date"] >= since) and (until is None or e["date"] <= until)
    ]
    balances = compute_balances(counted)

    if command == "balances":
        people = sorted(balances)
        if fmt == "json":
            payload = {"balances": [{"name": display[k], "cents": balances[k]} for k in people]}
            sys.stdout.write(json.dumps(payload) + "\n")
        else:
            out = [f"{display[k]} {format_signed(balances[k])}" for k in people]
            if out:
                sys.stdout.write("\n".join(out) + "\n")
        return 0

    transfers = settle(balances)
    if fmt == "json":
        payload = {"transfers": [
            {"from": display[d], "to": display[c], "cents": amount}
            for d, c, amount in transfers
        ]}
        sys.stdout.write(json.dumps(payload) + "\n")
    elif not transfers:
        sys.stdout.write("nothing to settle\n")
    else:
        out = [f"{display[d]} -> {display[c]} {format_plain(amount)}"
               for d, c, amount in transfers]
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
