import csv
import datetime
import io
import json
import random

import pytest

from helpers import run


HEADER = "date,payer,amount,participants,note\n"


def cli(ledger, command="balances", *options, raw=None):
    return run(ledger, command, "{f}", *options, raw=raw)


def success(ledger, command="balances", *options):
    code, out, err = cli(ledger, command, *options)
    assert (code, err) == (0, "")
    return out


def json_report(ledger, command, *options):
    return json.loads(success(ledger, command, *options, "--format", "json"))


def row(date="2024-02-29", payer="Ann", amount="1.00", participants="Ann;Bob", note=""):
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerow([date, payer, amount, participants, note])
    return stream.getvalue()


def test_csv_bom_comments_crlf_order_whitespace_and_names():
    ledger = ('\ufeff  # first comment\r\n\r\n'
              ' note , participants , amount , payer , date , extra\r\n'
              '"food, and ""drink""",  bOB ; ALICE , 1.01 , Alice , 2024-02-29 , x\r\n'
              '   # ignored\r\n'
              'taxi,alice; CAROL,2.00, BOB ,2024-03-01,z\r\n')
    assert success(ledger) == "Alice -0.50\nbOB +1.50\nCAROL -1.00\n"
    assert json_report(ledger, "balances") == {"balances": [
        {"name": "Alice", "cents": -50}, {"name": "bOB", "cents": 150},
        {"name": "CAROL", "cents": -100}]}


def test_first_appearance_payer_then_participants_and_zero_people():
    ledger = HEADER + row(payer="  zOE ", amount="0.01", participants="zoe; ALICE; Bob")
    ledger += row(payer="alice", amount="2", participants="ALICE")
    assert success(ledger) == "ALICE -0.01\nBob 0.00\nzOE +0.01\n"
    assert success(ledger, "settle") == "ALICE -> zOE 0.01\n"


def test_largest_remainder_not_participant_order_or_payer_priority():
    ledger = HEADER + row(payer="Zed", amount="0.05", participants="Zed:1;Bob:2;Ann:3")
    # Bases 0, 1, 2; remainders 5, 4, 3. Two cents go to Zed and Bob.
    assert success(ledger) == "Ann -0.02\nBob -0.02\nZed +0.04\n"
    ledger = HEADER + row(payer="Zed", amount="0.02", participants="Zed;bob;Ann")
    assert success(ledger) == "Ann -0.01\nbob -0.01\nZed +0.02\n"


def test_weighted_remainder_beats_alphabetical_order():
    ledger = HEADER + row(payer="Payer", amount="0.02", participants="Ann:1;Zoe:2")
    assert success(ledger) == "Ann -0.01\nPayer +0.02\nZoe -0.01\n"
    ledger = HEADER + row(payer="Payer", amount="0.01", participants="Ann:1;Zoe:2")
    assert success(ledger) == "Ann 0.00\nPayer +0.01\nZoe -0.01\n"


def test_settlement_recomputes_extremes_and_breaks_both_ties():
    ledger = HEADER + row(payer="Zoe", amount="0.08", participants="deb:1;Cal:1")
    ledger += row(payer="Amy", amount="0.08", participants="Deb:1;cal:1")
    ledger += row(payer="Bea", amount="0.03", participants="Eve")
    assert success(ledger) == ("Amy +0.08\nBea +0.03\nCal -0.08\ndeb -0.08\n"
                               "Eve -0.03\nZoe +0.08\n")
    transfers = [{"from": "Cal", "to": "Amy", "cents": 8},
                 {"from": "deb", "to": "Zoe", "cents": 8},
                 {"from": "Eve", "to": "Bea", "cents": 3}]
    assert json_report(ledger, "settle") == {"transfers": transfers}
    assert success(ledger, "settle") == "Cal -> Amy 0.08\ndeb -> Zoe 0.08\nEve -> Bea 0.03\n"


def test_settlement_partial_payment_then_new_extremes():
    ledger = HEADER + row(payer="A", amount="0.10", participants="X")
    ledger += row(payer="B", amount="0.07", participants="Y")
    ledger += row(payer="B", amount="0.02", participants="X")
    # A +10 B +9 X -12 Y -7: X pays A, then Y pays B, then X pays B.
    assert success(ledger, "settle") == "X -> A 0.10\nY -> B 0.07\nX -> B 0.02\n"


@pytest.mark.parametrize("ledger", [HEADER, HEADER + "\n # comment\n", HEADER + row(payer="Solo", participants="solo")])
def test_empty_or_zero_settlement(ledger):
    assert success(ledger, "settle") == "nothing to settle\n"
    assert json_report(ledger, "settle") == {"transfers": []}
    if "Solo" in ledger:
        assert success(ledger) == "Solo 0.00\n"
    else:
        assert success(ledger) == ""
        assert json_report(ledger, "balances") == {"balances": []}


def test_filter_inclusive_single_bounds_and_excludes_names():
    ledger = HEADER + row(date="2024-01-01", payer="Old", participants="Nobody")
    ledger += row(date="2024-01-02", payer="First", participants="Middle")
    ledger += row(date="2024-01-03", payer="Last", participants="Middle", amount="2")
    ledger += row(date="2024-01-04", payer="Future", participants="Other")
    expected = "First +1.00\nLast +2.00\nMiddle -3.00\n"
    assert success(ledger, "balances", "--since", "2024-01-02", "--until", "2024-01-03") == expected
    assert success(ledger, "settle", "--since", "2024-01-02", "--until", "2024-01-03") == "Middle -> Last 2.00\nMiddle -> First 1.00\n"
    assert "Old" not in success(ledger, "balances", "--since", "2024-01-02")
    assert "Future" not in success(ledger, "balances", "--until", "2024-01-03")
    assert success(ledger, "balances", "--since", "2024-01-04", "--until", "2024-01-02") == ""
    assert success(ledger, "settle", "--since", "2030-01-01") == "nothing to settle\n"


def test_filtered_rows_do_not_choose_output_spelling():
    ledger = HEADER + row(date="2023-01-01", payer="ALICE", participants="BOB")
    ledger += row(date="2024-01-01", payer="alice", participants="bob")
    assert success(ledger, "balances", "--since", "2024-01-01") == "alice +1.00\nbob -1.00\n"


def test_options_before_and_between_positionals():
    ledger = HEADER + row(payer="A", participants="B")
    expected = {"balances": [{"name": "A", "cents": 100}, {"name": "B", "cents": -100}]}
    for args in [("--format", "json", "balances", "{f}"),
                 ("balances", "--format=json", "{f}"),
                 ("--since", "2024-02-29", "balances", "{f}", "--format", "json")]:
        code, out, err = run(ledger, *args)
        assert (code, err) == (0, "")
        assert json.loads(out) == expected


@pytest.mark.parametrize("value", ["", "0", "0.0", "0.00", "-1", "+1", ".5", "1.", "1.001", "1e2", "1,000", "1 2", "NaN", "inf"])
def test_invalid_amounts(value):
    code, out, err = cli(HEADER + row(amount=value))
    assert (code, out, err) == (2, "", "line 2: bad-amount\n")


@pytest.mark.parametrize("value,cents", [("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), ("0001.02", 102), ("  7.1  ", 710), ("12345678901234567890.99", 1234567890123456789099)])
def test_valid_amounts_exact_cents(value, cents):
    ledger = HEADER + row(payer="A", participants="B", amount=value)
    assert json_report(ledger, "balances") == {"balances": [{"name": "A", "cents": cents}, {"name": "B", "cents": -cents}]}


@pytest.mark.parametrize("date", ["", "2024-2-01", "2024-02-1", "2024-02-30", "2023-02-29", "2024-13-01", "2024-00-01", "2024-01-00", "2024/01/01", "0000-01-01", "2024-01-01x"])
def test_invalid_dates(date):
    assert cli(HEADER + row(date=date)) == (2, "", "line 2: bad-date\n")


@pytest.mark.parametrize("participants", ["", "  ", ";Ann", "Ann;", "Ann;;Bob", "Ann; ;Bob", ":2", "Ann:", "Ann:0", "Ann:-1", "Ann:+1", "Ann:1.0", "Ann:1:2", "Ann:x", "Ann;ann", "Ann:2; ANN:3"])
def test_invalid_participants(participants):
    assert cli(HEADER + row(participants=participants)) == (2, "", "line 2: bad-participants\n")


def test_weight_and_name_whitespace_valid():
    ledger = HEADER + row(payer="  Payer  ", amount="0.06", participants=" Bob : 002 ; ann : 1 ")
    assert success(ledger) == "ann -0.02\nBob -0.04\nPayer +0.06\n"


def test_error_order_line_numbers_and_validate_even_outside_filter():
    ledger = ("# preface\n\n" + HEADER +
              row(date="bad", payer="", amount="bad", participants="") +
              "  # comment\n" + row(payer=" ", amount="bad", participants="") +
              row(amount="bad", participants="") + "\n" + row(participants="") +
              row(date="2020-01-01", amount="0") + row(date="2030-01-01", participants="X;x") +
              row(date="2024-01-01", payer="Good", participants="Good"))
    expected = ("line 4: bad-date\nline 6: bad-payer\nline 7: bad-amount\n"
                "line 9: bad-participants\nline 10: bad-amount\nline 11: bad-participants\n")
    for command in ("balances", "settle"):
        assert cli(ledger, command, "--since", "2024-01-01", "--until", "2024-12-31") == (2, "", expected)


def test_short_rows_and_extra_cells():
    ledger = HEADER + "2024-01-01\n2024-01-01,A\n2024-01-01,A,1\n"
    assert cli(ledger) == (2, "", "line 2: bad-payer\nline 3: bad-amount\nline 4: bad-participants\n")
    assert success(HEADER + "2024-01-01,A,1,B,,ignored\n") == "A +1.00\nB -1.00\n"


@pytest.mark.parametrize("columns,missing", [
    ([], "date"), (["payer", "amount", "participants", "note"], "date"),
    (["date", "participants", "note"], "payer"),
    (["date", "payer", "note"], "amount"),
    (["date", "payer", "amount"], "participants"),
    (["date", "payer", "amount", "participants"], "note"),
    (["Date", "payer", "amount", "participants", "note"], "date"),
])
def test_missing_header_priority(columns, missing):
    ledger = ",".join(columns) + "\ninvalid,row\n" if columns else "  \n# only comments\n"
    assert cli(ledger) == (2, "", f"header: missing {missing}\n")


def test_unreadable_file_and_invalid_utf8():
    code, out, err = run("", "balances", "{f}.missing")
    assert (code, out, err) == (3, "", "error: cannot read {f}.missing\n")
    assert cli("", raw=b"\xff\xfe") == (3, "", "error: cannot read {f}\n")


@pytest.mark.parametrize("args", [(), ("balances",), ("oops", "{f}"), ("balances", "{f}", "extra"),
    ("balances", "{f}", "--unknown"), ("balances", "{f}", "--format", "xml"),
    ("balances", "{f}", "--format"), ("balances", "{f}", "--since", "2023-02-29"),
    ("balances", "{f}", "--until", "2024-1-01"), ("balances", "{f}", "--until"),
    ("--help",)])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64 and out == "" and err


def test_generated_ledgers_against_independent_cents_oracle():
    rng = random.Random(491833)
    # Vary case, weights, ordering, dates, and ties. Compute each share with divmod,
    # then derive settlement afresh after each transfer.
    people = ["Alice", "bob", "CY", "dana", "Eve", "Frank"]
    for trial in range(12):
        rows = []
        first = {}
        balances = {}
        ledger = HEADER
        for _ in range(18):
            date = datetime.date(2024, 1, rng.randint(1, 5)).isoformat()
            payer = rng.choice(people)
            participants = rng.sample(people, rng.randint(1, len(people)))
            weights = [rng.randint(1, 5) for _ in participants]
            amount = rng.randint(1, 513)
            spell = lambda n: n.upper() if rng.randrange(2) else n
            payer = spell(payer)
            participants = [spell(p) for p in participants]
            ledger += row(date=date, payer=payer, amount=f"{amount // 100}.{amount % 100:02d}",
                          participants=";".join(f"{p}:{w}" for p, w in zip(participants, weights)))
            rows.append((date, payer, amount, list(zip(participants, weights))))
        for date, payer, amount, parts in rows:
            if not "2024-01-02" <= date <= "2024-01-04":
                continue
            for name in [payer] + [p for p, _ in parts]:
                key = name.lower()
                first.setdefault(key, name)
                balances.setdefault(key, 0)
            balances[payer.lower()] += amount
            total = sum(w for _, w in parts)
            quotients = [(p.lower(), *divmod(amount * w, total)) for p, w in parts]
            leftovers = amount - sum(q for _, q, _ in quotients)
            bonus = {name for name, _, _ in sorted(quotients, key=lambda x: (-x[2], x[0]))[:leftovers]}
            for name, q, _ in quotients:
                balances[name] -= q + (name in bonus)
        assert sum(balances.values()) == 0
        opts = ("--since", "2024-01-02", "--until", "2024-01-04")
        expected_balances = [{"name": first[k], "cents": balances[k]} for k in sorted(balances)]
        report = json_report(ledger, "balances", *opts)
        assert report == {"balances": expected_balances}, trial
        assert all(type(entry["cents"]) is int for entry in report["balances"])
        def text_amount(cents, signed=False):
            sign = ("+" if cents > 0 else "-") if signed and cents else ""
            return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"
        expected_text = "".join(f"{item['name']} {text_amount(item['cents'], True)}\n" for item in expected_balances)
        assert success(ledger, "balances", *opts) == expected_text
        transfers = []
        while any(balances.values()):
            debtor = min(balances, key=lambda k: (balances[k], k))
            creditor = min(balances, key=lambda k: (-balances[k], k))
            cents = min(-balances[debtor], balances[creditor])
            transfers.append({"from": first[debtor], "to": first[creditor], "cents": cents})
            balances[debtor] += cents
            balances[creditor] -= cents
        report = json_report(ledger, "settle", *opts)
        assert report == {"transfers": transfers}, trial
        assert all(type(entry["cents"]) is int for entry in report["transfers"])
        expected_text = "".join(f"{t['from']} -> {t['to']} {text_amount(t['cents'])}\n" for t in transfers)
        assert success(ledger, "settle", *opts) == (expected_text or "nothing to settle\n")


def test_quoted_names_and_notes_are_csv_fields():
    ledger = HEADER + row(payer='  #Alice, Jr  ', participants='bob smith; #ALICE, JR',
                          amount='1.01', note='he said "hello", then left')
    assert success(ledger) == '#Alice, Jr +0.50\nbob smith -0.50\n'
    assert json_report(ledger, 'settle') == {'transfers': [
        {'from': 'bob smith', 'to': '#Alice, Jr', 'cents': 50}]}


def test_multiple_expenses_cancel_balances_but_people_remain():
    ledger = HEADER + row(payer='Alice', participants='Bob', amount='123.45')
    ledger += row(payer='bOB', participants='ALICE', amount='123.45')
    assert success(ledger) == 'Alice 0.00\nBob 0.00\n'
    assert json_report(ledger, 'balances') == {'balances': [
        {'name': 'Alice', 'cents': 0}, {'name': 'Bob', 'cents': 0}]}
    assert success(ledger, 'settle') == 'nothing to settle\n'


def test_transfer_format_large_amount_and_partial_cents():
    ledger = HEADER + row(payer='Payee', participants='Debtor', amount='1234.05')
    assert success(ledger, 'settle') == 'Debtor -> Payee 1234.05\n'
    assert success(ledger) == 'Debtor -1234.05\nPayee +1234.05\n'


def test_missing_note_cell_is_valid():
    assert success(HEADER + '2024-02-29,A,1,B\n') == 'A +1.00\nB -1.00\n'


def test_ignored_lines_and_crlf_count_as_physical_lines():
    ledger = '\r\n# comment\r\n' + HEADER.replace('\n', '\r\n')
    ledger += '  \r\n2024-02-30,A,1,B,\r\n  # another\r\n2024-01-01,A,0,B,\r\n'
    assert cli(ledger) == (2, '', 'line 5: bad-date\nline 7: bad-amount\n')


def test_extra_kind_column_has_no_special_semantics():
    ledger = ('date,payer,amount,participants,note,kind\n'
              '2024-01-01,A,0.03,A;B,,payment\n'
              '2024-01-02,B,0.03,A;B,,anything\n')
    assert success(ledger) == 'A -0.01\nB +0.01\n'
    assert success(ledger, 'settle') == 'A -> B 0.01\n'


def test_eof_without_newline_and_bom_on_header():
    ledger = '\ufeff' + HEADER + '2024-02-29,A,0.01,B,'
    assert success(ledger) == 'A +0.01\nB -0.01\n'
