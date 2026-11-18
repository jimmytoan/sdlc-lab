import sys
import csv
import json
import re
import datetime
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants"]


class Usage(Exception):
    pass


def parse_args(argv):
    if len(argv) < 2 or argv[0] not in ("balances", "settle"):
        raise Usage()
    cmd, path = argv[0], argv[1]
    opts = {"format": "text", "since": None, "until": None}
    i = 2
    while i < len(argv):
        a = argv[i]
        if "=" in a and a.startswith("--"):
            k, v = a.split("=", 1)
            rest = 1
        else:
            k, v, rest = a, (argv[i + 1] if i + 1 < len(argv) else None), 2
        if k not in ("--format", "--since", "--until") or v is None:
            raise Usage()
        if k == "--format":
            if v not in ("text", "json"):
                raise Usage()
            opts["format"] = v
        else:
            try:
                opts[k[2:]] = parse_date(v)
            except ValueError:
                raise Usage()
        i += rest
    return cmd, path, opts


def parse_date(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(s)
    return datetime.date.fromisoformat(s)


def parse_amount(s):
    m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", s)
    if not m:
        return None
    return int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))


def parse_participants(s):
    out = []
    seen = set()
    for part in s.split(";"):
        part = part.strip()
        if not part:
            return None
        if ":" in part:
            name, w = part.rsplit(":", 1)
            name = name.strip()
            w = w.strip()
            if not re.fullmatch(r"\d+(\.\d+)?", w):
                return None
            w = Fraction(w)
            if w <= 0:
                return None
        else:
            name, w = part, Fraction(1)
        if not name or name in seen:
            return None
        seen.add(name)
        out.append((name, w))
    return out or None


def split(total, parts):
    tw = sum(w for _, w in parts)
    exact = [total * w / tw for _, w in parts]
    shares = [int(e) for e in exact]  # floor (non-negative)
    left = total - sum(shares)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def main(argv):
    try:
        cmd, path, opts = parse_args(argv)
    except Usage:
        return 64
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        return 3

    reader = csv.reader(text.splitlines(keepends=True))
    header = None
    errors = []
    expenses = []
    prev = 0
    for row in reader:
        lineno = prev + 1
        prev = reader.line_num
        if not row or all(not c.strip() for c in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            missing = [c for c in REQUIRED if c not in header]
            if missing:
                for c in missing:
                    sys.stderr.write("header: missing %s\n" % c)
                return 2
            continue
        rec = {h: (row[i].strip() if i < len(row) else "") for i, h in enumerate(header)}
        try:
            d = parse_date(rec["date"])
        except ValueError:
            errors.append((lineno, "bad-date"))
            continue
        payer = rec["payer"]
        if not payer:
            errors.append((lineno, "bad-payer"))
            continue
        amt = parse_amount(rec["amount"])
        if amt is None:
            errors.append((lineno, "bad-amount"))
            continue
        parts = parse_participants(rec["participants"])
        if parts is None:
            errors.append((lineno, "bad-participants"))
            continue
        expenses.append((d, payer, amt, parts))
    if header is None:
        for c in REQUIRED:
            sys.stderr.write("header: missing %s\n" % c)
        return 2
    if errors:
        for n, r in errors:
            sys.stderr.write("line %d: %s\n" % (n, r))
        return 2

    bal = {}
    for d, payer, amt, parts in expenses:
        if opts["since"] and d < opts["since"]:
            continue
        if opts["until"] and d > opts["until"]:
            continue
        bal[payer] = bal.get(payer, 0) + amt
        for (n, _), s in zip(parts, split(amt, parts)):
            bal[n] = bal.get(n, 0) - s

    out = []
    if cmd == "balances":
        items = sorted(bal.items())
        if opts["format"] == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                sign = "-" if c < 0 else "+"
                out.append("%s %s%d.%02d" % (n, sign, abs(c) // 100, abs(c) % 100))
    else:
        debt = {n: -c for n, c in bal.items() if c < 0}
        cred = {n: c for n, c in bal.items() if c > 0}
        transfers = []
        while debt and cred:
            dn = min(debt, key=lambda n: (-debt[n], n))
            cn = min(cred, key=lambda n: (-cred[n], n))
            a = min(debt[dn], cred[cn])
            transfers.append((dn, cn, a))
            debt[dn] -= a
            cred[cn] -= a
            if debt[dn] == 0:
                del debt[dn]
            if cred[cn] == 0:
                del cred[cn]
        if opts["format"] == "json":
            out.append(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            for a, b, c in transfers:
                out.append("%s -> %s %d.%02d" % (a, b, c // 100, c % 100))
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
