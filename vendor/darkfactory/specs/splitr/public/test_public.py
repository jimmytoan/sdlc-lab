"""Public acceptance examples for splitr. The coding agent may read and run these."""
import json
from helpers import run, lines

TRIP = """date,payer,amount,participants,note
2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner
2026-03-02,Bob,30,Ann;Bob,taxi
2026-03-03,Cy,9.00,Ann;Bob;Cy,"coffee, cake"
"""


def test_balances_basic():
    code, out, err = run(TRIP, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +22.00", "Bob -8.00", "Cy -14.00"]


def test_settle_basic():
    code, out, _ = run(TRIP, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Cy -> Ann 14.00", "Bob -> Ann 8.00"]


def test_balances_json():
    code, out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"balances": [
        {"name": "Ann", "cents": 2200}, {"name": "Bob", "cents": -800}, {"name": "Cy", "cents": -1400}]}


def test_weights():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,40.00,Ann:1;Bob:3,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]


def test_leftover_cent():
    led = "date,payer,amount,participants,note\n2026-03-01,Zed,10.00,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_invalid_row_reported():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,abc,Ann;Bob,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


def test_nothing_to_settle():
    led = "date,payer,amount,participants,note\n"
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["nothing to settle"]


def test_unknown_command():
    code, out, _ = run(TRIP, "explode", "{f}")
    assert code == 64 and out == ""
