"""Regression suite for splitr, derived from SPEC.md.

Every test drives splitr.py as a black box via helpers.run().
"""
import json
import random

import pytest

from helpers import run, lines

HDR = "date,payer,amount,participants,note"
COLUMNS = ["date", "payer", "amount", "participants", "note"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ledger(*rows, header=HDR, eol="\n"):
    """Build ledger text from a header and raw row strings."""
    return eol.join([header, *rows]) + eol


def row(date="2026-03-01", payer="Ann", amount="10.00", participants="Ann;Bob", note=""):
    return f"{date},{payer},{amount},{participants},{note}"


def parse_balance_lines(text_out):
    """'Ann +12.50' lines -> [(name, signed cents), ...] in output order."""
    pairs = []
    for ln in lines(text_out):
        name, amt = ln.rsplit(" ", 1)
        sign = -1 if amt.startswith("-") else 1
        whole, frac = amt.lstrip("+-").split(".")
        assert len(frac) == 2, f"not two decimals: {ln!r}"
        pairs.append((name, sign * (int(whole) * 100 + int(frac))))
    return pairs


def balances_of(led, *extra):
    """Run `balances` (text), return {name: cents}; asserts success."""
    code, out, err = run(led, "balances", "{f}", *extra)
    assert code == 0, err
    assert err == ""
    return dict(parse_balance_lines(out))


def fmt_cents(c, signed):
    sign = ""
    if signed and c != 0:
        sign = "+" if c > 0 else "-"
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


def assert_int_cents(doc, key="cents"):
    """Every `cents` value in a parsed JSON document must be a real integer (not float/str)."""
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == key:
                    assert type(v) is int, f"{key} is {type(v).__name__}: {v!r}"
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(doc)


# ---------------------------------------------------------------------------
# Independent oracle implementing SPEC.md (used for randomized comparisons)
# ---------------------------------------------------------------------------

def oracle_balances(rows):
    """rows: list of (payer, cents, [(name, weight), ...]) in file order.
    Returns [(display_name, cents), ...] sorted by name ignoring case."""
    display, bal = {}, {}

    def touch(name):
        k = name.lower()
        display.setdefault(k, name)
        bal.setdefault(k, 0)

    for payer, _cents, parts in rows:
        touch(payer)
        for name, _w in parts:
            touch(name)
    for payer, cents, parts in rows:
        bal[payer.lower()] += cents
        total_w = sum(w for _, w in parts)
        base = {n.lower(): cents * w // total_w for n, w in parts}
        leftover = cents - sum(base.values())
        ranked = sorted(parts, key=lambda p: (-((cents * p[1]) % total_w), p[0].lower()))
        for name, _w in ranked[:leftover]:
            base[name.lower()] += 1
        for k, share in base.items():
            bal[k] -= share
    return [(display[k], bal[k]) for k in sorted(bal)]


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


# ---------------------------------------------------------------------------
# Ledger file: encoding, line endings, columns, quoting, ignored lines
# ---------------------------------------------------------------------------

def test_bom_is_accepted():
    raw = ("\ufeff" + ledger(row(payer="Ann", participants="Bob"))).encode("utf-8")
    code, out, err = run(None, "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_crlf_line_endings_accepted():
    led = ledger(row(payer="Ann", participants="Bob"), eol="\r\n")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_crlf_line_numbers_still_counted():
    led = ledger(row(), "2026-13-01,Ann,1,Bob,", eol="\r\n")
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-date"]


def test_columns_may_come_in_any_order():
    led = ("note,participants,amount,payer,date\n"
           "coffee,Ann;Bob,10.00,Ann,2026-03-01\n")
    assert balances_of(led) == {"Ann": 500, "Bob": -500}


def test_quoted_note_with_commas_and_quotes():
    led = ledger('2026-03-01,Ann,10.00,Ann;Bob,"coffee, ""cake"", tea"')
    assert balances_of(led) == {"Ann": 500, "Bob": -500}


def test_note_column_may_be_omitted_from_short_row():
    led = "date,payer,amount,participants,note\n2026-03-01,Ann,10.00,Ann;Bob\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_empty_note_is_valid():
    assert balances_of(ledger(row(note=""))) == {"Ann": 500, "Bob": -500}


def test_blank_and_whitespace_lines_are_ignored():
    led = ("date,payer,amount,participants,note\n"
           "\n"
           "   \n"
           "\t\n"
           "2026-03-01,Ann,10.00,Ann;Bob,\n")
    assert balances_of(led) == {"Ann": 500, "Bob": -500}


def test_comment_lines_are_ignored_even_when_indented():
    led = ("date,payer,amount,participants,note\n"
           "# a comment, with commas, 2026-99-99\n"
           "   # indented comment\n"
           "2026-03-01,Ann,10.00,Ann;Bob,\n")
    assert balances_of(led) == {"Ann": 500, "Bob": -500}


def test_hash_not_at_line_start_is_data():
    led = ledger(row(note="# not a comment"), row(amount="x"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_ignored_lines_count_for_error_line_numbers():
    led = ("date,payer,amount,participants,note\n"
           "# comment\n"
           "\n"
           "   \n"
           "2026-03-01,Ann,abc,Ann;Bob,\n")
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 5: bad-amount"]


def test_header_only_is_valid_and_empty():
    led = HDR + "\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and out == "" and err == ""
    code, out, err = run(led, "settle", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["nothing to settle"]


def test_header_only_json_outputs():
    led = HDR + "\n"
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"balances": []}
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"transfers": []}


def test_fields_are_whitespace_trimmed():
    led = ledger("2026-03-01 ,  Ann  , 10.00 ,  Ann ; Bob:1 ,  ")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_names_with_internal_spaces_are_kept():
    led = ledger(row(payer="Mary Ann", participants="Mary Ann;Bo B"))
    assert balances_of(led) == {"Mary Ann": 500, "Bo B": -500}


def test_unreadable_ledger_exit_3_for_balances():
    code, out, err = run(HDR + "\n", "balances", "/nonexistent/dir/ledger.csv")
    assert code == 3
    assert out == ""
    assert lines(err) == ["error: cannot read /nonexistent/dir/ledger.csv"]


def test_unreadable_ledger_exit_3_for_settle():
    code, out, err = run(HDR + "\n", "settle", "/nonexistent/dir/ledger.csv")
    assert code == 3 and out == ""
    assert lines(err) == ["error: cannot read /nonexistent/dir/ledger.csv"]


def test_ledger_that_is_not_utf8_exit_3():
    raw = (HDR + "\n2026-03-01,Ann,10.00,Ann;Bob,caf\xe9\n").encode("latin-1")
    code, out, err = run(None, "balances", "{f}", raw=raw)
    assert code == 3 and out == ""
    assert lines(err) == ["error: cannot read {f}"]


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", COLUMNS)
def test_header_missing_each_column(missing):
    cols = [c for c in COLUMNS if c != missing]
    code, out, err = run(",".join(cols) + "\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"header: missing {missing}"]


def test_header_missing_reports_first_missing_column_only():
    # 'date' and 'note' are both missing: only 'date' is reported.
    led = "payer,amount,participants\n2026-99-99,Ann,x,Bob\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing date"]


def test_header_missing_reports_only_header_error_even_with_bad_rows():
    led = "date,payer,amount,participants\n2026-99-99,,x,\n"
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing note"]


def test_header_missing_participants_for_settle():
    led = "date,payer,amount,note\n"
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing participants"]


# ---------------------------------------------------------------------------
# Row validation: reasons, precedence, reporting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-13-01", "2026-00-10", "2026-3-1", "2026/03/01",
    "01-03-2026", "2026-03-01T00:00", "20260301", "", "tomorrow",
])
def test_bad_dates_rejected(date):
    code, out, err = run(ledger(row(date=date)), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_leap_day_is_valid():
    assert balances_of(ledger(row(date="2024-02-29"))) == {"Ann": 500, "Bob": -500}


@pytest.mark.parametrize("payer", ["", "   "])
def test_empty_payer_rejected(payer):
    code, out, err = run(ledger(row(payer=payer)), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", [
    "0", "0.00", "-1", "-0.50", "+1", '"1,000"', "1 000", "1e3", "12.345",
    "abc", "", "1.2.3", "12.", "$12", "1_000",
])
def test_bad_amounts_rejected(amount):
    code, out, err = run(ledger(row(amount=amount)), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


@pytest.mark.parametrize("participants", [
    "", "   ", "Ann;", ";Bob", "Ann;;Bob",
    "Ann:0", "Ann:-1", "Ann:1.5", "Ann:x", "Ann:",
    "Bob;bob", "Ann;ANN:2", "Bob:2;BOB",
])
def test_bad_participants_rejected(participants):
    code, out, err = run(ledger(row(participants=participants)), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-participants"]


@pytest.mark.parametrize("amount, cents", [
    ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1), ("100000", 10000000),
])
def test_valid_amount_forms(amount, cents):
    # Zed pays alone for Ann, so Zed's balance equals the amount.
    assert balances_of(ledger(row(payer="Zed", amount=amount, participants="Ann")))["Zed"] == cents


def test_precedence_bad_date_first():
    led = ledger(row(date="x", payer="", amount="x", participants=""))
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and lines(err) == ["line 2: bad-date"]


def test_precedence_bad_payer_before_amount_and_participants():
    led = ledger(row(payer="  ", amount="x", participants=""))
    code, out, err = run(led, "balances", "{f}")
    assert lines(err) == ["line 2: bad-payer"]


def test_precedence_bad_amount_before_participants():
    led = ledger(row(amount="x", participants=""))
    code, out, err = run(led, "balances", "{f}")
    assert lines(err) == ["line 2: bad-amount"]


def test_short_row_missing_amount_is_bad_amount():
    code, out, err = run(ledger("2026-03-01,Ann"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


def test_multiple_invalid_rows_one_line_each_in_order():
    led = ledger(
        row(payer="Ann"),                    # line 2: valid
        row(date="bad", payer=""),           # line 3: bad-date (first failing check)
        row(amount="0"),                     # line 4: bad-amount
        row(participants="Ann;ann"),         # line 5: bad-participants
        row(payer=" "),                      # line 6: bad-payer
        row(),                               # line 7: valid
    )
    code, out, err = run(led, "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [
        "line 3: bad-date",
        "line 4: bad-amount",
        "line 5: bad-participants",
        "line 6: bad-payer",
    ]


def test_invalid_row_in_json_settle_gives_no_stdout():
    led = ledger(row(), "2026-03-01,Ann,nope,Ann;Bob,")
    code, out, err = run(led, "settle", "{f}", "--format", "json")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


# ---------------------------------------------------------------------------
# Names and identity
# ---------------------------------------------------------------------------

def test_names_equal_ignoring_case_are_one_person():
    led = ledger(row(payer="ANN", participants="ann;Bob"),   # ANN +1000, ANN -500, Bob -500
                 row(payer="bob", participants="Ann"))       # bob +1000, ANN -1000
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["ANN -5.00", "Bob +5.00"]


def test_unicode_names_compare_ignoring_case():
    led = ledger(row(payer="Éva", participants="éva;Bob"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Bob -5.00", "Éva +5.00"]


def test_display_spelling_is_first_appearance():
    # 'bob' appears first as payer, later as 'BOB' participant -> shown as 'bob'.
    led = ledger(row(payer="bob", participants="Ann"), row(payer="Ann", participants="BOB"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0
    assert [n for n, _ in parse_balance_lines(out)] == ["Ann", "bob"]


def test_display_spelling_uses_first_participant_spelling():
    # 'Ty' first appears as a participant; the later payer 'ty' must not change the spelling.
    led = ledger(row(payer="Zed", participants="Mo;Ty", amount="3.00"),
                 row(payer="ty", participants="zed", amount="1.00"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert [n for n, _ in parse_balance_lines(out)] == ["Mo", "Ty", "Zed"]


def test_payer_who_is_also_participant_counts_as_both():
    led = ledger(row(payer="ann", participants="Ann;Bob", amount="10.00"))
    assert balances_of(led) == {"ann": 500, "Bob": -500}


def test_payer_need_not_be_a_participant():
    led = ledger(row(payer="Zed", participants="Ann;Bob", amount="10.00"))
    assert balances_of(led) == {"Zed": 1000, "Ann": -500, "Bob": -500}


# ---------------------------------------------------------------------------
# Splitting rule
# ---------------------------------------------------------------------------

def test_weighted_shares_example():
    # 40.00 split 1:3 -> 10.00 and 30.00
    led = ledger(row(payer="Ann", amount="40.00", participants="Ann:1;Bob:3"))
    assert balances_of(led) == {"Ann": 3000, "Bob": -3000}


def test_leftover_cent_goes_to_largest_remainder_not_first_listed():
    # 1.00 split 1:2 -> bases 33 (rem 1) and 66 (rem 2); leftover cent to Bob.
    led = ledger(row(payer="Zed", amount="1.00", participants="Ann;Bob:2"))
    assert balances_of(led) == {"Zed": 100, "Ann": -33, "Bob": -67}


def test_leftover_cent_by_remainder_beats_name_order():
    # 10.00 split Aa:1 and Zz:2 -> bases 333 (rem 1) and 666 (rem 2000 mod 3 = 2).
    # Leftover cent goes to Zz (larger remainder), not Aa (smaller name).
    led = ledger(row(payer="Mid", amount="10.00", participants="Aa;Zz:2"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["Aa -3.33", "Mid +10.00", "Zz -6.67"]


def test_tied_remainders_broken_by_name_ignoring_case():
    # Three equal shares of 10.00; all remainders tie. 'al' sorts before 'Bob' and 'Cy'
    # ignoring case (case-sensitive order would give the cent to 'Bob').
    led = ledger(row(payer="Zed", amount="10.00", participants="Bob;al;Cy"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0
    assert lines(out) == ["al -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_leftover_cent_with_huge_weight_skew():
    # 1.00 split A:1000000 and B:1 -> A base 99 (rem 999901), B base 0 (rem 100).
    # Leftover cent goes to A.
    led = ledger(row(payer="Zed", amount="1.00", participants="A:1000000;B:1"))
    assert balances_of(led) == {"Zed": 100, "A": -100, "B": 0}


def test_zero_balance_prints_without_sign():
    led = ledger(row(payer="Ann", amount="10.00", participants="Ann"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann 0.00"]


def test_signs_and_two_decimals_in_text_balances():
    led = ledger(row(payer="Ann", amount="0.05", participants="Bob"),
                 row(payer="Bob", amount="1234567.89", participants="Cy"))
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +0.05", "Bob +1234567.84", "Cy -1234567.89"]


def test_balances_sum_to_zero_on_a_messy_ledger():
    led = ledger(row(payer="Ann", amount="10.00", participants="Ann;Bob;Cy"),
                 row(payer="Bob", amount="7.77", participants="Dee:3;ann;cy:2"),
                 row(payer="Cy", amount="0.01", participants="Ann;Bob;Cy;Dee"))
    assert sum(balances_of(led).values()) == 0


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------

def test_settle_greedy_three_transfers():
    # Balances: A +10.00, B +5.00, C -8.00, D -7.00
    # C (most negative) pays A (most positive): C->A 8.00, leaving A +2.00.
    # B (+5.00) is now the largest creditor, so D->B 5.00; then D->A 2.00.
    led = ledger(row(payer="A", amount="8.00", participants="C"),
                 row(payer="A", amount="2.00", participants="D"),
                 row(payer="B", amount="5.00", participants="D"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["C -> A 8.00", "D -> B 5.00", "D -> A 2.00"]


def test_settle_debtor_tie_broken_ignoring_case():
    # Zed and amy both owe 5.00; 'amy' sorts first ignoring case.
    led = ledger(row(payer="Carl", amount="10.00", participants="Zed;amy"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["amy -> Carl 5.00", "Zed -> Carl 5.00"]


def test_settle_creditor_tie_broken_ignoring_case():
    # Zed and Amy are each owed 10.00; 'Amy' sorts first ignoring case.
    led = ledger(row(payer="zed", amount="10.00", participants="Carl"),
                 row(payer="Amy", amount="10.00", participants="Dan"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Carl -> Amy 10.00", "Dan -> zed 10.00"]


def test_settle_creditor_tie_where_case_sensitive_order_differs():
    # Creditors 'Zed' (+10) and 'amy' (+10). Ignoring case, 'amy' comes first.
    # Case-sensitively, 'Zed' would come first.
    led = ledger(row(payer="Zed", amount="10.00", participants="Carl"),
                 row(payer="amy", amount="10.00", participants="Dan"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["Carl -> amy 10.00", "Dan -> Zed 10.00"]


def test_settle_debtor_then_creditor_selection_order():
    # Balances: A +10, B +10, C -10, D -10.
    # Debtor C (ties with D, C first) pays creditor A (ties with B, A first); then D pays B.
    led = ledger(row(payer="A", amount="10.00", participants="C"),
                 row(payer="B", amount="10.00", participants="D"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0
    assert lines(out) == ["C -> A 10.00", "D -> B 10.00"]


def test_settle_nothing_when_all_zero():
    led = ledger(row(payer="Ann", amount="10.00", participants="Ann"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["nothing to settle"]


def test_settle_nothing_to_settle_is_exact_text():
    code, out, err = run(ledger(row(payer="Ann", participants="Ann")), "settle", "{f}")
    assert code == 0 and out == "nothing to settle\n"


def test_settle_output_text_is_exact():
    led = ledger(row(payer="A", amount="0.05", participants="B"))
    code, out, err = run(led, "settle", "{f}")
    assert code == 0
    assert out == "B -> A 0.05\n"


def test_settle_transfers_clear_all_balances():
    led = ledger(row(payer="Ann", amount="10.00", participants="Ann;Bob;Cy"),
                 row(payer="Bob", amount="7.77", participants="Dee:3;ann;cy:2"),
                 row(payer="Cy", amount="0.01", participants="Ann;Bob;Cy;Dee"))
    bal = balances_of(led)
    code, out, _ = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    transfers = json.loads(out)["transfers"]
    after = dict(bal)
    for t in transfers:
        assert t["cents"] > 0
        after[t["from"]] += t["cents"]
        after[t["to"]] -= t["cents"]
    assert all(v == 0 for v in after.values())


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def test_balances_json_exact_shape_and_order():
    led = ledger(row(payer="Ann", amount="60.00", participants="Ann;Bob;Cy"),
                 row(payer="Bob", amount="30.00", participants="Ann;Bob"),
                 row(payer="Cy", amount="9.00", participants="Ann;Bob;Cy"))
    code, out, err = run(led, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert doc == {"balances": [
        {"name": "Ann", "cents": 2200}, {"name": "Bob", "cents": -800}, {"name": "Cy", "cents": -1400}]}
    assert_int_cents(doc)


def test_balances_json_zero_is_integer_zero():
    led = ledger(row(payer="Ann", amount="10.00", participants="Ann"))
    code, out, _ = run(led, "balances", "{f}", "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc == {"balances": [{"name": "Ann", "cents": 0}]}
    assert_int_cents(doc)


def test_settle_json_exact_shape():
    led = ledger(row(payer="A", amount="8.00", participants="C"),
                 row(payer="A", amount="2.00", participants="D"),
                 row(payer="B", amount="5.00", participants="D"))
    code, out, err = run(led, "settle", "{f}", "--format", "json")
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert doc == {"transfers": [
        {"from": "C", "to": "A", "cents": 800},
        {"from": "D", "to": "B", "cents": 500},
        {"from": "D", "to": "A", "cents": 200},
    ]}
    assert_int_cents(doc)


def test_json_balances_match_text_balances_in_order():
    led = ledger(row(payer="zed", amount="10.00", participants="al;Bob;Cy"))
    _, text_out, _ = run(led, "balances", "{f}")
    _, json_out, _ = run(led, "balances", "{f}", "--format", "json")
    as_json = [(b["name"], b["cents"]) for b in json.loads(json_out)["balances"]]
    assert as_json == parse_balance_lines(text_out)


def test_format_text_is_default_and_explicit_equivalent():
    led = ledger(row())
    assert run(led, "balances", "{f}")[1] == run(led, "balances", "{f}", "--format", "text")[1]
    assert run(led, "settle", "{f}")[1] == run(led, "settle", "{f}", "--format", "text")[1]


# ---------------------------------------------------------------------------
# Options: --since / --until, option placement
# ---------------------------------------------------------------------------

RANGE = ledger(
    row(date="2026-02-28", payer="Early", amount="10.00", participants="Old"),
    row(date="2026-03-01", payer="Ann", amount="10.00", participants="Bob"),
    row(date="2026-03-15", payer="Cy", amount="6.00", participants="Bob"),
    row(date="2026-03-31", payer="Late", amount="4.00", participants="Ann"),
    row(date="2026-04-01", payer="After", amount="2.00", participants="Ann"),
)


def test_since_is_inclusive():
    # 03-01 Ann pays 10 for Bob; 03-15 Cy pays 6 for Bob; 03-31 Late pays 4 for Ann;
    # 04-01 After pays 2 for Ann (no --until, so it is in range).
    bal = balances_of(RANGE, "--since", "2026-03-01")
    assert bal == {"Ann": 400, "After": 200, "Bob": -1600, "Cy": 600, "Late": 400}


def test_until_is_inclusive():
    bal = balances_of(RANGE, "--until", "2026-03-15")
    # 02-28 Early +1000 / Old -1000 ; 03-01 Ann +1000 / Bob -1000 ; 03-15 Cy +600 / Bob -600
    assert bal == {"Early": 1000, "Old": -1000, "Ann": 1000, "Bob": -1600, "Cy": 600}


def test_since_and_until_together_inclusive_both_ends():
    bal = balances_of(RANGE, "--since", "2026-03-01", "--until", "2026-03-31")
    assert bal == {"Ann": 600, "Bob": -1600, "Cy": 600, "Late": 400}


def test_same_day_range_includes_that_day_only():
    bal = balances_of(RANGE, "--since", "2026-03-15", "--until", "2026-03-15")
    assert bal == {"Cy": 600, "Bob": -600}


def test_people_only_in_excluded_rows_are_not_printed():
    code, out, _ = run(RANGE, "balances", "{f}", "--since", "2026-03-01")
    names = [n for n, _ in parse_balance_lines(out)]
    assert "Early" not in names and "Old" not in names


def test_range_with_no_rows_prints_nothing_and_settle_says_nothing():
    code, out, err = run(RANGE, "balances", "{f}", "--since", "2030-01-01")
    assert code == 0 and out == "" and err == ""
    code, out, err = run(RANGE, "settle", "{f}", "--since", "2030-01-01")
    assert code == 0 and lines(out) == ["nothing to settle"]


def test_invalid_row_outside_range_still_fails_validation():
    led = ledger(row(date="2026-01-01", amount="oops"), row(date="2026-03-01"))
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


def test_invalid_date_outside_range_still_fails_validation():
    led = ledger(row(date="2026-03-01"), row(date="2026-99-01"))
    code, out, err = run(led, "settle", "{f}", "--until", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-date"]


def test_range_applies_to_settle():
    code, out, _ = run(RANGE, "settle", "{f}", "--since", "2026-03-15", "--until", "2026-03-15",
                       "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc == {"transfers": [{"from": "Bob", "to": "Cy", "cents": 600}]}
    assert_int_cents(doc)


def test_range_applies_to_json_balances():
    code, out, _ = run(RANGE, "balances", "{f}", "--until", "2026-02-28", "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc == {"balances": [{"name": "Early", "cents": 1000}, {"name": "Old", "cents": -1000}]}
    assert_int_cents(doc)


@pytest.mark.parametrize("args", [
    ["balances", "{f}", "--format", "json"],
    ["--format", "json", "balances", "{f}"],
    ["balances", "--format", "json", "{f}"],
    ["--format=json", "balances", "{f}"],
    ["balances", "{f}", "--format=json"],
])
def test_options_may_appear_before_or_after_positionals(args):
    code, out, err = run(ledger(row()), *args)
    assert code == 0 and err == ""
    assert json.loads(out) == {"balances": [{"name": "Ann", "cents": 500}, {"name": "Bob", "cents": -500}]}


def test_options_before_positionals_with_settle_and_range():
    # In range: 03-01 Ann pays 10 for Bob; 03-15 Cy pays 6 for Bob.
    # Bob (-16.00) pays Ann (+10.00) first, then Cy (+6.00).
    code, out, _ = run(RANGE, "--until", "2026-03-15", "settle", "--since", "2026-03-01", "{f}")
    assert code == 0
    assert lines(out) == ["Bob -> Ann 10.00", "Bob -> Cy 6.00"]


# ---------------------------------------------------------------------------
# Usage errors -> exit 64
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["explode", "{f}"],                        # unknown command
    [],                                        # no command at all
    ["balances"],                              # missing ledger argument
    ["settle"],                                # missing ledger argument
    ["balances", "{f}", "--format", "xml"],    # bad format value
    ["balances", "{f}", "--format"],           # option missing its value
    ["balances", "{f}", "--bogus"],            # unknown option
    ["balances", "{f}", "--verbose"],
    ["balances", "{f}", "--quiet"],
    ["balances", "{f}", "--currency", "EUR"],
    ["balances", "{f}", "--csv"],
    ["balances", "{f}", "--help"],
    ["balances", "{f}", "-v"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "2026-3-1"],
    ["balances", "{f}", "--since", "yesterday"],
    ["balances", "{f}", "--until", "03/01/2026"],
    ["settle", "{f}", "--until", "2026-13-01"],
    ["balances", "{f}", "--since"],            # option missing its value
    ["balances", "{f}", "--until", ""],
])
def test_usage_errors_exit_64_with_empty_stdout(args):
    code, out, err = run(ledger(row()), *args)
    assert code == 64
    assert out == ""


def test_usage_error_message_goes_to_stderr():
    code, out, err = run(ledger(row()), "explode", "{f}")
    assert code == 64 and out == ""
    assert err.strip() != ""


# ---------------------------------------------------------------------------
# Randomized comparison against the independent oracle
# ---------------------------------------------------------------------------

PERSONS = ["Ann", "Bob", "Cy", "Dee", "Éva", "zed", "Al", "Mo"]
WEIGHTS = [1, 1, 1, 2, 3, 7, 100]


def _variant(rng, name):
    return rng.choice([name, name.lower(), name.upper(), name.swapcase()])


def _random_ledger(rng, n_rows):
    rows, body = [], []
    for _ in range(n_rows):
        payer = _variant(rng, rng.choice(PERSONS))
        chosen = rng.sample(PERSONS, rng.randint(1, 4))
        parts = [(_variant(rng, p), rng.choice(WEIGHTS)) for p in chosen]
        cents = rng.randint(1, 500000)
        amount = f"{cents // 100}.{cents % 100:02d}"
        if cents % 100 == 0 and rng.random() < 0.5:
            amount = str(cents // 100)
        pcell = ";".join(n if w == 1 else f"{n}:{w}" for n, w in parts)
        body.append(f"2026-03-01,{payer},{amount},{pcell},x")
        rows.append((payer, cents, parts))
    return ledger(*body), rows


@pytest.mark.parametrize("seed", range(25))
def test_random_ledgers_match_oracle(seed):
    rng = random.Random(seed)
    led, rows = _random_ledger(rng, rng.randint(1, 15))
    expected_bal = oracle_balances(rows)
    assert sum(c for _, c in expected_bal) == 0

    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == [f"{n} {fmt_cents(c, True)}" for n, c in expected_bal]

    code, out, err = run(led, "balances", "{f}", "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc == {"balances": [{"name": n, "cents": c} for n, c in expected_bal]}
    assert_int_cents(doc)

    expected_tr = oracle_settle(expected_bal)
    code, out, err = run(led, "settle", "{f}")
    assert code == 0 and err == ""
    if expected_tr:
        assert lines(out) == [f"{d} -> {c} {fmt_cents(x, False)}" for d, c, x in expected_tr]
    else:
        assert lines(out) == ["nothing to settle"]

    code, out, err = run(led, "settle", "{f}", "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc == {"transfers": [{"from": d, "to": c, "cents": x} for d, c, x in expected_tr]}
    assert_int_cents(doc)
