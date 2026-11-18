import csv
import io
import json
import re
import sys
from datetime import date

COLS = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
AMT_RE = re.compile(r"(\d+)(?:\.(\d{1,2}))?")
INT_RE = re.compile(r"\d+")


def usage():
    sys.stderr.write("usage: splitr.py balances|settle <ledger.csv> "
                     "[--format text|json] [--since D] [--until D]\n")
    sys.exit(64)


def parse_date(s):
    if not DATE_RE.fullmatch(s):
        return None
    try:
        return date(int(s[:4]), int(s[5:7]), int(s[8:]))
    except ValueError:
        return None


def parse_args(argv):
    pos = []
    opts = {"format": "text", "since": None, "until": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a[2:].split("=", 1)
            else:
                k = a[2:]
                if k not in opts:
                    usage()
                i += 1
                if i >= len(argv):
                    usage()
                v = argv[i]
            if k not in opts:
                usage()
            opts[k] = v
        else:
            pos.append(a)
        i += 1
    if len(pos) != 2 or pos[0] not in ("balances", "settle"):
        usage()
    if opts["format"] not in ("text", "json"):
        usage()
    for k in ("since", "until"):
        if opts[k] is not None:
            d = parse_date(opts[k].strip())
            if d is None:
                usage()
            opts[k] = d
    return pos[0], pos[1], opts


def split_cents(total, weights):
    W = sum(weights)
    shares = [total * w // W for w in weights]
    rems = [total * w % W for w in weights]
    left = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-rems[i], i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def parse_participants(s):
    items = s.split(";")
    res = {}
    for it in items:
        it = it.strip()
        if not it:
            return None
        if ":" in it:
            name, w = it.rsplit(":", 1)
            name = name.strip()
            w = w.strip()
            if not name or not INT_RE.fullmatch(w) or int(w) <= 0:
                return None
            w = int(w)
        else:
            name, w = it, 1
        res[name] = res.get(name, 0) + w
    return res


def fmt(c):
    sign = "-" if c < 0 else "+"
    c = abs(c)
    return "%s%d.%02d" % (sign, c // 100, c % 100)


def main():
    cmd, path, opts = parse_args(sys.argv[1:])
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)

    lines = list(io.StringIO(text, newline=""))
    reader = csv.reader(iter(lines))
    header = None
    errors = []
    expenses = []
    prev = 0
    try:
        for row in reader:
            start = prev + 1
            prev = reader.line_num
            if start - 1 < len(lines) and lines[start - 1].startswith("#"):
                continue
            if not row:
                continue
            if header is None:
                header = [h.strip() for h in row]
                missing = [c for c in COLS if c not in header]
                if missing:
                    for c in missing:
                        errors.append("header: missing %s" % c)
                    break
                continue

            def get(name):
                idx = header.index(name)
                return row[idx] if idx < len(row) else None

            d = get("date")
            dd = parse_date(d.strip()) if d is not None else None
            if dd is None:
                errors.append("line %d: bad-date" % start)
                continue
            payer = (get("payer") or "").strip()
            if not payer:
                errors.append("line %d: bad-payer" % start)
                continue
            m = AMT_RE.fullmatch((get("amount") or "").strip())
            cents = None
            if m:
                cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
            if not cents:
                errors.append("line %d: bad-amount" % start)
                continue
            parts = parse_participants(get("participants") or "")
            if not parts:
                errors.append("line %d: bad-participants" % start)
                continue
            expenses.append((dd, payer, cents, parts))
    except csv.Error:
        errors.append("line %d: bad-participants" % (prev + 1))
    if header is None:
        errors = ["header: missing %s" % c for c in COLS]
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        sys.exit(2)

    bal = {}
    for dd, payer, cents, parts in expenses:
        if opts["since"] and dd < opts["since"]:
            continue
        if opts["until"] and dd > opts["until"]:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        names = list(parts)
        for n, s in zip(names, split_cents(cents, [parts[n] for n in names])):
            bal[n] = bal.get(n, 0) - s
    as_json = opts["format"] == "json"

    if cmd == "balances":
        items = sorted(bal.items())
        if as_json:
            print(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            for n, c in items:
                print("%s %s" % (n, fmt(c)))
        return

    debt = {n: -c for n, c in bal.items() if c < 0}
    cred = {n: c for n, c in bal.items() if c > 0}
    transfers = []
    while debt and cred:
        dn = min(debt, key=lambda n: (-debt[n], n))
        cn = min(cred, key=lambda n: (-cred[n], n))
        amt = min(debt[dn], cred[cn])
        transfers.append((dn, cn, amt))
        debt[dn] -= amt
        cred[cn] -= amt
        if debt[dn] == 0:
            del debt[dn]
        if cred[cn] == 0:
            del cred[cn]
    if as_json:
        print(json.dumps({"transfers": [{"from": a, "to": b, "cents": c}
                                        for a, b, c in transfers]}))
    elif not transfers:
        print("nothing to settle")
    else:
        for a, b, c in transfers:
            print("%s -> %s %d.%02d" % (a, b, c // 100, c % 100))


if __name__ == "__main__":
    main()
