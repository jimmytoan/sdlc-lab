"""Thorough test suite for splitr, guarding against regressions."""
import json
import os
import pytest
from helpers import run, lines


def test_bom_and_newlines():
    # Test UTF-8 BOM and CRLF line endings
    raw = b"\xef\xbb\xbfdate,payer,amount,participants,note\r\n2026-03-01,Ann,10.00,Ann;Bob,\r\n"
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_ignored_lines_and_line_numbers():
    led = """# Comment line
    
   
date,payer,amount,participants,note
# another comment
2026-03-01,Ann,abc,Ann;Bob, bad amount on line 6
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 6: bad-amount"]


def test_column_order_and_quoting():
    led = """participants,note,amount,payer,date
"Ann; Bob","comma, in note",20.00,Ann,2026-03-05
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_name_case_insensitive_and_spelling():
    # First appearance spelling should be preserved
    led = """date,payer,amount,participants,note
2026-03-01,aNn,10.00,ANN;bOb,
2026-03-02,Bob,10.00,ann;BOB,
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    # "aNn" first appeared as payer, "bOb" first appeared as participant in row 1
    # Zero balance prints as 0.00 with no sign
    assert lines(out) == ["aNn 0.00", "bOb 0.00"]


def test_date_validation():
    # bad-date: invalid format or non-existent date
    for bad_d in ["2026-02-30", "2025-02-29", "2026-13-01", "2026-00-01", "03-01-2026", "invalid"]:
        led = f"date,payer,amount,participants,note\n{bad_d},Ann,10.00,Ann;Bob,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-date"]


def test_payer_validation():
    for bad_p in ["", "   "]:
        led = f"date,payer,amount,participants,note\n2026-03-01,{bad_p},10.00,Ann;Bob,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-payer"]


def test_amount_validation():
    for bad_a in ["0", "0.0", "0.00", "-10", "10.", "10.000", '"12,34"', "abc", ""]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,{bad_a},Ann;Bob,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-amount"]


def test_participants_validation():
    # empty, empty name, bad weight (0, negative, non-integer), duplicate
    bad_participants = [
        "",
        "   ",
        ";Ann",
        "Ann;",
        "Ann;;Bob",
        "Ann:0",
        "Ann:-1",
        "Ann:1.5",
        "Ann:abc",
        "Ann;Ann",
        "Ann;ann",
        "Ann; ANN",
    ]
    for bad_part in bad_participants:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,10.00,{bad_part},\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-participants"]


def test_validation_error_priority():
    # Priority order: bad-date, bad-payer, bad-amount, bad-participants
    # If date and amount are both bad, date should be reported.
    led = """date,payer,amount,participants,note
bad-date,  ,abc,Ann:0,
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_multiple_invalid_rows_ordered():
    led = """date,payer,amount,participants,note
2026-03-01,Ann,abc,Ann;Bob,
2026-02-30,Ann,10.00,Ann;Bob,
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount", "line 3: bad-date"]


def test_header_missing_column():
    # Required: date, payer, amount, participants, note
    # Missing order: date, payer, amount, participants, note
    for missing, expected_err in [
        ("payer,amount,participants,note", "header: missing date"),
        ("date,amount,participants,note", "header: missing payer"),
        ("date,payer,participants,note", "header: missing amount"),
        ("date,payer,amount,note", "header: missing participants"),
        ("date,payer,amount,participants", "header: missing note"),
    ]:
        led = f"{missing}\n2026-03-01,Ann,10.00,Ann;Bob,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == [expected_err]


def test_multiple_missing_headers_reports_first():
    # If both date and payer are missing, report first missing in order: date
    led = "amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing date"]


def test_file_read_error():
    code, out, err = run("", "balances", "nonexistent_file_12345.csv")
    assert code == 3 and out == ""
    assert "error: cannot read" in err


def test_usage_errors():
    led = "date,payer,amount,participants,note\n"
    for args in [
        ["unknown_cmd", "{f}"],
        ["balances"],  # missing ledger
        ["balances", "{f}", "--format", "badformat"],
        ["balances", "{f}", "--since", "bad-date"],
        ["balances", "{f}", "--until", "bad-date"],
        ["balances", "{f}", "--unknown-opt"],
    ]:
        code, out, err = run(led, *args)
        assert code == 64 and out == ""
        assert "usage error" in err


def test_options_position():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    # Format option before command/ledger or after
    code1, out1, _ = run(led, "--format", "json", "balances", "{f}")
    code2, out2, _ = run(led, "balances", "{f}", "--format", "json")
    assert code1 == 0 and code2 == 0
    assert out1 == out2


def test_since_until_filtering():
    led = """date,payer,amount,participants,note
2026-02-28,Ann,10.00,Ann;Bob,out of range
2026-03-01,Ann,10.00,Ann;Bob,in range
2026-03-10,Bob,10.00,Bob;Charlie,in range
2026-03-15,Charlie,10.00,Charlie;Ann,out of range
2026-03-02,,10.00,Ann;Bob,validation still runs even out of range
"""
    # Validation should fail due to line 6 (bad-payer) even if filtered by since/until
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-05")
    assert code == 2 and out == ""
    assert lines(err) == ["line 6: bad-payer"]

    # When valid within range:
    valid_led = """date,payer,amount,participants,note
2026-02-28,Ann,100.00,Ann;Bob,out
2026-03-01,Ann,10.00,Ann;Bob,in
2026-03-02,Bob,20.00,Ann;Bob,in
2026-03-20,Charlie,50.00,Charlie;Ann,out
"""
    code, out, err = run(valid_led, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-05")
    assert code == 0 and err == ""
    # Only Ann and Bob should appear, Charlie only appeared outside range
    assert lines(out) == ["Ann -5.00", "Bob +5.00"]


def test_json_format_balances():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"balances": [
        {"name": "Ann", "cents": 500}, {"name": "Bob", "cents": -500}]}


def test_json_format_settle():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}

    # Nothing to settle json
    empty_led = "date,payer,amount,participants,note\n"
    code, out, _ = run(empty_led, "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": []}


def test_settle_tie_breaking():
    led = """date,payer,amount,participants,note
2026-03-01,Ann,20.00,Ann;Bob;Charlie,
"""
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Bob -> Ann 6.67", "Charlie -> Ann 6.66"]


def test_remainder_distribution_and_tie_breaking():
    led = "date,payer,amount,participants,note\n2026-03-01,Zed,10.00,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_empty_ledger_valid():
    led = "date,payer,amount,participants,note\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == "" and out.strip() == ""
