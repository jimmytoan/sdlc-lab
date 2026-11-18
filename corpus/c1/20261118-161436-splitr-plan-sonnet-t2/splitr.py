import csv
import datetime
import json
import re
import sys
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants"]
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")
WEIGHT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


class Usage(Exception):
    pass


def parse_date(s):
    if not DATE_RE.match(s):
        return None
    try:
        return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


def parse_args(argv):
    fmt, since, until = "text", None, None
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            if name not in ("--format", "--since", "--until"):
                raise Usage("unknown option " + a)
            if not eq:
                i += 1
                if i >= len(argv):
                    raise Usage("option needs a value")
                val = argv[i]
            if name == "--format":
                if val not in ("text", "json"):
                    raise Usage("bad format")
                fmt = val
            else:
                d = parse_date(val)
                if d is None:
                    raise Usage("bad date")
                if name == "--since":
                    since = d
                else:
                    until = d
        elif a.startswith("-") and a != "-":
            raise Usage("unknown option " + a)
        else:
            pos.append(a)
        i += 1
    if len(pos) != 2 or pos[0] not in ("balances", "settle"):
        raise Usage("usage: splitr.py <balances|settle> <ledger.csv> [options]")
    return pos[0], pos[1], fmt, since, until


def records(text):
    """Yield (start_line, fields) for non-comment, non-blank records."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("#") or ln.strip() == "":
            i += 1
            continue
        start = i + 1
        buf = ln
        while buf.count('"') % 2 == 1 and i + 1 < n:
            i += 1
            buf += "\n" + lines[i]
        i += 1
        try:
            rows = list(csv.reader([buf]))
        except csv.Error:
            rows = [[buf]]
        yield start, (rows[0] if rows else [])


def split_cents(cents, weights):
    total = sum(weights)
    exact = [Fraction(cents) * w / total for w in weights]
    shares = [int(e) for e in exact]  # floor (non-negative)
    rem = cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda k: (-(exact[k] - shares[k]), k))
    for k in order[:rem]:
        shares[k] += 1
    return shares


def parse_row(fields, header_idx):
    def get(c):
        j = header_idx.get(c)
        if j is None or j >= len(fields):
            return ""
        return fields[j].strip()

    d = parse_date(get("date"))
    if d is None:
        return "bad-date"
    payer = get("payer")
    if not payer:
        return "bad-payer"
    amt = get("amount")
    if not AMOUNT_RE.match(amt):
        return "bad-amount"
    whole, _, frac = amt.partition(".")
    cents = int(whole) * 100 + int((frac + "00")[:2])
    if cents <= 0:
        return "bad-amount"
    plist = get("participants")
    if not plist:
        return "bad-participants"
    names, weights = [], []
    for ent in plist.split(";"):
        ent = ent.strip()
        if ":" in ent:
            nm, _, w = ent.rpartition(":")
            nm, w = nm.strip(), w.strip()
            if not WEIGHT_RE.match(w):
                return "bad-participants"
            wf = Fraction(w)
            if wf <= 0:
                return "bad-participants"
        else:
            nm, wf = ent, Fraction(1)
        if not nm or nm in names:
            return "bad-participants"
        names.append(nm)
        weights.append(wf)
    return d, payer, cents, names, weights


def fmt_cents(c, sign):
    s = ""
    if sign:
        s = "-" if c < 0 else "+"
    c = abs(c)
    return "%s%d.%02d" % (s, c // 100, c % 100)


def run(argv):
    cmd, path, fmt, since, until = parse_args(argv)
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return 3, "", "error: cannot read %s\n" % path

    recs = records(text)
    header = None
    for start, fields in recs:
        header = [h.strip() for h in fields]
        break
    hidx = {}
    if header:
        for j, h in enumerate(header):
            hidx.setdefault(h, j)
    missing = [c for c in REQUIRED if c not in hidx]
    if missing:
        return 2, "", "".join("header: missing %s\n" % c for c in missing)

    errors = []
    expenses = []
    for start, fields in recs:
        r = parse_row(fields, hidx)
        if isinstance(r, str):
            errors.append("line %d: %s\n" % (start, r))
        else:
            expenses.append(r)
    if errors:
        return 2, "", "".join(errors)

    bal = {}
    for d, payer, cents, names, weights in expenses:
        if since and d < since:
            continue
        if until and d > until:
            continue
        bal[payer] = bal.get(payer, 0) + cents
        bal.setdefault(payer, 0)
        for nm, sh in zip(names, split_cents(cents, weights)):
            bal[nm] = bal.get(nm, 0) - sh

    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out = json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}) + "\n"
        else:
            out = "".join("%s %s\n" % (n, fmt_cents(c, True)) for n, c in items)
        return 0, out, ""

    debtors = {n: -c for n, c in bal.items() if c < 0}
    creditors = {n: c for n, c in bal.items() if c > 0}
    transfers = []
    while debtors and creditors:
        dn = min(debtors, key=lambda n: (-debtors[n], n))
        cn = min(creditors, key=lambda n: (-creditors[n], n))
        amt = min(debtors[dn], creditors[cn])
        transfers.append((dn, cn, amt))
        debtors[dn] -= amt
        creditors[cn] -= amt
        if debtors[dn] == 0:
            del debtors[dn]
        if creditors[cn] == 0:
            del creditors[cn]
    if fmt == "json":
        out = json.dumps({"transfers": [{"from": a, "to": b, "cents": c} for a, b, c in transfers]}) + "\n"
    elif not transfers:
        out = "nothing to settle\n"
    else:
        out = "".join("%s -> %s %s\n" % (a, b, fmt_cents(c, False)) for a, b, c in transfers)
    return 0, out, ""


def main():
    try:
        code, out, err = run(sys.argv[1:])
    except Usage as e:
        code, out, err = 64, "", "usage error: %s\n" % e
    except Exception as e:
        code, out, err = 1, "", "error: %s\n" % e
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
