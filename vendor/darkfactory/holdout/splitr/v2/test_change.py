# DFL-HOLDOUT-CANARY-7c1e4b9a2f6d — held-out scenarios for splitr change request 1.
import json
from helpers import run, lines

H = "date,payer,amount,participants,note\n"
HK = "date,payer,amount,participants,note,kind\n"


def test_kind_column_absent_still_works():
    code, out, _ = run(H + "2026-03-01,Ann,20.00,Ann;Bob,\n", "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_empty_kind_means_expense():
    code, out, _ = run(HK + "2026-03-01,Ann,20.00,Ann;Bob,x,\n", "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_kind_column_anywhere():
    led = "kind,date,payer,amount,participants,note\npayment,2026-03-01,Bob,7.50,Ann,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann -7.50", "Bob +7.50"]


def test_partial_payment_then_settle():
    led = HK + "2026-03-01,Ann,30.00,Bob;Cy,,expense\n2026-03-02,Bob,5.00,Ann,,payment\n"
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Cy -> Ann 15.00", "Bob -> Ann 10.00"]


def test_overpayment_flips_direction():
    led = HK + "2026-03-01,Ann,10.00,Bob,,expense\n2026-03-02,Bob,25.00,Ann,,payment\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert json.loads(out) == {"transfers": [{"from": "Ann", "to": "Bob", "cents": 1500}]}


def test_payment_is_not_split_or_rounded():
    led = HK + "2026-03-01,Bob,0.01,Ann,,payment\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann -0.01", "Bob +0.01"]


def test_payment_invalid_participants():
    bad = ["Ann;Cy", "Ann:2", "bob", ""]
    led = HK + "".join(f"2026-03-01,Bob,5.00,{p},,payment\n" for p in bad)
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"line {i}: bad-participants" for i in range(2, 6)]


def test_bad_kind_order_of_checks():
    led = HK + "2026-02-31,Ann,5.00,Bob,,gift\n2026-03-01,,x,,,gift\n2026-03-01,Ann,5.00,Bob,,PAYMENT\n"
    code, _, err = run(led, "balances", "{f}")
    assert code == 2
    assert lines(err) == ["line 2: bad-date", "line 3: bad-kind", "line 4: bad-kind"]


def test_payment_respects_date_filter():
    led = HK + "2026-03-01,Ann,10.00,Bob,,expense\n2026-03-09,Bob,10.00,Ann,,payment\n"
    code, out, _ = run(led, "balances", "{f}", "--until", "2026-03-05")
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]
    code, out, _ = run(led, "settle", "{f}")
    assert lines(out) == ["nothing to settle"]


def test_payer_first_two_leftover_cents():
    # 1001 / 3 = 333 r 2: payer Cy first, then Ann
    led = H + "2026-03-01,Cy,10.01,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann -3.34", "Bob -3.33", "Cy +6.67"]


def test_payer_first_beats_larger_remainder():
    # 100 cents, weights Ann 1, Bob 1, Cy 5: base 14,14,71, one cent left; Cy has the largest remainder but Ann paid
    led = H + "2026-03-01,Ann,1.00,Ann;Bob;Cy:5,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann +0.85", "Bob -0.14", "Cy -0.71"]


def test_payer_first_case_insensitive_payer():
    led = H + "2026-03-01,CY,10.00,ann;bob;cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["ann -3.33", "bob -3.33", "CY +6.66"]


def test_payer_not_participant_unchanged():
    led = H + "2026-03-01,Zed,10.00,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_no_leftover_unchanged():
    led = H + "2026-03-01,Cy,9.00,Ann;Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann -3.00", "Bob -3.00", "Cy +6.00"]
