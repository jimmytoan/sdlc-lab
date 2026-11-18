"""Black-box regression tests for the splitr command-line contract."""
import csv
import io
import json
import random

import pytest

from helpers import run


HEADER = "date,payer,amount,participants,note\n"


def invoke(ledger, command="balances", *options):
    return run(ledger, command, "{f}", *options)


def success(ledger, command="balances", *options):
    code, out, err = invoke(ledger, command, *options)
    assert code == 0
    assert err == ""
    return out


def test_csv_bom_comments_reordered_header_quoting_and_names():
    ledger = ('\ufeff  # comment before header\r\n\r\n'
              ' note , participants , amount , payer , date , extra\r\n'
              '"meal, with cake"," bob ; ALICE ", 10.01 , Alice , 2024-02-29,ignored\r\n'
              '  # an ignored comment\r\n'
              '"","alice; BOB; cY", 0.02, bOb, 2024-03-01,anything\r\n')
    assert success(ledger) == "Alice +4.99\nbob -4.99\ncY 0.00\n"
    assert json.loads(success(ledger, "balances", "--format", "json")) == {
        "balances": [{"name": "Alice", "cents": 499},
                     {"name": "bob", "cents": -499},
                     {"name": "cY", "cents": 0}]}


def test_first_spelling_payer_before_participants_and_zero_people():
    ledger = HEADER + ("2024-01-01,zED,0.01,Zed;alICE;BOB,\n"
                       "2024-01-02,Alice,1,alice,\n"
                       "2024-01-03,bob,1,BOB,\n")
    assert success(ledger) == "alICE -0.01\nBOB 0.00\nzED +0.01\n"


def test_weighted_largest_remainder_not_largest_weight_or_input_order():
    # 10 cents / 7: base shares 1, 2, 5 with two cents left. The
    # remainders 3, 6, 5 award them to beta and Alpha, respectively.
    ledger = HEADER + "2025-01-01,Payer,0.10,Zulu:1;beta:2;Alpha:4,\n"
    assert success(ledger) == "Alpha -0.06\nbeta -0.03\nPayer +0.10\nZulu -0.01\n"


def test_remainders_tie_case_insensitive_and_payer_has_no_priority():
    ledger = HEADER + "2025-01-01,Zed,0.02,ZED;bob;ALICE;cat,\n"
    assert success(ledger) == "ALICE -0.01\nbob -0.01\ncat 0.00\nZed +0.02\n"


def test_settlement_recalculates_extremes_after_each_transfer():
    ledger = HEADER + ("2025-01-01,A,6,X,\n"
                       "2025-01-01,B,4,Y,\n"
                       "2025-01-01,C,3,Y,\n")
    expected = ("Y -> A 6.00\nX -> B 4.00\nX -> C 2.00\nY -> C 1.00\n")
    assert success(ledger, "settle") == expected
    assert json.loads(success(ledger, "settle", "--format", "json")) == {
        "transfers": [{"from": "Y", "to": "A", "cents": 600},
                      {"from": "X", "to": "B", "cents": 400},
                      {"from": "X", "to": "C", "cents": 200},
                      {"from": "Y", "to": "C", "cents": 100}]}


def test_settlement_ties_use_case_insensitive_name_order():
    ledger = HEADER + ("2025-01-01,beta,1,Zoe,\n"
                       "2025-01-01,Alpha,1,yan,\n")
    assert success(ledger, "settle") == "yan -> Alpha 1.00\nZoe -> beta 1.00\n"


@pytest.mark.parametrize("ledger", [HEADER, HEADER + "\n # no rows\n", HEADER + "2025-01-01,A,1,a,\n"])
def test_nothing_to_settle(ledger):
    assert success(ledger, "settle") == "nothing to settle\n"
    assert json.loads(success(ledger, "settle", "--format", "json")) == {"transfers": []}


def test_empty_balances():
    assert success(HEADER) == ""
    assert json.loads(success(HEADER, "balances", "--format", "json")) == {"balances": []}
    assert success(HEADER + "2025-01-01,A,1,a,\n") == "A 0.00\n"


FILTER_LEDGER = HEADER + ("2024-01-01,Old,1,Other,\n"
                          "2024-02-01,Ann,2,Bob,\n"
                          "2024-02-15,Bob,1,Ann,\n"
                          "2024-03-01,Cy,3,Ann,\n"
                          "2024-04-01,New,4,Other,\n")


@pytest.mark.parametrize("options,expected", [
    (("--since", "2024-02-01", "--until", "2024-03-01"), "Ann -2.00\nBob -1.00\nCy +3.00\n"),
    (("--since", "2024-04-01"), "New +4.00\nOther -4.00\n"),
    (("--until", "2024-01-01"), "Old +1.00\nOther -1.00\n"),
    (("--since", "2024-02-15", "--until", "2024-02-15"), "Ann -1.00\nBob +1.00\n"),
    (("--since", "2026-01-01"), ""),
    (("--since", "2024-04-01", "--until", "2024-01-01"), ""),
])
def test_date_filter_inclusive_and_excludes_people(options, expected):
    assert success(FILTER_LEDGER, "balances", *options) == expected


def test_options_before_and_after_positionals():
    code, out, err = run(FILTER_LEDGER, "--since", "2024-02-15", "settle", "{f}",
                         "--until", "2024-02-15", "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"transfers": [{"from": "Ann", "to": "Bob", "cents": 100}]}


def test_invalid_rows_outside_filter_are_still_reported():
    ledger = HEADER + ("2020-01-01,A,nope,B,\n"
                       "2025-01-01,A,1,B,\n"
                       "2030-01-01,A,1,B;B,\n")
    assert invoke(ledger, "settle", "--since", "2025-01-01", "--until", "2025-01-01") == (
        2, "", "line 2: bad-amount\nline 4: bad-participants\n")


def test_error_order_physical_lines_and_first_failure():
    ledger = ("# before header\n\n" + HEADER +
              "nonsense,,oops,,\n"  # all four fields bad
              "  # ignore this line\n"
              "2024-01-01, ,oops,,\n"
              "\n"
              "2024-01-01,A,oops,,\n"
              "2024-01-01,A,1,,\n"
              "2024-01-01,A,1,A,\n"
              "2024-01-01\n")
    assert invoke(ledger) == (2, "", "line 4: bad-date\nline 6: bad-payer\n"
                              "line 8: bad-amount\nline 9: bad-participants\n"
                              "line 11: bad-payer\n")


@pytest.mark.parametrize("bad_dates", [
    ["2023-02-29", "2024-02-30", "2024-13-01", "2024-00-01", "2024-01-00"],
    ["2024-1-01", "2024-01-1", "24-01-01", "20240101", "2024/01/01", "", "2024-01-01T00:00:00"],
])
def test_bad_dates(bad_dates):
    ledger = HEADER + "".join(f"{d},A,1,B,\n" for d in bad_dates)
    assert invoke(ledger) == (2, "", "".join(f"line {i}: bad-date\n" for i in range(2, len(bad_dates) + 2)))


@pytest.mark.parametrize("bad_amounts", [
    ["0", "0.0", "0.00", "-1", "+1", "1.000", ".5", "1.", "1e2"],
    ["", "abc", "1 2", "--1", "NaN", "inf"],
])
def test_bad_amounts(bad_amounts):
    ledger = HEADER + "".join(f"2024-01-01,A,{a},B,\n" for a in bad_amounts)
    assert invoke(ledger) == (2, "", "".join(f"line {i}: bad-amount\n" for i in range(2, len(bad_amounts) + 2)))


def test_comma_thousands_separator_is_not_an_amount():
    assert invoke(HEADER + '2024-01-01,A,"1,000",B,\n') == (2, "", "line 2: bad-amount\n")


def test_valid_amount_forms_and_large_integers():
    ledger = HEADER + ("2024-01-01,A,12,B,\n2024-01-01,A,12.5,B,\n"
                       "2024-01-01,A,12.50,B,\n2024-01-01,A,000.01,B,\n"
                       "2024-01-01,A,999999999999999999.99,B,\n")
    assert json.loads(success(ledger, "balances", "--format", "json")) == {
        "balances": [{"name": "A", "cents": 100000000000000003700},
                     {"name": "B", "cents": -100000000000000003700}]}


def test_bad_participants():
    bad = ["", " ", ";A", "A;", "A;;B", "A; a ", "A:0", "A:-1", "A:+1",
           "A:1.5", "A:", ":2", "A:x", "A:1:2", "A:1; a:2"]
    ledger = HEADER + "".join(f"2024-01-01,P,1,{p},\n" for p in bad)
    assert invoke(ledger) == (2, "", "".join(f"line {i}: bad-participants\n" for i in range(2, len(bad) + 2)))


def test_valid_weights_and_trimming():
    ledger = HEADER + "2024-01-01, P ,0.08, b : 002 ; A : 1 ; c ,\n"
    assert success(ledger) == "A -0.02\nb -0.04\nc -0.02\nP +0.08\n"


@pytest.mark.parametrize("header,missing", [
    ("payer,amount,participants,note", "date"),
    ("date,amount,participants,note", "payer"),
    ("date,payer,participants,note", "amount"),
    ("date,payer,amount,note", "participants"),
    ("date,payer,amount,participants", "note"),
    ("note", "date"),
    ("Date,payer,amount,participants,note", "date"),
])
def test_missing_header_columns(header, missing):
    assert invoke(header + "\ninvalid,,,,\n") == (2, "", f"header: missing {missing}\n")


@pytest.mark.parametrize("ledger", ["", "\n  \n # comment\n"])
def test_no_header(ledger):
    assert invoke(ledger) == (2, "", "header: missing date\n")


def test_unreadable_file_and_invalid_utf8():
    assert run(HEADER, "balances", "{f}/missing") == (3, "", "error: cannot read {f}/missing\n")
    assert run("", "balances", "{f}", raw=b"\xff\xfe") == (3, "", "error: cannot read {f}\n")


@pytest.mark.parametrize("args", [
    (), ("balances",), ("{f}",), ("unknown", "{f}"),
    ("balances", "{f}", "extra"), ("balances", "{f}", "--unknown"),
    ("balances", "{f}", "--format", "yaml"), ("balances", "{f}", "--format"),
    ("balances", "{f}", "--since", "2023-02-29"),
    ("balances", "{f}", "--until", "2024-1-01"),
    ("balances", "{f}", "--since"), ("balances", "{f}", "--until", "nonsense"),
])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64
    assert out == ""
    assert err


@pytest.mark.parametrize("seed", [19, 823, 7419])
def test_mixed_expenses_against_independent_cent_oracle(seed):
    rng = random.Random(seed)
    people = ["Alice", "bob", "CHARLIE", "dora", "Eve", "frank", "Gina"]
    names, balances = {}, {}

    def spell(name):
        return rng.choice([name, name.upper(), name.lower()])

    def touch(name):
        key = name.lower()
        if key not in names:
            names[key] = name
            balances[key] = 0
        return key

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["date", "payer", "amount", "participants", "note"])
    for row in range(60):
        payer = spell(rng.choice(people))
        cents = rng.randint(1, 15000)
        selected = rng.sample(people, rng.randint(1, len(people)))
        parts = [(spell(person), rng.randint(1, 9)) for person in selected]
        fields = [name if weight == 1 else f"{name}:{weight}" for name, weight in parts]
        writer.writerow(["2024-05-01", payer, f"{cents // 100}.{cents % 100:02}",
                         ";".join(fields), f"row {row}, note"])
        payer_key = touch(payer)
        for name, _ in parts:
            touch(name)
        balances[payer_key] += cents
        weight_sum = sum(weight for _, weight in parts)
        divisions = [(name.lower(), *divmod(cents * weight, weight_sum)) for name, weight in parts]
        leftover = cents - sum(base for _, base, _ in divisions)
        winners = {name for name, _, _ in sorted(divisions, key=lambda item: (-item[2], item[0]))[:leftover]}
        for name, base, _ in divisions:
            balances[name] -= base + (name in winners)

    assert sum(balances.values()) == 0
    ledger = stream.getvalue()
    expected_balances = [{"name": names[key], "cents": balances[key]} for key in sorted(names)]
    assert json.loads(success(ledger, "balances", "--format", "json")) == {"balances": expected_balances}

    def amount(cents, signed=False):
        sign = ("+" if cents > 0 else "-") if signed and cents else ""
        return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02}"

    assert success(ledger) == "".join(
        f"{item['name']} {amount(item['cents'], True)}\n" for item in expected_balances)
    remaining = balances.copy()
    transfers = []
    while any(remaining.values()):
        debtor = sorted((k for k in remaining if remaining[k] < 0), key=lambda k: (remaining[k], k))[0]
        creditor = sorted((k for k in remaining if remaining[k] > 0), key=lambda k: (-remaining[k], k))[0]
        value = min(-remaining[debtor], remaining[creditor])
        remaining[debtor] += value
        remaining[creditor] -= value
        transfers.append({"from": names[debtor], "to": names[creditor], "cents": value})
    assert json.loads(success(ledger, "settle", "--format", "json")) == {"transfers": transfers}
    text = "".join(f"{item['from']} -> {item['to']} {amount(item['cents'])}\n" for item in transfers)
    assert success(ledger, "settle") == (text or "nothing to settle\n")
