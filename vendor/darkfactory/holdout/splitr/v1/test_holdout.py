# DFL-HOLDOUT-CANARY-7c1e4b9a2f6d — held-out scenarios for splitr v1. Never mounted into a coder sandbox.
import json
import pytest
from helpers import run, lines

H = "date,payer,amount,participants,note\n"


def test_bom_and_column_order():
    led = "note,participants,amount,payer,date\nlunch,Ann;Bob,20.00,Ann,2026-03-01\n"
    code, out, err = run(None, "balances", "{f}", raw=b"\xef\xbb\xbf" + led.encode())
    assert code == 0, err
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_comments_and_blank_lines_ignored():
    led = H + "# a comment\n\n   \n2026-03-01,Ann,20.00,Ann;Bob,\n  # indented comment\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_line_numbers_count_ignored_lines():
    led = H + "# c\n\n2026-03-01,Ann,20.00,Ann;Bob,\n2026-02-30,Ann,5,Ann,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 5: bad-date"]


def test_multiple_bad_rows_in_order_one_reason_each():
    led = H + "2026-13-01,,x,,\n2026-03-01,Ann,1.999,Ann,\n2026-03-01,Ann,5,Ann;ann,\n2026-03-01, ,5,Ann,\n"
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date", "line 3: bad-amount", "line 4: bad-participants", "line 5: bad-payer"]


def test_amount_forms():
    led = H + "2026-03-01,Ann,12,Bob,\n2026-03-01,Ann,12.5,Bob,\n2026-03-01,Ann,0.05,Bob,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann +24.55", "Bob -24.55"]


@pytest.mark.parametrize("amount", [pytest.param("0", id="arbitrary:zero"), pytest.param("0.00", id="arbitrary:zero_decimals"), pytest.param("-5", id="implied:negative"), pytest.param("+5", id="arbitrary:plus_sign"), pytest.param("1,000", id="arbitrary:thousands"), pytest.param("1e2", id="arbitrary:exponent"), pytest.param("5.", id="arbitrary:trailing_dot"), pytest.param(".5", id="arbitrary:leading_dot"), pytest.param("12.345", id="implied:three_decimals"), pytest.param("", id="implied:empty")])
def test_bad_amount(amount):
    code, out, err = run(H + f'2026-03-01,Ann,"{amount}",Bob,\n', "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


@pytest.mark.parametrize("parts", [pytest.param("", id="implied:empty"), pytest.param("Ann;;Bob", id="arbitrary:empty_name"), pytest.param("Ann:0", id="implied:weight_zero"), pytest.param("Ann:x", id="implied:weight_text"), pytest.param("Ann:1.5", id="arbitrary:weight_fraction"), pytest.param("Ann; ann ", id="arbitrary:same_person_twice"), pytest.param("Ann:-1", id="implied:weight_negative")])
def test_bad_participants(parts):
    code, out, err = run(H + f"2026-03-01,Ann,5,{parts},\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-participants"]


def test_names_case_insensitive_first_spelling():
    led = H + "2026-03-01,ann,10.00,Bob,\n2026-03-02,BOB,4.00, Ann ,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["ann +6.00", "Bob -6.00"]


def test_sort_ignores_case():
    led = H + "2026-03-01,bob,9.00,Zoe;adam;bob,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert [l.split()[0] for l in lines(out)] == ["adam", "bob", "Zoe"]


def test_zero_balance_prints_unsigned():
    led = H + "2026-03-01,Ann,10.00,Bob,\n2026-03-02,Bob,10.00,Ann,\n2026-03-02,Cy,2.00,Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann 0.00", "Bob 0.00", "Cy 0.00"]


def test_payer_not_participant():
    led = H + "2026-03-01,Ann,9.00,Bob;Cy,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann +9.00", "Bob -4.50", "Cy -4.50"]


def test_leftover_two_cents_alphabetical_ignoring_case():
    led = H + "2026-03-01,Zed,10.01,cy;Bob;ann,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    # 1001 / 3 = 333 r 2 -> ann and Bob get the extra cents
    assert lines(out) == ["ann -3.34", "Bob -3.34", "cy -3.33", "Zed +10.01"]


def test_leftover_goes_to_largest_remainder_with_weights():
    # 100 cents, weights 1,1,5 (W=7): base 14,14,71 = 99; remainders 2,2,3 -> Cy gets the cent
    led = H + "2026-03-01,Zed,1.00,Ann;Bob;Cy:5,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -0.14", "Bob -0.14", "Cy -0.72", "Zed +1.00"]


def test_leftover_remainder_tie_by_name():
    # 10 cents, weights 2,2,3 (W=7): base 2,2,4 = 8; remainders 6,6,2 -> Ann and Bob
    led = H + "2026-03-01,Zed,0.10,Cy:3;Bob:2;Ann:2,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann -0.03", "Bob -0.03", "Cy -0.04", "Zed +0.10"]


def test_settle_ties_and_multiple_rounds():
    led = H + "2026-03-01,Ann,30.00,Bob;Cy;Dee,\n2026-03-01,Eve,30.00,Bob;Cy;Dee,\n"
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Bob -> Ann 20.00", "Cy -> Eve 20.00", "Dee -> Ann 10.00", "Dee -> Eve 10.00"]


def test_settle_largest_first():
    led = H + "2026-03-01,Ann,100.00,Bob:7;Cy:3,\n2026-03-01,Dee,10.00,Cy,\n"
    code, out, _ = run(led, "settle", "{f}")
    assert lines(out) == ["Bob -> Ann 70.00", "Cy -> Ann 30.00", "Cy -> Dee 10.00"]


def test_settle_json_and_empty_json():
    led = H + "2026-03-01,Ann,5.00,Bob,\n"
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert json.loads(out) == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}
    code, out, _ = run(H, "settle", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"transfers": []}


def test_since_until_inclusive():
    led = H + "2026-03-01,Ann,10.00,Bob,\n2026-03-02,Ann,20.00,Bob,\n2026-03-03,Ann,40.00,Bob,\n"
    code, out, _ = run(led, "balances", "{f}", "--since", "2026-03-02", "--until", "2026-03-02")
    assert lines(out) == ["Ann +20.00", "Bob -20.00"]
    code, out, _ = run(led, "balances", "{f}", "--until", "2026-03-02")
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]


def test_filter_hides_people_outside_range():
    led = H + "2026-03-01,Ann,10.00,Bob,\n2026-03-05,Cy,10.00,Dee,\n"
    code, out, _ = run(led, "balances", "{f}", "--since", "2026-03-04")
    assert lines(out) == ["Cy +10.00", "Dee -10.00"]


def test_validation_applies_outside_range():
    led = H + "2026-03-01,Ann,oops,Bob,\n2026-03-05,Cy,10.00,Dee,\n"
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-04")
    assert code == 2 and out == "" and lines(err) == ["line 2: bad-amount"]


def test_options_before_positionals():
    led = H + "2026-03-01,Ann,5.00,Bob,\n"
    code, out, _ = run(led, "--format", "json", "balances", "{f}")
    assert code == 0 and json.loads(out)["balances"][0] == {"name": "Ann", "cents": 500}


def test_missing_header_column():
    code, out, err = run("date,payer,participants\n2026-03-01,Ann,Bob\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing amount"]


def test_file_not_found():
    code, out, err = run(H, "balances", "{f}.nope")
    assert code == 3 and out == ""
    assert lines(err) == ["error: cannot read {f}.nope"]


@pytest.mark.parametrize("args", [pytest.param(["balances"], id="stated:missing_ledger"), pytest.param(["balances", "{f}", "--format", "xml"], id="stated:bad_format"), pytest.param(["balances", "{f}", "--since", "2026-02-30"], id="implied:bad_since_date"), pytest.param(["balances", "{f}", "--bogus"], id="stated:unknown_option")])
def test_usage_error(args):
    code, out, _ = run(H, *args)
    assert code == 64 and out == ""


def test_quoted_fields():
    led = H + '2026-03-01,"Ann",10.00,"Bob;Cy","dinner, drinks; more"\n'
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann +10.00", "Bob -5.00", "Cy -5.00"]


def test_crlf_line_endings():
    led = (H + "2026-03-01,Ann,10.00,Bob,\n").replace("\n", "\r\n")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]
