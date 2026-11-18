"""Black-box regression tests for the settlement CLI."""
import csv
import io
import json
import random

import pytest

from helpers import run


HEADER = "date,payer,amount,participants,note\n"


def csv_ledger(rows, header=("date", "payer", "amount", "participants", "note")):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue()


def success(ledger, *args, raw=None):
    code, out, err = run(ledger, *args, raw=raw)
    assert (code, err) == (0, "")
    return out


def report(ledger, command, *options):
    return json.loads(success(ledger, command, "{f}", "--format", "json", *options))


def test_csv_bom_comments_reordered_header_and_quoted_fields():
    ledger = ('\ufeff  # before header\r\n\r\n'
              'note,participants,amount,payer,date\r\n'
              '"dinner, with ""friends""",  bob ; ALICE  ,1.01, Alice ,2024-02-29\r\n'
              '   # ignored,even,with,commas\r\n'
              '"",alice; BOB,1,Bob,2024-03-01\r\n')
    assert success(ledger, "balances", "{f}") == "Alice 0.00\nbob 0.00\n"
    assert report(ledger, "balances") == {"balances": [
        {"name": "Alice", "cents": 0}, {"name": "bob", "cents": 0}]}


def test_first_spelling_payer_before_participants_and_sorting():
    ledger = csv_ledger([
        ("2025-01-01", " zED ", "0.01", "Zed; bOB ;alice", ""),
        ("2025-01-02", "BOB", "1", "ALICE;zed", ""),
        ("2025-01-03", "Alice", "2", "alice", ""),
    ])
    assert report(ledger, "balances") == {"balances": [
        {"name": "alice", "cents": -51},
        {"name": "bOB", "cents": 100},
        {"name": "zED", "cents": -49},
    ]}


def test_largest_remainders_precede_alphabetical_order():
    ledger = csv_ledger([("2025-01-01", "Payer", "0.05", "Amy:1;Zoe:2;Bob:3", "")])
    # Bases 0, 1, 2, remainders 5, 4, 3: Amy and Zoe get the cents.
    assert report(ledger, "balances") == {"balances": [
        {"name": "Amy", "cents": -1}, {"name": "Bob", "cents": -2},
        {"name": "Payer", "cents": 5}, {"name": "Zoe", "cents": -2}]}


def test_remainder_ties_are_case_insensitive_not_input_order():
    ledger = csv_ledger([("2025-01-01", "P", "0.02", "zoe;BOB;amy", "")])
    assert success(ledger, "balances", "{f}") == (
        "amy -0.01\nBOB -0.01\nP +0.02\nzoe 0.00\n")


def test_settlement_reselects_largest_and_breaks_both_ties_by_name():
    ledger = csv_ledger([
        ("2025-01-01", "Zoe", "0.07", "debB", ""),
        ("2025-01-01", "Ann", "0.07", "debA", ""),
        ("2025-01-01", "Cy", "0.05", "debC", ""),
        ("2025-01-01", "debC", "0.03", "debA", ""),
    ])
    expected = [
        {"from": "debA", "to": "Ann", "cents": 7},
        {"from": "debB", "to": "Zoe", "cents": 7},
        {"from": "debA", "to": "Cy", "cents": 3},
        {"from": "debC", "to": "Cy", "cents": 2},
    ]
    assert report(ledger, "settle") == {"transfers": expected}
    assert success(ledger, "settle", "{f}") == (
        "debA -> Ann 0.07\ndebB -> Zoe 0.07\ndebA -> Cy 0.03\ndebC -> Cy 0.02\n")


@pytest.mark.parametrize("command,expected", [
    ("balances", ""), ("settle", "nothing to settle\n")])
def test_empty_ledger(command, expected):
    assert success(HEADER + "\n  # no expenses\n", command, "{f}") == expected
    data = report(HEADER, command)
    assert data == ({"balances": []} if command == "balances" else {"transfers": []})


def test_zero_balances_are_printed_but_no_transfers():
    ledger = csv_ledger([
        ("2025-01-01", "Ann", "5.00", "Ann", ""),
        ("2025-01-02", "Bob", "0.01", "Bob;Cy", ""),
    ])
    assert success(ledger, "balances", "{f}") == "Ann 0.00\nBob 0.00\nCy 0.00\n"
    assert success(ledger, "settle", "{f}") == "nothing to settle\n"
    assert report(ledger, "settle") == {"transfers": []}


FILTER_LEDGER = csv_ledger([
    ("2024-12-31", "Old", "10", "old;Outside", ""),
    ("2025-01-01", "First", "1", "A", ""),
    ("2025-01-02", "Middle", "2", "A", ""),
    ("2025-01-03", "Last", "3", "A", ""),
    ("2025-01-04", "Future", "4", "A", ""),
])


@pytest.mark.parametrize("options,names,cents", [
    (("--since", "2025-01-02"), ["A", "Future", "Last", "Middle"], [-900, 400, 300, 200]),
    (("--until", "2025-01-02"), ["A", "First", "Middle", "Old", "Outside"], [-300, 100, 200, 500, -500]),
    (("--since", "2025-01-01", "--until", "2025-01-03"), ["A", "First", "Last", "Middle"], [-600, 100, 300, 200]),
    (("--since", "2026-01-01"), [], []),
])
def test_inclusive_filters_and_people_scope(options, names, cents):
    assert report(FILTER_LEDGER, "balances", *options) == {"balances": [
        {"name": n, "cents": c} for n, c in zip(names, cents)]}


def test_filters_apply_to_settlement_and_options_can_precede_positionals():
    out = success(FILTER_LEDGER, "--since", "2025-01-02", "--format", "json",
                  "settle", "{f}", "--until", "2025-01-03")
    assert json.loads(out) == {"transfers": [
        {"from": "A", "to": "Last", "cents": 300},
        {"from": "A", "to": "Middle", "cents": 200}]}


def test_filtered_out_rows_are_still_validated():
    ledger = HEADER + ("2020-01-01,A,0,A,\n"
                       "2025-01-01,B,1,B,\n"
                       "2030-01-01,C,1,C:0,\n")
    for cmd in ("balances", "settle"):
        code, out, err = run(ledger, cmd, "{f}", "--since", "2025-01-01", "--until", "2025-01-01")
        assert (code, out, err) == (2, "", "line 2: bad-amount\nline 4: bad-participants\n")


def test_invalid_rows_in_physical_order_and_first_error_only():
    ledger = ("# comment before header\n\n" + HEADER +
              "# ignored\n"
              "not-a-date, ,bad,,\n"
              "2025-02-29, ,bad,,\n"
              "2024-02-29, ,bad,,\n"
              "   \n"
              "2025-01-01,A,bad,,\n"
              "2025-01-01,A,1,,\n"
              "2025-01-01,A,1,A,\n")
    for command in ("balances", "settle"):
        assert run(ledger, command, "{f}") == (2, "", (
            "line 5: bad-date\nline 6: bad-date\nline 7: bad-payer\n"
            "line 9: bad-amount\nline 10: bad-participants\n"))


def test_invalid_dates():
    values = ["", "2025-02-29", "2024-02-30", "2025-13-01", "2025-00-01",
              "2025-01-00", "2025-04-31", "25-01-01", "2025-1-01", "2025-01-1",
              "2025/01/01", "20250101", "abcd-ef-gh"]
    ledger = csv_ledger([(v, "A", "1", "A", "") for v in values])
    assert run(ledger, "balances", "{f}") == (2, "", "".join(
        f"line {i}: bad-date\n" for i in range(2, len(values) + 2)))


def test_invalid_amounts():
    values = ["", "0", "0.00", "0.0", "-1", "+1", "1.001", "1.000", "1e2",
              "1E2", "NaN", "inf", ".50", "1.", "1,000", "1 2", "abc"]
    ledger = csv_ledger([("2025-01-01", "A", v, "A", "") for v in values])
    assert run(ledger, "balances", "{f}") == (2, "", "".join(
        f"line {i}: bad-amount\n" for i in range(2, len(values) + 2)))


def test_invalid_participants():
    values = ["", " ", ";", "A;", ";A", "A;;B", "A; a ",
              "A:0", "A:-1", "A:+1", "A:1.5", "A:x", "A:", ":2", " :3", "A:1:2"]
    ledger = csv_ledger([("2025-01-01", "A", "1", v, "") for v in values])
    assert run(ledger, "settle", "{f}") == (2, "", "".join(
        f"line {i}: bad-participants\n" for i in range(2, len(values) + 2)))


@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_each_required_header_column(missing):
    cols = [c for c in ("date", "payer", "amount", "participants", "note") if c != missing]
    ledger = csv_ledger([["bad"] * len(cols)], cols)
    assert run(ledger, "balances", "{f}") == (2, "", f"header: missing {missing}\n")


def test_first_missing_header_column_takes_precedence_over_rows():
    assert run("note,amount\ninvalid,invalid\n", "settle", "{f}") == (
        2, "", "header: missing date\n")
    assert run("date,note\ninvalid,whatever\n", "balances", "{f}") == (
        2, "", "header: missing payer\n")


def test_unreadable_file():
    assert run(HEADER, "balances", "{f}/missing") == (3, "", "error: cannot read {f}/missing\n")


@pytest.mark.parametrize("args", [
    (), ("balances",), ("{f}",), ("unknown", "{f}"),
    ("balances", "{f}", "--unknown"), ("balances", "{f}", "extra"),
    ("balances", "{f}", "--format", "xml"), ("balances", "{f}", "--format"),
    ("balances", "{f}", "--since", "2025-02-29"),
    ("settle", "{f}", "--until", "not-a-date"),
    ("settle", "{f}", "--since"),
    ("settle", "{f}", "--until", "2025-1-01"),
])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64 and out == "" and err


def model(rows):
    """Independent integer model of identity, apportionment and greedy transfers."""
    display, balances = {}, {}
    def person(name):
        name = name.strip()
        key = name.lower()
        display.setdefault(key, name)
        balances.setdefault(key, 0)
        return key

    for payer, amount, participants in rows:
        payer_key = person(payer)
        keys = [(person(name), weight) for name, weight in participants]
        balances[payer_key] += amount
        total = sum(w for _, w in keys)
        shares = {key: amount * weight // total for key, weight in keys}
        remainder_order = sorted(keys, key=lambda kw: (-(amount * kw[1] % total), kw[0]))
        leftover = amount - sum(shares.values())
        for key, _ in remainder_order[:leftover]:
            shares[key] += 1
        for key, share in shares.items():
            balances[key] -= share
    balances_output = [{"name": display[k], "cents": balances[k]} for k in sorted(balances)]
    working = balances.copy()
    transfers = []
    while any(v < 0 for v in working.values()):
        debtor = min((k for k in working if working[k] < 0), key=lambda k: (working[k], k))
        creditor = min((k for k in working if working[k] > 0), key=lambda k: (-working[k], k))
        amount = min(-working[debtor], working[creditor])
        transfers.append({"from": display[debtor], "to": display[creditor], "cents": amount})
        working[debtor] += amount
        working[creditor] -= amount
    return {"balances": balances_output}, {"transfers": transfers}


def test_deterministic_mixed_ledgers_against_integer_model():
    rng = random.Random(20250212)
    names = ["Amy", "bob", "CHRIS", "Dee", "Eve", "fred", "Gus"]
    for case in range(12):
        rows, csv_rows = [], []
        for _ in range(rng.randint(3, 12)):
            payer = rng.choice(names)
            participants = [(n, rng.randint(1, 7)) for n in rng.sample(names, rng.randint(1, 6))]
            amount = rng.randint(1, 4000)
            rows.append((payer, amount, participants))
            csv_rows.append(("2025-01-01", payer, f"{amount // 100}.{amount % 100:02d}",
                             ";".join(f"{n}:{w}" for n, w in participants), f"case {case}"))
        ledger = csv_ledger(csv_rows)
        expected_balances, expected_transfers = model(rows)
        assert report(ledger, "balances") == expected_balances
        assert report(ledger, "settle") == expected_transfers
        def money(cents):
            return f"{abs(cents) // 100}.{abs(cents) % 100:02d}"
        text_balances = "".join(
            f"{b['name']} {'+' if b['cents'] > 0 else '-' if b['cents'] < 0 else ''}{money(b['cents'])}\n"
            for b in expected_balances['balances'])
        text_transfers = "".join(
            f"{t['from']} -> {t['to']} {money(t['cents'])}\n"
            for t in expected_transfers['transfers']) or "nothing to settle\n"
        assert success(ledger, "balances", "{f}", "--format", "text") == text_balances
        assert success(ledger, "settle", "{f}") == text_transfers


def test_exact_decimal_amounts_and_large_integer_arithmetic():
    ledger = csv_ledger([
        ("2024-02-29", "Fund", "12", "One", ""),
        ("2024-03-01", "Fund", "12.5", "One", ""),
        ("2024-03-02", "Fund", "12.50", "One", ""),
        ("2024-03-03", "Fund", "0.01", "One", ""),
        ("2024-03-04", "Fund", "90071992547409.93", "One", ""),
    ])
    total = 9007199254740993 + 1200 + 1250 + 1250 + 1
    assert report(ledger, "balances") == {"balances": [
        {"name": "Fund", "cents": total}, {"name": "One", "cents": -total}]}
    assert report(ledger, "settle") == {"transfers": [
        {"from": "One", "to": "Fund", "cents": total}]}


def test_quoted_names_with_commas_and_quoted_notes():
    ledger = csv_ledger([
        ("2025-01-01", " Smith, Jane ", "10.01", 'Smith, Jane;O\u2019Neil', 'a "quoted", note'),
    ])
    assert success(ledger, "balances", "{f}") == "O\u2019Neil -5.01\nSmith, Jane +5.01\n"
    assert report(ledger, "settle") == {"transfers": [
        {"from": "O\u2019Neil", "to": "Smith, Jane", "cents": 501}]}


def test_participant_order_does_not_decide_cent_ties():
    for participants in ("Zed;Bob;Amy", "Amy;Zed;Bob", "Bob;Amy;Zed"):
        ledger = csv_ledger([("2025-01-01", "P", "0.02", participants, "")])
        assert report(ledger, "balances") == {"balances": [
            {"name": "Amy", "cents": -1}, {"name": "Bob", "cents": -1},
            {"name": "P", "cents": 2}, {"name": "Zed", "cents": 0}]}


def test_weighted_duplicate_names_are_invalid_and_whitespace_payer_is_invalid():
    ledger = csv_ledger([
        ("2025-01-01", "\t ", "1", "A", ""),
        ("2025-01-01", "A", "1", " Alice:2 ; ALICE:3", ""),
        ("2025-01-01", "A", "1", "A:1;B:1; a", ""),
    ])
    assert run(ledger, "balances", "{f}") == (
        2, "", "line 2: bad-payer\nline 3: bad-participants\nline 4: bad-participants\n")
