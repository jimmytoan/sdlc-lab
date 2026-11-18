"""Thorough test suite for splitr covering all requirements in SPEC.md."""
import json
import os
import subprocess
import sys
import tempfile
import pytest
from helpers import run, lines


def test_bom_and_column_order():
    led = "\ufeffnote,participants,amount,payer,date\ndinner,Ann;Bob,60.00,Ann,2026-03-01\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]


def test_csv_quoting_and_line_endings():
    led = "date,payer,amount,participants,note\r\n2026-03-01,Ann,10.00,Ann;Bob,\"dinner, coffee\"\r\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_ignored_lines_and_line_numbering():
    led = """# Header comment
date,payer,amount,participants,note

   # another comment
2026-03-01,Ann,bad_amt,Ann;Bob,
"""
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 5: bad-amount"]


def test_date_validation():
    led = "date,payer,amount,participants,note\n2026-02-30,Ann,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2
    assert "bad-date" in err

    led2 = "date,payer,amount,participants,note\n03-01-2026,Ann,10.00,Ann;Bob,\n"
    code, out, err = run(led2, "balances", "{f}")
    assert code == 2
    assert "bad-date" in err


def test_payer_validation():
    led = "date,payer,amount,participants,note\n2026-03-01,   ,10.00,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2
    assert "bad-payer" in err


def test_amount_validation():
    for amt in ["0", "0.0", "0.00", "-10.00", "+10.00", "\"1,000.00\"", "1e2", "10..0", "abc"]:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,{amt},Ann;Bob,\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2, f"Failed for amount {amt}"
        assert "bad-amount" in err


def test_participants_validation():
    invalid_parts = [
        "",
        "   ",
        "Ann;;Bob",
        "Ann:0;Bob",
        "Ann:-1;Bob",
        "Ann:1.5;Bob",
        "Ann:abc;Bob",
        "Ann;ann",
        "Ann;ANN",
    ]
    for p in invalid_parts:
        led = f"date,payer,amount,participants,note\n2026-03-01,Ann,10.00,{p},\n"
        code, out, err = run(led, "balances", "{f}")
        assert code == 2, f"Failed for participants {p!r}"
        assert "bad-participants" in err


def test_error_priority_order():
    led = "date,payer,amount,participants,note\nnot-a-date,Ann,abc,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2
    assert "bad-date" in err
    assert "bad-amount" not in err


def test_name_spelling_and_case_insensitivity():
    led = "date,payer,amount,participants,note\n2026-03-01,aNn,10.00,bOb;ANN,test\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["aNn +5.00", "bOb -5.00"]


def test_settle_procedure():
    led = """date,payer,amount,participants,note
2026-03-01,Ann,20.00,Bob;Charlie,
"""
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Bob -> Ann 10.00", "Charlie -> Ann 10.00"]


def test_json_settle():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Bob,\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 1000}]}


def test_json_settle_empty():
    led = "date,payer,amount,participants,note\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    data = json.loads(out)
    assert data == {"transfers": []}


def test_since_until_filtering():
    led = """date,payer,amount,participants,note
2026-03-01,Ann,10.00,Bob,
2026-03-15,Ann,20.00,Bob,
2026-03-30,Ann,30.00,Bob,
"""
    code, out, _ = run(led, "balances", "{f}", "--since", "2026-03-10", "--until", "2026-03-20")
    assert code == 0
    assert lines(out) == ["Ann +20.00", "Bob -20.00"]

    code, out, _ = run(led, "balances", "{f}", "--since", "2026-03-10")
    assert code == 0
    assert lines(out) == ["Ann +50.00", "Bob -50.00"]

    code, out, _ = run(led, "balances", "{f}", "--until", "2026-03-15")
    assert code == 0
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]


def test_since_until_validation_still_applies_out_of_range():
    led = """date,payer,amount,participants,note
2026-01-01,Ann,bad_amount,Bob,
2026-03-15,Ann,20.00,Bob,
"""
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2
    assert "line 2: bad-amount" in err


def test_option_ordering():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Bob,\n"
    code, out, _ = run(led, "balances", "--format", "json", "{f}")
    assert code == 0
    assert "balances" in out


def test_missing_header_column():
    led = "date,payer,participants,note\n2026-03-01,Ann,Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.strip() == "header: missing amount"

    led2 = "payer,note\nAnn,test\n"
    code, out, err = run(led2, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.strip() == "header: missing date"


def test_unreadable_ledger():
    with tempfile.TemporaryDirectory() as d:
        bad_path = os.path.join(d, "nonexistent.csv")
        app_dir = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, os.path.join(app_dir, "splitr.py"), "balances", bad_path],
                           capture_output=True, text=True, timeout=20)
        assert p.returncode == 3
        assert p.stdout == ""
        assert "cannot read" in p.stderr


def test_usage_errors():
    for args in [
        ["unknown", "{f}"],
        ["balances"],
        ["balances", "{f}", "--bad-option"],
        ["balances", "{f}", "--format", "xml"],
        ["balances", "{f}", "--since", "not-a-date"],
        ["balances", "{f}", "--until", "2026-02-30"],
    ]:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.csv")
            with open(path, "w") as f:
                f.write("date,payer,amount,participants,note\n")
            argv = [a.replace("{f}", path) for a in args]
            app_dir = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p = subprocess.run([sys.executable, os.path.join(app_dir, "splitr.py"), *argv],
                               capture_output=True, text=True, timeout=20)
            assert p.returncode == 64, f"Failed for args {args}, got code {p.returncode}, stderr: {p.stderr}"
            assert p.stdout == ""
