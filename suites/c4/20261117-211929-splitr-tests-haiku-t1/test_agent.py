"""Regression suite for splitr, written against SPEC.md.

Everything runs the CLI as a black box through tests/helpers.py (or the thin
subprocess wrapper `cli` defined here for cases helpers.run cannot express).
"""
import json
import os
import random
import re
import subprocess
import sys

import pytest

from helpers import APP_DIR, run, lines

HEADER = "date,payer,amount,participants,note\n"


# ---------------------------------------------------------------- helpers

def cli(*args):
    """Run splitr.py with raw argv (no ledger file substitution)."""
    p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), *args],
                       capture_output=True, text=True, timeout=20, cwd=APP_DIR)
    return p.returncode, p.stdout, p.stderr


def ledger(*rows, header=HEADER):
    return header + "".join(r + "\n" for r in rows)


def cents_fmt(c, signed):
    """Reference formatter for expected text output."""
    sign = ""
    if signed and c != 0:
        sign = "+" if c > 0 else "-"
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


# Independent reference model of SPEC.md, used for randomized comparison.
def ref_shares(cents, parts):
    """parts: list of (name, weight). Returns {name: share_cents}."""
    total = sum(w for _, w in parts)
    shares = {n: cents * w // total for n, w in parts}
    leftover = cents - sum(shares.values())
    order = sorted(parts, key=lambda p: (-((cents * p[1]) % total), p[0].lower()))
    for n, _ in order[:leftover]:
        shares[n] += 1
    return shares


def ref_balances(rows):
    """rows: list of (payer, cents, [(name, weight), ...]) in file order.
    Returns list of (spelling, cents) sorted by name ignoring case."""
    spelling, bal = {}, {}

    def touch(n):
        k = n.lower()
        spelling.setdefault(k, n)
        bal.setdefault(k, 0)

    for payer, cents, parts in rows:
        touch(payer)
        for n, _ in parts:
            touch(n)
        bal[payer.lower()] += cents
        for n, s in ref_shares(cents, parts).items():
            bal[n.lower()] -= s
    return [(spelling[k], bal[k]) for k in sorted(bal)]


def ref_settle(balances):
    bal = {n.lower(): [n, c] for n, c in balances}
    out = []
    while any(v[1] for v in bal.values()):
        debtor = min(bal, key=lambda k: (bal[k][1], k))
        creditor = min(bal, key=lambda k: (-bal[k][1], k))
        amt = min(-bal[debtor][1], bal[creditor][1])
        out.append((bal[debtor][0], bal[creditor][0], amt))
        bal[debtor][1] += amt
        bal[creditor][1] -= amt
    return out


TRIP = ledger(
    "2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner",
    "2026-03-02,Bob,30,Ann;Bob,taxi",
    '2026-03-03,Cy,9.00,Ann;Bob;Cy,"coffee, cake"',
)


# ---------------------------------------------------------- basic reports

def test_balances_text_exact_output():
    code, out, err = run(TRIP, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +22.00\nBob -8.00\nCy -14.00\n"


def test_settle_text_exact_output():
    code, out, err = run(TRIP, "settle", "{f}")
    assert code == 0 and err == ""
    assert out == "Cy -> Ann 14.00\nBob -> Ann 8.00\n"


def test_balances_json_exact_document():
    code, out, err = run(TRIP, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    assert json.loads(out) == {"balances": [
        {"name": "Ann", "cents": 2200},
        {"name": "Bob", "cents": -800},
        {"name": "Cy", "cents": -1400}]}


def test_settle_json_exact_document():
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 1400},
        {"from": "Bob", "to": "Ann", "cents": 800}]}


def test_json_is_single_document_on_stdout():
    code, out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    assert code == 0
    assert out.count("\n") <= 1  # one document, at most a trailing newline
    json.loads(out)


def test_default_format_is_text():
    _, out_default, _ = run(TRIP, "balances", "{f}")
    _, out_text, _ = run(TRIP, "balances", "{f}", "--format", "text")
    assert out_default == out_text == "Ann +22.00\nBob -8.00\nCy -14.00\n"


def test_cents_are_integers_in_json():
    _, out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    for entry in json.loads(out)["balances"]:
        assert isinstance(entry["cents"], int) and not isinstance(entry["cents"], bool)


def test_balances_sum_to_zero_in_json():
    _, out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    assert sum(e["cents"] for e in json.loads(out)["balances"]) == 0


# ------------------------------------------------------ amount formatting

def test_positive_and_negative_sign_formatting():
    led = ledger("2026-01-01,Ann,3.00,Ann;Bob,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +1.50\nBob -1.50\n"


def test_zero_balance_has_no_sign_text_and_zero_json():
    led = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-01-02,Bob,10,Ann;Bob,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann 0.00\nBob 0.00\n"
    _, out, _ = run(led, "balances", "{f}", "--format", "json")
    assert json.loads(out) == {"balances": [{"name": "Ann", "cents": 0},
                                            {"name": "Bob", "cents": 0}]}


def test_zero_balance_person_not_in_settle():
    led = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-01-02,Bob,10,Ann;Bob,",
                 "2026-01-03,Cy,4,Cy,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "nothing to settle\n"


def test_sub_ten_cent_values_are_padded():
    led = ledger("2026-01-01,Ann,0.10,Ann;Bob,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +0.05\nBob -0.05\n"


def test_large_amount_formatting():
    # 123456789 cents split two ways: 61728394 each, leftover cent to Ann (name order).
    led = ledger("2026-01-01,Ann,1234567.89,Ann;Bob,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +617283.94\nBob -617283.94\n"


@pytest.mark.parametrize("amount, cents", [
    ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), ("0.5", 50),
])
def test_valid_amount_forms(amount, cents):
    led = ledger(f"2026-01-01,Ann,{amount},Bob,")
    code, out, err = run(led, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    assert json.loads(out) == {"balances": [{"name": "Ann", "cents": cents},
                                            {"name": "Bob", "cents": -cents}]}


def test_payer_outside_participants_pays_full_amount():
    led = ledger("2026-01-01,Ann,30,Bob;Cy;Dee,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +30.00\nBob -10.00\nCy -10.00\nDee -10.00\n"


def test_payer_as_participant_bears_own_share():
    led = ledger("2026-01-01,Ann,30,Ann;Bob;Cy,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +20.00\nBob -10.00\nCy -10.00\n"


# ---------------------------------------------------------- weights & rounding

def test_weights_split_proportionally():
    led = ledger("2026-03-01,Ann,40.00,Ann:1;Bob:3,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +30.00\nBob -30.00\n"


def test_default_weight_is_one():
    led = ledger("2026-03-01,Zed,9.00,Ann:1;Bob;Cy,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann -3.00\nBob -3.00\nCy -3.00\nZed +9.00\n"


def test_leftover_cent_goes_to_name_order_on_equal_remainders():
    led = ledger("2026-03-01,Zed,10.00,Ann;Bob;Cy,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann -3.34\nBob -3.33\nCy -3.33\nZed +10.00\n"


def test_leftover_goes_to_largest_remainder_not_first_name():
    # 10 cents split 1:2 -> base 3 and 6, remainders 1/3 and 2/3.
    # 1000 cents split 1:2 -> base 333 and 666, remainders 1 and 2 (out of 3).
    # The leftover cent must go to Bob (larger remainder), though Ann sorts first.
    led = ledger("2026-03-01,Zed,10.00,Ann:1;Bob:2,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann -3.33\nBob -6.67\nZed +10.00\n"


def test_two_cents_among_three_gives_one_cent_to_two_first_names():
    led = ledger("2026-03-01,Zed,0.02,Cy;Bob;Ann,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann -0.01\nBob -0.01\nCy 0.00\nZed +0.02\n"


def test_tie_break_ignores_case_for_leftover():
    # Equal remainders. Case-insensitive order is albert < Bert; a case-sensitive
    # sort would give the cent to Bert.
    led = ledger("2026-03-01,Zed,0.01,Bert;albert,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "albert -0.01\nBert 0.00\nZed +0.01\n"


def test_tie_break_lowercase_name_order_for_leftover_with_mixed_case_input():
    led = ledger("2026-03-01,Zed,10.00,cy;BOB;Ann,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann -3.34\nBOB -3.33\ncy -3.33\nZed +10.00\n"


def test_odd_weights_still_reconcile_to_zero():
    led = ledger("2026-03-01,Zed,0.01,A:3;B:5;C:7;D:2;E:1;F:1;G:4,")
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    assert code == 0
    assert sum(e["cents"] for e in json.loads(out)["balances"]) == 0


# ------------------------------------------------------------- names

def test_names_merge_case_insensitively_and_keep_first_spelling():
    led = ledger("2026-01-01,KIM,10,kim;Bob,", "2026-01-02,Bob,10,Kim;bob,")
    _, out, _ = run(led, "balances", "{f}")
    # Each pays 10 for the pair: KIM and Bob both net to zero.
    assert lines(out) == ["Bob 0.00", "KIM 0.00"]


def test_spelling_first_appearance_payer_before_participants():
    led = ledger("2026-01-01,KIM,10,kim;Bob,")
    _, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Bob -5.00", "KIM +5.00"]


def test_spelling_from_first_row_wins():
    led = ledger("2026-01-01,bob,10,Ann;Bob,", "2026-01-02,Bob,10,Ann;Bob,")
    _, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann -10.00", "bob +10.00"]


def test_names_are_trimmed():
    led = ledger("2026-01-01,  Ann  ,10, Bob ; Ann ,")
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Ann +5.00\nBob -5.00\n"


def test_balances_sorted_by_name_ignoring_case():
    led = ledger("2026-01-01,beta,10,gamma;Alpha,")
    _, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Alpha -5.00", "beta +10.00", "gamma -5.00"]


def test_quoted_name_with_comma():
    led = ledger('2026-01-01,"Smith, J",10,"Smith, J;Bob",')
    _, out, _ = run(led, "balances", "{f}")
    assert out == "Bob -5.00\nSmith, J +5.00\n"


def test_quoted_note_with_embedded_quote_and_comma():
    led = ledger('2026-01-01,Ann,10,Ann;Bob,"said ""hi"", ok"')
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_empty_note_is_allowed():
    led = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-01-01,Ann,10,Ann;Bob,")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +10.00\nBob -10.00\n"


def test_hash_inside_a_row_is_not_a_comment():
    led = ledger("2026-01-01,Ann,10,Ann;Bob,#lunch")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


# ------------------------------------------------------------- settle

def test_settle_most_negative_debtor_first():
    led = ledger("2026-01-01,Ann,10.00,Bob:1;Cy:4,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "Cy -> Ann 8.00\nBob -> Ann 2.00\n"


def test_settle_most_positive_creditor_first():
    led = ledger("2026-01-01,Zed,10.00,Ann,", "2026-01-02,Yan,4.00,Ann,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "Ann -> Zed 10.00\nAnn -> Yan 4.00\n"


def test_settle_debtor_tie_broken_by_name_ignoring_case():
    # Beta and alpha both -10; alpha sorts first ignoring case.
    led = ledger("2026-01-01,Zed,20.00,Beta;alpha,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "alpha -> Zed 10.00\nBeta -> Zed 10.00\n"


def test_settle_creditor_tie_broken_by_name_ignoring_case():
    led = ledger("2026-01-01,Beta,10.00,Ann,", "2026-01-02,alpha,10.00,Ann,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "Ann -> alpha 10.00\nAnn -> Beta 10.00\n"


def test_settle_tie_between_debtors_uses_name_order():
    led = ledger("2026-01-01,Ann,10.00,Bob;Cy,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "Bob -> Ann 5.00\nCy -> Ann 5.00\n"


def test_settle_partial_payment_on_smaller_side():
    # Ann +3, Bob -5, Cy +2. Bob pays Ann the smaller of 5 and 3, then Cy the rest.
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,", "2026-01-02,Cy,2.00,Ann,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "Bob -> Ann 3.00\nBob -> Cy 2.00\n"


def test_settle_text_formats_two_decimals():
    led = ledger("2026-01-01,Ann,0.10,Bob,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "Bob -> Ann 0.10\n"


def test_settle_amounts_are_unsigned():
    _, out, _ = run(TRIP, "settle", "{f}")
    for line in lines(out):
        assert re.fullmatch(r"\S+ -> \S+ \d+\.\d{2}", line), line


def test_settle_nothing_when_all_zero_text_and_json():
    led = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-01-02,Bob,10,Ann;Bob,")
    _, out, _ = run(led, "settle", "{f}")
    assert out == "nothing to settle\n"
    _, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert json.loads(out) == {"transfers": []}


def test_settle_transfers_clear_all_balances():
    _, bal_out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    _, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    net = {e["name"]: e["cents"] for e in json.loads(bal_out)["balances"]}
    for t in json.loads(out)["transfers"]:
        net[t["from"]] += t["cents"]
        net[t["to"]] -= t["cents"]
    assert all(v == 0 for v in net.values())


# ------------------------------------------------------- ledger parsing

def test_bom_is_ignored():
    raw = b"\xef\xbb\xbf" + ledger("2026-01-01,Ann,10,Ann;Bob,").encode()
    code, out, err = run(None, "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_crlf_line_endings():
    text = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-01-02,Bob,4,Ann;Bob,").replace("\n", "\r\n")
    code, out, err = run(None, "balances", "{f}", raw=text.encode())
    assert code == 0 and err == ""
    assert out == "Ann +3.00\nBob -3.00\n"


def test_last_row_without_trailing_newline():
    text = HEADER + "2026-01-01,Ann,10,Ann;Bob,"
    code, out, _ = run(None, "balances", "{f}", raw=text.encode())
    assert code == 0
    assert out == "Ann +5.00\nBob -5.00\n"


def test_columns_in_any_order():
    text = 'note,amount,participants,payer,date\n,10,"Ann;Bob",Ann,2026-01-01\n'
    code, out, err = run(text, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_header_only_is_valid_and_empty():
    code, out, err = run(HEADER, "balances", "{f}")
    assert code == 0 and out == "" and err == ""
    code, out, err = run(HEADER, "balances", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"balances": []}
    code, out, err = run(HEADER, "settle", "{f}")
    assert code == 0 and out == "nothing to settle\n" and err == ""
    code, out, err = run(HEADER, "settle", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"transfers": []}


def test_comment_and_blank_lines_are_ignored():
    text = (HEADER + "\n"
            "# a comment\n"
            "   \n"
            "\t# indented comment\n"
            "2026-01-01,Ann,10,Ann;Bob,\n")
    code, out, err = run(text, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_line_numbers_count_ignored_lines():
    text = (HEADER +
            "# comment\n"                      # line 2
            "\n"                               # line 3
            "2026-01-01,Ann,10,Ann;Bob,\n"     # line 4 valid
            "   \n"                            # line 5
            "2026-13-01,Ann,10,Ann;Bob,\n")    # line 6 invalid
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 6: bad-date"]


def test_line_numbers_with_crlf_and_bom():
    text = "\ufeff" + HEADER + "# c\r\n2026-01-01,Ann,x,Ann;Bob,\r\n"
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 3: bad-amount"]


def test_header_is_line_one_for_first_row_error():
    text = ledger("2026-01-01,Ann,nope,Ann;Bob,")
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 2: bad-amount"]


# --------------------------------------------------------- validation

@pytest.mark.parametrize("date", [
    "2026-02-30", "2026/03/01", "03-01-2026", "2026-3-1", "2026-03-01T00:00",
    "2026-13-01", "20260301", "", "tomorrow",
])
def test_bad_date(date):
    text = ledger(f"{date},Ann,10,Ann;Bob,")
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 2: bad-date"]


@pytest.mark.parametrize("payer", ['""', "   "])
def test_bad_payer_empty_after_trim(payer):
    text = ledger(f"2026-01-01,{payer},10,Ann;Bob,")
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", [
    "-5", "+5", "0", "0.00", "0.0", "12.345", "1,000", "1e3", "abc", "", "$12", "1_000",
])
def test_bad_amount(amount):
    text = ledger(f'2026-01-01,Ann,"{amount}",Ann;Bob,')
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 2: bad-amount"]


@pytest.mark.parametrize("participants", [
    "", "   ", "Ann;", ";Bob", "Ann;;Bob",
    "Ann:0", "Ann:-1", "Ann:1.5", "Ann:x", "Ann:",
    "Ann;bob:0",
    "Ann;Ann", "Ann;ANN:2", "Ann;ann",
])
def test_bad_participants(participants):
    text = ledger(f'2026-01-01,Zed,10,"{participants}",')
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 2: bad-participants"]


def test_first_failing_check_wins_date_before_amount():
    text = ledger("2026-02-30,Ann,abc,Ann;Bob,")
    _, _, err = run(text, "balances", "{f}")
    assert err.splitlines() == ["line 2: bad-date"]


def test_first_failing_check_wins_payer_before_amount():
    text = ledger("2026-01-01,  ,abc,Ann;Bob,")
    _, _, err = run(text, "balances", "{f}")
    assert err.splitlines() == ["line 2: bad-payer"]


def test_first_failing_check_wins_amount_before_participants():
    text = ledger("2026-01-01,Ann,abc,,")
    _, _, err = run(text, "balances", "{f}")
    assert err.splitlines() == ["line 2: bad-amount"]


def test_all_invalid_rows_reported_in_line_order_one_reason_each():
    text = ledger(
        "2026-01-01,Ann,10,Ann;Bob,",       # line 2 ok
        "bad,Ann,10,Ann;Bob,",              # line 3 bad-date
        "2026-01-01,Ann,10,Ann;Ann,",       # line 4 bad-participants
        "2026-01-01,,10,Ann;Bob,",          # line 5 bad-payer
        "2026-01-01,Ann,0,Ann;Bob,",        # line 6 bad-amount
    )
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == [
        "line 3: bad-date", "line 4: bad-participants",
        "line 5: bad-payer", "line 6: bad-amount"]


def test_invalid_row_suppresses_all_stdout_even_for_settle():
    text = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-01-01,Ann,x,Ann;Bob,")
    code, out, _ = run(text, "settle", "{f}")
    assert code == 2 and out == ""


def test_invalid_row_outside_date_range_still_fails():
    text = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-06-01,Ann,x,Ann;Bob,")
    code, out, err = run(text, "balances", "{f}", "--until", "2026-02-01")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 3: bad-amount"]


@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_required_column(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    text = ",".join(cols) + "\n"
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == [f"header: missing {missing}"]


def test_missing_column_reports_only_first_in_required_order():
    text = "payer,amount\n"  # missing date, participants, note
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["header: missing date"]


def test_missing_column_reports_nothing_else_even_with_bad_rows():
    text = "date,payer,amount,participants\n2026-99-99,,x,\n"  # missing note
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == ["header: missing note"]


# ---------------------------------------------------- unreadable ledger

def test_nonexistent_ledger_exit_3(tmp_path):
    missing = str(tmp_path / "nope.csv")
    code, out, err = cli("balances", missing)
    assert code == 3 and out == ""
    assert err.splitlines() == [f"error: cannot read {missing}"]


def test_directory_as_ledger_exit_3(tmp_path):
    code, out, err = cli("balances", str(tmp_path))
    assert code == 3 and out == ""
    assert err.startswith("error: cannot read ")


def test_unreadable_ledger_settle_also_exit_3(tmp_path):
    missing = str(tmp_path / "nope.csv")
    code, out, _ = cli("settle", missing, "--format", "json")
    assert code == 3 and out == ""


# -------------------------------------------------------------- usage

@pytest.mark.parametrize("args", [
    ["explode", "{f}"],
    ["{f}"],
    ["balances"],
    [],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "yesterday"],
    ["balances", "{f}", "--until", "2026-13-01"],
    ["balances", "{f}", "--until"],
    ["balances", "{f}", "--since"],
])
def test_usage_errors_exit_64(args):
    code, out, _ = run(TRIP, *args)
    assert code == 64 and out == ""


def test_usage_error_has_message_on_stderr():
    code, out, err = run(TRIP, "explode", "{f}")
    assert code == 64 and out == "" and err.strip() != ""


# ------------------------------------------------- option placement

def test_options_before_positional_arguments():
    code, out, _ = run(TRIP, "--format", "json", "balances", "{f}")
    assert code == 0
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": 2200}


def test_options_between_positional_arguments():
    code, out, _ = run(TRIP, "balances", "--format", "json", "{f}")
    assert code == 0
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": 2200}


def test_options_after_positional_arguments():
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0
    assert "transfers" in json.loads(out)


def test_since_before_positional_arguments():
    code, out, _ = run(TRIP, "--since", "2026-03-02", "balances", "{f}")
    assert code == 0
    assert out == "Ann -18.00\nBob +12.00\nCy +6.00\n"


# ---------------------------------------------------------- date range

RANGE = ledger(
    "2026-01-31,Ann,10.00,Ann;Bob,",
    "2026-02-01,Bob,20.00,Bob;Cy,",
    "2026-02-28,Cy,6.00,Cy;Dee,",
    "2026-03-01,Dee,8.00,Dee;Ann,",
)


def test_until_alone_is_inclusive():
    code, out, _ = run(RANGE, "balances", "{f}", "--until", "2026-01-31")
    assert code == 0
    assert out == "Ann +5.00\nBob -5.00\n"


def test_since_alone_is_inclusive():
    code, out, _ = run(RANGE, "balances", "{f}", "--since", "2026-02-01")
    assert code == 0
    assert out == "Ann -4.00\nBob +10.00\nCy -7.00\nDee +1.00\n"


def test_since_and_until_together_inclusive_both_ends():
    code, out, _ = run(RANGE, "balances", "{f}", "--since", "2026-01-31", "--until", "2026-02-01")
    assert code == 0
    assert out == "Ann +5.00\nBob +5.00\nCy -10.00\n"


def test_range_excludes_people_only_in_out_of_range_rows():
    code, out, _ = run(RANGE, "balances", "{f}", "--since", "2026-02-01", "--until", "2026-02-28")
    assert code == 0
    assert out == "Bob +10.00\nCy -7.00\nDee -3.00\n"


def test_range_with_no_rows_prints_nothing():
    code, out, err = run(RANGE, "balances", "{f}", "--since", "2030-01-01")
    assert code == 0 and out == "" and err == ""
    code, out, _ = run(RANGE, "balances", "{f}", "--since", "2030-01-01", "--format", "json")
    assert json.loads(out) == {"balances": []}


def test_range_empty_settle_says_nothing_to_settle():
    code, out, _ = run(RANGE, "settle", "{f}", "--since", "2030-01-01")
    assert code == 0 and out == "nothing to settle\n"
    code, out, _ = run(RANGE, "settle", "{f}", "--since", "2030-01-01", "--format", "json")
    assert json.loads(out) == {"transfers": []}


def test_settle_respects_range():
    code, out, _ = run(RANGE, "settle", "{f}", "--since", "2026-02-01")
    assert code == 0
    assert out == "Cy -> Bob 7.00\nAnn -> Bob 3.00\nAnn -> Dee 1.00\n"


def test_range_applies_to_settle_json():
    code, out, _ = run(RANGE, "settle", "{f}", "--until", "2026-01-31", "--format", "json")
    assert json.loads(out) == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}


def test_bad_row_outside_range_still_fails_even_when_later_rows_are_fine():
    text = ledger("2026-01-01,Ann,10,Ann;Bob,", "2026-06-01,Ann,10,Ann;Bob,", "2026-07-01,,10,Ann;Bob,")
    code, out, err = run(text, "balances", "{f}", "--since", "2026-02-01")
    assert code == 2 and out == ""
    assert err.splitlines() == ["line 4: bad-payer"]


# --------------------------------------------------- randomized model check

def _random_ledger(rng):
    people = ["Ann", "bob", "Cy", "dee", "Eve", "fay", "Gus"]
    rows, model = [], []

    def spell(n):
        return rng.choice([n, n.upper(), n.lower(), n.title()])

    for _ in range(rng.randint(1, 12)):
        day = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        chosen = rng.sample(people, rng.randint(1, 4))
        payer = spell(rng.choice(people))
        cents = rng.randint(1, 100000)
        if cents % 100 == 0:
            amt = str(cents // 100)
        elif cents % 10 == 0:
            amt = f"{cents // 100}.{(cents % 100) // 10}"
        else:
            amt = f"{cents // 100}.{cents % 100:02d}"
        parts_txt, parts_model = [], []
        for n in chosen:
            w = rng.randint(1, 5)
            s = spell(n)
            parts_txt.append(s if w == 1 and rng.random() < 0.5 else f"{s}:{w}")
            parts_model.append((s, w))
        rows.append(f"{day},{payer},{amt},{';'.join(parts_txt)},")
        model.append((payer, cents, parts_model))
    return ledger(*rows), model


@pytest.mark.parametrize("seed", range(40))
def test_random_ledgers_match_reference_model(seed):
    rng = random.Random(seed)
    text, model = _random_ledger(rng)
    expected_bal = ref_balances(model)
    assert sum(c for _, c in expected_bal) == 0

    code, out, err = run(text, "balances", "{f}")
    assert code == 0 and err == "", err
    assert out == "".join(f"{n} {cents_fmt(c, True)}\n" for n, c in expected_bal)

    code, out, err = run(text, "settle", "{f}")
    assert code == 0 and err == ""
    expected_tr = ref_settle(expected_bal)
    if expected_tr:
        assert out == "".join(f"{d} -> {c} {cents_fmt(x, False)}\n" for d, c, x in expected_tr)
    else:
        assert out == "nothing to settle\n"

    nonzero = sum(1 for _, c in expected_bal if c)
    assert len(expected_tr) <= max(0, nonzero - 1)
    assert all(x > 0 for _, _, x in expected_tr)
