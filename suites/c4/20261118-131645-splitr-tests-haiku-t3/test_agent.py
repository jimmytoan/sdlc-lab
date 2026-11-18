"""Regression suite for splitr, derived from SPEC.md.

Every test drives splitr.py as a black box through tests/helpers.py.
Expected values are hand-computed from the spec (see comments), plus a
randomized cross-check against an independent oracle written in this file.
"""
import csv
import io
import json
import random
from datetime import date, timedelta

import pytest

from helpers import run, lines

HDR = "date,payer,amount,participants,note"


def ledger(*rows, header=HDR):
    return header + "\n" + "".join(r + "\n" for r in rows)


def row(d, payer, amount, participants, note=""):
    """Build a CSV row, quoting the note when needed."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow([d, payer, amount, participants, note])
    return buf.getvalue().rstrip("\n")


def bal(text, *args, fmt=None):
    extra = ["--format", fmt] if fmt else []
    return run(text, "balances", "{f}", *extra, *args)


def settle_run(text, *args, fmt=None):
    extra = ["--format", fmt] if fmt else []
    return run(text, "settle", "{f}", *extra, *args)


def ok_lines(text, *args):
    code, out, err = bal(text, *args)
    assert code == 0, err
    assert err == ""
    return lines(out)


TRIP = ledger(
    "2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner",
    "2026-03-02,Bob,30,Ann;Bob,taxi",
    "2026-03-03,Cy,9.00,Ann;Bob;Cy,\"coffee, cake\"",
)


# ---------------------------------------------------------------- commands / usage

@pytest.mark.parametrize("args", [
    [],
    ["balances"],
    ["explode", "{f}"],
    ["settle"],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format", "JSON"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "03/01/2026"],
    ["balances", "{f}", "--since", ""],
    ["balances", "{f}", "--until", "2026-13-01"],
    ["settle", "{f}", "--until", "2026-3-1"],
])
def test_usage_errors_exit_64_with_empty_stdout(args):
    code, out, err = run(TRIP, *args)
    assert code == 64
    assert out == ""


def test_usage_error_writes_message_to_stderr():
    code, out, err = run(TRIP, "explode", "{f}")
    assert code == 64 and out == "" and err.strip() != ""


def test_options_may_precede_positionals():
    code, out, _ = run(TRIP, "--format", "json", "balances", "{f}")
    assert code == 0
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": 2200}


def test_options_between_command_and_ledger():
    code, out, _ = run(TRIP, "balances", "--format", "json", "{f}")
    assert code == 0
    assert json.loads(out)["balances"][1] == {"name": "Bob", "cents": -800}


def test_explicit_text_format_equals_default():
    a = run(TRIP, "balances", "{f}")
    b = run(TRIP, "balances", "{f}", "--format", "text")
    assert a == b


def test_spec_example_balances_text():
    code, out, err = run(TRIP, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +22.00\nBob -8.00\nCy -14.00\n"


def test_spec_example_settle_text_exact_output():
    code, out, _ = run(TRIP, "settle", "{f}")
    assert code == 0
    assert out == "Cy -> Ann 14.00\nBob -> Ann 8.00\n"


def test_balances_json_shape_and_order():
    code, out, _ = bal(TRIP, fmt="json")
    assert code == 0
    assert json.loads(out) == {"balances": [
        {"name": "Ann", "cents": 2200},
        {"name": "Bob", "cents": -800},
        {"name": "Cy", "cents": -1400},
    ]}


def test_settle_json_shape():
    code, out, _ = settle_run(TRIP, fmt="json")
    assert code == 0
    assert json.loads(out) == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 1400},
        {"from": "Bob", "to": "Ann", "cents": 800},
    ]}


def test_json_output_is_single_document():
    code, out, _ = settle_run(TRIP, fmt="json")
    assert out.count("\n") == 1 and out.endswith("\n")


# ---------------------------------------------------------------- ledger file format

def test_bom_is_accepted():
    raw = b"\xef\xbb\xbf" + ledger("2026-03-01,Ann,10,Bob").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_crlf_line_endings():
    text = ledger("2026-03-01,Ann,10,Bob,x", "2026-03-02,Bob,4,Ann;Bob,y").replace("\n", "\r\n")
    code, out, err = run("", "balances", "{f}", raw=text.encode())
    assert code == 0 and err == ""
    # Ann +10 (alone-participant Bob) and Bob +4 split with Ann: Ann -2, Bob +2
    assert lines(out) == ["Ann +8.00", "Bob -8.00"]


def test_last_line_without_newline():
    text = ledger("2026-03-01,Ann,10,Bob").rstrip("\n")
    code, out, _ = run("", "balances", "{f}", raw=text.encode())
    assert code == 0
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_columns_in_any_order():
    text = "note,participants,payer,amount,date\nx,Ann;Bob,Ann,10.00,2026-03-01\n"
    assert ok_lines(text) == ["Ann +5.00", "Bob -5.00"]


def test_extra_columns_are_ignored():
    text = "date,category,payer,amount,participants,note\n2026-03-01,food,Ann,10,Ann;Bob,\n"
    assert ok_lines(text) == ["Ann +5.00", "Bob -5.00"]


def test_quoted_note_with_commas_and_quotes_is_valid():
    text = ledger(row("2026-03-01", "Ann", "10", "Ann;Bob", 'pizza, "large", extra'))
    assert ok_lines(text) == ["Ann +5.00", "Bob -5.00"]


def test_blank_and_comment_lines_are_ignored():
    text = (HDR + "\n"
            "\n"
            "   \n"
            "\t\n"
            "# a comment\n"
            "   # indented comment\n"
            "2026-03-01,Ann,10,Ann;Bob,\n"
            "\n")
    assert ok_lines(text) == ["Ann +5.00", "Bob -5.00"]


def test_ignored_lines_count_toward_line_numbers():
    text = (HDR + "\n"          # line 1
            "# c\n"             # line 2
            "\n"                # line 3
            "2026-03-01,Ann,10,Ann;Bob,\n"  # line 4 valid
            "   \n"             # line 5
            "2026-03-01,Ann,abc,Ann;Bob,\n")  # line 6 invalid
    code, out, err = bal(text)
    assert code == 2 and out == ""
    assert lines(err) == ["line 6: bad-amount"]


def test_header_only_is_valid_and_empty():
    code, out, err = bal(HDR + "\n")
    assert code == 0 and out == "" and err == ""


def test_header_only_settle_says_nothing_to_settle():
    code, out, _ = settle_run(HDR + "\n")
    assert code == 0
    assert out == "nothing to settle\n"


def test_header_only_json():
    assert json.loads(bal(HDR + "\n", fmt="json")[1]) == {"balances": []}
    assert json.loads(settle_run(HDR + "\n", fmt="json")[1]) == {"transfers": []}


def test_empty_file_is_missing_header():
    code, out, err = run("", "balances", "{f}", raw=b"")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing date"]


@pytest.mark.parametrize("header,missing", [
    ("payer,amount,participants,note", "date"),
    ("date,amount,participants,note", "payer"),
    ("date,payer,participants,note", "amount"),
    ("date,payer,amount,note", "participants"),
    ("date,payer,amount,participants", "note"),
    ("amount,note", "date"),
    ("note", "date"),
])
def test_missing_header_column(header, missing):
    text = header + "\n2026-03-01,Ann,10,Ann;Bob,\n"
    code, out, err = bal(text)
    assert code == 2 and out == ""
    assert lines(err) == [f"header: missing {missing}"]


def test_missing_header_column_settle_also_exit_2():
    code, out, err = settle_run("date,payer\n")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing amount"]


def test_nonexistent_file_exit_3():
    code, out, err = run("", "balances", "/nonexistent/dir/ledger.csv")
    assert code == 3 and out == ""
    assert lines(err) == ["error: cannot read /nonexistent/dir/ledger.csv"]


def test_directory_as_ledger_exit_3():
    code, out, err = run("", "balances", ".")
    assert code == 3 and out == ""
    assert lines(err) == ["error: cannot read ."]


# ---------------------------------------------------------------- row validation

@pytest.mark.parametrize("d", [
    "2026-02-30", "2026-02-29", "2026/03/01", "03-01-2026", "2026-3-1",
    "2026-03-32", "2026-13-01", "", "2026-03-01T00:00", "20260301", "tomorrow",
])
def test_bad_date(d):
    code, out, err = bal(ledger(f"{d},Ann,10,Ann;Bob,"))
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_leap_day_is_valid():
    assert ok_lines(ledger("2024-02-29,Ann,10,Ann;Bob,")) == ["Ann +5.00", "Bob -5.00"]


@pytest.mark.parametrize("payer", ["", "   ", "\t"])
def test_bad_payer(payer):
    code, out, err = bal(ledger(f"2026-03-01,{payer},10,Ann;Bob,"))
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", [
    "0", "0.00", "-5", "-0.01", "+5", "1,000", "1e3", "1E3", "12.345", "12.",
    ".5", "abc", "", "1.2.3", "$5", "12 50", "5 USD", "0x10", "NaN", "inf",
])
def test_bad_amount(amount):
    code, out, err = bal(ledger(row("2026-03-01", "Ann", amount, "Ann;Bob", "")))
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


@pytest.mark.parametrize("amount,shown", [
    ("12", "12.00"),
    ("12.5", "12.50"),
    ("12.50", "12.50"),
    ("0.01", "0.01"),
    (" 7.25 ", "7.25"),
    ("1000000", "1000000.00"),
    ("1234567.89", "1234567.89"),
])
def test_valid_amounts(amount, shown):
    code, out, err = bal(ledger(f"2026-03-01,Ann,{amount},Bob,"))
    assert code == 0 and err == ""
    assert lines(out) == [f"Ann +{shown}", f"Bob -{shown}"]


@pytest.mark.parametrize("parts", [
    "", "   ", ";", "Ann;", ";Bob", "Ann;;Bob", " ; ",
    "Ann:0", "Ann:-1", "Ann:1.5", "Ann:x", "Ann:", "Ann:2:3",
    "Ann;Ann", "Ann;ann", "Bob:2;BOB", "Ann; Ann ",
])
def test_bad_participants(parts):
    code, out, err = bal(ledger(f"2026-03-01,Zed,10,{parts},"))
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-participants"]


def test_payer_may_be_a_participant_or_not():
    assert ok_lines(ledger("2026-03-01,Ann,10,Ann,")) == ["Ann 0.00"]
    assert ok_lines(ledger("2026-03-01,Ann,10,Bob;Cy,")) == ["Ann +10.00", "Bob -5.00", "Cy -5.00"]


@pytest.mark.parametrize("bad_row,reason", [
    (("2026-99-01", "", "abc", ";"), "bad-date"),
    (("2026-03-01", "", "abc", ";"), "bad-payer"),
    (("2026-03-01", "Ann", "abc", ";"), "bad-amount"),
    (("2026-03-01", "Ann", "10", ";"), "bad-participants"),
])
def test_one_reason_per_row_first_failing_check_wins(bad_row, reason):
    code, out, err = bal(ledger(row(*bad_row)))
    assert code == 2 and out == ""
    assert lines(err) == [f"line 2: {reason}"]


def test_bad_payer_reported_before_bad_amount():
    code, _, err = bal(ledger("2026-03-01,,abc,Ann;Bob,"))
    assert lines(err) == ["line 2: bad-payer"]


def test_bad_amount_reported_before_bad_participants():
    code, _, err = bal(ledger("2026-03-01,Ann,0,;,"))
    assert lines(err) == ["line 2: bad-amount"]


def test_multiple_invalid_rows_each_reported_in_line_order():
    text = ledger(
        "2026-03-01,Ann,10,Ann;Bob,",       # line 2 ok
        "2026-03-02,Ann,abc,Ann;Bob,",      # line 3 bad-amount
        "2026-03-03,Ann,10,Ann;Ann,",       # line 4 bad-participants
        "2026-13-03,Ann,10,Ann;Bob,",       # line 5 bad-date
        "2026-03-05,Ann,10,Ann;Bob,",       # line 6 ok
        "2026-03-06,,10,Ann;Bob,",          # line 7 bad-payer
    )
    code, out, err = bal(text)
    assert code == 2 and out == ""
    assert lines(err) == [
        "line 3: bad-amount",
        "line 4: bad-participants",
        "line 5: bad-date",
        "line 7: bad-payer",
    ]


def test_any_invalid_row_blocks_settle_and_json_too():
    text = ledger("2026-03-01,Ann,10,Ann;Bob,", "2026-03-02,Ann,-1,Ann;Bob,")
    for fmt in (None, "json"):
        code, out, err = settle_run(text, fmt=fmt)
        assert code == 2 and out == ""
        assert lines(err) == ["line 3: bad-amount"]


def test_invalid_row_outside_date_range_still_rejected():
    text = ledger("2026-01-01,Ann,abc,Ann;Bob,", "2026-03-05,Ann,10,Ann;Bob,")
    code, out, err = run("", "balances", "{f}", "--since", "2026-03-01", raw=text.encode())
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


def test_invalid_date_outside_range_still_rejected():
    text = ledger("2026-02-30,Ann,10,Ann;Bob,", "2026-03-05,Ann,10,Ann;Bob,")
    code, out, err = run("", "balances", "{f}", "--until", "2026-03-01", raw=text.encode())
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_stderr_contains_only_error_lines_on_invalid_ledger():
    code, out, err = bal(ledger("2026-03-01,Ann,0,Ann;Bob,"))
    assert all(l.startswith("line ") for l in lines(err))


# ---------------------------------------------------------------- splitting

def test_weighted_split_example():
    text = ledger("2026-03-01,Ann,40.00,Ann:1;Bob:3,")
    assert ok_lines(text) == ["Ann +30.00", "Bob -30.00"]


def test_leftover_cent_goes_to_name_first_on_tie_ignoring_case():
    # 10.00 / 3 = 3.33 each, 1 cent left; remainders all equal -> "ann" wins
    # case-insensitively (a case-sensitive sort would pick "Bob" instead).
    text = ledger("2026-03-01,Zed,10.00,Bob;ann;Cy,")
    assert ok_lines(text) == ["ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_largest_remainder_beats_name_order():
    # 1.01 with weights 1,1,2 (W=4): bases 25,25,50, leftover 1.
    # remainders: Ann 1, Bob 1, Cy 2 -> Cy gets the extra cent.
    text = ledger("2026-03-01,Zed,1.01,Ann:1;Bob:1;Cy:2,")
    assert ok_lines(text) == ["Ann -0.25", "Bob -0.25", "Cy -0.51", "Zed +1.01"]


def test_two_leftover_cents_and_zero_balance_shown_without_sign():
    # 0.02 among three equal people: bases 0, two cents left -> ann and Bob
    # (first two by name ignoring case) get one each; Cy owes nothing.
    text = ledger("2026-03-01,Zed,0.02,ann;Bob;Cy,")
    assert ok_lines(text) == ["ann -0.01", "Bob -0.01", "Cy 0.00", "Zed +0.02"]


def test_small_weight_loses_tie_break_on_remainder():
    # 0.01 among Ann:1 and Bob:2 (W=3): remainders 1 vs 2 -> Bob gets the cent.
    text = ledger("2026-03-01,Zed,0.01,Ann;Bob:2,")
    assert ok_lines(text) == ["Ann 0.00", "Bob -0.01", "Zed +0.01"]


def test_payer_as_participant_gets_share_back():
    text = ledger("2026-03-01,Ann,30,Ann;Bob;Cy,")
    assert ok_lines(text) == ["Ann +20.00", "Bob -10.00", "Cy -10.00"]


def test_payer_outside_participants():
    text = ledger("2026-03-01,Ann,30,Bob;Cy,")
    assert ok_lines(text) == ["Ann +30.00", "Bob -15.00", "Cy -15.00"]


def test_zero_balance_prints_without_sign_and_nothing_to_settle():
    text = ledger("2026-03-01,Ann,10,Ann,")
    assert ok_lines(text) == ["Ann 0.00"]
    assert run(text, "settle", "{f}")[1] == "nothing to settle\n"


def test_balances_cancel_out_to_nothing_to_settle():
    text = ledger("2026-03-01,Ann,10,Bob,", "2026-03-02,Bob,10,Ann,")
    assert ok_lines(text) == ["Ann 0.00", "Bob 0.00"]
    assert run(text, "settle", "{f}")[1] == "nothing to settle\n"
    assert json.loads(settle_run(text, fmt="json")[1]) == {"transfers": []}


def test_balance_sign_and_two_decimals_for_small_values():
    text = ledger("2026-03-01,Ann,0.01,Bob,")
    assert ok_lines(text) == ["Ann +0.01", "Bob -0.01"]


def test_negative_balance_formatting_with_cents():
    text = ledger("2026-03-01,Bob,0.05,Ann,")
    assert ok_lines(text) == ["Ann -0.05", "Bob +0.05"]


def test_json_cents_are_integers():
    code, out, _ = bal(ledger("2026-03-01,Ann,0.05,Bob,"), fmt="json")
    data = json.loads(out)
    assert data == {"balances": [{"name": "Ann", "cents": 5}, {"name": "Bob", "cents": -5}]}
    assert all(type(x["cents"]) is int for x in data["balances"])


# ---------------------------------------------------------------- names

def test_names_match_ignoring_case_and_show_first_spelling():
    text = ledger(
        "2026-03-01,ann,10,Ann;Bob,",
        "2026-03-02,BOB,4,ANN,",
    )
    # row 1: ann +5, Bob -5. row 2: Bob +4, ann -4 -> ann +1, Bob -1
    assert ok_lines(text) == ["ann +1.00", "Bob -1.00"]


def test_payer_spelling_precedes_participant_spelling_in_same_row():
    text = ledger("2026-03-01,zoe,10,Zoe;Abe,")
    assert ok_lines(text) == ["Abe -5.00", "zoe +5.00"]


def test_names_are_trimmed():
    text = ledger("2026-03-01,  Ann  ,10,Ann; Bob ,")
    assert ok_lines(text) == ["Ann +5.00", "Bob -5.00"]


def test_output_sorted_ignoring_case():
    text = ledger("2026-03-01,bob,3,Ann;Cy;dan,")
    assert ok_lines(text) == ["Ann -1.00", "bob +3.00", "Cy -1.00", "dan -1.00"]


def test_names_in_settle_use_first_spelling():
    text = ledger("2026-03-01,ANN,10,Bob,")
    assert settle_run(text)[1] == "Bob -> ANN 10.00\n"


# ---------------------------------------------------------------- settle

def test_settle_debtor_tie_broken_ignoring_case():
    # Ann +10; bob and Cy both -5. Case-insensitive: bob before Cy.
    text = ledger("2026-03-01,Ann,10,Cy;bob,")
    assert settle_run(text)[1] == "bob -> Ann 5.00\nCy -> Ann 5.00\n"


def test_settle_creditor_tie_broken_by_name():
    # Ann +10, Dan +10, Bob -20 -> Bob pays Ann first (ascending name).
    text = ledger("2026-03-01,Ann,10,Bob,", "2026-03-02,Dan,10,Bob,")
    assert settle_run(text)[1] == "Bob -> Ann 10.00\nBob -> Dan 10.00\n"


def test_settle_each_step_uses_most_negative_and_most_positive():
    # Ann +10, Bob -5, Cy -9, Dan +4.
    # Step 1: Cy (-9) pays Ann (+10) 9.00 -> Ann +1.
    # Step 2: Bob (-5) pays Dan (+4) 4.00 -> Dan 0, Bob -1.
    # Step 3: Bob (-1) pays Ann (+1) 1.00.
    text = ledger("2026-03-01,Ann,10,Bob;Cy,", "2026-03-02,Dan,4,Cy,")
    assert settle_run(text)[1] == "Cy -> Ann 9.00\nBob -> Dan 4.00\nBob -> Ann 1.00\n"


def test_settle_text_has_unsigned_amounts_and_arrow_format():
    text = ledger("2026-03-01,Ann,0.50,Bob,")
    assert settle_run(text)[1] == "Bob -> Ann 0.50\n"


def test_settle_json_uses_from_to_cents():
    text = ledger("2026-03-01,Ann,0.50,Bob,")
    assert json.loads(settle_run(text, fmt="json")[1]) == {"transfers": [
        {"from": "Bob", "to": "Ann", "cents": 50}]}


def test_settle_procedure_replay_matches_balances():
    # Replay the transfers from the balances and verify everything clears.
    code, out, _ = bal(TRIP, fmt="json")
    bals = {x["name"]: x["cents"] for x in json.loads(out)["balances"]}
    _, sout, _ = settle_run(TRIP, fmt="json")
    for t in json.loads(sout)["transfers"]:
        assert t["cents"] > 0
        bals[t["from"]] += t["cents"]
        bals[t["to"]] -= t["cents"]
    assert all(v == 0 for v in bals.values())


# ---------------------------------------------------------------- date filters

def test_since_is_inclusive():
    # rows on 03-02 and 03-03 only: Ann -18, Bob +12, Cy +6
    assert ok_lines(TRIP, "--since", "2026-03-02") == ["Ann -18.00", "Bob +12.00", "Cy +6.00"]


def test_until_is_inclusive():
    # rows on 03-01 and 03-02 only: Ann +25, Bob -5, Cy -20
    assert ok_lines(TRIP, "--until", "2026-03-02") == ["Ann +25.00", "Bob -5.00", "Cy -20.00"]


def test_since_and_until_both_inclusive_single_day():
    assert ok_lines(TRIP, "--since", "2026-03-02", "--until", "2026-03-02") == ["Ann -15.00", "Bob +15.00"]


def test_range_covering_everything_matches_unfiltered():
    assert ok_lines(TRIP, "--since", "2026-03-01", "--until", "2026-03-03") == ok_lines(TRIP)


def test_people_only_in_excluded_rows_are_not_printed():
    text = ledger(
        "2026-01-01,Zed,10,Yan,",
        "2026-03-01,Ann,10,Bob,",
    )
    assert ok_lines(text, "--since", "2026-02-01") == ["Ann +10.00", "Bob -10.00"]
    assert ok_lines(text, "--until", "2026-02-01") == ["Yan -10.00", "Zed +10.00"]


def test_no_rows_in_range_balances_empty_and_settle_nothing():
    code, out, err = bal(TRIP, "--since", "2027-01-01")
    assert code == 0 and out == "" and err == ""
    assert settle_run(TRIP, "--since", "2027-01-01")[1] == "nothing to settle\n"


def test_date_filter_applies_to_json_and_settle():
    code, out, _ = bal(TRIP, "--since", "2026-03-03", fmt="json")
    assert json.loads(out) == {"balances": [{"name": "Ann", "cents": -300}, {"name": "Bob", "cents": -300},
                                            {"name": "Cy", "cents": 600}]}
    # only row 1 (Ann +40, Bob -20, Cy -20): Bob first on tie
    code, out, _ = settle_run(TRIP, "--until", "2026-03-01", fmt="json")
    assert json.loads(out) == {"transfers": [
        {"from": "Bob", "to": "Ann", "cents": 2000},
        {"from": "Cy", "to": "Ann", "cents": 2000},
    ]}


def test_date_filter_options_before_positionals():
    code, out, _ = run(TRIP, "--since", "2026-03-02", "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -18.00", "Bob +12.00", "Cy +6.00"]


# ---------------------------------------------------------------- randomized oracle

def _oracle_balances(rows):
    """Independent implementation of the spec, working on structured data.

    rows: list of (date, payer, cents, [(name, weight), ...]) with names as written.
    Returns sorted [(first_spelling, cents)].
    """
    spelling, bals = {}, {}

    def touch(n):
        k = n.lower()
        spelling.setdefault(k, n)
        bals.setdefault(k, 0)
        return k

    for _d, payer, cents, parts in rows:
        p = touch(payer)
        keys = [(touch(n), w) for n, w in parts]
        bals[p] += cents
        total = sum(w for _, w in keys)
        base = {k: cents * w // total for k, w in keys}
        left = cents - sum(base.values())
        ranked = sorted(keys, key=lambda kw: (-((cents * kw[1]) % total), kw[0]))
        for k in base:
            bals[k] -= base[k]
        for k, _ in ranked[:left]:
            bals[k] -= 1
    assert sum(bals.values()) == 0
    return [(spelling[k], bals[k]) for k in sorted(bals)]


def _oracle_settle(balances):
    b = {n: c for n, c in balances}
    out = []
    while any(v != 0 for v in b.values()):
        debtor = min(b, key=lambda n: (b[n], n.lower()))
        creditor = min(b, key=lambda n: (-b[n], n.lower()))
        amt = min(-b[debtor], b[creditor])
        b[debtor] += amt
        b[creditor] -= amt
        out.append((debtor, creditor, amt))
    return out


def _fmt(c, signed):
    s = ""
    if signed and c:
        s = "+" if c > 0 else "-"
    return f"{s}{abs(c) // 100}.{abs(c) % 100:02d}"


def _random_case(rng):
    base_names = ["Ann", "Bob", "Cy", "dan", "Eve", "Fay"]
    rows_struct, rows_text = [], []
    for _ in range(rng.randint(1, 7)):
        d = date(2026, 1, 1) + timedelta(days=rng.randrange(365))
        def spell(n):
            v = rng.choice([n, n.upper(), n.lower(), n.title()])
            return " " * rng.randrange(2) + v + " " * rng.randrange(2)
        payer_base = rng.choice(base_names)
        k = rng.randint(1, 4)
        chosen = rng.sample(base_names, k)
        parts_struct, parts_text = [], []
        for n in chosen:
            w = rng.randint(1, 3)
            txt = spell(n)
            parts_struct.append((txt.strip(), w))
            if w != 1 or rng.random() < 0.2:
                txt += ":" + str(w)
            parts_text.append(txt)
        cents = rng.randint(1, 100000)
        if rng.random() < 0.3:
            cents -= cents % 100  # whole amounts sometimes
        if cents == 0:
            cents = 100
        if cents % 100 == 0:
            amt = str(cents // 100) if rng.random() < 0.5 else f"{cents // 100}.00"
        elif cents % 10 == 0:
            amt = f"{cents // 100}.{(cents % 100) // 10}"
        else:
            amt = f"{cents // 100}.{cents % 100:02d}"
        payer_txt = spell(payer_base)
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(
            [d.isoformat(), payer_txt, amt, ";".join(parts_text), rng.choice(["", "a, b", "x"])])
        rows_text.append(buf.getvalue().rstrip("\n"))
        rows_struct.append((d, payer_txt.strip(), cents, parts_struct))
    return ledger(*rows_text), rows_struct


@pytest.mark.parametrize("seed", range(40))
def test_random_ledgers_match_oracle(seed):
    text, struct = _random_case(random.Random(seed))
    expected_bal = _oracle_balances(struct)
    expected_lines = [f"{n} {_fmt(c, True)}" for n, c in expected_bal]

    code, out, err = bal(text)
    assert code == 0, err
    assert lines(out) == expected_lines

    code, out, _ = bal(text, fmt="json")
    assert json.loads(out) == {"balances": [{"name": n, "cents": c} for n, c in expected_bal]}

    expected_tr = _oracle_settle(expected_bal)
    code, out, _ = settle_run(text)
    assert code == 0
    if expected_tr:
        assert lines(out) == [f"{d} -> {c} {_fmt(x, False)}" for d, c, x in expected_tr]
    else:
        assert out == "nothing to settle\n"

    code, out, _ = settle_run(text, fmt="json")
    assert json.loads(out) == {"transfers": [
        {"from": d, "to": c, "cents": x} for d, c, x in expected_tr]}
