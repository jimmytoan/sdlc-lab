"""Black-box regression suite for splitr.py, written from SPEC.md.

Every test drives the CLI through tests/helpers.run(). Expected values come
from the spec (hand-computed for the targeted tests, and from an independent
reference model in this file for the randomized tests).
"""
import csv
import io
import json
import os
import random
import re
import subprocess
import sys

import pytest

from helpers import APP_DIR, run, lines

HEADER = "date,payer,amount,participants,note"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ledger(*rows, header=HEADER, bom=False, eol="\n"):
    """Build ledger text from raw row strings (already CSV-encoded)."""
    text = eol.join([header, *rows]) + eol
    return ("\ufeff" if bom else "") + text


def bal_text(led, *extra):
    return run(led, "balances", "{f}", *extra)


def settle_text(led, *extra):
    return run(led, "settle", "{f}", *extra)


def bal_json(led, *extra):
    code, out, err = run(led, "balances", "{f}", "--format", "json", *extra)
    return code, (json.loads(out) if code == 0 else None), err


def settle_json(led, *extra):
    code, out, err = run(led, "settle", "{f}", "--format", "json", *extra)
    return code, (json.loads(out) if code == 0 else None), err


def assert_ok(result, expected_lines):
    code, out, err = result
    assert code == 0, (code, out, err)
    assert err == ""
    assert lines(out) == expected_lines


def assert_invalid(result, expected_err_lines):
    code, out, err = result
    assert code == 2, (code, out, err)
    assert out == ""
    assert lines(err) == expected_err_lines


def fmt_signed(c):
    """Spec format for balances: sign + two decimals; zero has no sign."""
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    a = abs(c)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(c):
    return f"{c // 100}.{c % 100:02d}"


# --- reference model of the spec -------------------------------------------

def ref_split(cents, parts):
    """parts: list of (key, weight) in row order. Returns {key: share_cents}."""
    total_w = sum(w for _, w in parts)
    base = [cents * w // total_w for _, w in parts]
    rem = [cents * w % total_w for _, w in parts]
    leftover = cents - sum(base)
    order = sorted(range(len(parts)), key=lambda i: (-rem[i], parts[i][0]))
    for i in order[:leftover]:
        base[i] += 1
    assert sum(base) == cents
    return {parts[i][0]: base[i] for i in range(len(parts))}


def ref_report(rows):
    """rows: list of dicts with date, payer, cents, participants[(name, w)].

    Returns (display names by key, balances by key)."""
    display = {}
    bal = {}

    def see(name):
        k = name.lower()
        display.setdefault(k, name)
        bal.setdefault(k, 0)
        return k

    for r in rows:
        pk = see(r["payer"])
        parts = [(see(n), w) for n, w in r["parts"]]
        bal[pk] += r["cents"]
        for k, share in ref_split(r["cents"], parts).items():
            bal[k] -= share
    return display, bal


def ref_settle(display, bal):
    bal = dict(bal)
    transfers = []
    while any(v != 0 for v in bal.values()):
        debtor = min((k for k in bal if bal[k] < 0), key=lambda k: (bal[k], k))
        creditor = min((k for k in bal if bal[k] > 0), key=lambda k: (-bal[k], k))
        amt = min(-bal[debtor], bal[creditor])
        transfers.append((display[debtor], display[creditor], amt))
        bal[debtor] += amt
        bal[creditor] -= amt
    return transfers


# --- randomized ledger generator --------------------------------------------

NAMES = ["Ann", "bob", "CY", "dan", "Eve", "fay", "Gus", "hal"]


def _rand_case(rng, name):
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in name)


def _fmt_amount(rng, cents):
    if cents % 100 == 0 and rng.random() < 0.5:
        return str(cents // 100)
    if cents % 10 == 0:
        return f"{cents // 100}.{(cents // 10) % 10}"
    return f"{cents // 100}.{cents % 100:02d}"


def gen_case(seed):
    """Return (ledger_bytes, rows_all) for a random valid ledger."""
    rng = random.Random(seed)
    rows = []
    for _ in range(rng.randint(0, 14)):
        payer = _rand_case(rng, rng.choice(NAMES))
        k = rng.randint(1, 4)
        picked = rng.sample(NAMES, k)  # distinct ignoring case
        parts = []
        for n in picked:
            w = rng.randint(1, 4)
            parts.append((_rand_case(rng, n), w))
        cents = rng.randint(1, 300000)
        rows.append({
            "date": f"2026-{rng.randint(1, 3):02d}-{rng.randint(1, 28):02d}",
            "payer": payer,
            "cents": cents,
            "amount": _fmt_amount(rng, cents),
            "parts": parts,
            "note": rng.choice(["", "dinner", "a, b", 'say "hi"', "not # a comment"]),
            "blank_before": rng.random() < 0.2,
        })
    return rows


def render(rng, rows, header_order, eol, bom):
    out = io.StringIO()
    w = csv.writer(out, lineterminator=eol)
    w.writerow(header_order)
    for r in rows:
        if r["blank_before"]:
            out.write(rng.choice(["", "   ", "# comment", "  # indented"]) + eol)
        cells = {
            "date": r["date"],
            "payer": r["payer"],
            "amount": r["amount"],
            "participants": ";".join(
                n if w_ == 1 else f"{n}:{w_}" for n, w_ in r["parts"]),
            "note": r["note"],
        }
        w.writerow([cells[c] for c in header_order])
    text = out.getvalue()
    return ("\ufeff" if bom else "") + text


def random_case(seed):
    rng = random.Random(seed * 7919 + 1)
    rows = gen_case(seed)
    header_order = ["date", "payer", "amount", "participants", "note"]
    if rng.random() < 0.5:
        rng.shuffle(header_order)
    eol = rng.choice(["\n", "\r\n"])
    bom = rng.random() < 0.3
    text = render(rng, rows, header_order, eol, bom)
    since = until = None
    if rng.random() < 0.4:
        since = f"2026-{rng.randint(1, 3):02d}-{rng.randint(1, 28):02d}"
    if rng.random() < 0.4:
        until = f"2026-{rng.randint(1, 3):02d}-{rng.randint(1, 28):02d}"
    considered = [r for r in rows
                  if (since is None or r["date"] >= since)
                  and (until is None or r["date"] <= until)]
    return text, considered, since, until


# ---------------------------------------------------------------------------
# File format: BOM, line endings, header, comments, quoting
# ---------------------------------------------------------------------------

BASIC_ROW = "2026-03-01,Ann,10.00,Ann;Bob,dinner"
BASIC_OUT = ["Ann +5.00", "Bob -5.00"]


def test_bom_is_accepted():
    assert_ok(run(ledger(BASIC_ROW, bom=True), "balances", "{f}"), BASIC_OUT)


def test_crlf_line_endings_are_accepted():
    assert_ok(run(ledger(BASIC_ROW, eol="\r\n"), "balances", "{f}"), BASIC_OUT)


def test_bom_and_crlf_together():
    led = ledger(BASIC_ROW, bom=True, eol="\r\n")
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_last_line_without_newline_is_read():
    led = HEADER + "\n" + BASIC_ROW
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_columns_in_any_order():
    led = ("note,amount,payer,participants,date\n"
           "dinner,10.00,Ann,Ann;Bob,2026-03-01\n")
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_header_only_is_valid_and_empty():
    assert_ok(bal_text(HEADER + "\n"), [])
    code, out, err = settle_text(HEADER + "\n")
    assert (code, err) == (0, "")
    assert lines(out) == ["nothing to settle"]


def test_header_only_json():
    assert settle_json(HEADER + "\n") == (0, {"transfers": []}, "")
    assert bal_json(HEADER + "\n") == (0, {"balances": []}, "")


@pytest.mark.parametrize("junk", ["", "   ", "\t", "# a comment", "   # indented comment"])
def test_ignored_lines_are_skipped(junk):
    led = ledger(junk, BASIC_ROW, junk)
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_hash_not_at_line_start_is_data():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,# not a comment")
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_quoted_note_with_comma_and_doubled_quotes():
    led = ledger('2026-03-01,Ann,10.00,Ann;Bob,"coffee, ""cake"""')
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_quoted_amount_is_accepted():
    led = ledger('2026-03-01,Ann,"10.00",Ann;Bob,')
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_empty_note_is_allowed():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,")
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_line_numbers_count_ignored_lines_and_crlf():
    led = ledger("", "# c", "   ", "2026-03-01,Ann,x,Ann;Bob,", eol="\r\n")
    # header=1, blank=2, comment=3, whitespace=4, bad row=5
    assert_invalid(run(led, "balances", "{f}"), ["line 5: bad-amount"])


def test_line_numbers_for_bom_file():
    led = ledger("", "2026-03-01,Ann,x,Ann;Bob,", bom=True)
    assert_invalid(run(led, "balances", "{f}"), ["line 3: bad-amount"])


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_each_missing_header_column_is_reported(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    led = ",".join(cols) + "\n"
    assert_invalid(run(led, "balances", "{f}"), [f"header: missing {missing}"])


def test_missing_header_reports_first_in_fixed_order_only():
    led = "amount,note\n"  # missing date, payer, participants
    assert_invalid(run(led, "balances", "{f}"), ["header: missing date"])
    led = "date,amount,note\n"  # missing payer and participants
    assert_invalid(run(led, "balances", "{f}"), ["header: missing payer"])
    led = "date,payer,amount\n"  # missing participants and note
    assert_invalid(run(led, "balances", "{f}"), ["header: missing participants"])


def test_missing_header_is_error_for_settle_too():
    led = "date,payer,amount,note\n"
    assert_invalid(run(led, "settle", "{f}"), ["header: missing participants"])


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("date", [
    "2026-1-01", "01/03/2026", "2026-13-01", "2026-00-10", "2026-02-30",
    "2023-02-29", "20260301", "2026-03-32", "not-a-date", "",
])
def test_bad_date(date):
    led = ledger(f"{date},Ann,10.00,Ann;Bob,")
    assert_invalid(run(led, "balances", "{f}"), ["line 2: bad-date"])


@pytest.mark.parametrize("date", ["2024-02-29", "2026-12-31", "2026-01-01"])
def test_valid_dates(date):
    led = ledger(f"{date},Ann,10.00,Ann;Bob,")
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


@pytest.mark.parametrize("payer", ["", "   "])
def test_bad_payer(payer):
    led = ledger(f"2026-03-01,{payer},10.00,Ann;Bob,")
    assert_invalid(run(led, "balances", "{f}"), ["line 2: bad-payer"])


@pytest.mark.parametrize("amount", [
    "0", "0.00", "-1", "-0.50", "+1", '"1,000"', "1e2", "1.005", "abc", "",
    "1.2.3",
])
def test_bad_amount(amount):
    led = ledger(f"2026-03-01,Ann,{amount},Ann;Bob,")
    assert_invalid(run(led, "balances", "{f}"), ["line 2: bad-amount"])


@pytest.mark.parametrize("parts", [
    "", "Ann;;Bob", "Ann:0", "Ann:-1", "Ann:1.5", "Ann:x", "Ann;ann",
    "Ann:2;ANN", "Ann;Bob;bob:3",
])
def test_bad_participants(parts):
    led = ledger(f"2026-03-01,Ann,10.00,{parts},")
    assert_invalid(run(led, "balances", "{f}"), ["line 2: bad-participants"])


@pytest.mark.parametrize("row, reason", [
    ("2026-02-30,,abc,,", "bad-date"),          # date, payer, amount, participants all bad
    ("2026-03-01,,abc,,", "bad-payer"),         # payer before amount and participants
    ("2026-03-01,Ann,0,,", "bad-amount"),       # amount before participants
])
def test_only_first_failing_check_is_reported(row, reason):
    led = ledger(row)
    assert_invalid(run(led, "balances", "{f}"), [f"line 2: {reason}"])


def test_invalid_rows_reported_in_line_order_one_each():
    led = ledger(
        "2026-03-01,Ann,10.00,Ann;Bob,",   # line 2 ok
        "2026-03-01,Ann,x,Ann;Bob,",       # line 3 bad-amount
        "# skip",                          # line 4
        "2026-99-01,Ann,10.00,Ann;Bob,",   # line 5 bad-date
        "2026-03-01,Ann,10.00,Ann:0,",     # line 6 bad-participants
        "2026-03-01,,10.00,Ann,",          # line 7 bad-payer
    )
    assert_invalid(run(led, "balances", "{f}"), [
        "line 3: bad-amount",
        "line 5: bad-date",
        "line 6: bad-participants",
        "line 7: bad-payer",
    ])


@pytest.mark.parametrize("args", [
    ("balances", "{f}"),
    ("settle", "{f}"),
    ("balances", "{f}", "--format", "json"),
    ("settle", "{f}", "--format", "json"),
])
def test_invalid_rows_give_empty_stdout_for_every_command(args):
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-01,Ann,q,Ann;Bob,")
    code, out, err = run(led, *args)
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_invalid_row_outside_range_is_still_an_error():
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,", "2026-06-01,Ann,x,Ann;Bob,")
    assert_invalid(run(led, "balances", "{f}", "--until", "2026-02-01"),
                   ["line 3: bad-amount"])
    assert_invalid(run(led, "settle", "{f}", "--since", "2026-02-01"),
                   ["line 3: bad-amount"])


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_names_are_trimmed():
    led = ledger("2026-03-01, Ann ,10.00, Ann ; Bob ,")
    assert_ok(run(led, "balances", "{f}"), BASIC_OUT)


def test_names_are_case_insensitive_and_spelled_by_first_appearance():
    led = ledger(
        "2026-03-01,ann,10.00,Ann;Bob,",
        "2026-03-02,BOB,10.00,BOB;aNN,",
    )
    # first appearance: 'ann' (payer of row 1); 'Bob' (participant of row 1)
    assert_ok(run(led, "balances", "{f}"), ["ann 0.00", "Bob 0.00"])


def test_first_appearance_prefers_payer_over_participants():
    led = ledger("2026-03-01,bob,10.00,Bob;Ann,")
    assert_ok(run(led, "balances", "{f}"), ["Ann -5.00", "bob +5.00"])


def test_payer_need_not_be_a_participant():
    led = ledger("2026-03-01,Ann,10.00,Bob;Cy,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +10.00", "Bob -5.00", "Cy -5.00"])


def test_payer_may_be_a_participant():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob;Cy,")
    # 1000 cents / 3: base 333 each; leftover goes to Ann (name order).
    assert_ok(run(led, "balances", "{f}"), ["Ann +6.66", "Bob -3.33", "Cy -3.33"])


def test_sorting_is_case_insensitive():
    led = ledger("2026-03-01,b,10.00,a;C;B,")
    assert_ok(run(led, "balances", "{f}"), ["a -3.34", "b +6.67", "C -3.33"])


# ---------------------------------------------------------------------------
# Amounts and splitting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount, cents", [("12", 1200), ("12.5", 1250), ("12.50", 1250)])
def test_amount_spellings_are_equivalent(amount, cents):
    led = ledger(f"2026-03-01,Ann,{amount},Ann;Bob,")
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": cents // 2}


def test_amount_of_one_cent_is_accepted():
    led = ledger("2026-03-01,Ann,0.01,Ann;Bob,")
    code, out, err = run(led, "balances", "{f}")
    assert (code, err) == (0, "")


def test_default_weight_is_one():
    led = ledger("2026-03-01,Ann,30.00,Ann;Bob;Cy,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +20.00", "Bob -10.00", "Cy -10.00"])


def test_explicit_weights_split_proportionally():
    led = ledger("2026-03-01,Ann,40.00,Ann:1;Bob:3,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +30.00", "Bob -30.00"])


def test_leftover_cent_goes_to_name_first_when_remainders_tie():
    led = ledger("2026-03-01,Zed,10.00,Ann;Bob;Cy,")
    assert_ok(run(led, "balances", "{f}"),
              ["Ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"])


def test_leftover_tie_break_ignores_case():
    # 1 cent among four equal shares: ANN is first ignoring case.
    led = ledger("2026-03-01,zed,0.01,Zed;bob;ANN;cy,")
    assert_ok(run(led, "balances", "{f}"),
              ["ANN -0.01", "bob 0.00", "cy 0.00", "zed +0.01"])


def test_leftover_goes_to_largest_remainder_not_largest_weight():
    # 7 cents, A:3 B:2 (W=5): A base 4 rem 1; B base 2 rem 4 -> B gets the cent.
    led = ledger("2026-03-01,X,0.07,A:3;B:2,")
    assert_ok(run(led, "balances", "{f}"), ["A -0.04", "B -0.03", "X +0.07"])


def test_leftover_largest_remainder_with_two_weights():
    # 100 cents, A:2 B:1 (W=3): A 66 rem 2, B 33 rem 1 -> A gets the cent.
    led = ledger("2026-03-01,X,1.00,A:2;B:1,")
    assert_ok(run(led, "balances", "{f}"), ["A -0.67", "B -0.33", "X +1.00"])


def test_exact_split_has_no_leftover():
    led = ledger("2026-03-01,X,0.10,A:1;B:1;C:1;D:2,")
    assert_ok(run(led, "balances", "{f}"),
              ["A -0.02", "B -0.02", "C -0.02", "D -0.04", "X +0.10"])


def test_large_amount_is_exact():
    led = ledger("2026-03-01,Ann,12345678.90,Ann;Bob,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +6172839.45", "Bob -6172839.45"])


def test_balances_sum_to_zero_in_json():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob;Cy,", "2026-03-02,Cy,7.77,Ann:2;Dan,")
    _, data, _ = bal_json(led)
    assert sum(b["cents"] for b in data["balances"]) == 0


def test_zero_balance_prints_without_sign():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-02,Bob,10.00,Ann;Bob,")
    assert_ok(run(led, "balances", "{f}"), ["Ann 0.00", "Bob 0.00"])


def test_balance_signs_and_two_decimals():
    led = ledger("2026-03-01,Ann,0.05,Ann;Bob,", "2026-03-02,Cy,3,Cy;Ann,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    for ln in lines(out):
        name, amt = ln.rsplit(" ", 1)
        assert re.fullmatch(r"0\.00|[+-]\d+\.\d{2}", amt), ln


# ---------------------------------------------------------------------------
# balances command
# ---------------------------------------------------------------------------

TRIP = """date,payer,amount,participants,note
2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner
2026-03-02,Bob,30,Ann;Bob,taxi
2026-03-03,Cy,9.00,Ann;Bob;Cy,"coffee, cake"
"""


def test_balances_example_from_spec_text():
    assert_ok(run(TRIP, "balances", "{f}"), ["Ann +22.00", "Bob -8.00", "Cy -14.00"])


def test_balances_nothing_prints_nothing_with_exit_zero():
    code, out, err = run(TRIP, "balances", "{f}", "--since", "2030-01-01")
    assert (code, out, err) == (0, "", "")


def test_balances_json_shape_and_order():
    code, data, err = bal_json(TRIP)
    assert (code, err) == (0, "")
    assert data == {"balances": [
        {"name": "Ann", "cents": 2200},
        {"name": "Bob", "cents": -800},
        {"name": "Cy", "cents": -1400},
    ]}


def test_balances_json_zero_is_integer_zero():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-02,Bob,10.00,Ann;Bob,")
    _, data, _ = bal_json(led)
    assert data == {"balances": [{"name": "Ann", "cents": 0}, {"name": "Bob", "cents": 0}]}
    assert all(isinstance(b["cents"], int) for b in data["balances"])


# ---------------------------------------------------------------------------
# settle command
# ---------------------------------------------------------------------------

def test_settle_example_from_spec_text():
    assert_ok(run(TRIP, "settle", "{f}"), ["Cy -> Ann 14.00", "Bob -> Ann 8.00"])


def test_settle_nothing_to_settle_text_exact():
    code, out, err = run(ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-02,Bob,10.00,Ann;Bob,"),
                         "settle", "{f}")
    assert (code, err) == (0, "")
    assert out.strip() == "nothing to settle"


def test_settle_json_empty_when_nothing_to_settle():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-02,Bob,10.00,Ann;Bob,")
    assert settle_json(led) == (0, {"transfers": []}, "")


def test_settle_json_shape():
    code, data, err = settle_json(TRIP)
    assert (code, err) == (0, "")
    assert data == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 1400},
        {"from": "Bob", "to": "Ann", "cents": 800},
    ]}


def test_settle_debtor_ties_broken_by_name_ignoring_case():
    # B, C, D all owe 7.50 (most negative tie); B is first by name.
    led = ledger("2026-03-01,A,30.00,A;B;C;D,", "2026-03-02,E,5.00,E;F,")
    assert_ok(run(led, "settle", "{f}"),
              ["B -> A 7.50", "C -> A 7.50", "D -> A 7.50", "F -> E 2.50"])


def test_settle_creditor_ties_broken_by_name():
    # Ann and Eve are both +5.00, Bob is -10.00: Ann is paid first.
    led = ledger("2026-03-01,Ann,5.00,Bob,", "2026-03-02,Eve,5.00,Bob,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +5.00", "Bob -10.00", "Eve +5.00"])
    assert_ok(run(led, "settle", "{f}"), ["Bob -> Ann 5.00", "Bob -> Eve 5.00"])


def test_settle_creditor_ties_broken_by_name():
    # Creditors Ann and Eve both +5; debtor Bob -10. Ann is paid first.
    led = ledger("2026-03-01,Ann,5.00,Ann;Bob,", "2026-03-01,Eve,5.00,Eve;Bob,")
    # Bob owes 2.50 + 2.50 ... compute exactly via reference below instead
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    transfers = lines(out)
    assert transfers[0].startswith("Bob -> Ann ")


def test_settle_pays_smaller_absolute_balance_first():
    # Ann +15, Bob -5, Cy -10. Most negative debtor Cy pays 10 (smaller of 10 and 15).
    led = ledger("2026-03-01,Ann,5.00,Bob,", "2026-03-02,Ann,10.00,Cy,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +15.00", "Bob -5.00", "Cy -10.00"])
    assert_ok(run(led, "settle", "{f}"), ["Cy -> Ann 10.00", "Bob -> Ann 5.00"])


def test_settle_exact_spec_procedure_small_case():
    # Row 1: Ann pays 10.00 for Ann;Bob;Cy -> shares 3.34 (Ann, leftover cent) / 3.33 / 3.33
    # Row 2: Cy pays 5.00 for Ann;Cy -> 2.50 each
    # Balances: Ann +4.16, Bob -3.33, Cy -0.83
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob;Cy,", "2026-03-02,Cy,5.00,Ann;Cy,")
    assert_ok(run(led, "balances", "{f}"), ["Ann +4.16", "Bob -3.33", "Cy -0.83"])
    # Bob (-3.33) pays Ann min(3.33, 4.16); then Cy (-0.83) pays Ann 0.83.
    assert_ok(run(led, "settle", "{f}"), ["Bob -> Ann 3.33", "Cy -> Ann 0.83"])


def test_settle_transfers_are_smaller_of_two_absolute_balances():
    led = ledger("2026-03-01,A,10.00,A;B;C,", "2026-03-01,B,1.00,B,",
                 "2026-03-02,C,5.00,A;C,")
    assert_ok(run(led, "settle", "{f}"), ["B -> A 3.33", "C -> A 0.83"])


def test_settle_multiple_debtors_and_creditors_order():
    led = ledger("2026-03-01,A,30.00,A;B;C;D,", "2026-03-01,E,5.00,E;F,")
    assert_ok(run(led, "settle", "{f}"),
              ["B -> A 7.50", "C -> A 7.50", "D -> A 7.50", "F -> E 2.50"])


def test_settle_text_and_json_agree():
    led = ledger("2026-03-01,A,30.00,A;B;C;D,", "2026-03-01,E,5.00,E;F,")
    _, text_out, _ = run(led, "settle", "{f}")
    _, data, _ = settle_json(led)
    rebuilt = [f"{t['from']} -> {t['to']} {fmt_unsigned(t['cents'])}" for t in data["transfers"]]
    assert lines(text_out) == rebuilt


def test_settle_transfers_clear_every_balance():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob;Cy,", "2026-03-02,Cy,5.00,Ann;Cy,")
    _, bal, _ = bal_json(led)
    _, data, _ = settle_json(led)
    net = {b["name"].lower(): b["cents"] for b in bal["balances"]}
    for t in data["transfers"]:
        net[t["from"].lower()] += t["cents"]
        net[t["to"].lower()] -= t["cents"]
    assert all(v == 0 for v in net.values())


# ---------------------------------------------------------------------------
# Options and usage
# ---------------------------------------------------------------------------

def test_format_json_for_balances_and_settle():
    assert run(TRIP, "balances", "{f}", "--format", "json")[0] == 0
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0 and "transfers" in json.loads(out)


def test_format_text_is_default():
    _, text_out, _ = run(TRIP, "balances", "{f}")
    _, explicit, _ = run(TRIP, "balances", "{f}", "--format", "text")
    assert text_out == explicit
    assert not text_out.lstrip().startswith("{")


def test_options_before_positional_arguments():
    code, out, _ = run(TRIP, "--format", "json", "balances", "{f}")
    assert code == 0 and "balances" in json.loads(out)


def test_options_after_positional_arguments():
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0 and "transfers" in json.loads(out)


def test_since_only():
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,", "2026-02-01,Cy,10.00,Cy;Dan,")
    assert_ok(run(led, "balances", "{f}", "--since", "2026-01-15"),
              ["Cy +5.00", "Dan -5.00"])


def test_until_only():
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,", "2026-03-01,Cy,10.00,Cy;Dan,")
    assert_ok(run(led, "balances", "{f}", "--until", "2026-02-01"),
              ["Ann +5.00", "Bob -5.00"])


def test_since_and_until_are_inclusive():
    led = ledger("2026-01-01,A,10.00,A;B,", "2026-02-01,C,10.00,C;D,",
                 "2026-03-01,E,10.00,E;F,")
    assert_ok(run(led, "balances", "{f}", "--since", "2026-02-01", "--until", "2026-03-01"),
              ["C +5.00", "D -5.00", "E +5.00", "F -5.00"])


def test_range_excludes_people_only_in_other_rows():
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,", "2026-03-01,Zed,10.00,Zed;Yan,")
    assert_ok(run(led, "balances", "{f}", "--until", "2026-02-01"),
              ["Ann +5.00", "Bob -5.00"])


def test_range_with_no_rows():
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,")
    assert_ok(run(led, "balances", "{f}", "--since", "2027-01-01"), [])
    assert_ok(run(led, "settle", "{f}", "--since", "2027-01-01"), ["nothing to settle"])
    assert bal_json(led, "--since", "2027-01-01") == (0, {"balances": []}, "")
    assert settle_json(led, "--since", "2027-01-01") == (0, {"transfers": []}, "")


def test_range_applies_to_settle():
    led = ledger("2026-01-01,Ann,10.00,Ann;Bob,", "2026-03-01,Zed,10.00,Zed;Yan,")
    assert_ok(run(led, "settle", "{f}", "--until", "2026-02-01"), ["Bob -> Ann 5.00"])


@pytest.mark.parametrize("args", [
    ("explode", "{f}"),
    ("balances",),
    ("settle",),
    (),
    ("balances", "{f}", "--bogus"),
    ("balances", "{f}", "--format", "xml"),
    ("balances", "{f}", "--format"),
    ("balances", "{f}", "--since", "2026-02-31"),
    ("balances", "{f}", "--since", "2026/02/01"),
    ("settle", "{f}", "--until", "tomorrow"),
    ("balances", "{f}", "--since"),
])
def test_usage_errors_exit_64_with_empty_stdout(args):
    code, out, err = run(TRIP, *args)
    assert code == 64
    assert out == ""
    assert err.strip() != ""


def test_missing_ledger_file_exits_3():
    path = os.path.join(APP_DIR, "no-such-ledger.csv")
    for cmd in ("balances", "settle"):
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), cmd, path],
                           capture_output=True, text=True, timeout=20)
        assert p.returncode == 3, cmd
        assert p.stdout == ""
        assert p.stderr.strip() == f"error: cannot read {path}"





# ---------------------------------------------------------------------------
# Randomized comparison against the reference model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(40))
def test_random_ledgers_match_reference_model(seed):
    text, rows, since, until = random_case(seed)
    args = []
    if since:
        args += ["--since", since]
    if until:
        args += ["--until", until]
    display, bal = ref_report(rows)
    keys = sorted(bal, key=lambda k: k)  # keys are lowercased names

    expected_bal_lines = [f"{display[k]} {fmt_signed(bal[k])}" for k in keys]
    expected_bal_json = {"balances": [{"name": display[k], "cents": bal[k]} for k in keys]}

    code, out, err = run(text, "balances", "{f}", *args)
    assert (code, err) == (0, ""), (code, err)
    assert lines(out) == expected_bal_lines

    code, out, err = run(text, "balances", "{f}", "--format", "json", *args)
    assert (code, err) == (0, "")
    assert json.loads(out) == expected_bal_json

    transfers = ref_settle(display, bal)
    expected_settle = [f"{d} -> {c} {fmt_unsigned(a)}" for d, c, a in transfers]
    code, out, err = run(text, "settle", "{f}", *args)
    assert (code, err) == (0, "")
    assert lines(out) == (expected_settle if expected_settle else ["nothing to settle"])

    code, out, err = run(text, "settle", "{f}", "--format", "json", *args)
    assert (code, err) == (0, "")
    assert json.loads(out) == {"transfers": [
        {"from": d, "to": c, "cents": a} for d, c, a in transfers]}
