"""Public acceptance examples for splitr change request 1."""
from helpers import run, lines

HK = "date,payer,amount,participants,note,kind\n"


def test_payment_clears_debt():
    led = HK + "2026-03-01,Ann,20.00,Ann;Bob,dinner,expense\n2026-03-05,Bob,10.00,Ann,,payment\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0, err
    assert lines(out) == ["Ann 0.00", "Bob 0.00"]


def test_bad_kind():
    led = HK + "2026-03-01,Ann,20.00,Ann;Bob,dinner,gift\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-kind"]


def test_payer_first_rounding():
    led = "date,payer,amount,participants,note\n2026-03-01,Cy,10.00,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -3.33", "Bob -3.33", "Cy +6.66"]
