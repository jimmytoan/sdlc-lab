"""Black-box regression tests for the splitr command line contract."""
import csv
import datetime
import io
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import APP_DIR, run

HEADER = "date,payer,amount,participants,note\n"


def invoke(ledger, *args):
    code, out, err = run(ledger, *args)
    assert code == 0, (code, out, err)
    assert err == ""
    return out


def json_report(ledger, command, *options):
    return json.loads(invoke(ledger, command, "{f}", "--format", "json", *options))


def test_csv_bom_comments_reordering_crlf_and_quoted_fields():
    ledger = ("\ufeff  # before the header\r\n\r\n"
              " note , participants ,amount,payer,date,extra\r\n"
              '"lunch, and cake","bob; ALICE",2, Alice ,2024-01-01,x\r\n')
    assert invoke(ledger, "balances", "{f}") == "Alice +1.00\nbob -1.00\n"


def test_names_first_appearance_sorting_trimming_and_zero():
    ledger = HEADER + (
        "2024-01-01, zOE ,0.02, ALICE ; Zoe ;bob,\n"
        "2024-01-02, alice ,0.01, BOB ,\n"
        "2024-01-03,BOB,0.01, bob ,\n"
        "2024-01-04,Zero,1.00,ZERO,\n"
    )
    # On the first row ALICE and bob receive the tied extra cents, not Zoe.
    assert invoke(ledger, "balances", "{f}") == (
        "ALICE 0.00\nbob -0.02\nZero 0.00\nzOE +0.02\n"
    )
    assert json_report(ledger, "balances") == {"balances": [
        {"name": "ALICE", "cents": 0}, {"name": "bob", "cents": -2},
        {"name": "Zero", "cents": 0}, {"name": "zOE", "cents": 2},
    ]}


def test_weighted_largest_remainders_and_name_tie_not_input_order():
    ledger = HEADER + "2024-02-29,Payer,0.05,Zed:1;bob:2;Ann:2,\n"
    # Exact 1,2,2: no remainder. Add a row where remainders differ,
    # and another where equal remainders are awarded by name.
    ledger += "2024-03-01,Payer,0.07,Zed:1;bob:2;Ann:3,\n"
    ledger += "2024-03-02,Payer,0.02,Zed;bob;Ann,\n"
    assert json_report(ledger, "balances") == {"balances": [
        {"name": "Ann", "cents": -7}, {"name": "bob", "cents": -5},
        {"name": "Payer", "cents": 14}, {"name": "Zed", "cents": -2},
    ]}


def test_payer_can_receive_remainder_but_has_no_priority():
    ledger = HEADER + "2024-01-01,Zed,0.01,Zed;Ann,\n"
    assert invoke(ledger, "balances", "{f}") == "Ann -0.01\nZed +0.01\n"
    ledger = HEADER + "2024-01-01,Ann,0.01,Zed;Ann,\n"
    assert invoke(ledger, "balances", "{f}") == "Ann 0.00\nZed 0.00\n"


def test_settlement_reselects_extremes_and_breaks_both_ties_by_name():
    ledger = HEADER + (
        "2024-01-01,Cora,0.05,Zoe,\n"
        "2024-01-02,anna,0.05,bob,\n"
        "2024-01-03,Dave,0.02,zoe,\n"
    )
    expected = [
        {"from": "Zoe", "to": "anna", "cents": 5},
        {"from": "bob", "to": "Cora", "cents": 5},
        {"from": "Zoe", "to": "Dave", "cents": 2},
    ]
    assert json_report(ledger, "settle") == {"transfers": expected}
    assert invoke(ledger, "settle", "{f}") == (
        "Zoe -> anna 0.05\nbob -> Cora 0.05\nZoe -> Dave 0.02\n"
    )


def test_settle_debtor_and_creditor_ties():
    ledger = HEADER + (
        "2024-01-01,Zed,1,Bob,\n"
        "2024-01-02,Ann,1,amy,\n"
    )
    assert invoke(ledger, "settle", "{f}") == (
        "amy -> Ann 1.00\nBob -> Zed 1.00\n"
    )


@pytest.mark.parametrize("command,expected", [
    ("balances", ""), ("settle", "nothing to settle\n"),
])
def test_empty_ledger_text(command, expected):
    assert invoke(HEADER + "\n # comment\n", command, "{f}") == expected


def test_empty_and_zero_json():
    assert json_report(HEADER, "balances") == {"balances": []}
    assert json_report(HEADER, "settle") == {"transfers": []}
    ledger = HEADER + "2024-01-01,A,1,A,\n"
    assert invoke(ledger, "settle", "{f}") == "nothing to settle\n"
    assert json_report(ledger, "settle") == {"transfers": []}
    assert json_report(ledger, "balances") == {"balances": [{"name": "A", "cents": 0}]}


FILTER_LEDGER = HEADER + (
    "2024-01-01,Old,1,old,\n"
    "2024-01-02,alice,1,Bob,\n"
    "2024-01-03,BOB,2,Alice,\n"
    "2024-01-04,New,1,new,\n"
)


@pytest.mark.parametrize("options,expected", [
    (("--since", "2024-01-02", "--until", "2024-01-03"),
     [{"name": "alice", "cents": -100}, {"name": "Bob", "cents": 100}]),
    (("--since", "2024-01-03"), [
        {"name": "Alice", "cents": -200}, {"name": "BOB", "cents": 200},
        {"name": "New", "cents": 0}]),
    (("--until", "2024-01-02"), [
        {"name": "alice", "cents": 100}, {"name": "Bob", "cents": -100},
        {"name": "Old", "cents": 0}]),
    (("--since", "2024-01-04", "--until", "2024-01-02"), []),
    (("--since", "2025-01-01"), []),
])
def test_date_filters_inclusive_and_first_considered_spelling(options, expected):
    assert json_report(FILTER_LEDGER, "balances", *options) == {"balances": expected}


def test_filter_applies_to_settlement_and_options_can_precede_positionals():
    args = ("--format", "json", "--since", "2024-01-03", "settle", "--until", "2024-01-03", "{f}")
    assert json.loads(invoke(FILTER_LEDGER, *args)) == {"transfers": [
        {"from": "Alice", "to": "BOB", "cents": 200}]}


def test_validation_includes_excluded_rows_and_reports_all_in_line_order():
    ledger = (HEADER + "# ignored\n2020-01-01,A,0,A,\n\n"
              "2030-01-01,,1,A,\n2024-01-01,A,1,A,\n"
              "  # ignored\nnot-a-date,A,1,A,\n")
    for command in ("balances", "settle"):
        code, out, err = run(ledger, command, "{f}", "--since", "2024-01-01", "--until", "2024-12-31")
        assert (code, out, err) == (2, "", "line 3: bad-amount\nline 5: bad-payer\nline 8: bad-date\n")


@pytest.mark.parametrize("date", ["2024-2-01", "24-02-01", "2023-02-29", "2024-02-30", "2024-13-01", "2024-00-01", "2024-01-00", "2024/01/01", "", "not-a-date"])
def test_bad_dates(date):
    code, out, err = run(HEADER + f"{date},A,1,A,\n", "balances", "{f}")
    assert (code, out, err) == (2, "", "line 2: bad-date\n")


@pytest.mark.parametrize("amount", ["", "0", "0.00", "+1", "-1", ".5", "1.", "1.234", "1e2", "NaN", "1 000", '"1,000"'])
def test_bad_amounts(amount):
    code, out, err = run(HEADER + f"2024-01-01,A,{amount},A,\n", "balances", "{f}")
    assert (code, out, err) == (2, "", "line 2: bad-amount\n")


@pytest.mark.parametrize("amount,cents", [("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), ("001.02", 102), (" 3.10 ", 310), ("123456789.99", 12345678999)])
def test_valid_amounts(amount, cents):
    ledger = HEADER + f"2024-01-01,A,{amount},B,\n"
    assert json_report(ledger, "balances") == {"balances": [
        {"name": "A", "cents": cents}, {"name": "B", "cents": -cents}]}


@pytest.mark.parametrize("participants", ["", " ", ";A", "A;", "A;;B", ":1", "A:0", "A:-1", "A:+1", "A:1.0", "A:", "A:x", "A:1:2", "Alice; ALICE", "a:2;A:3"])
def test_bad_participants(participants):
    code, out, err = run(HEADER + f"2024-01-01,A,1,{participants},\n", "settle", "{f}")
    assert (code, out, err) == (2, "", "line 2: bad-participants\n")


def test_weight_whitespace_and_leading_zero():
    ledger = HEADER + "2024-01-01, P ,0.03, A : 02 ; B : 1 ,\n"
    assert invoke(ledger, "balances", "{f}") == "A -0.02\nB -0.01\nP +0.03\n"


def test_validation_precedence_and_short_rows():
    ledger = HEADER + (
        "bad,,0,;,\n"
        "2024-01-01,  ,0,;,\n"
        "2024-01-01,A,0,;,\n"
        "2024-01-01,A,1,;,\n"
        "2024-01-01\n"
        "2024-01-01,A\n"
        "2024-01-01,A,1\n"
        "2024-01-01,A,1,A\n"  # absent note is free text
    )
    assert run(ledger, "balances", "{f}") == (2, "", (
        "line 2: bad-date\nline 3: bad-payer\nline 4: bad-amount\n"
        "line 5: bad-participants\nline 6: bad-payer\n"
        "line 7: bad-amount\nline 8: bad-participants\n"))


@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_header_column_precedes_row_errors(missing):
    columns = [c for c in ("date", "payer", "amount", "participants", "note") if c != missing]
    ledger = ",".join(columns) + "\ninvalid,row\n"
    assert run(ledger, "balances", "{f}") == (2, "", f"header: missing {missing}\n")


@pytest.mark.parametrize("ledger,missing", [("", "date"), ("\n # comment\n", "date"),
    ("note,amount\n", "date"), ("date,note\n", "payer"), ("date,payer,note\n", "amount")])
def test_first_missing_header(ledger, missing):
    assert run(ledger, "settle", "{f}") == (2, "", f"header: missing {missing}\n")


@pytest.mark.parametrize("args", [
    (), ("balances",), ("unknown", "{f}"), ("balances", "{f}", "extra"),
    ("balances", "{f}", "--wat"), ("balances", "{f}", "--format", "xml"),
    ("balances", "{f}", "--format"), ("balances", "{f}", "--since"),
    ("balances", "{f}", "--since", "2024-2-01"),
    ("settle", "{f}", "--until", "2023-02-29"),
])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64 and out == "" and err


def test_cannot_read_file_and_invalid_utf8(tmp_path):
    missing = tmp_path / "does-not-exist.csv"
    p = subprocess.run([sys.executable, str(Path(APP_DIR) / "splitr.py"), "balances", str(missing)],
                       capture_output=True, text=True, timeout=20)
    assert (p.returncode, p.stdout, p.stderr) == (3, "", f"error: cannot read {missing}\n")
    assert run("", "balances", "{f}", raw=b"\xff\xfe") == (3, "", "error: cannot read {f}\n")


def reference_balances(expenses):
    """Independent integer apportionment oracle for generated valid ledgers."""
    display, amounts = {}, {}
    for payer, cents, parts in expenses:
        for name in [payer] + [name for name, _ in parts]:
            key = name.lower()
            display.setdefault(key, name)
            amounts.setdefault(key, 0)
        amounts[payer.lower()] += cents
        total_weight = sum(weight for _, weight in parts)
        allocations = [divmod(cents * weight, total_weight) for _, weight in parts]
        left = cents - sum(base for base, _ in allocations)
        ranked = sorted(range(len(parts)), key=lambda i: (-allocations[i][1], parts[i][0].lower()))
        bonus = set(ranked[:left])
        for i, (name, _) in enumerate(parts):
            amounts[name.lower()] -= allocations[i][0] + (i in bonus)
    return [{"name": display[key], "cents": amounts[key]} for key in sorted(display)]


def reference_transfers(balances):
    remaining = {entry["name"]: entry["cents"] for entry in balances}
    result = []
    while any(value < 0 for value in remaining.values()):
        debtors = [n for n, v in remaining.items() if v < 0]
        creditors = [n for n, v in remaining.items() if v > 0]
        debtor = sorted(debtors, key=lambda n: (remaining[n], n.lower()))[0]
        creditor = sorted(creditors, key=lambda n: (-remaining[n], n.lower()))[0]
        cents = min(-remaining[debtor], remaining[creditor])
        result.append({"from": debtor, "to": creditor, "cents": cents})
        remaining[debtor] += cents
        remaining[creditor] -= cents
    return result


@pytest.mark.parametrize("seed", range(6))
def test_generated_ledgers_against_integer_oracle(seed):
    rng = random.Random(seed)
    people = ["Alice", "BOB", "cy", "Dora", "Eve", "Frank"]
    expenses = []
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "payer", "amount", "participants", "note"])
    for i in range(25):
        def spelling(name):
            return rng.choice([name, name.lower(), name.upper()])
        payer = spelling(rng.choice(people))
        cents = rng.randint(1, 20000)
        parts = [(spelling(n), rng.randint(1, 8)) for n in rng.sample(people, rng.randint(1, len(people)))]
        expenses.append((payer, cents, parts))
        writer.writerow([datetime.date(2024, 1, 1) + datetime.timedelta(days=i), payer,
                         f"{cents // 100}.{cents % 100:02d}",
                         ";".join(f"{n}:{w}" for n, w in parts), "quoted, note"])
    ledger = output.getvalue()
    balances = reference_balances(expenses)
    assert sum(entry["cents"] for entry in balances) == 0
    assert json_report(ledger, "balances") == {"balances": balances}
    assert json_report(ledger, "settle") == {"transfers": reference_transfers(balances)}


def test_quoted_names_note_and_extra_columns():
    ledger = ('note,amount,participants,payer,date,unused\n'
              '"say ""hello"", then eat",2.50,"Smith, Jo;Other","Smith, Jo",2024-01-01,anything\n')
    assert invoke(ledger, "balances", "{f}", "--format", "text") == (
        "Other -1.25\nSmith, Jo +1.25\n")
    assert json_report(ledger, "settle") == {"transfers": [
        {"from": "Other", "to": "Smith, Jo", "cents": 125}]}


def test_settlement_partial_transfers_reselect_every_time():
    # A pays C in full, then B is the largest remaining debtor;
    # the last payment clears both A and D.
    ledger = HEADER + (
        "2024-01-01,C,1.00,A,\n"
        "2024-01-01,C,0.60,B,\n"
        "2024-01-01,D,0.70,A,\n"
        "2024-01-01,D,0.30,B,\n"
    )
    assert invoke(ledger, "settle", "{f}") == (
        "A -> C 1.60\nB -> D 0.90\nA -> D 0.10\n")


def test_text_amount_signs_padding_and_explicit_option_placement():
    ledger = HEADER + "2024-01-01,Z,123.04,A;B:2,\n"
    assert invoke(ledger, "--format=text", "balances", "{f}") == (
        "A -41.01\nB -82.03\nZ +123.04\n")
    assert invoke(ledger, "settle", "--format", "text", "{f}") == (
        "B -> Z 82.03\nA -> Z 41.01\n")
