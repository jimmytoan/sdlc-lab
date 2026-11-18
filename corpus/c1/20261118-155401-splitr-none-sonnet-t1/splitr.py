import csv
import datetime
import io
import json
import re
import sys
from fractions import Fraction

REQUIRED = ["date", "payer", "amount", "participants"]


def usage():
    sys.stderr.write("usage: splitr.py {balances,settle} <ledger.csv> "
                     "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
    sys.exit(64)


def pdate(s):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def parse_args(argv):
    if not argv or argv[0] not in ("balances", "settle"):
        usage()
    cmd = argv[0]
    rest = argv[1:]
    fmt, since, until, pos = "text", None, None, []
    i = 0
    while i < len(rest):
        a = rest[i]
        name, val = a, None
        if a.startswith("--") and "=" in a:
            name, val = a.split("=", 1)
        if name in ("--format", "--since", "--until"):
            if val is None:
                i += 1
                if i >= len(rest):
                    usage()
                val = rest[i]
            if name == "--format":
                if val not in ("text", "json"):
                    usage()
                fmt = val
            else:
                d = pdate(val)
                if d is None:
                    usage()
                if name == "--since":
                    since = d
                else:
                    until = d
        elif a.startswith("-") and a != "-":
            usage()
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1:
        usage()
    return cmd, pos[0], fmt, since, until


def parse_amount(s):
    s = (s or "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return None
    whole, _, frac = s.partition(".")
    c = int(whole) * 100 + int((frac + "00")[:2])
    return c if c > 0 else None


def parse_participants(s):
    out = []
    seen = set()
    if not (s or "").strip():
        return None
    for part in s.split(";"):
        part = part.strip()
        if not part:
            return None
        if ":" in part:
            name, _, w = part.rpartition(":")
            name, w = name.strip(), w.strip()
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
    return out


def split(total, parts):
    tw = sum(w for _, w in parts)
    exact = [total * w / tw for _, w in parts]
    shares = [int(e) for e in exact]  # floor (positive)
    left = total - sum(shares)
    order = sorted(range(len(parts)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in order[:left]:
        shares[i] += 1
    return shares


def fmt_amt(c):
    sign = "-" if c < 0 else "+"
    c = abs(c)
    return "%s%d.%02d" % (sign, c // 100, c % 100)


def main():
    cmd, path, fmt, since, until = parse_args(sys.argv[1:])
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write("error: cannot read %s\n" % path)
        sys.exit(3)

    reader = csv.reader(io.StringIO(text, newline=""))
    header = None
    rows = []
    errors = []
    try:
        for rec in reader:
            ln = reader.line_num
            if not rec or all(not x.strip() for x in rec):
                continue
            if rec[0].lstrip().startswith("#"):
                continue
            if header is None:
                header = [x.strip().lower() for x in rec]
                missing = [c for c in REQUIRED if c not in header]
                if missing:
                    for c in missing:
                        sys.stderr.write("header: missing %s\n" % c)
                    sys.exit(2)
                idx = {c: header.index(c) for c in REQUIRED}
                continue

            def get(c):
                i = idx[c]
                return rec[i] if i < len(rec) else ""
            date = pdate(get("date").strip())
            if date is None:
                errors.append((ln, "bad-date"))
                continue
            payer = get("payer").strip()
            if not payer:
                errors.append((ln, "bad-payer"))
                continue
            amt = parse_amount(get("amount"))
            if amt is None:
                errors.append((ln, "bad-amount"))
                continue
            parts = parse_participants(get("participants"))
            if parts is None:
                errors.append((ln, "bad-participants"))
                continue
            rows.append((date, payer, amt, parts))
    except csv.Error:
        errors.append((reader.line_num, "bad-participants"))
    if header is None:
        sys.stderr.write("header: missing %s\n" % REQUIRED[0])
        sys.exit(2)
    if errors:
        for ln, r in errors:
            sys.stderr.write("line %d: %s\n" % (ln, r))
        sys.exit(2)

    bal = {}
    for date, payer, amt, parts in rows:
        if since and date < since:
            continue
        if until and date > until:
            continue
        bal[payer] = bal.get(payer, 0) + amt
        for (n, _), s in zip(parts, split(amt, parts)):
            bal[n] = bal.get(n, 0) - s

    out = []
    if cmd == "balances":
        items = sorted(bal.items())
        if fmt == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in items]}))
        else:
            out.extend("%s %s" % (n, fmt_amt(c)) for n, c in items)
    else:
        cred = sorted([[n, c] for n, c in bal.items() if c > 0], key=lambda x: (-x[1], x[0]))
        debt = sorted([[n, -c] for n, c in bal.items() if c < 0], key=lambda x: (-x[1], x[0]))
        transfers = []
        while cred and debt:
            cred.sort(key=lambda x: (-x[1], x[0]))
            debt.sort(key=lambda x: (-x[1], x[0]))
            a = min(cred[0][1], debt[0][1])
            transfers.append((debt[0][0], cred[0][0], a))
            cred[0][1] -= a
            debt[0][1] -= a
            if cred[0][1] == 0:
                cred.pop(0)
            if debt[0][1] == 0:
                debt.pop(0)
        if fmt == "json":
            out.append(json.dumps({"transfers": [{"from": f, "to": t, "cents": c}
                                                 for f, t, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            out.extend("%s -> %s %d.%02d" % (f, t, c // 100, c % 100) for f, t, c in transfers)
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
