"""Black-box regression tests for the shared-expense command line interface."""
import csv
import datetime
import io
import json
import random

import pytest

from helpers import run


HEADER = "date,payer,amount,participants,note\n"


def success(ledger, *args, raw=None):
    code, out, err = run(ledger, *args, raw=raw)
    assert (code, err) == (0, "")
    return out


def json_report(ledger, command, *options):
    return json.loads(success(ledger, command, "{f}", "--format", "json", *options))


def test_csv_bom_crlf_comments_quotes_column_order_and_names():
    ledger = (
        '\ufeff  # comment before header\r\n\r\n'
        'note,participants,amount,payer,date\r\n'
        '"meal, with ""quotes""",  bOB ; ALICE  ,10.01,  Alice  ,2024-02-29\r\n'
        '   # ignored\r\n   \r\n'
        ',alice;BOB,1.00, bob ,2024-03-01\r\n'
    )
    # Payer Alice is the first spelling, despite ALICE in the same row.
    assert success(ledger, "balances", "{f}") == "Alice +4.50\nbOB -4.50\n"
    assert json_report(ledger, "balances") == {"balances": [
        {"name": "Alice", "cents": 450}, {"name": "bOB", "cents": -450}
    ]}
    assert success(ledger, "settle", "{f}") == "bOB -> Alice 4.50\n"


def test_first_appearance_payer_then_participants_and_zero_balances():
    ledger = HEADER + (
        '2025-01-01,ZOE,1.00,zoe,\n'
        '2025-01-02,ben,2.00,ALICE;alice2,\n'
        '2025-01-03,Alice,2.00,BEN;Alice2,\n'
    )
    assert success(ledger, "balances", "{f}") == (
        "ALICE +1.00\nalice2 -2.00\nben +1.00\nZOE 0.00\n"
    )
    assert success(ledger, "settle", "{f}") == (
        "alice2 -> ALICE 1.00\nalice2 -> ben 1.00\n"
    )


def test_largest_remainder_weighted_and_name_ties_not_payer_first():
    ledger = HEADER + (
        '2025-01-01,Zed,0.05,Zed:1;bob:2;Ann:3,\n'
        '2025-01-02,zed,0.02,zed;BOB;ann,\n'
    )
    # First row: floors 0,1,2; remainders 5,4,3: Zed and bob get cents.
    # Second row: equal remainders; Ann and bob get cents, not payer Zed.
    assert success(ledger, "balances", "{f}") == (
        "Ann -0.03\nbob -0.03\nZed +0.06\n"
    )
    assert success(ledger, "settle", "{f}") == (
        "Ann -> Zed 0.03\nbob -> Zed 0.03\n"
    )


def test_settlement_extremes_ties_and_reselection():
    # Directly construct balances via payers who are not participants.
    ledger = HEADER + (
        '2025-01-01,CredB,5,DebB,\n'
        '2025-01-01,CredA,5,DebA,\n'
        '2025-01-01,CredC,3,DebC,\n'
    )
    assert success(ledger, "settle", "{f}") == (
        "DebA -> CredA 5.00\nDebB -> CredB 5.00\nDebC -> CredC 3.00\n"
    )
    ledger = HEADER + (
        '2025-01-01,Alpha,7,Xavier,\n'
        '2025-01-01,Beta,5,Yara,\n'
        '2025-01-01,Beta,1,Zack,\n'
    )
    expected = [
        {"from": "Xavier", "to": "Alpha", "cents": 700},
        {"from": "Yara", "to": "Beta", "cents": 500},
        {"from": "Zack", "to": "Beta", "cents": 100},
    ]
    assert json_report(ledger, "settle") == {"transfers": expected}
    # Partial transfers must update the ranking, rather than exhausting a pair.
    ledger = HEADER + (
        '2025-01-01,C1,9,D1,\n'
        '2025-01-01,C2,8,D1,\n'
        '2025-01-01,C3,7,D2,\n'
    )
    assert success(ledger, "settle", "{f}") == (
        "D1 -> C1 9.00\nD1 -> C2 8.00\nD2 -> C3 7.00\n"
    )
    ledger = HEADER + (
        '2025-01-01,C1,10,D1,\n'
        '2025-01-01,C2,8,D2,\n'
        '2025-01-01,C2,1,D1,\n'
        '2025-01-01,C3,6,D2,\n'
    )
    assert success(ledger, "settle", "{f}") == (
        "D2 -> C1 10.00\nD1 -> C2 9.00\nD2 -> C3 4.00\nD1 -> C3 2.00\n"
    )


@pytest.mark.parametrize("ledger", [HEADER, HEADER + "\n # comment\n \t\n", HEADER + "2024-01-01,Ann,1,Ann,\n"])
def test_empty_or_zero_reports(ledger):
    is_empty = "2024-01-01" not in ledger
    assert success(ledger, "balances", "{f}") == ("" if is_empty else "Ann 0.00\n")
    assert success(ledger, "settle", "{f}") == "nothing to settle\n"
    assert json_report(ledger, "settle") == {"transfers": []}
    assert json_report(ledger, "balances") == {"balances": [] if is_empty else [{"name": "Ann", "cents": 0}]}


def test_range_inclusive_and_only_considered_names():
    ledger = HEADER + (
        '2024-01-01,Outside,1,OnlyBefore,\n'
        '2024-01-02,alice,1,Bob,\n'
        '2024-01-03,BOB,2,ALICE,\n'
        '2024-01-04,After,3,OnlyAfter,\n'
    )
    assert success(ledger, "--since", "2024-01-02", "--until", "2024-01-03", "balances", "{f}") == (
        "alice -1.00\nBob +1.00\n"
    )
    assert success(ledger, "balances", "{f}", "--since", "2024-01-03") == (
        "After +3.00\nALICE -2.00\nBOB +2.00\nOnlyAfter -3.00\n"
    )
    assert success(ledger, "balances", "{f}", "--until", "2024-01-02") == (
        "alice +1.00\nBob -1.00\nOnlyBefore -1.00\nOutside +1.00\n"
    )
    assert json_report(ledger, "balances", "--since", "2025-01-01") == {"balances": []}
    assert json_report(ledger, "settle", "--until", "2023-12-31") == {"transfers": []}


def test_errors_include_out_of_range_and_physical_line_numbers_and_precedence():
    ledger = HEADER + (
        '# skip\n'
        '\n'
        'not-a-date,,bad,,\n'             # date before payer, amount, parts
        '2020-01-01,  ,bad,,\n'          # payer before amount, parts
        '  # another comment\n'
        '2020-01-01,A,0,,\n'             # amount before parts
        '2020-01-01,A,1,A;a,\n'
        '2025-01-01,Valid,1,Valid,\n'
        '2029-01-01,A,1,,\n'
    )
    for command in ("balances", "settle"):
        code, out, err = run(ledger, command, "{f}", "--since", "2025-01-01", "--until", "2025-01-01")
        assert (code, out, err) == (2, "", "line 4: bad-date\nline 5: bad-payer\nline 7: bad-amount\nline 8: bad-participants\nline 10: bad-participants\n")


@pytest.mark.parametrize("bad_date", [
    "2023-02-29", "2024-02-30", "2024-13-01", "2024-00-01", "2024-01-00",
    "2024-1-01", "2024-01-1", "24-01-01", "20240101", "2024/01/01", "", "0000-01-01"
])
def test_invalid_row_dates(bad_date):
    ledger = HEADER + f"{bad_date},A,1,A,\n"
    assert run(ledger, "balances", "{f}") == (2, "", "line 2: bad-date\n")


@pytest.mark.parametrize("amount", [
    "", "0", "0.00", "00.0", "-1", "+1", "1.", ".5", "1.234", "1e2", "1E2",
    "NaN", "inf", "1 2", "1_000", '"1,000"', "--1"
])
def test_invalid_amounts(amount):
    assert run(HEADER + f"2024-01-01,A,{amount},A,\n", "balances", "{f}") == (2, "", "line 2: bad-amount\n")


@pytest.mark.parametrize("amount,cents", [("1", 100), ("1.5", 150), ("1.05", 105), ("0.01", 1), ("0002.50", 250), ("123456789.99", 12345678999)])
def test_valid_amounts(amount, cents):
    ledger = HEADER + f"2024-01-01,A,{amount},B,\n"
    assert json_report(ledger, "balances") == {"balances": [{"name": "A", "cents": cents}, {"name": "B", "cents": -cents}]}


@pytest.mark.parametrize("parts", [
    "", " ", ";", "A;", ";A", "A;;B", "A; ;B", ":1", "A:0", "A:-1", "A:+1",
    "A:1.0", "A:", "A:one", "A:1:2", "A; a ", "A:1;a:2"
])
def test_invalid_participants(parts):
    ledger = HEADER + f"2024-01-01,P,1,{parts},\n"
    assert run(ledger, "balances", "{f}") == (2, "", "line 2: bad-participants\n")


def test_valid_weights_whitespace_and_missing_note_cell():
    ledger = HEADER + '2024-01-01, P , 0.07 , B : 02 ; A : 1\n'
    assert success(ledger, "balances", "{f}") == "A -0.02\nB -0.05\nP +0.07\n"


@pytest.mark.parametrize("header,missing", [
    ("", "date"), ("payer,amount,participants,note", "date"),
    ("date,amount,participants,note", "payer"),
    ("date,payer,participants,note", "amount"),
    ("date,payer,amount,note", "participants"),
    ("date,payer,amount,participants", "note"),
    ("date,note", "payer"),
])
def test_missing_header_column_precedes_row_errors(header, missing):
    ledger = "# ignored\n" + header + "\nnot,a,valid,row\n" if header else "# ignored\n \n"
    assert run(ledger, "balances", "{f}") == (2, "", f"header: missing {missing}\n")


@pytest.mark.parametrize("args", [
    (), ("balances",), ("unknown", "{f}"), ("Balances", "{f}"),
    ("balances", "{f}", "--unknown"), ("balances", "{f}", "extra"),
    ("balances", "{f}", "--format", "xml"), ("balances", "{f}", "--format"),
    ("balances", "{f}", "--since"), ("balances", "{f}", "--until"),
    ("balances", "{f}", "--since", "2023-02-29"),
    ("settle", "{f}", "--until", "2024-1-01"),
    ("settle", "{f}", "--since", "20240101"),
])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64 and out == "" and err


def test_unreadable_file_and_invalid_utf8():
    assert run(HEADER, "balances", "{f}/missing") == (3, "", "error: cannot read {f}/missing\n")
    assert run("", "settle", "{f}", raw=b"\xff\xfe\xff") == (3, "", "error: cannot read {f}\n")


def test_json_options_before_positionals():
    ledger = HEADER + '2024-01-01,A,0.01,B,\n'
    out = success(ledger, "--format", "json", "--since", "2024-01-01", "settle", "{f}")
    assert json.loads(out) == {"transfers": [{"from": "B", "to": "A", "cents": 1}]}


def test_short_rows_are_checked_as_missing_cells():
    ledger = HEADER + (
        '2024-01-01\n'
        '2024-01-01,A\n'
        '2024-01-01,A,1\n'
        '2024-01-01,A,1,A\n'  # note is optional content
    )
    assert run(ledger, 'balances', '{f}') == (
        2, '', 'line 2: bad-payer\nline 3: bad-amount\nline 4: bad-participants\n'
    )


def test_rounding_remainders_beat_alphabetical_order():
    # 7 cents, weights 1:2:3: bases 1,2,3; remainder ranks Z > B > A.
    ledger = HEADER + '2024-01-01,P,0.07,Ann:1;Bob:2;Zed:3,\n'
    assert success(ledger, 'balances', '{f}') == 'Ann -0.01\nBob -0.02\nP +0.07\nZed -0.04\n'


def independently_calculate(rows, since=None, until=None):
    """A rational-arithmetic oracle for the split and the greedy settlement."""
    from fractions import Fraction

    names = {}
    balances = {}
    for date, payer, cents, parts in rows:
        if (since is not None and date < since) or (until is not None and date > until):
            continue
        for name in [payer, *(name for name, _ in parts)]:
            key = name.lower()
            if key not in names:
                names[key] = name
                balances[key] = 0
        balances[payer.lower()] += cents
        total = sum(weight for _, weight in parts)
        exact = [(name.lower(), Fraction(cents * weight, total)) for name, weight in parts]
        floors = {key: int(share) for key, share in exact}
        remaining = cents - sum(floors.values())
        ranked = sorted(exact, key=lambda item: (-(item[1] - int(item[1])), item[0]))
        winners = {key for key, _ in ranked[:remaining]}
        for key, _ in exact:
            balances[key] -= floors[key] + (key in winners)

    ordered = [(names[key], balances[key]) for key in sorted(names)]
    transfers = []
    pending = balances.copy()
    while True:
        debtors = [key for key in pending if pending[key] < 0]
        if not debtors:
            break
        creditors = [key for key in pending if pending[key] > 0]
        debtor = min(debtors, key=lambda key: (pending[key], key))
        creditor = min(creditors, key=lambda key: (-pending[key], key))
        amount = min(-pending[debtor], pending[creditor])
        transfers.append((names[debtor], names[creditor], amount))
        pending[debtor] += amount
        pending[creditor] -= amount
    return ordered, transfers


def signed_amount(cents):
    return ('+' if cents > 0 else '-' if cents < 0 else '') + f'{abs(cents) // 100}.{abs(cents) % 100:02d}'


@pytest.mark.parametrize('seed', [2, 73, 1201, 4019])
def test_generated_ledgers_against_independent_oracle(seed):
    rng = random.Random(seed)
    people = ['Ada', 'bOB', 'Cy', 'dora', 'Eve', 'Fay', 'Gus', 'Hana', 'Ian', 'Zoë', 'Kim, Jr', 'O"Neil']
    rows = []
    for _ in range(90):
        date = datetime.date(2024, 2, 1) + datetime.timedelta(days=rng.randrange(40))
        payer = rng.choice(people)
        payer = rng.choice([payer, payer.upper(), payer.lower()])
        selected = rng.sample(people, rng.randrange(1, 8))
        parts = [(rng.choice([n, n.lower(), n.upper()]), rng.randint(1, 8)) for n in selected]
        cents = rng.choice([1, 2, 3, 5, 7, 11, 99, rng.randrange(1, 100000)])
        rows.append((date, payer, cents, parts))

    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(['date', 'payer', 'amount', 'participants', 'note'])
    for date, payer, cents, parts in rows:
        writer.writerow([date.isoformat(), payer, f'{cents // 100}.{cents % 100:02}',
                         ';'.join(f'{name}:{weight}' for name, weight in parts), 'food, "treats"'])
    ledger = stream.getvalue()
    since, until = datetime.date(2024, 2, 8), datetime.date(2024, 3, 6)
    for low, high in [(None, None), (since, until)]:
        options = ([] if low is None else ['--since', low.isoformat()]) + ([] if high is None else ['--until', high.isoformat()])
        balances, transfers = independently_calculate(rows, low, high)
        assert sum(cents for _, cents in balances) == 0
        assert json_report(ledger, 'balances', *options) == {
            'balances': [{'name': name, 'cents': cents} for name, cents in balances]
        }
        assert json_report(ledger, 'settle', *options) == {
            'transfers': [{'from': debtor, 'to': creditor, 'cents': cents} for debtor, creditor, cents in transfers]
        }
        assert success(ledger, 'balances', '{f}', *options) == ''.join(
            f'{name} {signed_amount(cents)}\n' for name, cents in balances
        )
        expected = ''.join(
            f'{debtor} -> {creditor} {cents // 100}.{cents % 100:02d}\n'
            for debtor, creditor, cents in transfers
        ) or 'nothing to settle\n'
        assert success(ledger, 'settle', '{f}', *options) == expected


def test_participant_with_zero_share_still_appears():
    ledger = HEADER + '2024-01-01,Ann,0.01,zed;Ann,\n'
    assert success(ledger, 'balances', '{f}') == 'Ann 0.00\nzed 0.00\n'
    assert success(ledger, 'settle', '{f}') == 'nothing to settle\n'


def test_large_values_and_weights_use_exact_integer_arithmetic():
    ledger = HEADER + '2024-01-01,P,100000000000000000.01,A:100000000000000000;B:100000000000000001,\n'
    assert json_report(ledger, 'balances') == {'balances': [
        {'name': 'A', 'cents': -4999999999999999976},
        {'name': 'B', 'cents': -5000000000000000025},
        {'name': 'P', 'cents': 10000000000000000001},
    ]}
