"""Thorough test suite for splitr guarding against regressions."""
import json
import os
import subprocess
import tempfile
from helpers import run, lines


def test_bom_and_crlf():
    led = "\ufeffdate,payer,amount,participants,note\r\n2026-03-01,Ann,10.00,Ann;Bob,\r\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_csv_quoting():
    led = 'date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,"dinner, drinks"\n'
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_ignored_lines_and_line_numbering():
    led = """# Comment line 1
    
date,payer,amount,participants,note
# Comment line 4
2026-03-01,Ann,abc,Ann;Bob,bad amount on line 5
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 5: bad-amount"]


def test_column_order():
    led = "note,participants,amount,payer,date\ndinner,Ann;Bob,10.00,Ann,2026-03-01\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_missing_header_columns():
    for i, col in enumerate(["date", "payer", "amount", "participants", "note"]):
        cols = ["date", "payer", "amount", "participants", "note"]
        cols.pop(i)
        led = ",".join(cols) + "\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == [f"header: missing {col}"]


def test_validation_error_order_in_row():
    led = "date,payer,amount,participants,note\nbad-date,   ,abc, ,note\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_bad_dates():
    for bad_d in ["2026-02-30", "26-03-01", "not-a-date"]:
        led = f"date,payer,amount,participants,note\n{bad_d},Ann,10.00,Ann,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-date"]


def test_bad_payers():
    for bad_p in ["", "   "]:
        led = f"date,payer,amount,participants,note\n2026-03-01,{bad_p},10.00,Ann,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-payer"]


def test_bad_amounts():
    for bad_a in ["0", "0.00", "-10", "12.345", '"12,34"', "1e3", "abc"]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,{bad_a},Ann,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and err != "", f"Failed for bad_a={bad_a!r}"
        assert code == 2 and out == ""


def test_bad_participants():
    bad_participants_list = [
        "",
        "   ",
        "Ann;",
        ";Ann",
        "Ann:0",
        "Ann:-1",
        "Ann:1.5",
        "Ann:abc",
        "Ann;Ann",
        "ann;ANN",
    ]
    for bad_parts in bad_participants_list:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,10.00,{bad_parts},\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2 and out == ""
        assert lines(err) == ["line 2: bad-participants"], f"Failed for participants: {bad_parts!r}"


def test_multiple_error_lines_ascending():
    led_multi = """date,payer,amount,participants,note
2026-03-01,Ann,abc,Ann,
invalid-date,Ann,10.00,Ann,
"""
    code, out, err = run(led_multi, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount", "line 3: bad-date"]


def test_name_casing_and_first_appearance():
    led = """date,payer,amount,participants,note
2026-03-01,bob,10.00,BOB;Ann,
2026-03-02,aNN,10.00,ANN;bob,
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann 0.00", "bob 0.00"]


def test_since_until_filtering():
    led = """date,payer,amount,participants,note
2026-02-28,Ann,10.00,Ann;Bob,outside range start
2026-03-01,Ann,20.00,Ann;Bob,inside range start
2026-03-15,Ann,20.00,Ann;Bob,inside range mid
2026-03-31,Ann,10.00,Ann;Bob,inside range end (inclusive)
2026-04-01,Ann,10.00,Ann;Bob,outside range end
"""
    # Test --since alone
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 0 and err == ""
    # Included: Mar 1 (20), Mar 15 (20), Mar 31 (10), Apr 1 (10). Total = 60. Ann: +30, Bob: -30.
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]

    # Test --until alone
    code, out, err = run(led, "balances", "{f}", "--until", "2026-03-31")
    assert code == 0 and err == ""
    # Included: Feb 28 (10), Mar 1 (20), Mar 15 (20), Mar 31 (10). Total = 60. Ann: +30, Bob: -30.
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]

    # Test both --since and --until
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-31")
    assert code == 0 and err == ""
    # Included: Mar 1 (20), Mar 15 (20), Mar 31 (10). Total = 50. Ann: +25, Bob: -25.
    assert lines(out) == ["Ann +25.00", "Bob -25.00"]

    # Test validation still runs on out-of-range rows:
    led_invalid_outside = """date,payer,amount,participants,note
2026-01-01,Ann,abc,Ann,invalid outside range
2026-03-01,Ann,10.00,Ann;Bob,valid inside
"""
    code, out, err = run(led_invalid_outside, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]

    # Test people only appearing outside range are not printed
    led_exclusive_person = """date,payer,amount,participants,note
2026-02-01,Charlie,10.00,Charlie;Dave,only outside
2026-03-01,Ann,10.00,Ann;Bob,only inside
"""
    code, out, err = run(led_exclusive_person, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-31")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_settle_tie_breaking():
    # Test settle tie-breaking: multiple debtors/creditors with same balance resolved by name ascending
    led = """date,payer,amount,participants,note
2026-03-01,Ann,30.00,Bob:1;Carl:1;David:1,
"""
    # Ann paid 30, Bob share 10, Carl share 10, David share 10.
    # Balances: Ann +30, Bob -10, Carl -10, David -10.
    # Debtors: Bob (-10), Carl (-10), David (-10). Creditor: Ann (+30).
    # Debtor tie broken by name ascending: Bob, then Carl, then David.
    code, out, err = run(led, "settle", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == [
        "Bob -> Ann 10.00",
        "Carl -> Ann 10.00",
        "David -> Ann 10.00",
    ]


def test_json_formats():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    
    # balances json
    code, out, err = run(led, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    data = json.loads(out)
    assert data == {"balances": [{"name": "Ann", "cents": 500}, {"name": "Bob", "cents": -500}]}

    # settle json
    code, out, err = run(led, "settle", "{f}", "--format", "json")
    assert code == 0 and err == ""
    data = json.loads(out)
    assert data == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}

    # settle json with nothing to settle
    led_empty = "date,payer,amount,participants,note\n"
    code, out, err = run(led_empty, "settle", "{f}", "--format", "json")
    assert code == 0 and err == ""
    data = json.loads(out)
    assert data == {"transfers": []}


def test_option_placement():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    code1, out1, _ = run(led, "--format", "json", "balances", "{f}")
    code2, out2, _ = run(led, "balances", "{f}", "--format", "json")
    assert code1 == 0 and code2 == 0
    assert out1 == out2


def test_file_read_error():
    code, out, err = run("", "balances", "nonexistent_ledger_file_12345.csv")
    assert code == 3
    assert out == ""
    assert "error: cannot read" in err


def test_usage_errors():
    led = "date,payer,amount,participants,note\n"
    code, out, err = run(led, "unknowncmd", "{f}")
    assert code == 64 and out == ""
    assert "usage error" in err

    code, out, err = run(led, "balances", "{f}", "--format", "xml")
    assert code == 64 and out == ""
    assert "usage error" in err

    code, out, err = run(led, "balances", "{f}", "--since", "bad-date")
    assert code == 64 and out == ""
    assert "usage error" in err

    code, out, err = run(led, "balances", "{f}", "--until", "2026-13-40")
    assert code == 64 and out == ""
    assert "usage error" in err

    code, out, err = run(led, "balances")
    assert code == 64


def test_empty_ledger():
    led = "date,payer,amount,participants,note\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert err == ""

    code, out, err = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["nothing to settle"]
