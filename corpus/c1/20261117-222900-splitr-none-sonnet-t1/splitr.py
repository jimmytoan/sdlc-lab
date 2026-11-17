import sys, csv, re, json, datetime
from fractions import Fraction

REQ = ["date", "payer", "amount", "participants"]


def usage():
    sys.exit(64)


def pdate(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def split(cents, parts):
    total = sum(w for _, w in parts)
    shares = []
    for n, w in parts:
        q, r = divmod(cents * w, total)
        shares.append([n, int(q), r])
    left = cents - sum(s[1] for s in shares)
    order = sorted(range(len(shares)), key=lambda i: (-shares[i][2], i))
    for i in order[:left]:
        shares[i][1] += 1
    return [(s[0], s[1]) for s in shares]


def parse(argv):
    if len(argv) < 2 or argv[0] not in ("balances", "settle"):
        usage()
    cmd, path = argv[0], argv[1]
    fmt, since, until = "text", None, None
    rest = argv[2:]
    i = 0
    while i < len(rest):
        a = rest[i]
        v = None
        if "=" in a and a.startswith("--"):
            a, v = a.split("=", 1)
        elif a in ("--format", "--since", "--until"):
            if i + 1 >= len(rest):
                usage()
            i += 1
            v = rest[i]
        else:
            usage()
        if a == "--format":
            if v not in ("text", "json"):
                usage()
            fmt = v
        elif a in ("--since", "--until"):
            d = pdate(v)
            if d is None:
                usage()
            if a == "--since":
                since = d
            else:
                until = d
        else:
            usage()
        i += 1
    return cmd, path, fmt, since, until


def main():
    cmd, path, fmt, since, until = parse(sys.argv[1:])
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)
    rd = csv.reader(text.splitlines(True))
    header = None
    idx = {}
    errors = []
    expenses = []
    for row in rd:
        ln = rd.line_num
        if not row or all(not c.strip() for c in row):
            continue
        if row[0].lstrip().startswith("#"):
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            miss = [c for c in REQ if c not in header]
            if miss:
                for c in miss:
                    sys.stderr.write("header: missing %s\n" % c)
                sys.exit(2)
            idx = {c: header.index(c) for c in REQ}
            continue
        def g(c):
            k = idx[c]
            return row[k].strip() if k < len(row) else ""
        d = pdate(g("date"))
        if d is None:
            errors.append((ln, "bad-date")); continue
        payer = g("payer")
        if not payer:
            errors.append((ln, "bad-payer")); continue
        m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", g("amount"))
        cents = None
        if m:
            cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
        if not cents:
            errors.append((ln, "bad-amount")); continue
        parts = []
        ok = True
        for p in g("participants").split(";"):
            p = p.strip()
            if not p:
                ok = False; break
            if ":" in p:
                n, w = p.rsplit(":", 1)
                n = n.strip(); w = w.strip()
                if not re.fullmatch(r"\d+(\.\d+)?", w):
                    ok = False; break
                w = Fraction(w)
            else:
                n, w = p, Fraction(1)
            if not n or w <= 0:
                ok = False; break
            parts.append((n, w))
        if ok and len({n for n, _ in parts}) != len(parts):
            ok = False
        if not ok:
            errors.append((ln, "bad-participants")); continue
        expenses.append((d, payer, cents, parts))
    if header is None:
        sys.stderr.write("header: missing %s\n" % REQ[0])
        for c in REQ[1:]:
            sys.stderr.write("header: missing %s\n" % c)
        sys.exit(2)
    if errors:
        for ln, r in errors:
            sys.stderr.write("line %d: %s\n" % (ln, r))
        sys.exit(2)
    bal = {}
    for d, payer, cents, parts in expenses:
        if since and d < since:
            continue
        if until and d > until:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        for n, s in split(cents, parts):
            bal[n] = bal.get(n, 0) - s
    out = []
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                out.append("%s %s%d.%02d" % (n, "-" if c < 0 else "+", abs(c) // 100, abs(c) % 100))
    else:
        cr = sorted([[n, c] for n, c in bal.items() if c > 0], key=lambda x: (-x[1], x[0]))
        db = sorted([[n, -c] for n, c in bal.items() if c < 0], key=lambda x: (-x[1], x[0]))
        tr = []
        while cr and db:
            cr.sort(key=lambda x: (-x[1], x[0]))
            db.sort(key=lambda x: (-x[1], x[0]))
            a = min(cr[0][1], db[0][1])
            tr.append((db[0][0], cr[0][0], a))
            cr[0][1] -= a; db[0][1] -= a
            if cr[0][1] == 0: cr.pop(0)
            if db[0][1] == 0: db.pop(0)
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in tr]}))
        elif not tr:
            out.append("nothing to settle")
        else:
            for a, b, c in tr:
                out.append("%s -> %s %d.%02d" % (a, b, c // 100, c % 100))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


main()
