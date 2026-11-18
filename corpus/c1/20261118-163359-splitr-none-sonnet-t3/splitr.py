import sys
import csv
import re
import json
import datetime
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants", "note"]


def usage():
    sys.exit(64)


def parse_date(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_amount(s):
    m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", s)
    if not m:
        return None
    c = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    return c if c > 0 else None


def parse_parts(s):
    out = []
    seen = set()
    if not s.strip():
        return None
    for item in s.split(";"):
        item = item.strip()
        if not item:
            return None
        if ":" in item:
            name, w = item.rsplit(":", 1)
            name = name.strip()
            w = w.strip()
            if not re.fullmatch(r"\d+(\.\d+)?", w):
                return None
            wt = Fraction(w)
            if wt <= 0:
                return None
        else:
            name, wt = item, Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        out.append((name, wt))
    return out


def split(total, parts):
    tw = sum(w for _, w in parts)
    exact = [total * w / tw for _, w in parts]
    shares = [int(e) for e in exact]  # floor (positive)
    rem = total - sum(shares)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in order[:rem]:
        shares[i] += 1
    return shares


def lines_gen(text):
    for ln in text.splitlines(keepends=True):
        yield "\n" if ln.startswith("#") else ln


def main(argv):
    if len(argv) < 2 or argv[0] not in ("balances", "settle"):
        usage()
    cmd, path = argv[0], argv[1]
    fmt, since, until = "text", None, None
    rest = argv[2:]
    i = 0
    while i < len(rest):
        a = rest[i]
        val = None
        if a.startswith("--") and "=" in a:
            a, val = a.split("=", 1)
        if a not in ("--format", "--since", "--until"):
            usage()
        if val is None:
            i += 1
            if i >= len(rest):
                usage()
            val = rest[i]
        i += 1
        if a == "--format":
            if val not in ("text", "json"):
                usage()
            fmt = val
        else:
            d = parse_date(val)
            if d is None:
                usage()
            if a == "--since":
                since = d
            else:
                until = d

    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        print("error: cannot read " + path, file=sys.stderr)
        return 3

    reader = csv.reader(lines_gen(text))
    header = None
    errors = []
    expenses = []
    prev = 0
    try:
        for row in reader:
            start = prev + 1
            prev = reader.line_num
            if not row or all(not c.strip() for c in row):
                continue
            if header is None:
                header = [c.strip().lower() for c in row]
                missing = [c for c in REQUIRED if c not in header]
                if missing:
                    for c in missing:
                        print("header: missing " + c, file=sys.stderr)
                    return 2
                continue
            rec = {h: (row[k].strip() if k < len(row) else "") for k, h in enumerate(header)}
            d = parse_date(rec["date"])
            if d is None:
                errors.append((start, "bad-date"))
                continue
            payer = rec["payer"]
            if not payer:
                errors.append((start, "bad-payer"))
                continue
            amt = parse_amount(rec["amount"])
            if amt is None:
                errors.append((start, "bad-amount"))
                continue
            parts = parse_parts(rec["participants"])
            if parts is None:
                errors.append((start, "bad-participants"))
                continue
            expenses.append((d, payer, amt, parts))
    except csv.Error:
        print("error: cannot read " + path, file=sys.stderr)
        return 3

    if header is None:
        for c in REQUIRED:
            print("header: missing " + c, file=sys.stderr)
        return 2
    if errors:
        for n, r in errors:
            print("line %d: %s" % (n, r), file=sys.stderr)
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

    def money(c):
        return "%s%d.%02d" % ("-" if c < 0 else "+", abs(c) // 100, abs(c) % 100)

    out = []
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            out = ["%s %s" % (n, money(c)) for n, c in items]
    else:
        cred = sorted([[c, n] for n, c in bal.items() if c > 0], key=lambda x: (-x[0], x[1]))
        debt = sorted([[-c, n] for n, c in bal.items() if c < 0], key=lambda x: (-x[0], x[1]))
        transfers = []
        while cred and debt:
            cred.sort(key=lambda x: (-x[0], x[1]))
            debt.sort(key=lambda x: (-x[0], x[1]))
            a = min(cred[0][0], debt[0][0])
            transfers.append((debt[0][1], cred[0][1], a))
            cred[0][0] -= a
            debt[0][0] -= a
            if cred[0][0] == 0:
                cred.pop(0)
            if debt[0][0] == 0:
                debt.pop(0)
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
        elif transfers:
            out = ["%s -> %s %d.%02d" % (f, t, c // 100, c % 100) for f, t, c in transfers]
        else:
            out = ["nothing to settle"]
    if out:
        print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
