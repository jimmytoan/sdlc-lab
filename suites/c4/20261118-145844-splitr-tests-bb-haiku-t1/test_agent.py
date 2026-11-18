"""Black-box regression suite for splitr, derived from SPEC.md.

The product is driven only through its command line (see helpers.run).
"""
import csv
import io
import json
import os
import random
import subprocess
import sys
import tempfile

import pytest

from helpers import APP_DIR, run, lines

HEADER = "date,payer,amount,participants,note"


# ---------------------------------------------------------------- helpers

def ledger(*rows, header=HEADER):
    """Build ledger text from raw data-row strings (no trailing blank)."""
    return header + "\n" + "".join(r + "\n" for r in rows)


def cli(*args):
    """Run splitr with literal arguments (no ledger substitution)."""
    p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), *args],
                       capture_output=True, text=True, timeout=20, cwd=APP_DIR)
    return p.returncode, p.stdout, p.stderr


def run_raw(data: bytes, *args):
    """Write raw bytes to a ledger file and run the CLI on it."""
    return run("", *args, raw=data)


def err_lines(err):
    return [x for x in err.splitlines() if x.strip()]


def fmt_signed(cents):
    if cents == 0:
        return "0.00"
    sign = "+" if cents > 0 else "-"
    a = abs(cents)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(cents):
    return f"{cents // 100}.{cents % 100:02d}"


# Reference model of the SPEC, used for randomised cross-checks.
def model(rows):
    """rows: list of (payer, cents, [(name, weight), ...]).

    Returns (display_by_key, balance_by_key, key_order_of_first_appearance)."""
    display, bal = {}, {}

    def see(name):
        k = name.lower()
        display.setdefault(k, name)
        bal.setdefault(k, 0)
        return k

    for payer, cents, parts in rows:
        pk = see(payer)
        bal[pk] += cents
        kw = [(see(n), w) for n, w in parts]
        total_w = sum(w for _, w in kw)
        base = {k: cents * w // total_w for k, w in kw}
        rem = {k: cents * w % total_w for k, w in kw}
        left = cents - sum(base.values())
        ranked = sorted((k for k, _ in kw), key=lambda k: (-rem[k], k))
        for k in ranked[:left]:
            base[k] += 1
        for k, _ in kw:
            bal[k] -= base[k]
    return display, bal


def model_settle(display, bal):
    bal = dict(bal)
    out = []
    while any(v != 0 for v in bal.values()):
        d = min(bal, key=lambda k: (bal[k], k))
        c = min(bal, key=lambda k: (-bal[k], k))
        amt = min(-bal[d], bal[c])
        out.append((display[d], display[c], amt))
        bal[d] += amt
        bal[c] -= amt
    return out


# ---------------------------------------------------------------- balances: output format

def test_balances_signs_and_zero_format():
    led = ledger("2026-01-01,Zed,3.00,Ann;Bob,",   # Zed +3, Ann -1.50, Bob -1.50
                 "2026-01-02,Solo,5.00,Solo,")       # Solo 0.00
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann -1.50", "Bob -1.50", "Solo 0.00", "Zed +3.00"]


def test_balances_sorted_ignoring_case():
    # Case-sensitive sorting would put Bob and Zed before amy.
    led = ledger("2026-01-01,Zed,3.00,amy;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert [l.split()[0] for l in lines(out)] == ["amy", "Bob", "Zed"]


def test_balances_name_spelling_is_first_appearance():
    # payer before participants; first appearance decides display spelling.
    led = ledger("2026-01-01,bob,2.00,ANN;Cy,",
                 "2026-01-02,Ann,4.00,bob;cy,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["ANN +3.00", "bob 0.00", "Cy -3.00"]


def test_balances_payer_spelling_precedes_participant_spelling_in_same_row():
    led = ledger("2026-01-01,PAT,2.00,pat;Quinn,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["PAT +1.00", "Quinn -1.00"]


def test_names_are_trimmed():
    led = ledger("2026-01-01, Ann ,6.00, Ann ; Bob:2 ,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann +4.00", "Bob -4.00"]


def test_balances_large_amount_formatting():
    led = ledger("2026-01-01,Rich,12345678.90,Rich;Poor,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Poor -6172839.45", "Rich +6172839.45"]


def test_one_cent_balance_formatting():
    led = ledger("2026-01-01,Zed,0.05,Ann,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -0.05", "Zed +0.05"]


def test_success_has_empty_stderr():
    code, out, err = run(ledger("2026-01-01,A,1,B,"), "balances", "{f}")
    assert code == 0 and err == ""


# ---------------------------------------------------------------- splitting rule

def test_leftover_cent_goes_to_name_order_ignoring_case():
    # 1000 cents / 3: each base 333, remainders tie, so ann (case-insensitive first) gets the cent.
    led = ledger("2026-01-01,Zed,10.00,Bob;ann;Cy,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_largest_remainder_beats_alphabetical_order():
    # 10 cents, weights 1 and 2: bases 3 and 6, leftover 1 goes to Bob (remainder 2 > 1).
    led = ledger("2026-01-01,Zed,0.10,Ann;Bob:2,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Ann -0.03", "Bob -0.07", "Zed +0.10"]


def test_multiple_leftover_cents_handed_out_by_name():
    # 2 cents among 3 equal weights: bases 0, two leftover cents to ann and Bob; Cy gets none.
    led = ledger("2026-01-01,Zed,0.02,Cy;ann;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["ann -0.01", "Bob -0.01", "Cy 0.00", "Zed +0.02"]


def test_weights_split_proportionally():
    led = ledger("2026-01-01,Ann,40.00,Ann:1;Bob:3,")
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann +30.00", "Bob -30.00"]


def test_default_weight_is_one():
    led = ledger("2026-01-01,Zed,9.00,A;B;C,")
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["A -3.00", "B -3.00", "C -3.00", "Zed +9.00"]


def test_explicit_weight_one_equals_default():
    a = run(ledger("2026-01-01,Zed,9.00,A:1;B,"), "balances", "{f}")
    b = run(ledger("2026-01-01,Zed,9.00,A;B,"), "balances", "{f}")
    assert a == b


def test_payer_need_not_be_participant():
    led = ledger("2026-01-01,Out,6.00,In1;In2,")
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["In1 -3.00", "In2 -3.00", "Out +6.00"]


def test_payer_may_be_a_participant():
    led = ledger("2026-01-01,Ann,6.00,Ann;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["Ann +3.00", "Bob -3.00"]


def test_balances_always_sum_to_zero_example():
    led = ledger("2026-01-01,A,10.00,A;B;C,",
                 "2026-01-02,B,7.77,A;B;C:2,",
                 "2026-01-03,C,0.01,D;E;F;G,")
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    total = sum(x["cents"] for x in json.loads(out)["balances"])
    assert code == 0 and total == 0


# ---------------------------------------------------------------- settle

def test_settle_greedy_sequence():
    # Balances: X +10, Y +5, Z -8, W -7.
    led = ledger("2026-01-01,X,10.00,Z,",
                 "2026-01-02,Y,5.00,W,",
                 "2026-01-03,Z,2.00,W,")
    code, out, err = run(led, "settle", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Z -> X 8.00", "W -> Y 5.00", "W -> X 2.00"]


def test_settle_tie_breaks_ignore_case():
    # Debtors b and C tie at -5: b first (ignoring case; case-sensitive would pick C).
    # Creditors a and Z tie at +5: a first (ignoring case; case-sensitive would pick Z).
    led = ledger("2026-01-01,a,5.00,b,",
                 "2026-01-02,Z,5.00,C,")
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["b -> a 5.00", "C -> Z 5.00"]


def test_settle_tie_breaks_by_name_for_debtors_and_creditors():
    led = ledger("2026-01-01,A,5.00,C,",
                 "2026-01-02,B,5.00,D,")
    code, out, _ = run(led, "settle", "{f}")
    assert lines(out) == ["C -> A 5.00", "D -> B 5.00"]


def test_settle_nothing_to_settle_text():
    code, out, err = run(ledger("2026-01-01,A,5.00,A,"), "settle", "{f}")
    assert code == 0 and err == ""
    assert out.strip() == "nothing to settle"
    assert lines(out) == ["nothing to settle"]


def test_settle_amount_unsigned_two_decimals():
    led = ledger("2026-01-01,Ann,12.50,Bob,")
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Bob -> Ann 12.50"]


def test_settle_transfers_clear_balances():
    led = ledger("2026-01-01,A,10.00,A;B;C,",
                 "2026-01-02,B,7.77,A;B;C:2,",
                 "2026-01-03,C,0.01,D;E;F;G,")
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    bal = {x["name"]: x["cents"] for x in json.loads(out)["balances"]}
    code2, out2, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0 and code2 == 0
    for t in json.loads(out2)["transfers"]:
        assert t["cents"] > 0
        bal[t["from"]] += t["cents"]
        bal[t["to"]] -= t["cents"]
    assert all(v == 0 for v in bal.values())


# ---------------------------------------------------------------- JSON output

def test_balances_json_shape_and_types():
    led = ledger("2026-01-01,Ann,60.00,Ann;Bob;Cy,", "2026-01-02,Bob,30,Ann;Bob,")
    code, out, err = run(led, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert list(doc.keys()) == ["balances"]
    for item in doc["balances"]:
        assert set(item.keys()) == {"name", "cents"}
        assert type(item["cents"]) is int
    assert [i["name"] for i in doc["balances"]] == ["Ann", "Bob", "Cy"]


def test_balances_json_matches_text_order_and_values():
    led = ledger("2026-01-01,zed,10.00,amy;Bob;Cy,", "2026-01-02,amy,0.01,Solo,")
    _, text, _ = run(led, "balances", "{f}")
    _, js, _ = run(led, "balances", "{f}", "--format", "json")
    text_pairs = [(l.rsplit(" ", 1)[0], l.rsplit(" ", 1)[1]) for l in lines(text)]
    json_pairs = [(i["name"], i["cents"]) for i in json.loads(js)["balances"]]
    assert [n for n, _ in text_pairs] == [n for n, _ in json_pairs]
    for (_, t), (_, c) in zip(text_pairs, json_pairs):
        assert t == fmt_signed(c)


def test_settle_json_shape():
    led = ledger("2026-01-01,X,10.00,Z,",
                 "2026-01-02,Y,5.00,W,",
                 "2026-01-03,Z,2.00,W,")
    code, out, err = run(led, "settle", "{f}", "--format", "json")
    assert code == 0 and err == ""
    assert json.loads(out) == {"transfers": [
        {"from": "Z", "to": "X", "cents": 800},
        {"from": "W", "to": "Y", "cents": 500},
        {"from": "W", "to": "X", "cents": 200},
    ]}


def test_settle_json_nothing_to_settle_is_empty_list():
    code, out, _ = run(ledger("2026-01-01,A,5.00,A,"), "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": []}


def test_balances_json_empty_ledger():
    code, out, _ = run(ledger(), "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"balances": []}


def test_text_is_default_format():
    led = ledger("2026-01-01,A,1.00,B,")
    assert run(led, "balances", "{f}") == run(led, "balances", "{f}", "--format", "text")


# ---------------------------------------------------------------- empty / header-only ledgers

def test_header_only_balances_prints_nothing():
    code, out, err = run(ledger(), "balances", "{f}")
    assert code == 0 and out == "" and err == ""


def test_header_only_settle_says_nothing_to_settle():
    code, out, err = run(ledger(), "settle", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["nothing to settle"]


def test_balances_all_zero_still_lists_people():
    led = ledger("2026-01-01,A,5.00,A,")
    code, out, _ = run(led, "balances", "{f}")
    assert lines(out) == ["A 0.00"]


# ---------------------------------------------------------------- --since / --until

RANGE_LEDGER = ledger(
    "2025-12-31,Old,4.00,Old,",
    "2026-01-01,Start,6.00,Start;Mid,",
    "2026-01-31,End,2.00,End,",
    "2026-02-01,Late,8.00,Late,",
)


def test_since_and_until_are_inclusive():
    code, out, err = run(RANGE_LEDGER, "balances", "{f}",
                         "--since", "2026-01-01", "--until", "2026-01-31")
    assert code == 0 and err == ""
    assert lines(out) == ["End 0.00", "Mid -3.00", "Start +3.00"]


def test_people_only_in_excluded_rows_not_printed():
    code, out, _ = run(RANGE_LEDGER, "balances", "{f}", "--since", "2026-01-01")
    names = [l.split()[0] for l in lines(out)]
    assert "Old" not in names
    assert lines(out) == ["End 0.00", "Late 0.00", "Mid -3.00", "Start +3.00"]


def test_until_alone():
    code, out, _ = run(RANGE_LEDGER, "balances", "{f}", "--until", "2026-01-31")
    assert lines(out) == ["End 0.00", "Mid -3.00", "Old 0.00", "Start +3.00"]


def test_since_equal_until_selects_single_day():
    code, out, _ = run(RANGE_LEDGER, "balances", "{f}",
                       "--since", "2026-01-01", "--until", "2026-01-01")
    assert lines(out) == ["Mid -3.00", "Start +3.00"]


def test_range_applies_to_settle():
    code, out, _ = run(RANGE_LEDGER, "settle", "{f}",
                       "--since", "2026-01-01", "--until", "2026-01-31")
    assert lines(out) == ["Mid -> Start 3.00"]


def test_range_applies_to_settle_json():
    code, out, _ = run(RANGE_LEDGER, "settle", "{f}", "--format", "json",
                       "--since", "2026-01-01", "--until", "2026-01-31")
    assert json.loads(out) == {"transfers": [{"from": "Mid", "to": "Start", "cents": 300}]}


def test_range_excluding_everything_gives_empty_result():
    code, out, _ = run(RANGE_LEDGER, "balances", "{f}",
                       "--since", "2030-01-01", "--until", "2030-12-31")
    assert code == 0 and out == ""
    code, out, _ = run(RANGE_LEDGER, "settle", "{f}",
                       "--since", "2030-01-01", "--until", "2030-12-31")
    assert lines(out) == ["nothing to settle"]


def test_invalid_row_outside_range_still_fails_validation():
    led = ledger("2026-01-01,A,1.00,B,",
                 "2026-06-01,C,abc,D,")  # bad amount, outside the requested range
    code, out, err = run(led, "balances", "{f}", "--until", "2026-01-31")
    assert code == 2 and out == ""
    assert err_lines(err) == ["line 3: bad-amount"]


def test_options_before_and_after_positionals_are_equivalent():
    forms = [
        ("balances", "{f}", "--format", "json"),
        ("--format", "json", "balances", "{f}"),
        ("balances", "--format", "json", "{f}"),
    ]
    outs = [run(RANGE_LEDGER, *f, "--since", "2026-01-01")[1] for f in forms]
    assert outs[0] == outs[1] == outs[2]
    assert json.loads(outs[0])["balances"][0]["name"] == "End"


def test_range_options_may_follow_positionals():
    a = run(RANGE_LEDGER, "settle", "{f}", "--since", "2026-01-01", "--until", "2026-01-31")
    b = run(RANGE_LEDGER, "--since", "2026-01-01", "--until", "2026-01-31", "settle", "{f}")
    assert a == b


# ---------------------------------------------------------------- ledger file parsing

def test_utf8_bom_is_accepted():
    data = b"\xef\xbb\xbf" + ledger("2026-01-01,Ann,4.00,Ann;Bob,").encode()
    code, out, err = run_raw(data, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_crlf_line_endings_are_accepted():
    data = (HEADER + "\r\n2026-01-01,Ann,4.00,Ann;Bob,\r\n# c\r\n\r\n").encode()
    code, out, err = run_raw(data, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_quoted_note_with_comma_and_escaped_quotes():
    led = ledger('2026-01-01,Ann,4.00,Ann;Bob,"coffee, cake"',
                 '2026-01-02,Ann,2.00,Ann;Bob,"say ""hi"""')
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +3.00", "Bob -3.00"]


def test_empty_note_allowed():
    led = ledger("2026-01-01,Ann,4.00,Ann;Bob,", "2026-01-01,Ann,4.00,Ann;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +4.00", "Bob -4.00"]


def test_columns_in_any_order():
    led = ("note,participants,amount,payer,date\n"
           "hi,\"Ann;Bob\",4.00,Ann,2026-01-01\n")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_extra_unknown_column_is_tolerated():
    led = ("date,payer,amount,participants,note,extra\n"
           "2026-01-01,Ann,4.00,Ann;Bob,,zzz\n")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_blank_comment_and_whitespace_lines_are_ignored():
    led = ("date,payer,amount,participants,note\n"
           "\n"
           "# a comment\n"
           "   \n"
           "   # indented comment\n"
           "2026-01-01,Ann,4.00,Ann;Bob,\n")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +2.00", "Bob -2.00"]


def test_line_numbers_count_ignored_lines():
    led = ("date,payer,amount,participants,note\n"   # 1
           "# comment\n"                              # 2
           "   \n"                                    # 3
           "\n"                                       # 4
           "2026-01-01,Ann,4.00,Ann;Bob,\n"           # 5 valid
           "2026-02-30,Ann,4.00,Ann;Bob,\n")          # 6 bad date
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert err_lines(err) == ["line 6: bad-date"]


def test_header_line_is_line_one_even_with_bom():
    data = b"\xef\xbb\xbf" + ledger("2026-02-30,Ann,4.00,Ann;Bob,").encode()
    code, out, err = run_raw(data, "balances", "{f}")
    assert code == 2
    assert err_lines(err) == ["line 2: bad-date"]


# ---------------------------------------------------------------- header errors

ALL_COLS = ["date", "payer", "amount", "participants", "note"]
ROW = {"date": "2026-01-01", "payer": "Ann", "amount": "4.00", "participants": "Ann;Bob", "note": "x"}


def _header_without(*missing):
    cols = [c for c in ALL_COLS if c not in missing]
    row = ",".join(ROW[c] for c in cols)
    return ",".join(cols) + "\n" + row + "\n"


@pytest.mark.parametrize("missing", ALL_COLS)
def test_single_missing_header_column(missing):
    code, out, err = run(_header_without(missing), "balances", "{f}")
    assert code == 2 and out == ""
    assert err_lines(err) == [f"header: missing {missing}"]


def test_missing_header_reports_first_in_fixed_order_only():
    led = _header_without("payer", "note") + "2026-02-30,,abc,,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert err_lines(err) == ["header: missing payer"]


def test_missing_amount_and_participants_reports_amount():
    code, out, err = run(_header_without("participants", "amount"), "settle", "{f}")
    assert code == 2 and out == ""
    assert err_lines(err) == ["header: missing amount"]


def test_missing_header_with_valid_rows_still_fails():
    code, out, err = run(_header_without("date"), "settle", "{f}")
    assert code == 2 and out == ""
    assert err_lines(err) == ["header: missing date"]


# ---------------------------------------------------------------- row validation

def _row_err(row, command="balances"):
    code, out, err = run(ledger(row), command, "{f}")
    assert out == ""
    return code, err_lines(err)


@pytest.mark.parametrize("cmd", ["balances", "settle"])
@pytest.mark.parametrize("date", [
    "2026-02-29",      # 2026 is not a leap year
    "2026-04-31",      # April has 30 days
    "2026-00-10",
    "2026-01-00",
    "2026-13-01",
    "2026-2-1",
    "2026/01/01",
    "01/01/2026",
    "20260101",
    "",
    "tomorrow",
])
def test_bad_date(date, cmd):
    code, errs = _row_err(f"{date},Ann,4.00,Ann;Bob,", cmd)
    assert code == 2
    assert errs == ["line 2: bad-date"]


@pytest.mark.parametrize("date", ["2024-02-29", "2026-12-31", "2000-02-29"])
def test_valid_dates(date):
    code, out, err = run(ledger(f"{date},Ann,4.00,Ann;Bob,"), "balances", "{f}")
    assert code == 0 and err == ""


@pytest.mark.parametrize("payer", ["", "   "])
def test_bad_payer(payer):
    code, errs = _row_err(f"2026-01-01,{payer},4.00,Ann;Bob,")
    assert code == 2 and errs == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", ["0", "0.00", "-1", "-0.50", "+1", "1,000", "1e3", "1.234",
                                    "", "abc", "12.5.1", "$12", "1 000"])
def test_bad_amount(amount):
    quoted = f'"{amount}"' if "," in amount else amount
    code, errs = _row_err(f"2026-01-01,Ann,{quoted},Ann;Bob,")
    assert code == 2 and errs == ["line 2: bad-amount"]


@pytest.mark.parametrize("amount,cents", [
    ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), ("1000000", 100000000),
])
def test_valid_amounts(amount, cents):
    code, out, err = run(ledger(f"2026-01-01,Zed,{amount},Ann,"), "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    bal = {x["name"]: x["cents"] for x in json.loads(out)["balances"]}
    assert bal == {"Ann": -cents, "Zed": cents}


@pytest.mark.parametrize("parts", [
    "", ";", "Ann;", ";Ann", "Ann;;Bob", "   ",
    "Ann:0", "Ann:-1", "Ann:1.5", "Ann:x", "Ann:", 
    "Ann;Ann", "Ann;ann", "Ann;Bob;ANN:2",
])
def test_bad_participants(parts):
    code, errs = _row_err(f"2026-01-01,Zed,4.00,{parts},")
    assert code == 2 and errs == ["line 2: bad-participants"]


@pytest.mark.parametrize("parts", ["Ann", "Ann:2", "Ann:10;Bob:1", " Ann ; Bob "])
def test_valid_participants(parts):
    code, out, err = run(ledger(f"2026-01-01,Zed,4.00,{parts},"), "balances", "{f}")
    assert code == 0 and err == ""


def test_first_failing_check_is_reported():
    # bad date + bad payer + bad amount + bad participants -> only bad-date
    code, errs = _row_err("2026-13-01,,abc,;;")
    assert errs == ["line 2: bad-date"]
    code, errs = _row_err("2026-01-01,,abc,;;")
    assert errs == ["line 2: bad-payer"]
    code, errs = _row_err("2026-01-01,Ann,abc,;;")
    assert errs == ["line 2: bad-amount"]


def test_multiple_invalid_rows_each_reported_in_line_order():
    led = ledger("2026-02-30,Ann,1.00,Ann,",     # line 2 bad-date
                 "2026-01-01,,1.00,Ann,",         # line 3 bad-payer
                 "2026-01-01,Ann,0,Ann,",         # line 4 bad-amount
                 "2026-01-01,Ann,1.00,Ann;Ann,",  # line 5 bad-participants
                 "2026-01-01,Ann,1.00,Ann,")      # line 6 valid
    for cmd in ("balances", "settle"):
        code, out, err = run(led, cmd, "{f}")
        assert code == 2 and out == ""
        assert err_lines(err) == [
            "line 2: bad-date", "line 3: bad-payer",
            "line 4: bad-amount", "line 5: bad-participants"]


def test_one_invalid_row_suppresses_all_stdout():
    led = ledger("2026-01-01,Ann,4.00,Ann;Bob,", "2026-01-01,Ann,x,Ann;Bob,")
    for cmd in ("balances", "settle"):
        code, out, err = run(led, cmd, "{f}", "--format", "json")
        assert code == 2 and out == ""
        assert err_lines(err) == ["line 3: bad-amount"]


def test_bad_row_in_file_with_only_valid_rows_otherwise():
    led = ledger("2026-01-01,Ann,4.00,Ann;Bob,", "2026-01-01,Ann,4.00,Ann;Bob;Bob:0,")
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert err_lines(err) == ["line 3: bad-participants"]


# ---------------------------------------------------------------- usage errors (exit 64)

@pytest.mark.parametrize("args", [
    (),
    ("balances",),
    ("settle",),
    ("explode", "{f}"),
    ("balances", "{f}", "--bogus"),
    ("settle", "{f}", "--verbose"),
    ("balances", "{f}", "--format", "xml"),
    ("balances", "{f}", "--format"),
    ("balances", "{f}", "--since", "2026-02-30"),
    ("balances", "{f}", "--since", "nope"),
    ("balances", "{f}", "--until", "2026-13-01"),
    ("settle", "{f}", "--until", "yesterday"),
    ("balances", "{f}", "--since"),
])
def test_usage_errors_exit_64_with_empty_stdout(args):
    led = ledger("2026-01-01,Ann,4.00,Ann;Bob,")
    if args == ():
        code, out, err = cli()
    else:
        code, out, err = run(led, *args)
    assert code == 64
    assert out == ""


# ---------------------------------------------------------------- unreadable ledger (exit 3)

def test_missing_file_exit_3():
    missing = os.path.join(tempfile.gettempdir(), "splitr-no-such-ledger-xyz.csv")
    for cmd in ("balances", "settle"):
        code, out, err = cli(cmd, missing)
        assert code == 3 and out == ""
        assert err.strip() == f"error: cannot read {missing}"


def test_directory_as_ledger_exit_3():
    with tempfile.TemporaryDirectory() as d:
        code, out, err = cli("balances", d)
        assert code == 3 and out == ""
        assert err.startswith("error: cannot read ")


# ---------------------------------------------------------------- randomised cross-check against the model

def _random_ledger(rng):
    base = ["ann", "bob", "cy", "dan", "eve", "fay", "gus"]
    def spell(n):
        r = rng.random()
        return n.upper() if r < 0.2 else n.capitalize() if r < 0.5 else n
    rows = []
    for _ in range(rng.randint(1, 12)):
        day = rng.randint(1, 10)
        date = f"2026-01-{day:02d}"
        payer = rng.choice(base)
        cents = rng.randint(1, 99999)
        k = rng.randint(1, 4)
        names = rng.sample(base, k)
        parts = []
        for n in names:
            w = rng.randint(1, 3)
            txt = spell(n) + (f":{w}" if w != 1 or rng.random() < 0.3 else "")
            parts.append((txt, w))
        note = rng.choice(["", "lunch", "a, b"])
        rows.append((date, spell(payer), cents, parts, note))
    return rows


def _render(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "payer", "amount", "participants", "note"])
    for date, payer, cents, parts, note in rows:
        amt = f"{cents // 100}" if cents % 100 == 0 else (
            f"{cents // 100}.{(cents % 100) // 10}" if cents % 10 == 0 else f"{cents // 100}.{cents % 100:02d}")
        w.writerow([date, payer, amt, ";".join(t for t, _ in parts), note])
    return buf.getvalue()


@pytest.mark.parametrize("seed", range(25))
def test_random_ledgers_match_reference_model(seed):
    rng = random.Random(1000 + seed)
    rows = _random_ledger(rng)
    since, until = (None, None)
    if seed % 2:
        since, until = "2026-01-03", "2026-01-07"
    led = _render(rows)

    considered = [r for r in rows
                  if (since is None or r[0] >= since) and (until is None or r[0] <= until)]
    model_rows = [(payer, cents, [(t.split(":")[0].strip(), int(t.split(":")[1]) if ":" in t else 1)
                                  for t, _ in parts])
                  for _, payer, cents, parts, _ in considered]
    display, bal = model(model_rows)

    args = []
    if since:
        args += ["--since", since]
    if until:
        args += ["--until", until]

    code, out, err = run(led, "balances", "{f}", *args)
    assert code == 0 and err == ""
    expected = [f"{display[k]} {fmt_signed(bal[k])}" for k in sorted(bal)]
    assert lines(out) == expected

    code, out, err = run(led, "balances", "{f}", "--format", "json", *args)
    assert code == 0
    assert json.loads(out) == {"balances": [{"name": display[k], "cents": bal[k]} for k in sorted(bal)]}

    transfers = model_settle(display, bal)
    code, out, err = run(led, "settle", "{f}", "--format", "json", *args)
    assert code == 0
    assert json.loads(out) == {"transfers": [
        {"from": d, "to": c, "cents": a} for d, c, a in transfers]}

    code, out, err = run(led, "settle", "{f}", *args)
    if transfers:
        assert lines(out) == [f"{d} -> {c} {fmt_unsigned(a)}" for d, c, a in transfers]
    else:
        assert lines(out) == ["nothing to settle"]
