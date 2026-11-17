#!/usr/bin/env python3
"""splitr: shared-expense settlement CLI."""
import csv
import datetime
import json
import re
import sys

REQUIRED = ["date", "payer", "amount", "participants", "note"]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMOUNT_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")
WEIGHT_RE = re.compile(r"^\d+$")


class UsageError(Exception):
    pass


def parse_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def usage(msg):
    raise UsageError(msg)


def parse_args(argv):
    if not argv:
        usage("missing command")
    command = argv[0]
    if command not in ("balances", "settle"):
        usage(f"unknown command: {command}")
    opts = {"format": "text", "since": None, "until": None}
    positional = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and len(a) > 2:
            if "=" in a:
                name, value = a[2:].split("=", 1)
            else:
                name = a[2:]
                if i + 1 >= len(argv):
                    usage(f"option --{name} needs a value")
                value = argv[i + 1]
                i += 1
            if name == "format":
                if value not in ("text", "json"):
                    usage(f"bad format: {value}")
                opts["format"] = value
            elif name in ("since", "until"):
                d = parse_date(value)
                if d is None:
                    usage(f"bad date for --{name}: {value}")
                opts[name] = d
            else:
                usage(f"unknown option: --{name}")
        elif a == "--":
            positional.extend(argv[i + 1:])
            break
        elif a.startswith("-") and a != "-":
            usage(f"unknown option: {a}")
        else:
            positional.append(a)
        i += 1
    if len(positional) != 1:
        usage("expected exactly one ledger file")
    return command, positional[0], opts


def norm_name(s):
    return s.strip()


def key_of(name):
    return name.casefold()


def parse_amount(s):
    s = (s or "").strip()
    m = AMOUNT_RE.match(s)
    if not m:
        return None
    frac = (m.group(2) or "").ljust(2, "0")
    cents = int(m.group(1)) * 100 + int(frac or "0")
    if cents <= 0:
        return None
    return cents


def parse_participants(s):
    s = s or ""
    if not s.strip():
        return None
    result = []
    seen = set()
    for part in s.split(";"):
        if ":" in part:
            name, weight = part.split(":", 1)
            weight = weight.strip()
            if not WEIGHT_RE.match(weight):
                return None
            w = int(weight)
            if w <= 0:
                return None
        else:
            name, w = part, 1
        name = norm_name(name)
        if not name:
            return None
        k = key_of(name)
        if k in seen:
            return None
        seen.add(k)
        result.append((name, w))
    return result


def load(path, opts):
    """Returns list of (date, payer, cents, participants) or raises SystemExit."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        sys.stderr.write(f"error: cannot read {path}\n")
        sys.exit(3)

    physical = text.split("\n")
    if physical and physical[-1] == "":
        physical.pop()
    header = None
    errors = []
    rows = []
    for idx, raw in enumerate(physical, start=1):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = next(csv.reader([line]), [])
        if header is None:
            header = [h.strip().lstrip("\ufeff").lower() for h in fields]
            for col in REQUIRED:
                if col not in header:
                    sys.stderr.write(f"header: missing {col}\n")
                    sys.exit(2)
            pos = {col: header.index(col) for col in REQUIRED}
            continue
        rec = {col: (fields[p] if p < len(fields) else "") for col, p in pos.items()}
        date = parse_date(rec["date"].strip())
        if date is None:
            errors.append(f"line {idx}: bad-date")
            continue
        payer = norm_name(rec["payer"])
        if not payer or ";" in payer or ":" in payer:
            errors.append(f"line {idx}: bad-payer")
            continue
        cents = parse_amount(rec["amount"])
        if cents is None:
            errors.append(f"line {idx}: bad-amount")
            continue
        parts = parse_participants(rec["participants"])
        if parts is None:
            errors.append(f"line {idx}: bad-participants")
            continue
        rows.append((date, payer, cents, parts))
    if header is None:
        sys.stderr.write("header: missing date\n")
        sys.exit(2)
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        sys.exit(2)
    display = {}
    for _, payer, _, parts in rows:
        for n in [payer] + [p[0] for p in parts]:
            display.setdefault(key_of(n), n)
    opts["display"] = display
    out = []
    for r in rows:
        if opts["since"] is not None and r[0] < opts["since"]:
            continue
        if opts["until"] is not None and r[0] > opts["until"]:
            continue
        out.append(r)
    return out


def split(cents, parts):
    total_w = sum(w for _, w in parts)
    shares = []
    for name, w in parts:
        q, r = divmod(cents * w, total_w)
        shares.append([name, q, r])
    left = cents - sum(s[1] for s in shares)
    order = sorted(range(len(shares)), key=lambda i: (-shares[i][2], key_of(shares[i][0]), shares[i][0]))
    for i in order[:left]:
        shares[i][1] += 1
    return [(s[0], s[1]) for s in shares]


def compute_balances(rows, canon=None):
    canon = canon or {}
    display = {}
    bal = {}

    def add(name, delta):
        k = key_of(name)
        if k not in display:
            display[k] = canon.get(k, name)
            bal[k] = 0
        bal[k] += delta

    for _, payer, cents, parts in rows:
        add(payer, cents)
        for name, share in split(cents, parts):
            add(name, -share)
    items = sorted(bal.items(), key=lambda kv: (kv[0], display[kv[0]]))
    return [(display[k], v) for k, v in items]


def fmt(cents, signed=True):
    sign = ""
    if cents < 0:
        sign = "-"
    elif cents > 0 and signed:
        sign = "+"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def settle(balances):
    bal = {name: c for name, c in balances}
    transfers = []
    while True:
        debtors = [(c, key_of(n), n) for n, c in bal.items() if c < 0]
        creditors = [(-c, key_of(n), n) for n, c in bal.items() if c > 0]
        if not debtors or not creditors:
            break
        d = min(debtors)[2]
        c = min(creditors)[2]
        amt = min(-bal[d], bal[c])
        bal[d] += amt
        bal[c] -= amt
        transfers.append((d, c, amt))
    return transfers


def main(argv):
    try:
        command, path, opts = parse_args(argv)
    except UsageError as e:
        sys.stderr.write(f"usage error: {e}\n"
                         "usage: splitr.py balances|settle <ledger.csv> "
                         "[--format text|json] [--since YYYY-MM-DD] [--until YYYY-MM-DD]\n")
        return 64
    rows = load(path, opts)
    balances = compute_balances(rows, opts.get("display"))
    out = []
    if command == "balances":
        if opts["format"] == "json":
            out.append(json.dumps({"balances": [{"name": n, "cents": c} for n, c in balances]}))
        else:
            out.extend(f"{n} {fmt(c)}" for n, c in balances)
    else:
        transfers = settle(balances)
        if opts["format"] == "json":
            out.append(json.dumps({"transfers": [
                {"from": f, "to": t, "cents": c} for f, t, c in transfers]}))
        elif not transfers:
            out.append("nothing to settle")
        else:
            out.extend(f"{f} -> {t} {fmt(c, signed=False)}" for f, t, c in transfers)
    if out:
        sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
