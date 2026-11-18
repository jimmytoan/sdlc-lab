#!/usr/bin/env python3
"""splitr: split shared expenses from a CSV ledger."""
import csv
import datetime
import json
import re
import sys

USAGE = ("usage: splitr.py {balances|settle} <ledger.csv> "
         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
REQUIRED = ("date", "payer", "amount", "participants")


class UsageError(Exception):
    pass


class LedgerReadError(Exception):
    pass


def parse_date(s):
    s = s.strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
        return None
    try:
        return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_amount(s):
    s = s.strip()
    m = re.fullmatch(r"([0-9]+)(?:\.([0-9]{1,2}))?", s)
    if not m:
        return None
    cents = int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0") or 0)
    return cents if cents > 0 else None


def parse_participants(s):
    if not s.strip():
        return None
    out = {}
    for part in s.split(";"):
        bits = part.split(":")
        if len(bits) > 2:
            return None
        name = bits[0].strip()
        if not name:
            return None
        w = 1
        if len(bits) == 2:
            ws = bits[1].strip()
            if not re.fullmatch(r"[0-9]+", ws) or int(ws) <= 0:
                return None
            w = int(ws)
        out[name] = out.get(name, 0) + w
    return out


def parse_args(argv):
    pos = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a.startswith("--"):
            if "=" in a:
                k, v = a[2:].split("=", 1)
            else:
                k = a[2:]
                if k not in opts:
                    raise UsageError("unknown option " + a)
                if i >= len(argv):
                    raise UsageError("missing value for " + a)
                v = argv[i]
                i += 1
            if k not in opts:
                raise UsageError("unknown option " + a)
            opts[k] = v
        elif a.startswith("-") and len(a) > 1:
            raise UsageError("unknown option " + a)
        else:
            pos.append(a)
    if not pos or pos[0] not in ("balances", "settle"):
        raise UsageError("bad command")
    if len(pos) < 2:
        raise UsageError("missing ledger path")
    if len(pos) > 2:
        raise UsageError("too many arguments")
    if opts["format"] not in ("text", "json"):
        raise UsageError("bad format")
    for k in ("since", "until"):
        if opts[k] is not None:
            d = parse_date(opts[k])
            if d is None:
                raise UsageError("bad date for --" + k)
            opts[k] = d
    return pos[0], pos[1], opts


def read_text(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise LedgerReadError(path)


def records(text):
    """Yield (start_line_number, fields) for non-comment, non-blank records."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [l[:-1] if l.endswith("\r") else l for l in lines]
    i = 0
    n = len(lines)
    while i < n:
        start = i + 1
        line = lines[i]
        i += 1
        if line.startswith("#") or not line.strip():
            continue
        buf = line
        while buf.count('"') % 2 == 1 and i < n:
            buf += "\n" + lines[i]
            i += 1
        try:
            rows = list(csv.reader([buf]))
            fields = rows[0] if rows else []
        except csv.Error:
            fields = [buf]
        yield start, fields


def parse_ledger(text):
    recs = records(text)
    header = next(recs, None)
    names = [h.strip().lower() for h in header[1]] if header else []
    idx = {}
    for j, h in enumerate(names):
        idx.setdefault(h, j)
    missing = [c for c in REQUIRED if c not in idx]
    if missing:
        return [], ["header: missing " + c for c in missing]
    expenses, errors = [], []
    for ln, f in recs:
        def get(c):
            j = idx[c]
            return f[j] if j < len(f) else ""
        d = parse_date(get("date"))
        payer = get("payer").strip()
        amt = parse_amount(get("amount"))
        parts = parse_participants(get("participants"))
        reason = None
        if d is None:
            reason = "bad-date"
        elif not payer or ":" in payer or ";" in payer:
            reason = "bad-payer"
        elif amt is None:
            reason = "bad-amount"
        elif parts is None:
            reason = "bad-participants"
        if reason:
            errors.append("line %d: %s" % (ln, reason))
        else:
            expenses.append((d, payer, amt, parts))
    return expenses, errors


def split_amount(cents, weights):
    total = sum(weights.values())
    shares, rems = {}, []
    for name, w in weights.items():
        q, r = divmod(cents * w, total)
        shares[name] = q
        rems.append((-r, name))
    left = cents - sum(shares.values())
    rems.sort()
    for _, name in rems[:left]:
        shares[name] += 1
    return shares


def filter_by_date(expenses, since, until):
    return [e for e in expenses
            if (since is None or e[0] >= since) and (until is None or e[0] <= until)]


def compute_balances(expenses):
    bal = {}
    for _, payer, amt, parts in expenses:
        bal[payer] = bal.get(payer, 0) + amt
        for name, s in split_amount(amt, parts).items():
            bal[name] = bal.get(name, 0) - s
    return bal


def settle(balances):
    bal = {k: v for k, v in balances.items() if v != 0}
    out = []
    while bal:
        debtor = min(bal, key=lambda k: (bal[k], k))
        creditor = min(bal, key=lambda k: (-bal[k], k))
        if bal[debtor] >= 0 or bal[creditor] <= 0:
            break
        amt = min(-bal[debtor], bal[creditor])
        out.append((debtor, creditor, amt))
        bal[debtor] += amt
        bal[creditor] -= amt
        for k in (debtor, creditor):
            if bal[k] == 0:
                del bal[k]
    return out


def fmt_plain(c):
    q, r = divmod(abs(c), 100)
    return "%d.%02d" % (q, r)


def fmt_signed(c):
    return ("-" if c < 0 else "+") + fmt_plain(c)


def render_balances(bal, fmt):
    names = sorted(bal)
    if fmt == "json":
        return json.dumps({"balances": [{"name": n, "cents": bal[n]} for n in names]}) + "\n"
    return "".join("%s %s\n" % (n, fmt_signed(bal[n])) for n in names)


def render_settle(transfers, fmt):
    if fmt == "json":
        return json.dumps({"transfers": [{"from": a, "to": b, "cents": c}
                                         for a, b, c in transfers]}) + "\n"
    if not transfers:
        return "nothing to settle\n"
    return "".join("%s -> %s %s\n" % (a, b, fmt_plain(c)) for a, b, c in transfers)


def main(argv):
    try:
        cmd, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write("error: %s\n%s" % (e, USAGE))
        return 64
    try:
        text = read_text(path)
    except LedgerReadError:
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3
    expenses, errors = parse_ledger(text)
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2
    expenses = filter_by_date(expenses, opts["since"], opts["until"])
    bal = compute_balances(expenses)
    if cmd == "balances":
        out = render_balances(bal, opts["format"])
    else:
        out = render_settle(settle(bal), opts["format"])
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
