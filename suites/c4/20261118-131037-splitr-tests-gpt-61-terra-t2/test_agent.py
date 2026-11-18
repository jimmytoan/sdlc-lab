"""Black-box regression tests for the shared-expense CLI."""
import csv
import io
import json
import random
from datetime import date, timedelta

import pytest

from helpers import run

HEADER = "date,payer,amount,participants,note\n"


def ledger(rows):
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["date", "payer", "amount", "participants", "note"])
    writer.writerows(rows)
    return buf.getvalue()


def ok(text, *args):
    code, out, err = run(text, *args)
    assert (code, err) == (0, "")
    return out


def test_csv_bom_crlf_comments_quoting_and_column_order():
    text = ('\ufeff  # before header\r\n \t\r\n'
            ' note , participants , amount , payer , date , extra\r\n'
            '"coffee, and ""cake""",  bob ; ALICE , 1.01 , Alice , 2024-02-29 , x\r\n'
            '  # ignored,not,a,row\r\n'
            ',alice;BOB,0.01, bOb ,2024-03-01,\r\n')
    assert ok(text, "balances", "{f}") == "Alice +0.49\nbob -0.49\n"
    assert ok(text, "settle", "{f}") == "bob -> Alice 0.49\n"


def test_first_spelling_payer_before_participants_and_case_insensitive_sort():
    text = ledger([
        ("2023-01-01", "zED", "1", "zed;ALICE;bob", ""),
        ("2023-01-02", "Alice", "1", "BOB;Zed", ""),
        ("2023-01-03", "BOB", "1", "alice", ""),
    ])
    assert ok(text, "balances", "{f}") == "ALICE -0.34\nbob +0.17\nzED +0.17\n"
    assert ok(text, "settle", "{f}") == "ALICE -> bob 0.17\nALICE -> zED 0.17\n"


def test_remainders_rank_before_name_and_ties_ignore_case():
    text = ledger([("2025-01-01", "Payer", "0.07", "z:1;B:2;a:3", "")])
    assert ok(text, "balances", "{f}") == "a -0.04\nB -0.02\nPayer +0.07\nz -0.01\n"
    # Equal remainders: alphabetical order wins, not input order or payer.
    text = ledger([("2025-01-01", "Zed", "0.02", "Zed;bob;Ann", "")])
    assert ok(text, "balances", "{f}") == "Ann -0.01\nbob -0.01\nZed +0.02\n"


def test_zero_balances_and_json_integer_cents():
    text = ledger([("2024-01-01", "Self", "12.50", "SELF;Zero:1", ""),
                   ("2024-01-02", "zero", "6.25", "self", "")])
    assert ok(text, "balances", "{f}") == "Self 0.00\nZero 0.00\n"
    assert json.loads(ok(text, "balances", "{f}", "--format", "json")) == {
        "balances": [{"name": "Self", "cents": 0}, {"name": "Zero", "cents": 0}]}
    assert ok(text, "settle", "{f}") == "nothing to settle\n"
    assert json.loads(ok(text, "settle", "{f}", "--format", "json")) == {"transfers": []}


def test_settlement_extremes_ties_and_json_order():
    text = ledger([
        ("2025-01-01", "Xavier", "0.07", "bob", ""),
        ("2025-01-01", "Yara", "0.07", "Ann", ""),
        ("2025-01-01", "Zoe", "0.04", "Carl", ""),
    ])
    expected = [
        {"from": "Ann", "to": "Xavier", "cents": 7},
        {"from": "bob", "to": "Yara", "cents": 7},
        {"from": "Carl", "to": "Zoe", "cents": 4},
    ]
    assert ok(text, "settle", "{f}") == "Ann -> Xavier 0.07\nbob -> Yara 0.07\nCarl -> Zoe 0.04\n"
    assert json.loads(ok(text, "settle", "{f}", "--format", "json")) == {"transfers": expected}


def test_settlement_reselects_after_partial_payment():
    text = ledger([("2025-01-01", "X", "1.00", "A", ""),
                   ("2025-01-01", "X", "0.50", "B", ""),
                   ("2025-01-01", "Y", "0.80", "C", "")])
    assert ok(text, "settle", "{f}") == "A -> X 1.00\nC -> Y 0.80\nB -> X 0.50\n"


DATES = ledger([
    ("2024-01-01", "Outside", "1", "old", ""),
    ("2024-02-01", "ANN", "2", "bob", ""),
    ("2024-02-02", "Bob", "1", "ann", ""),
    ("2024-02-03", "Later", "4", "other", ""),
])


def test_date_filters_inclusive_and_exclude_people():
    assert ok(DATES, "--since", "2024-02-01", "balances", "{f}", "--until", "2024-02-02") == "ANN +1.00\nbob -1.00\n"
    assert ok(DATES, "balances", "--until", "2024-01-01", "{f}") == "old -1.00\nOutside +1.00\n"
    assert ok(DATES, "balances", "{f}", "--since=2024-02-03") == "Later +4.00\nother -4.00\n"
    assert ok(DATES, "settle", "{f}", "--since", "2024-02-04") == "nothing to settle\n"
    assert json.loads(ok(DATES, "--format=json", "balances", "{f}", "--since", "2025-01-01")) == {"balances": []}
    assert ok(DATES, "balances", "{f}", "--since", "2024-03-01", "--until", "2024-01-01") == ""


def test_spelling_first_appearance_in_considered_rows():
    text = ledger([("2020-01-01", "alice", "1", "BOB", ""),
                   ("2024-01-01", "ALICE", "1", "Bob", "")])
    assert ok(text, "balances", "{f}", "--since", "2024-01-01") == "ALICE +1.00\nBob -1.00\n"


def test_filtered_out_rows_are_still_validated():
    text = HEADER + "2020-01-01,A,wrong,B,\n2024-01-01,A,1,B,\n2030-01-01,,1,B,\n"
    assert run(text, "settle", "{f}", "--since", "2024-01-01", "--until", "2024-12-31") == (
        2, "", "line 2: bad-amount\nline 4: bad-payer\n")


@pytest.mark.parametrize("command, expected", [("balances", ""), ("settle", "nothing to settle\n")])
def test_empty_ledger(command, expected):
    assert ok("# comment\n\n" + HEADER + " \n # trailing\n", command, "{f}") == expected


@pytest.mark.parametrize("header, missing", [
    ("", "date"), ("# nothing\n \n", "date"),
    ("payer,amount,participants,note\n", "date"),
    ("date,amount,participants,note\n", "payer"),
    ("date,payer,participants,note\n", "amount"),
    ("date,payer,amount,note\n", "participants"),
    ("date,payer,amount,participants\n", "note"),
    ("note,payer\n", "date"), ("DATE,payer,amount,participants,note\n", "date"),
])
def test_missing_header_first_required_column_only(header, missing):
    assert run(header + "bad,row\n", "balances", "{f}") == (2, "", f"header: missing {missing}\n")


def test_completely_empty_file():
    assert run("", "balances", "{f}") == (2, "", "header: missing date\n")


def test_row_errors_order_priority_and_physical_line_numbers():
    text = (HEADER + "\n # skip\n"
            "bad,,oops,,\n"
            "2024-01-01, ,oops,,\n"
            "2024-01-01,A,oops,,\n"
            "2024-01-01,A,1,A;a,\n"
            "2024-01-01,A,1,A,\n"
            "2024-01-01,A\n"
            "2024-02-30,A,1,A,\n")
    assert run(text, "balances", "{f}") == (2, "", (
        "line 4: bad-date\nline 5: bad-payer\nline 6: bad-amount\n"
        "line 7: bad-participants\nline 9: bad-amount\nline 10: bad-date\n"))


@pytest.mark.parametrize("bad", [
    "", "2024-1-01", "2024-01-1", "24-01-01", "2024/01/01", "20240101",
    "2023-02-29", "2024-02-30", "2024-00-01", "2024-13-01", "2024-01-00",
    "0000-01-01", "2024-01-01T00:00:00", "tomorrow",
])
def test_bad_row_dates(bad):
    text = ledger([(bad, "A", "1", "A", "")])
    assert run(text, "balances", "{f}") == (2, "", "line 2: bad-date\n")


@pytest.mark.parametrize("amount", [
    "", "0", "0.00", "00.0", "-1", "+1", ".5", "1.", "1.234", "1e2",
    "1,000", "1 000", "NaN", "inf", "--1", "1.0.0",
])
def test_bad_amounts(amount):
    text = ledger([("2024-01-01", "A", amount, "A", "")])
    assert run(text, "balances", "{f}") == (2, "", "line 2: bad-amount\n")


def test_valid_amounts_and_trimming():
    text = ledger([(" 2024-02-29 ", " A ", amount, " B ", "")
                   for amount in ("1", "1.5", "1.50", "0.01", "0002.09")])
    assert ok(text, "balances", "{f}") == "A +6.10\nB -6.10\n"


@pytest.mark.parametrize("participants", [
    "", "  ", ";", ";A", "A;", "A;;B", "A; ;B", ":1", "A:0", "A:00",
    "A:-1", "A:+1", "A:1.0", "A:1e2", "A:", "A:1:2", "A; a ", "A:2;a:3",
])
def test_bad_participants(participants):
    text = ledger([("2024-01-01", "A", "1", participants, "")])
    assert run(text, "balances", "{f}") == (2, "", "line 2: bad-participants\n")


def test_valid_weight_whitespace_and_large_weight():
    text = ledger([("2024-01-01", "P", "0.11", " Z : 00010 ; a: 1 ; B ", "")])
    assert ok(text, "balances", "{f}") == "a -0.01\nB -0.01\nP +0.11\nZ -0.09\n"


@pytest.mark.parametrize("args", [
    (), ("balances",), ("explode", "{f}"), ("BALANCES", "{f}"),
    ("balances", "{f}", "--unknown"), ("balances", "{f}", "extra"),
    ("balances", "{f}", "--format", "xml"), ("balances", "{f}", "--format"),
    ("balances", "{f}", "--since"), ("balances", "{f}", "--until"),
    ("balances", "{f}", "--since", "2023-02-29"),
    ("balances", "{f}", "--until", "2024-1-01"),
    ("balances", "{f}", "--since", "2024-13-01"),
])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64 and out == "" and err


def test_unreadable_file_and_invalid_utf8():
    assert run("", "balances", "{f}/missing") == (3, "", "error: cannot read {f}/missing\n")
    assert run("", "settle", "{f}", raw=b"\xff\xfe\xff") == (3, "", "error: cannot read {f}\n")


def model(rows, since=None, until=None):
    """Integer oracle for generated valid expenses."""
    names, balances = {}, {}
    for day, payer, cents, parts in rows:
        if (since and day < since) or (until and day > until):
            continue
        for name in [payer] + [n for n, _ in parts]:
            key = name.lower()
            if key not in names:
                names[key], balances[key] = name, 0
        balances[payer.lower()] += cents
        total = sum(weight for _, weight in parts)
        base = [cents * weight // total for _, weight in parts]
        left = cents - sum(base)
        ranking = sorted(range(len(parts)), key=lambda i: (-(cents * parts[i][1] % total), parts[i][0].lower()))
        for i in ranking[:left]:
            base[i] += 1
        for (name, _), share in zip(parts, base):
            balances[name.lower()] -= share
    ordered = sorted(names)
    balances_json = [{"name": names[k], "cents": balances[k]} for k in ordered]
    transfers = []
    while any(balances.values()):
        debtors = [k for k in ordered if balances[k] < 0]
        creditors = [k for k in ordered if balances[k] > 0]
        debtor = min(debtors, key=lambda k: (balances[k], k))
        creditor = min(creditors, key=lambda k: (-balances[k], k))
        amount = min(-balances[debtor], balances[creditor])
        transfers.append({"from": names[debtor], "to": names[creditor], "cents": amount})
        balances[debtor] += amount
        balances[creditor] -= amount
    return balances_json, transfers


@pytest.mark.parametrize("seed", range(12))
def test_generated_ledgers_against_integer_oracle(seed):
    rng = random.Random(seed)
    people = ["Ann", "bob", "CY", "Dee", "ed", "Fran"]
    rows, csv_rows = [], []
    for _ in range(18):
        day = date(2024, 1, 1) + timedelta(days=rng.randrange(12))
        payer = rng.choice(people)
        if rng.randrange(2):
            payer = payer.swapcase()
        chosen = rng.sample(people, rng.randint(1, len(people)))
        parts = [(n.swapcase() if rng.randrange(2) else n, rng.randint(1, 7)) for n in chosen]
        cents = rng.randint(1, 2000)
        rows.append((day, payer, cents, parts))
        csv_rows.append((day.isoformat(), payer, f"{cents // 100}.{cents % 100:02d}",
                         ";".join(f"{n}:{w}" for n, w in parts), "comma, quote \""))
    text = ledger(csv_rows)
    since, until = date(2024, 1, 4), date(2024, 1, 9)
    for options, lo, hi in [((), None, None), (("--since", since.isoformat(), "--until", until.isoformat()), since, until)]:
        expected_balances, expected_transfers = model(rows, lo, hi)
        assert sum(item["cents"] for item in expected_balances) == 0
        assert json.loads(ok(text, "balances", "{f}", "--format", "json", *options)) == {"balances": expected_balances}
        assert json.loads(ok(text, "settle", "{f}", "--format", "json", *options)) == {"transfers": expected_transfers}
