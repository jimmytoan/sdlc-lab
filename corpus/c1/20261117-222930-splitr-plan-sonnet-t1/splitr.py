import sys
import csv
import re
import json
import datetime
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
AMOUNT_RE = re.compile(r"^([0-9]+)(?:\.([0-9]{1,2}))?$")
WEIGHT_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def usage_exit():
    sys.exit(64)


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    if not argv or argv[0] not in ("balances", "settle"):
        usage_exit()
    cmd = argv[0]
    fmt = "text"
    since = until = None
    path = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name, val = a, None
            if name not in ("--format", "--since", "--until"):
                usage_exit()
            if val is None:
                i += 1
                if i >= len(argv):
                    usage_exit()
                val = argv[i]
            if name == "--format":
                if val not in ("text", "json"):
                    usage_exit()
                fmt = val
            else:
                d = parse_date(val)
                if d is None:
                    usage_exit()
                if name == "--since":
                    since = d
                else:
                    until = d
        else:
            if path is not None:
                usage_exit()
            path = a
        i += 1
    if path is None:
        usage_exit()
    return cmd, path, fmt, since, until


def parse_amount(s):
    m = AMOUNT_RE.match(s)
    if not m:
        return None
    c = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    return c if c > 0 else None


def parse_participants(s):
    items = s.split(";")
    res = []
    seen = set()
    for it in items:
        it = it.strip()
        if not it:
            return None
        if ":" in it:
            name, w = it.rsplit(":", 1)
            name = name.strip()
            w = w.strip()
            if not WEIGHT_RE.match(w):
                return None
            wt = Fraction(w)
            if wt <= 0:
                return None
        else:
            name, wt = it, Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        res.append((name, wt))
    return res


def split_cents(amount, parts):
    total = sum(w for _, w in parts)
    exact = [Fraction(amount) * w / total for _, w in parts]
    shares = [int(e // 1) for e in exact]
    rem = amount - sum(shares)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in order[:rem]:
        shares[i] += 1
    return shares


def fmt_cents(c, sign):
    s = "-" if c < 0 else ("+" if sign else "")
    c = abs(c)
    return "%s%d.%02d" % (s, c // 100, c % 100)


def main(argv):
    cmd, path, fmt, since, until = parse_args(argv)
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    header = None
    errors = []
    expenses = []
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip() or line.startswith("#"):
            continue
        try:
            row = next(csv.reader([line]))
        except (csv.Error, StopIteration):
            row = []
        if header is None:
            header = [h.strip() for h in row]
            continue

        def get(col):
            if col in header:
                idx = header.index(col)
                if idx < len(row):
                    return row[idx].strip()
            return ""
        d = parse_date(get("date"))
        if d is None:
            errors.append("line %d: bad-date" % n)
            continue
        payer = get("payer")
        if not payer:
            errors.append("line %d: bad-payer" % n)
            continue
        amt = parse_amount(get("amount"))
        if amt is None:
            errors.append("line %d: bad-amount" % n)
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append("line %d: bad-participants" % n)
            continue
        expenses.append((d, payer, amt, parts))

    if header is None:
        header = []
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for c in missing:
            sys.stderr.write("header: missing %s\n" % c)
        return 2
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 2

    bal = {}
    for d, payer, amt, parts in expenses:
        if since and d < since:
            continue
        if until and d > until:
            continue
        bal[payer] = bal.get(payer, 0) + amt
        shares = split_cents(amt, parts)
        for (name, _), s in zip(parts, shares):
            bal[name] = bal.get(name, 0) - s

    names = sorted(bal)
    out = []
    if cmd == "balances":
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": bal[n]} for n in names]}))
        else:
            out.extend("%s %s" % (n, fmt_cents(bal[n], True)) for n in names)
    else:
        transfers = []
        b = dict(bal)
        while True:
            debtors = [n for n in b if b[n] < 0]
            creditors = [n for n in b if b[n] > 0]
            if not debtors or not creditors:
                break
            dn = min(debtors, key=lambda n: (b[n], n))
            cn = sorted(creditors, key=lambda n: (-b[n], n))[0]
            amt = min(-b[dn], b[cn])
            transfers.append((dn, cn, amt))
            b[dn] += amt
            b[cn] -= amt
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            out.extend("%s -> %s %s" % (f, t, fmt_cents(c, False)) for f, t, c in transfers)
    data = "\n".join(out) + "\n" if out else ""
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
