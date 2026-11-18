import csv
import datetime
import io
import json
import re
import sys
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")
WEIGHT_RE = re.compile(r"^\d+(\.\d+)?$")


def usage():
    sys.stderr.write("usage: splitr.py <balances|settle> <ledger.csv> "
                     "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
    sys.exit(64)


def parse_date(s):
    s = s.strip()
    if not (DATE_RE.match(s) and s.isascii()):
        return None
    try:
        return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    if not argv or argv[0] not in ("balances", "settle"):
        usage()
    cmd = argv[0]
    fmt, since, until, path = "text", None, None, None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                name, val = a.split("=", 1)
            else:
                name = a
                i += 1
                if i >= len(argv):
                    usage()
                val = argv[i]
            if name == "--format":
                if val not in ("text", "json"):
                    usage()
                fmt = val
            elif name in ("--since", "--until"):
                d = parse_date(val)
                if d is None:
                    usage()
                if name == "--since":
                    since = d
                else:
                    until = d
            else:
                usage()
        elif path is None:
            path = a
        else:
            usage()
        i += 1
    if path is None:
        usage()
    return cmd, path, fmt, since, until


def parse_amount(s):
    s = s.strip()
    if not (AMOUNT_RE.match(s) and s.isascii()):
        return None
    if "." in s:
        w, f = s.split(".")
        c = int(w) * 100 + int(f.ljust(2, "0"))
    else:
        c = int(s) * 100
    return c if c > 0 else None


def parse_participants(s):
    if not s.strip():
        return None
    out = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        w = Fraction(1)
        if ":" in part:
            name, ws = part.rsplit(":", 1)
            ws = ws.strip()
            if not (WEIGHT_RE.match(ws) and ws.isascii()):
                return None
            w = Fraction(ws)
            if w <= 0:
                return None
            name = name.strip()
        else:
            name = part
        if not name or name in seen:
            return None
        seen.add(name)
        out.append((name, w))
    return out


def split(cents, parts):
    total = sum(w for _, w in parts)
    shares = []
    rems = []
    for idx, (_, w) in enumerate(parts):
        exact = Fraction(cents) * w / total
        fl = exact.numerator // exact.denominator
        shares.append(fl)
        rems.append((exact - fl, idx))
    left = cents - sum(shares)
    order = sorted(rems, key=lambda r: (-r[0], r[1]))
    for k in range(left):
        shares[order[k][1]] += 1
    assert sum(shares) == cents
    return shares


def fmt_amount(c):
    sign = "-" if c < 0 else "+"
    c = abs(c)
    return "%s%d.%02d" % (sign, c // 100, c % 100)


def main(argv):
    cmd, path, fmt, since, until = parse_args(argv)
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError, ValueError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    phys = []

    def gen():
        for n, line in enumerate(io.StringIO(text, newline="\n"), 1):
            if line.startswith("#"):
                continue
            phys.append(n)
            yield line

    reader = csv.reader(gen())
    header = None
    rows = []  # (lineno, fields)
    consumed = 0
    try:
        for rec in reader:
            first = phys[consumed]
            consumed = reader.line_num
            if not rec:
                continue
            if header is None:
                header = [h.strip() for h in rec]
            else:
                rows.append((first, rec))
    except csv.Error:
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    if header is None:
        header = []
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        for c in missing:
            sys.stderr.write("header: missing %s\n" % c)
        return 2
    idx = {c: header.index(c) for c in REQUIRED}

    errors = []
    expenses = []
    for ln, rec in rows:
        def get(c):
            i = idx[c]
            return rec[i] if i < len(rec) else ""
        if all(not x.strip() for x in rec):
            continue
        d = parse_date(get("date"))
        if d is None:
            errors.append("line %d: bad-date" % ln)
            continue
        payer = get("payer").strip()
        if not payer:
            errors.append("line %d: bad-payer" % ln)
            continue
        amt = parse_amount(get("amount"))
        if amt is None:
            errors.append("line %d: bad-amount" % ln)
            continue
        parts = parse_participants(get("participants"))
        if parts is None:
            errors.append("line %d: bad-participants" % ln)
            continue
        expenses.append((d, payer, amt, parts))
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
        for (name, _), s in zip(parts, split(amt, parts)):
            bal[name] = bal.get(name, 0) - s

    out = []
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            out.extend("%s %s" % (n, fmt_amount(c)) for n, c in items)
    else:
        debt = {n: -c for n, c in bal.items() if c < 0}
        cred = {n: c for n, c in bal.items() if c > 0}
        transfers = []
        while debt and cred:
            dn = min(debt, key=lambda n: (-debt[n], n))
            cn = min(cred, key=lambda n: (-cred[n], n))
            t = min(debt[dn], cred[cn])
            transfers.append((dn, cn, t))
            debt[dn] -= t
            cred[cn] -= t
            if debt[dn] == 0:
                del debt[dn]
            if cred[cn] == 0:
                del cred[cn]
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": a, "to": b, "cents": c}
                                                 for a, b, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            out.extend("%s -> %s %d.%02d" % (a, b, c // 100, c % 100) for a, b, c in transfers)
    data = "".join(l + "\n" for l in out)
    sys.stdout.buffer.write(data.encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
