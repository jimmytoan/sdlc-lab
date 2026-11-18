"""Regression suite for splitr, driven as a black box through the CLI (see SPEC.md)."""
import datetime as dt
import json
import os
import random
import subprocess
import sys

import pytest

from helpers import APP_DIR, run, lines

HEADER = "date,payer,amount,participants,note"
SPLITR = os.path.join(APP_DIR, "splitr.py")


# ---------------------------------------------------------------- helpers

def ledger(*rows, header=HEADER):
    return header + "\n" + "".join(r + "\n" for r in rows)


def row(date="2026-03-01", payer="Zed", amount="10.00", participants="Ann;Bob", note=""):
    return f"{date},{payer},{amount},{participants},{note}"


def text_balances(led, *extra):
    code, out, err = run(led, "balances", "{f}", *extra)
    assert code == 0, err
    return out


def text_settle(led, *extra):
    code, out, err = run(led, "settle", "{f}", *extra)
    assert code == 0, err
    return out


def json_out(led, command, *extra):
    code, out, err = run(led, command, "{f}", "--format", "json", *extra)
    assert code == 0, err
    return json.loads(out)


def fmt_signed(c):
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


def fmt_unsigned(c):
    return f"{abs(c) // 100}.{abs(c) % 100:02d}"


def run_literal(*args):
    """Run the CLI with literal arguments (no temp ledger). Returns (code, stdout, stderr)."""
    p = subprocess.run([sys.executable, SPLITR, *args], capture_output=True, text=True,
                       timeout=20, cwd=APP_DIR)
    return p.returncode, p.stdout, p.stderr


# Independent oracle implementing SPEC.md, used for randomized comparison.

def oracle_split(cents, parts):
    """parts: list of (name, weight). Returns {name: share} using the largest-remainder rule."""
    total = sum(w for _, w in parts)
    shares = {n: cents * w // total for n, w in parts}
    left = cents - sum(shares.values())
    order = sorted(parts, key=lambda p: (-((cents * p[1]) % total), p[0].lower()))
    for n, _ in order[:left]:
        shares[n] += 1
    assert sum(shares.values()) == cents
    return shares


def oracle_balances(expenses, since=None, until=None):
    """expenses: list of (iso_date, payer, cents, [(name, weight)]) in file order.
    Returns sorted [(display_name, cents)]."""
    display, bal = {}, {}

    def touch(name):
        k = name.lower()
        display.setdefault(k, name)
        bal.setdefault(k, 0)

    for date, payer, cents, parts in expenses:
        if since and date < since:
            continue
        if until and date > until:
            continue
        touch(payer)
        for n, _ in parts:
            touch(n)
        bal[payer.lower()] += cents
        for n, share in oracle_split(cents, parts).items():
            bal[n.lower()] -= share
    out = [(display[k], bal[k]) for k in bal]
    return sorted(out, key=lambda x: x[0].lower())


def oracle_settle(balances):
    bal = {n: c for n, c in balances}
    out = []
    while any(v != 0 for v in bal.values()):
        debtor = min(bal, key=lambda n: (bal[n], n.lower()))
        creditor = min(bal, key=lambda n: (-bal[n], n.lower()))
        amt = min(-bal[debtor], bal[creditor])
        bal[debtor] += amt
        bal[creditor] -= amt
        out.append((debtor, creditor, amt))
    return out


TRIP = ledger(
    "2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner",
    "2026-03-02,Bob,30,Ann;Bob,taxi",
    '2026-03-03,Cy,9.00,Ann;Bob;Cy,"coffee, cake"',
)


# ---------------------------------------------------------------- output format

def test_text_balances_exact_output():
    assert text_balances(TRIP) == "Ann +22.00\nBob -8.00\nCy -14.00\n"


def test_text_settle_exact_output():
    assert text_settle(TRIP) == "Cy -> Ann 14.00\nBob -> Ann 8.00\n"


def test_success_has_empty_stderr():
    code, out, err = run(TRIP, "settle", "{f}")
    assert code == 0 and err == ""


def test_balances_and_settle_json_shape():
    assert json_out(TRIP, "balances") == {"balances": [
        {"name": "Ann", "cents": 2200}, {"name": "Bob", "cents": -800}, {"name": "Cy", "cents": -1400}]}
    assert json_out(TRIP, "settle") == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 1400}, {"from": "Bob", "to": "Ann", "cents": 800}]}


def test_json_empty_lists():
    assert json_out(ledger(), "balances") == {"balances": []}
    assert json_out(ledger(), "settle") == {"transfers": []}


def test_json_settle_empty_when_all_zero():
    led = ledger(row(payer="Ann", participants="Ann;Bob"), row(payer="Bob", participants="Ann;Bob"))
    assert json_out(led, "settle") == {"transfers": []}
    assert json_out(led, "balances") == {"balances": [
        {"name": "Ann", "cents": 0}, {"name": "Bob", "cents": 0}]}


def test_text_nothing_to_settle_exact():
    assert text_settle(ledger()) == "nothing to settle\n"


def test_header_only_balances_prints_nothing():
    code, out, err = run(ledger(), "balances", "{f}")
    assert (code, out, err) == (0, "", "")


def test_zero_balance_prints_unsigned_zero():
    led = ledger(row(payer="Ann", participants="Ann;Bob"), row(payer="Bob", participants="Ann;Bob"))
    assert text_balances(led) == "Ann 0.00\nBob 0.00\n"
    assert text_settle(led) == "nothing to settle\n"


def test_sign_and_two_decimals_for_large_amounts():
    led = ledger(row(payer="Ann", amount="1234567.89", participants="Ann;Bob"))
    # 123456789 cents over 2 people: 61728395 for Ann (tie on remainder, name order), 61728394 for Bob
    assert text_balances(led) == "Ann +617283.94\nBob -617283.94\n"
    assert text_settle(led) == "Bob -> Ann 617283.94\n"


def test_settle_amount_is_unsigned_two_decimals():
    led = ledger(row(payer="Ann", amount="5", participants="Bob"))
    assert text_settle(led) == "Bob -> Ann 5.00\n"


# ---------------------------------------------------------------- ledger parsing

@pytest.mark.parametrize("amount,cents", [
    ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), ("0.10", 10), ("007", 700),
])
def test_accepted_amount_formats(amount, cents):
    led = ledger(row(payer="Zed", amount=amount, participants="Ann"))
    assert json_out(led, "balances")["balances"][-1] == {"name": "Zed", "cents": cents}


def test_leap_day_is_a_real_date():
    led = ledger(row(date="2024-02-29", payer="Ann", amount="1", participants="Bob"))
    assert text_balances(led) == "Ann +1.00\nBob -1.00\n"


def test_bom_is_ignored():
    raw = b"\xef\xbb\xbf" + ledger(row(payer="Ann", amount="2", participants="Bob")).encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert (code, out, err) == (0, "Ann +2.00\nBob -2.00\n", "")


def test_crlf_line_endings():
    raw = ledger(row(payer="Ann", amount="2", participants="Bob")).replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert (code, out, err) == (0, "Ann +2.00\nBob -2.00\n", "")


def test_no_trailing_newline():
    raw = (HEADER + "\n" + row(payer="Ann", amount="2", participants="Bob")).encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert (code, out, err) == (0, "Ann +2.00\nBob -2.00\n", "")


def test_columns_in_any_order_and_extra_column_allowed():
    led = ("note,participants,amount,payer,category,date\n"
           "lunch,Ann;Bob,4.00,Ann,food,2026-03-01\n")
    assert text_balances(led) == "Ann +2.00\nBob -2.00\n"


def test_quoted_note_with_comma_and_escaped_quotes():
    led = ledger('2026-03-01,Ann,4.00,Ann;Bob,"a, ""b"", c"')
    assert text_balances(led) == "Ann +2.00\nBob -2.00\n"


def test_empty_note_allowed():
    led = ledger("2026-03-01,Ann,4.00,Ann;Bob,")
    assert text_balances(led) == "Ann +2.00\nBob -2.00\n"


def test_hash_inside_row_is_not_a_comment():
    led = ledger("2026-03-01,Ann,4.00,Ann;Bob,# not a comment")
    assert text_balances(led) == "Ann +2.00\nBob -2.00\n"


def test_ignored_lines_skipped_but_counted_in_numbering():
    led = ("# leading comment\n"                          # line 1
           "date,payer,amount,participants,note\n"       # line 2 (header)
           "\n"                                          # line 3 empty
           "   \t \n"                                    # line 4 whitespace only
           "  # indented comment\n"                      # line 5
           "2026-03-01,Ann,4.00,Ann;Bob,\n"              # line 6 valid
           "2026-03-01,Ann,abc,Ann;Bob,\n")              # line 7 invalid
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 7: bad-amount"]


def test_invalid_line_number_counts_ignored_lines():
    led = ("date,payer,amount,participants,note\n"  # 1
           "# c\n"                                   # 2
           "\n"                                      # 3
           "2026-03-01,Ann,abc,Ann;Bob,\n")          # 4
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and lines(err) == ["line 4: bad-amount"]


def test_crlf_line_numbering():
    raw = "date,payer,amount,participants,note\r\n\r\n2026-03-01,Ann,x,Ann,\r\n".encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 2 and lines(err) == ["line 3: bad-amount"]


def test_names_are_trimmed():
    led = ledger('2026-03-01,"  Ann ",4.00," Bob ;  Cy ",')
    assert text_balances(led) == "Ann +4.00\nBob -2.00\nCy -2.00\n"


def test_names_match_ignoring_case_and_merge():
    led = ledger(row(payer="ANN", amount="4.00", participants="bob;Cy"),
                 row(payer="ann", amount="2.00", participants="Bob"))
    # ANN: +4 +2 = +6 ; bob: -2 -2 = -4 ; Cy: -2 ; spelling of bob is first appearance ("bob")
    assert text_balances(led) == "ANN +6.00\nbob -4.00\nCy -2.00\n"


def test_display_spelling_is_first_appearance_payer_before_participants():
    led = ledger(row(payer="Bob", amount="2.00", participants="bob;Zed"),
                 row(payer="zed", amount="2.00", participants="ZED;Ann"))
    # Bob: +2 -1 = +1 ; Zed: -1 +2 -1 = 0 ; Ann: -1
    assert text_balances(led) == "Ann -1.00\nBob +1.00\nZed 0.00\n"


def test_display_spelling_uses_earliest_row():
    led = ledger(row(payer="ann", amount="2.00", participants="Bob"),
                 row(payer="Ann", amount="2.00", participants="Bob"))
    assert text_balances(led).split("\n")[0].startswith("ann ")


def test_payer_in_same_row_spelled_before_participant():
    led = ledger(row(payer="BOB", amount="2.00", participants="bob;Cy"))
    assert text_balances(led).split("\n")[0].startswith("BOB ")


def test_payer_need_not_be_participant():
    led = ledger(row(payer="Zed", amount="9.00", participants="Ann;Bob"))
    assert text_balances(led) == "Ann -4.50\nBob -4.50\nZed +9.00\n"


def test_payer_who_is_participant_pays_own_share():
    led = ledger(row(payer="Ann", amount="9.00", participants="Ann;Bob;Cy"))
    assert text_balances(led) == "Ann +6.00\nBob -3.00\nCy -3.00\n"


def test_default_weight_is_one_and_equals_explicit_one():
    a = ledger(row(payer="Zed", amount="10.00", participants="Ann;Bob"))
    b = ledger(row(payer="Zed", amount="10.00", participants="Ann:1;Bob:1"))
    assert text_balances(a) == text_balances(b)


def test_person_listed_in_participants_only_still_appears():
    led = ledger(row(payer="Zed", amount="10.00", participants="Ann"))
    assert text_balances(led) == "Ann -10.00\nZed +10.00\n"


def test_balances_list_sorted_ignoring_case():
    led = ledger(row(payer="bob", amount="3.00", participants="Cy;alf;Dan"))
    names = [l.split(" ")[0] for l in text_balances(led).strip().split("\n")]
    assert names == ["alf", "bob", "Cy", "Dan"]


# ---------------------------------------------------------------- splitting rule

def test_weights_split_proportionally():
    led = ledger("2026-03-01,Ann,40.00,Ann:1;Bob:3,")
    assert text_balances(led) == "Ann +30.00\nBob -30.00\n"


def test_leftover_cent_goes_to_largest_remainder_not_first_listed():
    # 100 cents, weights 1 and 2: floors 33 (rem 1) and 66 (rem 2). Leftover cent -> Bob.
    led = ledger("2026-03-01,Zed,1.00,Ann:1;Bob:2,")
    assert text_balances(led) == "Ann -0.33\nBob -0.67\nZed +1.00\n"


def test_leftover_cent_three_way_equal_split_goes_to_name_order():
    led = ledger("2026-03-01,Zed,1.00,Cy;Bob;ann,")
    assert text_balances(led) == "ann -0.34\nBob -0.33\nCy -0.33\nZed +1.00\n"


def test_remainder_tie_broken_by_name_ignoring_case():
    # 1 cent, equal remainders: 'bob' < 'Cy' ignoring case (case-sensitive order would pick Cy)
    led = ledger("2026-03-01,Zed,0.01,Cy;bob,")
    assert text_balances(led) == "bob -0.01\nCy 0.00\nZed +0.01\n"


def test_remainder_tie_not_broken_by_listing_order():
    led_a = ledger("2026-03-01,Zed,0.01,Bob;Ann,")
    led_b = ledger("2026-03-01,Zed,0.01,Ann;Bob,")
    assert text_balances(led_a) == text_balances(led_b) == "Ann -0.01\nBob 0.00\nZed +0.01\n"


def test_two_leftover_cents_among_three():
    led = ledger("2026-03-01,Zed,0.02,A;B;C,")
    assert text_balances(led) == "A -0.01\nB -0.01\nC 0.00\nZed +0.02\n"


def test_largest_remainder_with_weights_then_name_tie():
    # 7 cents, weights Ann:1 Bob:1 Cy:3 (W=5): floors 1,1,4 ; remainders 2,2,1 ; leftover 1 -> Ann
    led = ledger("2026-03-01,Zed,0.07,Bob:1;Ann:1;Cy:3,")
    assert text_balances(led) == "Ann -0.02\nBob -0.01\nCy -0.04\nZed +0.07\n"


def test_balances_always_sum_to_zero():
    led = ledger("2026-03-01,Zed,0.01,A;B;C;D;E;F;G,",
                 "2026-03-02,Yan,99.99,A:2;B:3;C,",
                 "2026-03-03,A,1.00,D;E:4,")
    assert sum(e["cents"] for e in json_out(led, "balances")["balances"]) == 0


# ---------------------------------------------------------------- settle

def test_settle_largest_debtor_and_creditor_first():
    led = ledger("2026-03-01,Ann,25.00,Cy,",
                 "2026-03-02,Bob,10.00,Dan,",
                 "2026-03-03,Ann,5.00,Dan,")
    # balances: Ann +30, Bob +10, Cy -25, Dan -15
    assert text_balances(led) == "Ann +30.00\nBob +10.00\nCy -25.00\nDan -15.00\n"
    assert text_settle(led) == "Cy -> Ann 25.00\nDan -> Bob 10.00\nDan -> Ann 5.00\n"
    assert json_out(led, "settle") == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 2500},
        {"from": "Dan", "to": "Bob", "cents": 1000},
        {"from": "Dan", "to": "Ann", "cents": 500}]}


def test_settle_creditor_tie_broken_by_name_ignoring_case():
    led = ledger("2026-03-01,bob,10.00,Dan,", "2026-03-02,Cy,10.00,Dan,")
    assert text_settle(led) == "Dan -> bob 10.00\nDan -> Cy 10.00\n"


def test_settle_debtor_tie_broken_by_name_ignoring_case():
    led = ledger("2026-03-01,Zed,20.00,dan;Eve,")
    assert text_settle(led) == "dan -> Zed 10.00\nEve -> Zed 10.00\n"


def test_settle_transfers_clear_all_balances():
    led = ledger("2026-03-01,Ann,10.00,Bob;Cy;Dan;Eve,",
                 "2026-03-02,Bob,7.77,Ann;Cy,",
                 "2026-03-03,Cy,3.33,Dan:2;Eve,")
    bal = {e["name"]: e["cents"] for e in json_out(led, "balances")["balances"]}
    for t in json_out(led, "settle")["transfers"]:
        assert t["cents"] > 0
        bal[t["from"]] += t["cents"]
        bal[t["to"]] -= t["cents"]
    assert all(v == 0 for v in bal.values())


def test_settle_text_matches_procedure_example():
    led = ledger("2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner",
                 "2026-03-02,Bob,30,Ann;Bob,taxi")
    expected = oracle_settle(oracle_balances([
        ("2026-03-01", "Ann", 6000, [("Ann", 1), ("Bob", 1), ("Cy", 1)]),
        ("2026-03-02", "Bob", 3000, [("Ann", 1), ("Bob", 1)]),
    ]))
    want = "".join(f"{d} -> {c} {fmt_unsigned(x)}\n" for d, c, x in expected)
    assert text_settle(led) == want


# ---------------------------------------------------------------- validation errors

INVALID_ROWS = [
    # bad-date
    ("2026-02-30,Ann,1.00,Ann;Bob,", "bad-date"),
    ("2023-02-29,Ann,1.00,Ann;Bob,", "bad-date"),
    ("2026-3-1,Ann,1.00,Ann;Bob,", "bad-date"),
    ("03/01/2026,Ann,1.00,Ann;Bob,", "bad-date"),
    ("2026-13-01,Ann,1.00,Ann;Bob,", "bad-date"),
    ("2026-00-10,Ann,1.00,Ann;Bob,", "bad-date"),
    (",Ann,1.00,Ann;Bob,", "bad-date"),
    # bad-payer
    ("2026-03-01,,1.00,Ann;Bob,", "bad-payer"),
    ('2026-03-01,"   ",1.00,Ann;Bob,', "bad-payer"),
    # bad-amount
    ("2026-03-01,Ann,0,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,0.00,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,-1,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,+1,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,1.234,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,1e2,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,abc,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,,Ann;Bob,", "bad-amount"),
    ('2026-03-01,Ann,"1,000",Ann;Bob,', "bad-amount"),
    ("2026-03-01,Ann,12.,Ann;Bob,", "bad-amount"),
    ("2026-03-01,Ann,.5,Ann;Bob,", "bad-amount"),
    # bad-participants
    ("2026-03-01,Ann,1.00,,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann;,", "bad-participants"),
    ("2026-03-01,Ann,1.00,;Bob,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann;;Bob,", "bad-participants"),
    ("2026-03-01,Ann,1.00,:2,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann:0,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann:-1,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann:1.5,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann:x,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann:,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann;ann,", "bad-participants"),
    ("2026-03-01,Ann,1.00,Ann:2;Ann:3,", "bad-participants"),
]


@pytest.mark.parametrize("line,reason", INVALID_ROWS)
def test_invalid_row_reason(line, reason):
    code, out, err = run(ledger(line), "balances", "{f}")
    assert code == 2
    assert out == ""
    assert lines(err) == [f"line 2: {reason}"]


@pytest.mark.parametrize("line,reason", INVALID_ROWS[:5])
def test_invalid_row_also_fails_settle_with_empty_stdout(line, reason):
    code, out, err = run(ledger(line), "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"line 2: {reason}"]


@pytest.mark.parametrize("line,reason", [
    ("2026-02-30,,abc,Ann:0,", "bad-date"),        # date beats payer, amount, participants
    ("2026-03-01,,abc,Ann:0,", "bad-payer"),       # payer beats amount, participants
    ("2026-03-01,Zed,abc,Ann:0,", "bad-amount"),   # amount beats participants
    ("2026-03-01,Zed,0,,", "bad-amount"),
])
def test_only_first_failing_check_is_reported(line, reason):
    code, out, err = run(ledger(line), "balances", "{f}")
    assert code == 2
    assert lines(err) == [f"line 2: {reason}"]


def test_multiple_invalid_rows_reported_in_line_order_one_each():
    led = ledger(
        "2026-03-01,Ann,4.00,Ann;Bob,",          # line 2 valid
        "2026-03-01,Ann,abc,Ann;Bob,",           # line 3 bad-amount
        "2026-03-01,Ann,4.00,Ann;Bob;Ann,",      # line 4 bad-participants
        "2026-99-01,Ann,4.00,Ann;Bob,",          # line 5 bad-date
        "2026-03-01,,4.00,Ann;Bob,",             # line 6 bad-payer
    )
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert err.splitlines() == [
        "line 3: bad-amount", "line 4: bad-participants", "line 5: bad-date", "line 6: bad-payer"]


def test_invalid_row_anywhere_fails_whole_ledger():
    led = ledger(row(payer="Ann", amount="2.00", participants="Bob"),
                 row(payer="Ann", amount="2.00", participants="Bob"),
                 row(payer="Ann", amount="x", participants="Bob"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == "" and lines(err) == ["line 4: bad-amount"]


@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_required_header_column(missing):
    cols = [c for c in HEADER.split(",") if c != missing]
    led = ",".join(cols) + "\n" + row(payer="Ann", amount="1.00", participants="Bob") + "\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"header: missing {missing}"]


def test_only_first_missing_header_column_reported():
    code, out, err = run("date,payer,participants\n2026-03-01,Ann,Bob\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing amount"]


def test_missing_header_reported_before_row_errors():
    code, out, err = run("date,payer,amount,participants\n2026-99-01,,x,,\n", "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing note"]


def test_out_of_range_invalid_row_still_fails():
    led = ledger(row(date="2025-01-01", payer="Ann", amount="x", participants="Bob"),
                 row(date="2026-03-05", payer="Ann", amount="2.00", participants="Bob"))
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


def test_out_of_range_invalid_date_still_fails_with_until():
    led = ledger(row(date="2026-02-30", payer="Ann", amount="2.00", participants="Bob"))
    code, out, err = run(led, "settle", "{f}", "--until", "2026-01-01")
    assert code == 2 and out == "" and lines(err) == ["line 2: bad-date"]


# ---------------------------------------------------------------- file-level errors

def test_missing_file_exit_3(tmp_path):
    path = str(tmp_path / "nope.csv")
    code, out, err = run_literal("balances", path)
    assert code == 3 and out == ""
    assert err.strip() == f"error: cannot read {path}"


def test_directory_as_ledger_exit_3(tmp_path):
    code, out, err = run_literal("balances", str(tmp_path))
    assert code == 3 and out == ""
    assert err.startswith("error: cannot read ")


# ---------------------------------------------------------------- usage errors

@pytest.mark.parametrize("args", [
    [],
    ["explode", "{f}"],
    ["balances"],
    ["settle"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--since", "2026/03/01"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--until", "2026-13-01"],
    ["balances", "{f}", "--until"],
    ["settle", "{f}", "--bogus"],
    ["balances", "{f}", "extra"],
])
def test_usage_errors_exit_64(args):
    code, out, err = run(TRIP, *args)
    assert code == 64
    assert out == ""


# ---------------------------------------------------------------- options placement

def test_options_before_and_after_positionals():
    after = run(TRIP, "settle", "{f}", "--format", "json")
    before = run(TRIP, "--format", "json", "settle", "{f}")
    assert after[0] == before[0] == 0
    assert json.loads(after[1]) == json.loads(before[1])


def test_since_before_positionals():
    a = run(TRIP, "--since", "2026-03-02", "balances", "{f}")
    b = run(TRIP, "balances", "{f}", "--since", "2026-03-02")
    assert a == b


# ---------------------------------------------------------------- date range

RANGE = ledger(
    "2026-02-28,Ann,10.00,Bob,",
    "2026-03-01,Ann,20.00,Cy,",
    "2026-03-31,Bob,6.00,Bob;Cy,",
    "2026-04-01,Dan,8.00,Ann,",
)


def test_since_and_until_inclusive_bounds():
    assert text_balances(RANGE, "--since", "2026-03-01", "--until", "2026-03-31") == \
        "Ann +20.00\nBob +3.00\nCy -23.00\n"


def test_people_only_outside_range_not_printed():
    # rows 1-3 only: Ann +10+20, Bob -10 +6-3, Cy -20-3. Dan (only in row 4) is absent.
    assert text_balances(RANGE, "--until", "2026-03-31") == "Ann +30.00\nBob -7.00\nCy -23.00\n"


def test_until_only():
    assert text_balances(RANGE, "--until", "2026-02-28") == "Ann +10.00\nBob -10.00\n"


def test_since_only():
    assert text_balances(RANGE, "--since", "2026-03-01") == \
        "Ann +12.00\nBob +3.00\nCy -23.00\nDan +8.00\n"


def test_since_on_last_day_includes_only_that_day():
    assert text_settle(RANGE, "--since", "2026-04-01") == "Ann -> Dan 8.00\n"


def test_range_with_no_rows_prints_nothing():
    code, out, err = run(RANGE, "balances", "{f}", "--since", "2027-01-01")
    assert (code, out, err) == (0, "", "")
    assert text_settle(RANGE, "--since", "2027-01-01") == "nothing to settle\n"


def test_inverted_range_is_empty_not_error():
    code, out, err = run(RANGE, "balances", "{f}", "--since", "2026-05-01", "--until", "2026-01-01")
    assert (code, out, err) == (0, "", "")


def test_range_json():
    assert json_out(RANGE, "balances", "--since", "2026-04-01") == {"balances": [
        {"name": "Ann", "cents": -800}, {"name": "Dan", "cents": 800}]}
    assert json_out(RANGE, "settle", "--since", "2026-04-01") == {"transfers": [
        {"from": "Ann", "to": "Dan", "cents": 800}]}


def test_range_limits_settle_too():
    assert text_settle(RANGE, "--until", "2026-02-28") == "Bob -> Ann 10.00\n"


# ---------------------------------------------------------------- randomized agreement with oracle

NAMES = ["Ann", "bob", "Cy", "dan", "Eve", "fay", "Gus"]


def _random_ledger(rng):
    """Returns (csv_rows, oracle_expenses, since, until). Case variants are only used
    without a date range so that the spelling rule is exercised unambiguously."""
    use_range = rng.random() < 0.4
    since = until = None
    if use_range:
        base = dt.date(2026, 1, 1)
        s = base + dt.timedelta(days=rng.randint(0, 90))
        since = s.isoformat()
        until = (s + dt.timedelta(days=rng.randint(-5, 60))).isoformat()

    def spell(n):
        return n if use_range else rng.choice([n, n.upper(), n.lower(), n.title()])

    rows, expenses = [], []
    for _ in range(rng.randint(0, 12)):
        date = (dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 120))).isoformat()
        cents = rng.randint(1, 200000)
        if cents % 100 == 0:
            amount = str(cents // 100)
        elif cents % 10 == 0:
            amount = f"{cents // 100}.{(cents % 100) // 10}"
        else:
            amount = f"{cents // 100}.{cents % 100:02d}"
        people = rng.sample(NAMES, rng.randint(1, 4))
        parts = [(p, rng.randint(1, 5) if rng.random() < 0.3 else 1) for p in people]
        payer = rng.choice(NAMES)

        parts_txt = []
        spelled_parts = []
        for p, w in parts:
            sp = spell(p)
            spelled_parts.append((sp, w))
            parts_txt.append(f"{sp}:{w}" if w != 1 else sp)
        spelled_payer = spell(payer)
        rows.append(f"{date},{spelled_payer},{amount},{';'.join(parts_txt)},")
        expenses.append((date, spelled_payer, cents, spelled_parts))
    return rows, expenses, since, until


@pytest.mark.parametrize("seed", range(60))
def test_agrees_with_reference_oracle(seed):
    rng = random.Random(seed)
    rows, expenses, since, until = _random_ledger(rng)
    led = ledger(*rows)
    extra = []
    if since:
        extra += ["--since", since]
    if until:
        extra += ["--until", until]

    exp_bal = oracle_balances(expenses, since, until)
    assert sum(c for _, c in exp_bal) == 0

    assert text_balances(led, *extra) == "".join(f"{n} {fmt_signed(c)}\n" for n, c in exp_bal)

    exp_settle = "".join(f"{d} -> {c} {fmt_unsigned(x)}\n" for d, c, x in oracle_settle(exp_bal))
    assert text_settle(led, *extra) == (exp_settle or "nothing to settle\n")

    assert json_out(led, "balances", *extra) == {
        "balances": [{"name": n, "cents": c} for n, c in exp_bal]}
