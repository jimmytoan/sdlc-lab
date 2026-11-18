"""Comprehensive test suite for splitr covering all specifications and edge cases."""
import json
from helpers import run, lines


def test_bom_handling():
    raw = b'\xef\xbb\xbfdate,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n'
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_column_reordering():
    led = "amount,note,participants,payer,date\n10.00,,Ann;Bob,Ann,2026-03-01\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_quoted_notes_and_newlines():
    led = "date,payer,amount,participants,note\r\n" \
          "2026-03-01,Ann,10.00,Ann;Bob,\"dinner, drinks\"\r\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_ignored_lines_and_line_numbers():
    led = "date,payer,amount,participants,note\n" \
          "# comment line\n" \
          "   \n" \
          "\n" \
          "2026-03-01,Ann,abc,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 5: bad-amount"]


def test_name_spelling_and_casing():
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,ANN,10.00,bob;ann,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    # First appearance is BOB (in participants) and ANN (payer)
    assert lines(out) == ["ANN +5.00", "bob -5.00"]


def test_zero_balance_formatting():
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,Ann,10.00,Ann,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann 0.00"]


def test_remainder_tie_breaking():
    # 10 cents split between Ann and Bob (equal weight 1).
    # 10 * 1 / 2 = 5 each. Leftover 0.
    # What about unequal or ties in remainder?
    # 10 cents split among Ann, Bob, Cy with weights 1, 1, 1 -> shares 3, 3, 3. Remainders 1, 1, 1.
    # Sorted by remainder desc (-1), then name ascending (Ann, Bob, Cy). Leftover 1 cent goes to Ann.
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,Zed,10.00,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_settle_tie_breaking():
    # Ann has +10, Bob -5, Cy -5.
    # Bob vs Cy both have -5 balance. Tie broken by name ascending: Bob comes before Cy.
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,Ann,10.00,Bob;Cy,\n"
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Bob -> Ann 5.00", "Cy -> Ann 5.00"]


def test_settle_json():
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}


def test_settle_json_nothing():
    led = "date,payer,amount,participants,note\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": []}


def test_date_filtering():
    led = "date,payer,amount,participants,note\n" \
          "2026-02-28,Ann,10.00,Ann;Bob,\n" \
          "2026-03-01,Ann,20.00,Ann;Bob,\n" \
          "2026-03-05,Ann,30.00,Ann;Bob,\n" \
          "2026-03-10,Ann,40.00,Ann;Bob,\n"
    code, out, _ = run(led, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-05")
    assert code == 0
    # Between 2026-03-01 and 2026-03-05 inclusive: rows 2 and 3 (amounts 20 and 30 -> total 50, split 25 each).
    # Ann paid 50, share 25 -> +25.00. Bob paid 0, share 25 -> -25.00.
    assert lines(out) == ["Ann +25.00", "Bob -25.00"]


def test_date_filtering_validation_out_of_range():
    # Out of range row has invalid data (bad-amount), validation should still trigger.
    led = "date,payer,amount,participants,note\n" \
          "2026-01-01,Ann,bad,Ann;Bob,\n" \
          "2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


def test_date_filtering_excludes_out_of_range_people():
    # Cy only appears in out-of-range row
    led = "date,payer,amount,participants,note\n" \
          "2026-02-28,Cy,10.00,Cy;Bob,\n" \
          "2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 0
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_option_ordering():
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,Ann,10.00,Ann;Bob,\n"
    # Format option after args
    code1, out1, _ = run(led, "balances", "{f}", "--format", "json")
    # Format option before args
    code2, out2, _ = run(led, "--format", "json", "balances", "{f}")
    assert code1 == 0 and code2 == 0
    assert json.loads(out1) == json.loads(out2)


def test_missing_header_columns():
    # Missing 'amount'
    led = "date,payer,participants,note\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing amount"]

    # Missing multiple required columns: reports first missing in order: date, payer, amount, participants, note
    led2 = "note,participants\n"
    code, out, err = run(led2, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing date"]


def test_invalid_row_reasons():
    # bad-date
    led = "date,payer,amount,participants,note\n2026-02-30,Ann,10.00,Ann;Bob,\n"
    code, _, err = run(led, "balances", "{f}")
    assert code == 2 and "bad-date" in err

    # bad-payer (empty)
    led = "date,payer,amount,participants,note\n2026-03-01,  ,10.00,Ann;Bob,\n"
    code, _, err = run(led, "balances", "{f}")
    assert code == 2 and "bad-payer" in err

    # bad-amount (zero, negative, format)
    for amt in ["0", "0.00", "-10", "10.00.0", "abc", "10.555"]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,{amt},Ann;Bob,\n"
        code, _, err = run(led, "balances", "{f}")
        assert code == 2, f"Expected failure for amount={amt}"
        assert "bad-amount" in err

    # bad-participants (empty, empty name, bad weight, repeated person)
    for parts in ["", "   ", ";", "Ann;;Bob", "Ann:0", "Ann:-2", "Ann:abc", "Ann;Ann", "Ann;ANN"]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,10.00,{parts},\n"
        code, _, err = run(led, "balances", "{f}")
        assert code == 2, f"Expected failure for participants={parts}"
        assert "bad-participants" in err


def test_multiple_invalid_rows_order():
    led = "date,payer,amount,participants,note\n" \
          "2026-03-01,Ann,abc,Ann;Bob,\n" \
          "2026-02-30,Ann,10.00,Ann;Bob,\n" \
          "2026-03-01,,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [
        "line 2: bad-amount",
        "line 3: bad-date",
        "line 4: bad-payer"
    ]


def test_cannot_read_file():
    code, out, err = run("", "balances", "nonexistent_file_12345.csv")
    assert code == 3 and out == ""
    assert "error: cannot read" in err


def test_usage_errors():
    # Unknown command
    code, out, err = run("date,payer,amount,participants,note\n", "unknowncmd", "{f}")
    assert code == 64 and out == ""
    assert "usage error" in err

    # Bad format
    code, out, err = run("date,payer,amount,participants,note\n", "balances", "{f}", "--format", "xml")
    assert code == 64 and out == ""
    assert "usage error" in err

    # Bad since date
    code, out, err = run("date,payer,amount,participants,note\n", "balances", "{f}", "--since", "bad-date")
    assert code == 64 and out == ""
    assert "usage error" in err

    # Bad until date
    code, out, err = run("date,payer,amount,participants,note\n", "balances", "{f}", "--until", "2026-13-45")
    assert code == 64 and out == ""
    assert "usage error" in err
