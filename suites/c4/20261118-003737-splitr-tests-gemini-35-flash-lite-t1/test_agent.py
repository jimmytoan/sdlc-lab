"""Comprehensive pytest test suite for splitr."""
import json
import pytest
from helpers import run, lines


def test_bom_and_line_endings():
    # Test UTF-8 BOM (\ufeff) and \r\n line endings, plus comments and blank lines
    ledger = "\ufeff# comment line\r\n\r\n  \r\ndate,payer,amount,participants,note\r\n2026-04-01,Ann,10.00,Ann;Bob,test\r\n"
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 0
    assert err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_comment_line_numbering():
    # Verify line numbering counts ignored lines correctly
    ledger = "date,payer,amount,participants,note\n# comment\n\n2026-04-01,Ann,abc,Ann;Bob,\n"
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["line 4: bad-amount"]


def test_header_arbitrary_order():
    ledger = "note,participants,amount,payer,date\nnote1,Ann;Bob,20.00,Ann,2026-04-01\n"
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 0
    assert err == ""
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_header_missing_required():
    # Required missing in order: date, payer, amount, participants, note
    # If date and amount missing, reports date first
    ledger = "payer,participants,note\nAnn,Ann;Bob,note\n"
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["header: missing date"]

    ledger2 = "date,payer,participants,note\n2026-04-01,Ann,Ann;Bob,note\n"
    code, out, err = run(ledger2, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["header: missing amount"]


def test_row_validation_order_and_rules():
    # Validation order: bad-date, bad-payer, bad-amount, bad-participants
    # 1. bad-date
    led = "date,payer,amount,participants,note\n2026-02-30,Ann,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["line 2: bad-date"]

    # 2. bad-payer (empty after strip)
    led = "date,payer,amount,participants,note\n2026-03-01,   ,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["line 2: bad-payer"]

    # 3. bad-amount (zero, negative, format errors)
    for bad_amt in ["0", "0.00", "-10.00", "abc", "10.", "10.345", '"1,000.00"', "1e2"]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,{bad_amt},Ann;Bob,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2, f"Failed for amount: {bad_amt}"
        assert lines(err) == ["line 2: bad-amount"]

    # 4. bad-participants (empty, empty name, weight not positive int, repeated)
    for bad_parts in ["", "   ", "Ann;;Bob", ";Ann", "Ann;", "Ann:0", "Ann:-1", "Ann:abc", "Ann:1.5", "Ann;ann", "Ann;BoB;aNn"]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,10.00,{bad_parts},\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2, f"Failed for participants: {bad_parts}"
        assert lines(err) == ["line 2: bad-participants"]


def test_multiple_invalid_rows():
    ledger = """date,payer,amount,participants,note
2026-03-99,Ann,10.00,Ann;Bob,
2026-03-01,,10.00,Ann;Bob,
2026-03-01,Ann,bad,Ann;Bob,
"""
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 2
    assert lines(err) == [
        "line 2: bad-date",
        "line 3: bad-payer",
        "line 4: bad-amount",
    ]


def test_name_casing_and_spelling():
    # First appearance spelling: payer before participants, left to right
    ledger = """date,payer,amount,participants,note
2026-03-01,aNN,10.00,BOB;cy;Ann,
"""
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 0
    assert lines(out) == [
        "aNN +6.66",
        "BOB -3.33",
        "cy -3.33",
    ]


def test_splitting_weights_and_leftovers():
    # C = 1000 cents ($10.00), weights Ann:1, Bob:2, Cy:2 (sum 5)
    # Ann: 1000 * 1 / 5 = 200. rem = 0
    # Bob: 1000 * 2 / 5 = 400. rem = 0
    # Cy: 1000 * 2 / 5 = 400. rem = 0
    ledger = "date,payer,amount,participants,note\n2026-03-01,Zed,10.00,Ann:1;Bob:2;Cy:2,\n"
    code, out, _ = run(ledger, "balances", "{f}")
    assert code == 0
    assert lines(out) == [
        "Ann -2.00",
        "Bob -4.00",
        "Cy -4.00",
        "Zed +10.00",
    ]


def test_settle_tie_breaking():
    # Balances: Ann +10.00, Bob -5.00, aaron -5.00
    # Debtors: 'aaron' and 'Bob'. Tie broken by name ascending ignoring case: 'aaron' then 'Bob'.
    ledger = """date,payer,amount,participants,note
2026-03-01,Ann,10.00,aaron;Bob,
"""
    code, out, _ = run(ledger, "settle", "{f}")
    assert code == 0
    assert lines(out) == [
        "aaron -> Ann 5.00",
        "Bob -> Ann 5.00",
    ]


def test_settle_json():
    ledger = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(ledger, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}


def test_settle_nothing_json():
    ledger = "date,payer,amount,participants,note\n"
    code, out, _ = run(ledger, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": []}


def test_empty_ledger_no_expenses():
    ledger = "date,payer,amount,participants,note\n# comment\n"
    code, out, err = run(ledger, "balances", "{f}")
    assert code == 0
    assert out.strip() == ""
    assert err == ""

    code, out, err = run(ledger, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["nothing to settle"]
    assert err == ""


def test_zero_balance_formatting():
    # Ann pays 10.00 for Ann
    ledger = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann,\n"
    code, out, _ = run(ledger, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann 0.00"]


def test_options_placement():
    ledger = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    # Options before or after positional args
    code1, out1, _ = run(ledger, "--format", "json", "balances", "{f}")
    code2, out2, _ = run(ledger, "balances", "{f}", "--format", "json")
    assert code1 == 0 and code2 == 0
    assert json.loads(out1) == json.loads(out2)


def test_since_until_filtering_and_validation():
    ledger = """date,payer,amount,participants,note
2026-01-01,Ann,10.00,Ann;Bob,
2026-03-01,Ann,10.00,Ann;Bob,
2026-05-01,Bob,20.00,Ann;Bob,
"""
    # --since and --until inclusive filtering (Ann & Bob outside range are omitted if they only appear outside range)
    code, out, _ = run(ledger, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-31")
    assert code == 0
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]

    # People who only appear in rows outside the range are not printed
    code, out, _ = run(ledger, "balances", "{f}", "--since", "2026-05-01")
    assert code == 0
    assert lines(out) == ["Ann -10.00", "Bob +10.00"]

    # Validation still applies to rows outside range (invalid row outside range triggers exit code 2)
    invalid_ledger = """date,payer,amount,participants,note
2026-01-01,Ann,bad-amount,Ann;Bob,
2026-03-01,Ann,10.00,Ann;Bob,
"""
    code, out, err = run(invalid_ledger, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2
    assert lines(err) == ["line 2: bad-amount"]


def test_file_read_error():
    code, out, err = run("", "balances", "nonexistent_ledger_12345.csv")
    assert code == 3
    assert out == ""
    assert "error: cannot read" in err


def test_usage_errors():
    ledger = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob,\n"
    # Unknown command
    code, out, err = run(ledger, "unknowncmd", "{f}")
    assert code == 64
    assert "usage error" in err

    # Bad format
    code, out, err = run(ledger, "balances", "{f}", "--format", "xml")
    assert code == 64
    assert "usage error" in err

    # Bad --since date
    code, out, err = run(ledger, "balances", "{f}", "--since", "2026-13-99")
    assert code == 64
    assert "usage error" in err

    # Bad --until date
    code, out, err = run(ledger, "balances", "{f}", "--until", "abc")
    assert code == 64
    assert "usage error" in err
