import json
import os
import tempfile
import pytest
from helpers import run, lines

def test_bom_and_crlf():
    content = "\ufeffdate,payer,amount,participants,note\r\n2026-01-01,Ann,10.00,Ann;Bob,\r\n"
    code, out, err = run(content, "balances", "{f}")
    assert code == 0
    assert err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]

def test_ignored_lines_and_line_numbering():
    content = (
        "date,payer,amount,participants,note\n"
        "# comment\n"
        "   \n"
        "\n"
        "2026-01-01,Ann,abc,Ann;Bob,\n"
    )
    code, out, err = run(content, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["line 5: bad-amount"]

def test_column_order():
    content = "note,amount,participants,date,payer\nnote1,10.00,Ann;Bob,2026-01-01,Ann\n"
    code, out, err = run(content, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]

def test_name_casing_and_first_appearance():
    content = (
        "date,payer,amount,participants,note\n"
        "2026-01-01,bOb,10.00,aNn;carl,\n"
        "2026-01-02,Ann,20.00,BOB;carl,\n"
    )
    code, out, err = run(content, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["aNn +15.00", "bOb 0.00", "carl -15.00"]

def test_zero_balance_formatting():
    content = (
        "date,payer,amount,participants,note\n"
        "2026-01-01,Ann,10.00,Ann;Bob,\n"
        "2026-01-02,Bob,10.00,Ann;Bob,\n"
    )
    code, out, _ = run(content, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann 0.00", "Bob 0.00"]

def test_since_until_filtering():
    content = (
        "date,payer,amount,participants,note\n"
        "2026-01-01,Ann,10.00,Ann;Bob,\n"
        "2026-06-01,Ann,20.00,Ann;Bob,\n"
        "2026-12-01,Ann,30.00,Ann;Bob,\n"
    )
    code, out, _ = run(content, "balances", "{f}", "--since", "2026-05-01", "--until", "2026-07-01")
    assert code == 0
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]

def test_since_until_validation_still_applies():
    content = (
        "date,payer,amount,participants,note\n"
        "2026-01-01,Ann,invalid,Ann;Bob,\n"
    )
    code, out, err = run(content, "balances", "{f}", "--since", "2026-06-01")
    assert code == 2
    assert lines(err) == ["line 2: bad-amount"]

def test_people_only_outside_range_omitted():
    content = (
        "date,payer,amount,participants,note\n"
        "2026-01-01,Ann,10.00,Ann;Charlie,\n"
        "2026-06-01,Ann,20.00,Ann;Bob,\n"
    )
    code, out, _ = run(content, "balances", "{f}", "--since", "2026-05-01")
    assert code == 0
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]

def test_invalid_row_error_order():
    content = (
        "date,payer,amount,participants,note\n"
        "2026-13-01,,abc,Ann;Bob,\n"
        "2026-01-01,,abc,Ann;Bob,\n"
        "2026-01-01,Ann,abc,Ann;Bob,\n"
        "2026-01-01,Ann,10.00,,\n"
    )
    code, out, err = run(content, "balances", "{f}")
    assert code == 2
    assert lines(err) == [
        "line 2: bad-date",
        "line 3: bad-payer",
        "line 4: bad-amount",
        "line 5: bad-participants",
    ]

def test_header_missing_column_order():
    content = "payer,amount,participants,note\nAnn,10.00,Ann;Bob,\n"
    code, out, err = run(content, "balances", "{f}")
    assert code == 2
    assert "header: missing date" in err

    content = "date,amount,participants,note\n2026-01-01,10.00,Ann;Bob,\n"
    code, out, err = run(content, "balances", "{f}")
    assert code == 2
    assert "header: missing payer" in err

def test_cannot_read_ledger():
    code, out, err = run("", "balances", "nonexistent_file_xyz_123.csv")
    assert code == 3
    assert "error: cannot read" in err

def test_usage_errors():
    code, out, err = run("date,payer,amount,participants,note\n", "foo", "{f}")
    assert code == 64
    code, out, err = run("date,payer,amount,participants,note\n", "balances", "{f}", "--format", "yaml")
    assert code == 64
    code, out, err = run("date,payer,amount,participants,note\n", "balances", "{f}", "--since", "bad-date")
    assert code == 64
    code, out, err = run("date,payer,amount,participants,note\n", "balances", "{f}", "--until", "bad-date")
    assert code == 64

def test_settle_json():
    content = "date,payer,amount,participants,note\n2026-01-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(content, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}

def test_options_placement():
    content = "date,payer,amount,participants,note\n2026-01-01,Ann,10.00,Ann;Bob,\n"
    code, out, _ = run(content, "--format", "json", "balances", "{f}")
    assert code == 0
    assert "balances" in json.loads(out)
