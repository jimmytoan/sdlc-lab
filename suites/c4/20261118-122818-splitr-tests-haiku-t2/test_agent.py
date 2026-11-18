"""Regression suite for splitr, derived from SPEC.md. Black-box: drives splitr.py via subprocess."""
import json
import os
import random
import subprocess
import sys

import pytest

from helpers import APP_DIR, run, lines

HDR = "date,payer,amount,participants,note\n"


def ledger(*rows, header=HDR):
    return header + "".join(r + "\n" for r in rows)


def row(date="2026-03-01", payer="Ann", amount="10.00", participants="Bob", note=""):
    return f"{date},{payer},{amount},{participants},{note}"


def balances_text(text, *extra):
    code, out, err = run(text, "balances", "{f}", *extra)
    assert code == 0, err
    return lines(out)


def settle_text(text, *extra):
    code, out, err = run(text, "settle", "{f}", *extra)
    assert code == 0, err
    return lines(out)


def balances_json(text, *extra):
    code, out, err = run(text, "balances", "{f}", "--format", "json", *extra)
    assert code == 0, err
    return json.loads(out)["balances"]


def settle_json(text, *extra):
    code, out, err = run(text, "settle", "{f}", "--format", "json", *extra)
    assert code == 0, err
    return json.loads(out)["transfers"]


def assert_invalid(text, expected_stderr_lines, *args):
    code, out, err = run(text, *args)
    assert code == 2, (code, err)
    assert out == ""
    assert lines(err) == expected_stderr_lines


# ---------------------------------------------------------------- reference model
# An independent model of SPEC.md used to cross-check the CLI on random ledgers.

def ref_split(cents, parts):
    """parts: list of (key_lower, weight). Returns {key: share_cents}."""
    total = sum(w for _, w in parts)
    shares = [cents * w // total for _, w in parts]
    left = cents - sum(shares)
    by_priority = sorted(range(len(parts)),
                         key=lambda i: (-((cents * parts[i][1]) % total), parts[i][0]))
    for i in by_priority[:left]:
        shares[i] += 1
    return {parts[i][0]: shares[i] for i in range(len(parts))}


def ref_balances(expenses):
    """expenses: list of (payer_display, cents, [(display, weight)]). Returns sorted [(display, cents)]."""
    display, bal = {}, {}

    def touch(name):
        k = name.strip().lower()
        display.setdefault(k, name.strip())
        bal.setdefault(k, 0)
        return k

    for payer, cents, parts in expenses:
        p = touch(payer)
        keyed = [(touch(n), w) for n, w in parts]
        bal[p] += cents
        for k, s in ref_split(cents, keyed).items():
            bal[k] -= s
    return [(display[k], bal[k]) for k in sorted(bal)]


def ref_settle(balances):
    bal = {n: c for n, c in balances}
    out = []
    while any(v != 0 for v in bal.values()):
        debtor = sorted((n for n in bal if bal[n] < 0), key=lambda n: (bal[n], n.lower()))[0]
        creditor = sorted((n for n in bal if bal[n] > 0), key=lambda n: (-bal[n], n.lower()))[0]
        amt = min(-bal[debtor], bal[creditor])
        bal[debtor] += amt
        bal[creditor] -= amt
        out.append((debtor, creditor, amt))
    return out


def fmt_signed(c):
    if c == 0:
        return "0.00"
    return f"{'+' if c > 0 else '-'}{abs(c) // 100}.{abs(c) % 100:02d}"


def fmt_unsigned(c):
    return f"{abs(c) // 100}.{abs(c) % 100:02d}"


# ---------------------------------------------------------------- balances: output format

def test_signed_format_and_zero_without_sign():
    # Ann: +5.00 paid, then 0.03 share in Cy's expense. Cy: +0.03. Dan nets to zero.
    led = ledger(row(payer="Ann", amount="5.00", participants="Bob"),
                 row(date="2026-03-02", payer="Cy", amount="0.05", participants="Ann;Cy"),
                 row(date="2026-03-03", payer="Dan", amount="2.00", participants="Dan"))
    code, out, err = run(led, "balances", "{f}")
    assert (code, err) == (0, "")
    assert out == "Ann +4.97\nBob -5.00\nCy +0.03\nDan 0.00\n"


def test_exact_stdout_for_balances():
    led = ledger(row(payer="Ann", amount="60.00", participants="Ann;Bob;Cy"),
                 row(date="2026-03-02", payer="Bob", amount="30", participants="Ann;Bob"),
                 row(date="2026-03-03", payer="Cy", amount="9.00", participants="Ann;Bob;Cy"))
    code, out, err = run(led, "balances", "{f}")
    assert (code, err) == (0, "")
    assert out == "Ann +22.00\nBob -8.00\nCy -14.00\n"


def test_balances_zero_line_has_no_sign():
    # Ann pays 10 for Ann;Bob (5 each) and Bob pays 10 for Ann;Bob: both net to zero.
    led = ledger(row(payer="Ann", amount="10", participants="Ann;Bob"),
                 row(payer="Bob", amount="10", participants="Ann;Bob"))
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Ann 0.00\nBob 0.00\n"


def test_balances_negative_cent_sign():
    led = ledger(row(payer="Zed", amount="0.01", participants="Ann"))
    assert balances_text(led) == ["Ann -0.01", "Zed +0.01"]


def test_balances_large_amount_formatting():
    led = ledger(row(payer="Ann", amount="1234567.89", participants="Bob"))
    assert balances_text(led) == ["Ann +1234567.89", "Bob -1234567.89"]


def test_balances_sorted_ignoring_case():
    led = ledger(row(payer="bob", amount="3", participants="Zed"),
                 row(payer="Ann", amount="3", participants="cy"),
                 row(payer="dan", amount="3", participants="Eve"))
    names = [l.rsplit(" ", 1)[0] for l in balances_text(led)]
    assert names == ["Ann", "bob", "cy", "dan", "Eve", "Zed"]


def test_name_shown_with_first_appearance_spelling():
    led = ledger(row(payer="ann", amount="10", participants="Bob"),
                 row(date="2026-03-02", payer="ANN", amount="4", participants="bob"))
    assert [l.rsplit(" ", 1)[0] for l in balances_text(led)] == ["ann", "Bob"]


def test_first_appearance_payer_before_participants():
    # Same row: payer spelling wins over the participant spelling.
    led = ledger(row(payer="pat", amount="10", participants="PAT;Bob"))
    assert [l.rsplit(" ", 1)[0] for l in balances_text(led)] == ["Bob", "pat"]


def test_first_appearance_left_to_right_in_participants():
    led = ledger(row(payer="Zed", amount="10", participants="Mo"),
                 row(date="2026-03-02", payer="Zed", amount="10", participants="MO"))
    assert [l.rsplit(" ", 1)[0] for l in balances_text(led)] == ["Mo", "Zed"]


def test_names_are_trimmed():
    led = ledger('2026-03-01, Ann ,10.00,  Bob ;Cy  ,')
    assert balances_text(led) == ["Ann +10.00", "Bob -5.00", "Cy -5.00"]


def test_payer_need_not_participate():
    led = ledger(row(payer="Zed", amount="9.00", participants="Ann;Bob;Cy"))
    assert balances_text(led) == ["Ann -3.00", "Bob -3.00", "Cy -3.00", "Zed +9.00"]


def test_payer_may_also_be_participant():
    led = ledger(row(payer="Ann", amount="10", participants="Ann;Bob"))
    assert balances_text(led) == ["Ann +5.00", "Bob -5.00"]


def test_balances_text_is_empty_when_no_rows():
    code, out, err = run(ledger(), "balances", "{f}")
    assert (code, out, err) == (0, "", "")


# ---------------------------------------------------------------- splitting rule

def test_amount_forms_12_12_5_12_50():
    led = ledger(row(amount="12"), row(amount="12.5"), row(amount="12.50"))
    assert balances_text(led) == ["Ann +37.00", "Bob -37.00"]


def test_weights_split_proportionally():
    led = ledger(row(payer="Ann", amount="40.00", participants="Ann:1;Bob:3"))
    assert balances_text(led) == ["Ann +30.00", "Bob -30.00"]


def test_default_weight_is_one():
    led = ledger(row(payer="Zed", amount="30.00", participants="Ann:1;Bob;Cy:1"))
    assert balances_text(led) == ["Ann -10.00", "Bob -10.00", "Cy -10.00", "Zed +30.00"]


def test_weighted_leftover_goes_to_largest_remainder_not_alphabetical():
    # 4 cents, weights Ann:1 Bob:2 (W=3): floors 1 and 2, remainders 1 and 2.
    # The leftover cent must go to Bob (largest remainder), not Ann.
    led = ledger(row(payer="Zed", amount="0.04", participants="Ann:1;Bob:2"))
    assert balances_text(led) == ["Ann -0.01", "Bob -0.03", "Zed +0.04"]


def test_leftover_with_mixed_weights():
    # 1001 cents, Ann:2 Bob:1 Cy:1 (W=4): floors 500,250,250; leftover 1 to Ann (remainder 2).
    led = ledger(row(payer="Zed", amount="10.01", participants="Ann:2;Bob;Cy"))
    assert balances_text(led) == ["Ann -5.01", "Bob -2.50", "Cy -2.50", "Zed +10.01"]


def test_equal_split_leftover_cents_to_names_ascending():
    led = ledger(row(payer="Zed", amount="10.00", participants="Cy;bob;Ann"))
    assert balances_text(led) == ["Ann -3.34", "bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_leftover_tie_broken_ignoring_case_not_by_codepoint():
    # One cent, equal remainders. Ascending ignoring case picks "ann" over "Bob".
    # A case-sensitive sort would pick "Bob" (uppercase sorts first).
    led = ledger(row(payer="Zed", amount="0.01", participants="Bob;ann"))
    assert balances_text(led) == ["ann -0.01", "Bob 0.00", "Zed +0.01"]


def test_leftover_tie_case_insensitive_by_name_order():
    # "Zed" (uppercase) vs "amy": ascending ignoring case puts amy first.
    led = ledger(row(payer="Mia", amount="0.01", participants="Zed;amy"))
    assert balances_text(led) == ["amy -0.01", "Mia +0.01", "Zed 0.00"]


def test_each_share_is_floor_plus_at_most_one_cent():
    # 100 cents among 7 equal participants: floors 14, 2 leftover cents.
    parts = ";".join(f"P{i}" for i in range(7))
    led = ledger(row(payer="Zed", amount="1.00", participants=parts))
    shares = sorted(int(l.rsplit(" ", 1)[1].replace(".", "").replace("-", ""))
                    for l in balances_text(led) if not l.startswith("Zed"))
    assert shares == [14] * 5 + [15] * 2


def test_balances_always_sum_to_zero():
    led = ledger(row(payer="Ann", amount="7.77", participants="Bob:2;Cy;Dan:5"),
                 row(date="2026-03-02", payer="Cy", amount="0.99", participants="Ann;Eve:3"),
                 row(date="2026-03-03", payer="Eve", amount="13.13", participants="Ann;Bob;Cy;Dan;Eve"))
    assert sum(b["cents"] for b in balances_json(led)) == 0


# ---------------------------------------------------------------- settle

def test_settle_exact_greedy_sequence():
    # Ann +10, Bob -5, Cy -3, Dan -2.
    led = ledger(row(payer="Ann", amount="10", participants="Bob:5;Cy:3;Dan:2"))
    assert settle_text(led) == ["Bob -> Ann 5.00", "Cy -> Ann 3.00", "Dan -> Ann 2.00"]


def test_settle_debtor_is_most_negative_not_first_listed():
    # Ann +30, Bob -10, Cy -20: Cy (most negative) settles first.
    led = ledger(row(payer="Ann", amount="10", participants="Bob"),
                 row(date="2026-03-02", payer="Ann", amount="20", participants="Cy"))
    assert settle_text(led) == ["Cy -> Ann 20.00", "Bob -> Ann 10.00"]


def test_settle_debtor_tie_broken_by_name_ascending():
    # Cy and Dan both owe 10. Ann and Bob are owed 10 each.
    led = ledger(row(payer="Ann", amount="10", participants="Cy"),
                 row(date="2026-03-02", payer="Bob", amount="10", participants="Dan"))
    assert settle_text(led) == ["Cy -> Ann 10.00", "Dan -> Bob 10.00"]


def test_settle_debtor_tie_case_insensitive():
    # "amy" and "Zed" both owe 10: ignoring case, amy comes first.
    led = ledger(row(payer="Bob", amount="10", participants="Zed"),
                 row(date="2026-03-02", payer="Cy", amount="10", participants="amy"))
    assert settle_text(led) == ["amy -> Bob 10.00", "Zed -> Cy 10.00"]


def test_settle_creditor_tie_broken_by_name_ascending():
    led = ledger(row(payer="Zed", amount="10", participants="Bob"),
                 row(date="2026-03-02", payer="Ann", amount="10", participants="Cy"))
    # Creditors Ann and Zed are both +10; debtors Bob and Cy are both -10.
    out = settle_text(led)
    assert out == ["Bob -> Ann 10.00", "Cy -> Zed 10.00"]


def test_settle_partial_payment_then_next_debtor():
    # Ann +10, Bob -4, Cy -6: Cy pays 6 first; Bob pays the remaining 4.
    led = ledger(row(payer="Ann", amount="10", participants="Bob:4;Cy:6"))
    # With equal-weight style shares, the 10-cent split is exact (4 and 6).
    assert settle_text(led) == ["Cy -> Ann 6.00", "Bob -> Ann 4.00"]


def test_settle_nothing_to_settle_text():
    code, out, err = run(ledger(), "settle", "{f}")
    assert (code, out, err) == (0, "nothing to settle\n", "")


def test_settle_nothing_when_all_balances_zero():
    led = ledger(row(payer="Ann", amount="10", participants="Ann;Bob"),
                 row(date="2026-03-02", payer="Bob", amount="10", participants="Ann;Bob"))
    assert settle_text(led) == ["nothing to settle"]


def test_settle_cents_format():
    led = ledger(row(payer="Ann", amount="0.05", participants="Bob"))
    assert settle_text(led) == ["Bob -> Ann 0.05"]


def test_settle_never_prints_zero_balance_people():
    # Ann +5, Bob -5, Cy +4, Dan -4. Nobody else appears in transfers.
    led = ledger(row(payer="Ann", amount="10", participants="Ann;Bob"),
                 row(date="2026-03-02", payer="Cy", amount="4", participants="Dan"),
                 row(date="2026-03-03", payer="Eve", amount="2", participants="Eve"))
    assert settle_text(led) == ["Bob -> Ann 5.00", "Dan -> Cy 4.00"]


# ---------------------------------------------------------------- ledger parsing

def test_bom_is_accepted():
    raw = b"\xef\xbb\xbf" + ledger(row(payer="Ann", amount="2", participants="Bob")).encode()
    code, out, err = run(None, "balances", "{f}", raw=raw)
    assert (code, err) == (0, "")
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_crlf_line_endings_accepted():
    raw = (HDR + row(payer="Ann", amount="2", participants="Bob") + "\r\n").replace("\n", "\r\n").encode()
    code, out, err = run(None, "balances", "{f}", raw=raw)
    assert (code, err) == (0, "")
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_columns_in_any_order():
    led = "note,amount,participants,payer,date\nlunch,8.00,Bob,Ann,2026-03-01\n"
    assert balances_text(led) == ["Ann +8.00", "Bob -8.00"]


def test_quoted_note_with_commas_and_escaped_quotes():
    led = ledger('2026-03-01,Ann,6.00,Bob,"coffee, cake, ""big"""')
    assert balances_text(led) == ["Ann +6.00", "Bob -6.00"]


def test_quoted_participants_field():
    led = ledger('2026-03-01,Ann,6.00,"Bob;Cy",x')
    assert balances_text(led) == ["Ann +6.00", "Bob -3.00", "Cy -3.00"]


def test_empty_note_is_allowed():
    led = ledger("2026-03-01,Ann,6.00,Bob,")
    assert balances_text(led) == ["Ann +6.00", "Bob -6.00"]


def test_note_starting_with_hash_inside_data_row_is_not_a_comment():
    led = ledger(row(payer="Ann", amount="6", participants="Bob", note="# not a comment"))
    assert balances_text(led) == ["Ann +6.00", "Bob -6.00"]


def test_comment_and_blank_lines_are_ignored():
    led = (HDR
           + "# a comment\n"
           + "\n"
           + "   \n"
           + "\t\n"
           + "   # indented comment\n"
           + row(payer="Ann", amount="6", participants="Bob") + "\n"
           + "# trailing comment\n")
    assert balances_text(led) == ["Ann +6.00", "Bob -6.00"]


def test_comment_before_header_is_ignored():
    led = "# generated file\n" + ledger(row(payer="Ann", amount="6", participants="Bob"))
    assert balances_text(led) == ["Ann +6.00", "Bob -6.00"]


def test_header_only_ledger_is_valid():
    code, out, err = run(HDR, "balances", "{f}")
    assert (code, out, err) == (0, "", "")
    code, out, err = run(HDR, "settle", "{f}")
    assert (code, out, err) == (0, "nothing to settle\n", "")


def test_ignored_lines_count_toward_line_numbers():
    led = (HDR
           + "# comment\n"       # line 2
           + "\n"                # line 3
           + row(amount="1") + "\n"  # line 4 valid
           + "   \n"            # line 5
           + row(date="bad") + "\n")  # line 6
    assert_invalid(led, ["line 6: bad-date"], "balances", "{f}")


def test_line_numbers_with_crlf_and_bom():
    raw = ("\ufeff" + HDR.rstrip("\n") + "\r\n" + row(payer="Ann", amount="1", participants="Bob") + "\r\n"
           + row(payer="", amount="1", participants="Bob") + "\r\n").encode("utf-8")
    code, out, err = run(None, "balances", "{f}", raw=raw)
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-payer"]


def test_multiple_invalid_rows_reported_in_line_order_one_each():
    led = (HDR
           + row(amount="1") + "\n"        # line 2 valid
           + "# comment\n"                 # line 3
           + row(date="2026-02-30") + "\n" # line 4 bad-date
           + "\n"                          # line 5
           + row(amount="1") + "\n"        # line 6 valid
           + "   \n"                       # line 7
           + row(amount="-1") + "\n")      # line 8 bad-amount
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 4: bad-date", "line 8: bad-amount"]


def test_invalid_row_suppresses_all_stdout_even_for_valid_rows():
    led = ledger(row(payer="Ann", amount="1", participants="Bob"),
                 row(date="nope", payer="Ann", amount="1", participants="Bob"))
    for cmd in (["balances"], ["settle"], ["balances", "--format", "json"], ["settle", "--format", "json"]):
        code, out, err = run(led, cmd[0], "{f}", *cmd[1:])
        assert code == 2 and out == ""
        assert lines(err) == ["line 3: bad-date"]


def test_invalid_row_outside_range_still_fails():
    led = ledger(row(date="2026-01-01", payer="Ann", amount="1", participants="Bob"),
                 row(date="2026-03-01", payer="Ann", amount="zero", participants="Bob"))
    code, out, err = run(led, "balances", "{f}", "--since", "2026-02-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_header_names_must_be_present_as_exact_required_columns():
    # A column named differently is a missing required column.
    led = "date,payer,amount,participants,comment\n2026-03-01,Ann,1,Bob,x\n"
    code, out, err = run(led, "balances", "{f}")
    assert (code, out) == (2, "")
    assert lines(err) == ["header: missing note"]


# ---------------------------------------------------------------- header errors

@pytest.mark.parametrize("col", ["date", "payer", "amount", "participants", "note"])
def test_each_missing_header_column_is_reported(col):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != col]
    led = ",".join(cols) + "\n" + ",".join(["x"] * len(cols)) + "\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert err == f"header: missing {col}\n"


@pytest.mark.parametrize("missing, expected", [
    (("payer", "amount"), "payer"),
    (("note", "date"), "date"),
    (("participants", "note"), "participants"),
    (("amount", "participants", "note"), "amount"),
])
def test_only_first_missing_column_is_reported(missing, expected):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c not in missing]
    led = ",".join(cols) + "\n2026-03-01,Ann,bad,,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert err == f"header: missing {expected}\n"


def test_missing_header_column_reports_nothing_else():
    led = "date,payer,amount,participants\n2026-03-01,,bad,\n2026-13-01,,,\n"
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert err == "header: missing note\n"


# ---------------------------------------------------------------- row validation

@pytest.mark.parametrize("amount", [
    "0", "0.00", "-1", "-0.50", "+1", '"1,000"', "1e3", "1E2", "12.345",
    "abc", "", "1.2.3", "nan", "inf", "1 000", "$5",
])
def test_invalid_amounts(amount):
    assert_invalid(ledger(row(amount=amount)), ["line 2: bad-amount"], "balances", "{f}")


@pytest.mark.parametrize("amount", ["1", "1.0", "1.5", "1.05", "0.01", "99999999.99"])
def test_valid_amounts(amount):
    code, out, err = run(ledger(row(amount=amount)), "balances", "{f}")
    assert (code, err) == (0, "")


@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-13-01", "2026-00-10", "2026-04-31", "26-03-01", "2026/03/01",
    "03-01-2026", "2026-3-1", "", "2026-03-01T00:00", "yesterday",
])
def test_invalid_dates(date):
    assert_invalid(ledger(row(date=date)), ["line 2: bad-date"], "balances", "{f}")


def test_leap_day_rules():
    code, out, err = run(ledger(row(date="2024-02-29", amount="1")), "balances", "{f}")
    assert (code, err) == (0, "")
    assert_invalid(ledger(row(date="2023-02-29")), ["line 2: bad-date"], "balances", "{f}")


@pytest.mark.parametrize("payer", ["", "   "])
def test_empty_payer_is_bad(payer):
    assert_invalid(ledger(row(payer=payer)), ["line 2: bad-payer"], "balances", "{f}")


@pytest.mark.parametrize("participants", [
    "", "   ", "Ann;", ";Bob", "Ann;;Bob", "Ann:0", "Ann:-1", "Ann:1.5", "Ann:x",
    "Ann:", "Ann;ann", "Ann:2;ANN:3", "Ann;Bob;bob", "Bob:2;Bob", "Bob:0",
])
def test_invalid_participants(participants):
    assert_invalid(ledger(row(participants=participants)), ["line 2: bad-participants"], "balances", "{f}")


@pytest.mark.parametrize("bad_date, bad_payer, bad_amount, bad_parts, expected", [
    ("nope", "", "x", "", "bad-date"),
    ("2026-03-01", "", "x", "", "bad-payer"),
    ("2026-03-01", "Ann", "x", "", "bad-amount"),
    ("2026-03-01", "Ann", "1", "", "bad-participants"),
])
def test_only_first_failing_check_is_reported(bad_date, bad_payer, bad_amount, bad_parts, expected):
    led = ledger(row(date=bad_date, payer=bad_payer, amount=bad_amount, participants=bad_parts))
    assert_invalid(led, [f"line 2: {expected}"], "balances", "{f}")


def test_bad_amount_precedes_bad_participants():
    led = ledger(row(amount="0", participants=""))
    assert_invalid(led, ["line 2: bad-amount"], "balances", "{f}")


# ---------------------------------------------------------------- date range

RANGE_LEDGER = ledger(
    row(date="2026-01-31", payer="Ann", amount="10", participants="Bob"),
    row(date="2026-02-01", payer="Cy", amount="6", participants="Dan"),
    row(date="2026-02-28", payer="Cy", amount="4", participants="Dan"),
    row(date="2026-03-01", payer="Eve", amount="8", participants="Fay"),
)


def test_since_and_until_are_inclusive():
    assert balances_text(RANGE_LEDGER, "--since", "2026-02-01", "--until", "2026-02-28") == \
        ["Cy +10.00", "Dan -10.00"]


def test_since_alone():
    assert balances_text(RANGE_LEDGER, "--since", "2026-02-01") == \
        ["Cy +10.00", "Dan -10.00", "Eve +8.00", "Fay -8.00"]


def test_until_alone():
    assert balances_text(RANGE_LEDGER, "--until", "2026-02-28") == \
        ["Ann +10.00", "Bob -10.00", "Cy +10.00", "Dan -10.00"]


def test_single_day_range():
    assert balances_text(RANGE_LEDGER, "--since", "2026-02-28", "--until", "2026-02-28") == \
        ["Cy +4.00", "Dan -4.00"]


def test_people_only_in_excluded_rows_are_not_printed():
    names = [l.rsplit(" ", 1)[0] for l in balances_text(RANGE_LEDGER, "--since", "2026-02-01", "--until", "2026-02-28")]
    assert "Ann" not in names and "Bob" not in names
    assert "Eve" not in names and "Fay" not in names


def test_range_with_no_rows_gives_empty_output():
    code, out, err = run(RANGE_LEDGER, "balances", "{f}", "--since", "2030-01-01")
    assert (code, out, err) == (0, "", "")
    code, out, err = run(RANGE_LEDGER, "settle", "{f}", "--since", "2030-01-01")
    assert (code, out, err) == (0, "nothing to settle\n", "")


def test_settle_respects_range():
    assert settle_text(RANGE_LEDGER, "--since", "2026-02-01", "--until", "2026-02-28") == ["Dan -> Cy 10.00"]


def test_range_applies_to_json_output():
    assert balances_json(RANGE_LEDGER, "--since", "2026-03-01") == \
        [{"name": "Eve", "cents": 800}, {"name": "Fay", "cents": -800}]


def test_range_boundary_is_not_off_by_one_on_either_side():
    led = ledger(row(date="2026-02-01", payer="Ann", amount="1", participants="Bob"),
                 row(date="2026-01-31", payer="Cy", amount="1", participants="Dan"))
    assert balances_text(led, "--since", "2026-02-01") == ["Ann +1.00", "Bob -1.00"]


# ---------------------------------------------------------------- options

def test_format_text_is_default_and_explicit_text_matches():
    led = RANGE_LEDGER
    code1, out1, _ = run(led, "balances", "{f}")
    code2, out2, _ = run(led, "balances", "{f}", "--format", "text")
    assert code1 == code2 == 0 and out1 == out2


def test_options_before_positional_arguments():
    code, out, err = run(RANGE_LEDGER, "--format", "json", "balances", "{f}")
    assert (code, err) == (0, "")
    assert json.loads(out)["balances"][0]["name"] == "Ann"


def test_options_between_positional_arguments():
    code, out, err = run(RANGE_LEDGER, "balances", "--since", "2026-02-01", "{f}")
    assert (code, err) == (0, "")
    assert lines(out) == ["Cy +10.00", "Dan -10.00", "Eve +8.00", "Fay -8.00"]


def test_options_after_positional_arguments_with_range():
    assert balances_text(RANGE_LEDGER, "--until", "2026-01-31") == ["Ann +10.00", "Bob -10.00"]


def test_balances_json_shape_and_order_matches_text():
    led = ledger(row(payer="bob", amount="10", participants="Zed;ann"))
    doc = json.loads(run(led, "balances", "{f}", "--format", "json")[1])
    assert set(doc) == {"balances"}
    assert doc["balances"] == [
        {"name": "ann", "cents": -500},
        {"name": "bob", "cents": 1000},
        {"name": "Zed", "cents": -500},
    ]
    text_names = [l.rsplit(" ", 1)[0] for l in balances_text(led)]
    assert [b["name"] for b in doc["balances"]] == text_names


def test_balances_json_cents_are_integers_with_sign():
    led = ledger(row(payer="Ann", amount="0.07", participants="Bob;Cy"))
    for b in balances_json(led):
        assert isinstance(b["cents"], int)
    assert {b["name"]: b["cents"] for b in balances_json(led)} == {"Ann": 7, "Bob": -4, "Cy": -3}


def test_balances_json_empty_list_when_nothing_considered():
    code, out, err = run(ledger(), "balances", "{f}", "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"balances": []}


def test_settle_json_shape():
    led = ledger(row(payer="Ann", amount="12.50", participants="Bob"))
    code, out, err = run(led, "settle", "{f}", "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 1250}]}


def test_settle_json_empty_when_nothing_to_settle():
    code, out, err = run(ledger(), "settle", "{f}", "--format", "json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {"transfers": []}


def test_settle_json_matches_text_transfers():
    led = ledger(row(payer="Ann", amount="10", participants="Bob:5;Cy:3;Dan:2"))
    t = settle_json(led)
    assert [f"{x['from']} -> {x['to']} {fmt_unsigned(x['cents'])}" for x in t] == settle_text(led)


def test_settle_json_cents_are_positive_integers():
    led = ledger(row(payer="Ann", amount="10", participants="Bob:5;Cy:3;Dan:2"))
    for x in settle_json(led):
        assert isinstance(x["cents"], int) and x["cents"] > 0


# ---------------------------------------------------------------- usage errors

@pytest.mark.parametrize("args", [
    [],
    ["balances"],
    ["settle"],
    ["explode", "{f}"],
    ["Balances", "{f}"],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--bogus", "x"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "yesterday"],
    ["balances", "{f}", "--until", "2026-13-01"],
    ["balances", "{f}", "--until"],
    ["settle", "{f}", "--format", "JSON"],
])
def test_usage_errors_exit_64(args):
    code, out, err = run(RANGE_LEDGER, *args)
    assert code == 64
    assert out == ""


# ---------------------------------------------------------------- unreadable ledger

def test_missing_ledger_file_exit_3(tmp_path):
    path = str(tmp_path / "nope.csv")
    p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), "balances", path],
                       capture_output=True, text=True, timeout=20, cwd=APP_DIR)
    assert p.returncode == 3
    assert p.stdout == ""
    assert p.stderr == f"error: cannot read {path}\n"


def test_directory_as_ledger_exit_3(tmp_path):
    p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), "settle", str(tmp_path)],
                       capture_output=True, text=True, timeout=20, cwd=APP_DIR)
    assert p.returncode == 3
    assert p.stdout == ""
    assert p.stderr.startswith("error: cannot read ")


# ---------------------------------------------------------------- randomized cross-check

def _random_ledger(rng):
    base = ["ann", "bob", "cy", "dan", "eve", "fay"]
    variants = {b: [b, b.capitalize(), b.upper(), " " + b.capitalize() + " "] for b in base}
    rows, expenses = [], []
    seen_display = {}
    for _ in range(rng.randint(1, 8)):
        def pick():
            b = rng.choice(base)
            disp = rng.choice(variants[b])
            return b, disp

        payer_key, payer_disp = pick()
        k = rng.randint(1, 4)
        chosen = rng.sample(base, k)
        parts = []
        for b in chosen:
            disp = rng.choice(variants[b])
            w = rng.randint(1, 3)
            parts.append((disp, w))
        cents = rng.randint(1, 200000)
        if cents % 100 == 0:
            amt = str(cents // 100)
        elif cents % 10 == 0:
            amt = f"{cents // 100}.{cents % 100 // 10}"
        else:
            amt = f"{cents // 100}.{cents % 100:02d}"
        date = f"2026-0{rng.randint(1, 3)}-{rng.randint(1, 28):02d}"
        ptxt = ";".join(d if w == 1 else f"{d}:{w}" for d, w in parts)
        rows.append(f"{date},{payer_disp},{amt},{ptxt},n{rng.randint(0, 9)}")
        expenses.append((payer_disp, cents, parts))
    return HDR + "".join(r + "\n" for r in rows), expenses


@pytest.mark.parametrize("seed", range(40))
def test_random_ledgers_match_reference_model(seed):
    rng = random.Random(seed)
    text, expenses = _random_ledger(rng)
    expected_bal = ref_balances(expenses)

    code, out, err = run(text, "balances", "{f}", "--format", "json")
    assert code == 0, err
    got = json.loads(out)["balances"]
    assert [(b["name"], b["cents"]) for b in got] == expected_bal
    assert sum(c for _, c in expected_bal) == 0

    code, out, err = run(text, "balances", "{f}")
    assert code == 0, err
    assert lines(out) == [f"{n} {fmt_signed(c)}" for n, c in expected_bal]

    exp_tr = ref_settle(expected_bal)
    code, out, err = run(text, "settle", "{f}")
    assert code == 0, err
    if exp_tr:
        assert lines(out) == [f"{d} -> {c} {fmt_unsigned(x)}" for d, c, x in exp_tr]
    else:
        assert lines(out) == ["nothing to settle"]

    code, out, err = run(text, "settle", "{f}", "--format", "json")
    assert code == 0, err
    assert [(t["from"], t["to"], t["cents"]) for t in json.loads(out)["transfers"]] == exp_tr


@pytest.mark.parametrize("seed", range(10))
def test_settlement_clears_every_balance(seed):
    rng = random.Random(1000 + seed)
    text, _ = _random_ledger(rng)
    bal = {b["name"]: b["cents"] for b in balances_json(text)}
    for t in settle_json(text):
        assert t["cents"] > 0
        bal[t["from"]] += t["cents"]
        bal[t["to"]] -= t["cents"]
    assert all(v == 0 for v in bal.values())
