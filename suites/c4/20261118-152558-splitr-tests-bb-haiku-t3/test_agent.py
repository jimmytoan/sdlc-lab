"""Black-box regression suite for splitr, written from SPEC.md.

Every test drives splitr.py as a subprocess via tests/helpers.run.
Expectations come from the specification (and from a small reference model
of the spec used for randomised comparisons), not from the implementation.
"""
import datetime as dt
import json
import random

import pytest

from helpers import run, lines

HEADER = "date,payer,amount,participants,note"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ledger(*rows, header=HEADER):
    return header + "\n" + "".join(r + "\n" for r in rows)


def fmt_signed(c):
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    a = abs(c)
    return f"{sign}{a // 100}.{a % 100:02d}"


def fmt_unsigned(c):
    a = abs(c)
    return f"{a // 100}.{a % 100:02d}"


def _key(name):
    return name.strip().casefold()


def ref_split(cents, parts):
    """parts: list of (name, weight). Returns {casefold_key: share_cents}."""
    total_w = sum(w for _, w in parts)
    shares = {}
    rem = []
    for name, w in parts:
        shares[_key(name)] = cents * w // total_w
        rem.append((-((cents * w) % total_w), _key(name)))
    leftover = cents - sum(shares.values())
    rem.sort()
    for _, k in rem[:leftover]:
        shares[k] += 1
    return shares


def ref_model(rows, since=None, until=None):
    """Reference model of the spec.

    rows: list of dicts {date, payer, cents, parts: [(name, weight)]} covering
    the WHOLE file (display spelling comes from first appearance in the file).
    Returns (balances, transfers): balances is [(display, cents)] sorted by
    name ignoring case; transfers is [(from_display, to_display, cents)].
    """
    display = {}
    for r in rows:
        for nm in [r["payer"]] + [p for p, _ in r["parts"]]:
            display.setdefault(_key(nm), nm)

    bal = {}
    for r in rows:
        if since and r["date"] < since:
            continue
        if until and r["date"] > until:
            continue
        bal[_key(r["payer"])] = bal.get(_key(r["payer"]), 0) + r["cents"]
        for p, _ in r["parts"]:
            bal.setdefault(_key(p), 0)
        for k, s in ref_split(r["cents"], r["parts"]).items():
            bal[k] -= s

    balances = [(display[k], v) for k, v in
                sorted(bal.items(), key=lambda kv: kv[0])]

    work = dict(bal)
    transfers = []
    while any(v != 0 for v in work.values()):
        debtor = min(work, key=lambda k: (work[k], k))
        creditor = min(work, key=lambda k: (-work[k], k))
        amt = min(-work[debtor], work[creditor])
        transfers.append((display[debtor], display[creditor], amt))
        work[debtor] += amt
        work[creditor] -= amt
    return balances, transfers


def run_ok(led, *args):
    code, out, err = run(led, *args)
    assert code == 0, (code, out, err)
    assert err == ""
    return out


# ---------------------------------------------------------------------------
# Ledger file format
# ---------------------------------------------------------------------------

def test_bom_is_ignored():
    raw = ("\ufeff" + ledger("2026-03-01,Ann,10,Ann;Bob,")).encode("utf-8")
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_crlf_line_endings():
    text = HEADER + "\r\n2026-03-01,Ann,10,Ann;Bob,\r\n2026-03-02,Bob,4,Bob,\r\n"
    code, out, err = run("", "balances", "{f}", raw=text.encode())
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_crlf_error_line_numbers_are_physical():
    text = HEADER + "\r\n2026-03-01,Ann,10,Ann;Bob,\r\n2026-13-01,Ann,1,Ann,\r\n"
    code, out, err = run("", "balances", "{f}", raw=text.encode())
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-date"]


def test_columns_in_any_order():
    led = "note,amount,participants,payer,date\nlunch,10,Ann;Bob,Ann,2026-03-01\n"
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


def test_quoted_note_with_comma_and_escaped_quotes():
    led = ledger('2026-03-01,Ann,10,Ann;Bob,"coffee, cake, ""big"""')
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


def test_quoted_fields_accepted_for_all_columns():
    led = ledger('"2026-03-01","Ann","10","Ann;Bob",""')
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


def test_empty_note_is_valid():
    led = ledger("2026-03-01,Ann,10,Ann;Bob,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


def test_hash_inside_row_is_not_a_comment():
    led = ledger("2026-03-01,Ann,10,Ann;Bob,#not a comment")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


def test_blank_whitespace_and_comment_lines_are_ignored():
    led = (HEADER + "\n"
           "\n"
           "   \t \n"
           "# a comment\n"
           "   # indented comment\n"
           "2026-03-01,Ann,10,Ann;Bob,\n")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


def test_ignored_lines_still_count_for_numbering():
    led = (HEADER + "\n"                       # line 1
           "# comment\n"                       # line 2
           "\n"                                # line 3
           "   \n"                             # line 4
           "2026-03-01,Ann,abc,Ann,\n")        # line 5
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 5: bad-amount"]


def test_names_are_trimmed_and_case_insensitive():
    led = ledger("2026-03-01,  Ann ,10, ann ; BOB ,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "BOB -5.00"]


def test_display_name_is_first_appearance_in_file():
    led = ledger("2026-03-01,bob,10,Ann;BOB,",
                 "2026-03-02,Ann,4,ANN;Bob,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann -3.00", "bob +3.00"]


def test_first_appearance_order_payer_before_participants():
    led = ledger("2026-03-01,zed,10,Zed;Ann,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann -5.00", "zed +5.00"]


def test_payer_need_not_be_participant():
    led = ledger("2026-03-01,Ann,9,Bob;Cy,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann +9.00", "Bob -4.50", "Cy -4.50"]


def test_header_only_is_valid_and_empty():
    led = HEADER + "\n"
    assert run_ok(led, "balances", "{f}") == ""
    assert lines(run_ok(led, "settle", "{f}")) == ["nothing to settle"]


def test_header_only_json():
    led = HEADER + "\n"
    assert json.loads(run_ok(led, "balances", "{f}", "--format", "json")) == {"balances": []}
    assert json.loads(run_ok(led, "settle", "{f}", "--format", "json")) == {"transfers": []}


def test_only_comments_after_header_is_valid():
    led = HEADER + "\n# nothing here\n\n"
    assert run_ok(led, "balances", "{f}") == ""


# ---------------------------------------------------------------------------
# Row validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-02-29", "2026-13-01", "2026-00-10", "2026-04-31",
    "2026-1-5", "26-01-05", "2026/01/05", "05.01.2026", "2026-01-05T00:00",
    "", "tomorrow", "2026-01-32",
])
def test_bad_date(date):
    code, out, err = run(ledger(f"{date},Ann,10,Ann;Bob,"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_leap_day_is_valid():
    led = ledger("2028-02-29,Ann,10,Ann;Bob,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +5.00", "Bob -5.00"]


@pytest.mark.parametrize("payer", ["", "   ", "\t"])
def test_bad_payer_empty_after_trim(payer):
    code, out, err = run(ledger(f"2026-03-01,{payer},10,Ann;Bob,"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", [
    "0", "0.00", "-1", "-0.50", "+1", "1,000", "1e3", "1E2", "1.234",
    "", "abc", "1.2.3", "1 000", "$5", "NaN", "inf",
])
def test_bad_amount(amount):
    code, out, err = run(ledger(f'2026-03-01,Ann,"{amount}",Ann;Bob,'), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


@pytest.mark.parametrize("amount,money", [
    ("12", "12.00"), ("12.5", "12.50"), ("12.50", "12.50"), ("0.01", "0.01"),
])
def test_valid_amount_forms(amount, money):
    # Bob pays, Ann owes the whole amount
    out = lines(run_ok(ledger(f"2026-03-01,Bob,{amount},Ann,"), "balances", "{f}"))
    assert out == [f"Ann -{money}", f"Bob +{money}"]


@pytest.mark.parametrize("parts", [
    "",
    "Ann;",
    ";Ann",
    "Ann;;Bob",
    "Ann:0",
    "Ann:-1",
    "Ann:1.5",
    "Ann:x",
    "Ann:",
    "Ann;Ann",
    "Ann;ann",
    "Bob;Ann:2;BOB",
    "  ;  ",
])
def test_bad_participants(parts):
    code, out, err = run(ledger(f"2026-03-01,Ann,10,{parts},"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-participants"]


def test_weighted_participants_are_valid():
    led = ledger("2026-03-01,Ann,40,Ann:1;Bob:3,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +30.00", "Bob -30.00"]


def test_one_reason_per_row_first_failing_check_wins():
    # bad date, bad payer, bad amount and bad participants all at once
    code, out, err = run(ledger("2026-02-30,,abc,,"), "balances", "{f}")
    assert code == 2 and lines(err) == ["line 2: bad-date"]


def test_bad_payer_reported_before_bad_amount():
    code, out, err = run(ledger("2026-03-01,  ,abc,Ann;Bob,"), "balances", "{f}")
    assert lines(err) == ["line 2: bad-payer"]


def test_bad_amount_reported_before_bad_participants():
    code, out, err = run(ledger("2026-03-01,Ann,0,Ann;;Bob,"), "balances", "{f}")
    assert lines(err) == ["line 2: bad-amount"]


def test_multiple_invalid_rows_all_reported_in_line_order():
    led = ledger("2026-03-01,Ann,10,Ann;Bob,",   # line 2 valid
                 "2026-03-02,Ann,abc,Ann,",      # line 3
                 "2026-03-03,Ann,10,,",          # line 4
                 "2026-99-03,Ann,10,Ann,",       # line 5
                 "2026-03-05,,10,Ann,")          # line 6
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [
        "line 3: bad-amount",
        "line 4: bad-participants",
        "line 5: bad-date",
        "line 6: bad-payer",
    ]


@pytest.mark.parametrize("args", [
    ("balances", "{f}"),
    ("settle", "{f}"),
    ("balances", "{f}", "--format", "json"),
    ("settle", "{f}", "--format", "json"),
])
def test_invalid_row_means_no_stdout(args):
    led = ledger("2026-03-01,Ann,10,Ann;Bob,", "2026-03-02,Ann,x,Ann,")
    code, out, err = run(led, *args)
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_invalid_row_outside_range_still_fails():
    led = ledger("2026-03-01,Ann,10,Ann;Bob,", "2026-05-01,Ann,x,Ann,")
    code, out, err = run(led, "balances", "{f}",
                         "--since", "2026-03-01", "--until", "2026-03-31")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_header_missing_column(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    led = ",".join(cols) + "\n" + ",".join(["x"] * len(cols)) + "\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"header: missing {missing}"]


def test_header_missing_reports_first_in_spec_order():
    code, out, err = run("payer,note\n2026-03-01,x\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing date"]


def test_header_missing_amount_and_participants_reports_amount():
    code, out, err = run("date,payer,note\n2026-03-01,Ann,\n", "balances", "{f}")
    assert code == 2
    assert lines(err) == ["header: missing amount"]


def test_header_missing_column_suppresses_row_errors():
    code, out, err = run("date,payer,amount,note\n2026-99-01,,abc,\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing participants"]


def test_header_missing_applies_to_settle_too():
    code, out, err = run("date,payer,amount,participants\n", "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing note"]


# ---------------------------------------------------------------------------
# Unreadable file / usage errors
# ---------------------------------------------------------------------------

def test_missing_file_is_exit_3(tmp_path):
    code, out, err = run("", "balances", str(tmp_path / "nope.csv"))
    assert code == 3 and out == ""
    assert err.startswith("error: cannot read ")


def test_missing_file_settle_is_exit_3(tmp_path):
    code, out, err = run("", "settle", str(tmp_path / "nope.csv"))
    assert code == 3 and out == ""
    assert err.startswith("error: cannot read ")


@pytest.mark.parametrize("args", [
    [],
    ["explode", "{f}"],
    ["balances"],
    ["settle"],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "03/01/2026"],
    ["balances", "{f}", "--until", "2026-13-01"],
    ["balances", "{f}", "--since"],
    ["balances", "{f}", "--until", "nope"],
])
def test_usage_errors_exit_64(args):
    code, out, err = run(ledger("2026-03-01,Ann,10,Ann;Bob,"), *args)
    assert code == 64
    assert out == ""


# ---------------------------------------------------------------------------
# Splitting rule
# ---------------------------------------------------------------------------

def test_even_split_exact():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob;Cy;Dee,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann +7.50", "Bob -2.50", "Cy -2.50", "Dee -2.50"]


def test_leftover_cent_tie_broken_by_name_ignoring_case():
    # 1000 cents / 3 -> 333 each, one leftover cent, all remainders equal:
    # the extra cent goes to the name that sorts first ignoring case ("Ann").
    led = ledger("2026-03-01,Zed,10.00,bob;Ann;CY,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann -3.34", "bob -3.33", "CY -3.33", "Zed +10.00"]


def test_leftover_goes_to_largest_remainder_not_alphabetical():
    # 1 cent, weights Ann:1 Bob:2 (W=3): remainders 1 and 2 -> Bob gets the cent
    led = ledger("2026-03-01,Zed,0.01,Ann:1;Bob:2,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann 0.00", "Bob -0.01", "Zed +0.01"]


def test_leftover_with_weights_and_tie():
    # 7 cents among Ann;Bob;Cy: base 2 each, leftover 1 -> tie -> Ann
    led = ledger("2026-03-01,Zed,0.07,Ann;Bob;Cy,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann -0.03", "Bob -0.02", "Cy -0.02", "Zed +0.07"]


def test_zero_balance_prints_without_sign():
    led = ledger("2026-03-01,Ann,10,Ann,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann 0.00"]


def test_zero_balance_person_listed_but_not_settled():
    led = ledger("2026-03-01,Ann,10,Ann;Bob,",
                 "2026-03-02,Cy,10,Cy,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann +5.00", "Bob -5.00", "Cy 0.00"]
    assert lines(run_ok(led, "settle", "{f}")) == ["Bob -> Ann 5.00"]


def test_balances_sum_to_zero_in_json():
    led = ledger("2026-03-01,Ann,10.01,Ann;Bob;Cy,",
                 "2026-03-02,Dee,7.77,dee;ann:3;Bob:2,")
    data = json.loads(run_ok(led, "balances", "{f}", "--format", "json"))
    assert sum(b["cents"] for b in data["balances"]) == 0


def test_json_balances_shape_and_order():
    led = ledger("2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner",
                 "2026-03-02,Bob,30,Ann;Bob,taxi",
                 '2026-03-03,Cy,9.00,Ann;Bob;Cy,"coffee, cake"')
    assert json.loads(run_ok(led, "balances", "{f}", "--format", "json")) == {"balances": [
        {"name": "Ann", "cents": 2200}, {"name": "Bob", "cents": -800},
        {"name": "Cy", "cents": -1400}]}


def test_balances_sorted_ignoring_case():
    led = ledger("2026-03-01,bob,2,Zed;adam;Cy,")
    names = [l.split(" ")[0] for l in lines(run_ok(led, "balances", "{f}"))]
    assert names == ["adam", "bob", "Cy", "Zed"]


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------

def test_settle_most_negative_debtor_first():
    # Ann +10, Bob -4, Cy -6 -> Cy pays first
    led = ledger("2026-03-01,Ann,4,Bob,",
                 "2026-03-02,Ann,6,Cy,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann +10.00", "Bob -4.00", "Cy -6.00"]
    assert lines(run_ok(led, "settle", "{f}")) == [
        "Cy -> Ann 6.00", "Bob -> Ann 4.00"]


def test_settle_creditor_is_most_positive():
    # Ann +2, Bob +8 (creditors); Zed -10 -> Zed pays Bob first, then Ann
    led = ledger("2026-03-01,Bob,8,Zed,",
                 "2026-03-02,Ann,2,Zed,")
    assert lines(run_ok(led, "settle", "{f}")) == [
        "Zed -> Bob 8.00", "Zed -> Ann 2.00"]


def test_settle_exact_known_sequence():
    # Ann +7, Bob -5, Cy -2 -> Bob first (most negative)
    led = ledger("2026-03-01,Ann,10,Ann;Bob,",
                 "2026-03-02,Ann,2,Cy,")
    assert lines(run_ok(led, "balances", "{f}")) == ["Ann +7.00", "Bob -5.00", "Cy -2.00"]
    assert lines(run_ok(led, "settle", "{f}")) == [
        "Bob -> Ann 5.00", "Cy -> Ann 2.00"]


def test_settle_debtor_tie_broken_by_name_ignoring_case():
    # Bob -5 and Cy -5: ascending ignoring case puts "bob" before "Cy"
    # (a case-sensitive sort would put "Cy" first)
    led = ledger("2026-03-01,Ann,10,bob;Cy,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann +10.00", "bob -5.00", "Cy -5.00"]
    assert lines(run_ok(led, "settle", "{f}")) == [
        "bob -> Ann 5.00", "Cy -> Ann 5.00"]


def test_settle_creditor_tie_broken_by_name_not_first_appearance():
    # Bob and Ann both +5; Bob appears first in the file, Ann must be paid first
    led = ledger("2026-03-01,Bob,5,Zed,",
                 "2026-03-02,Ann,5,Zed,")
    assert lines(run_ok(led, "balances", "{f}")) == [
        "Ann +5.00", "Bob +5.00", "Zed -10.00"]
    assert lines(run_ok(led, "settle", "{f}")) == [
        "Zed -> Ann 5.00", "Zed -> Bob 5.00"]


def test_settle_nothing_to_settle_text():
    led = ledger("2026-03-01,Ann,10,Ann,")
    assert run_ok(led, "settle", "{f}") == "nothing to settle\n"


def test_settle_amount_is_unsigned_two_decimals():
    led = ledger("2026-03-01,Ann,1,Ann;Bob,")
    assert lines(run_ok(led, "settle", "{f}")) == ["Bob -> Ann 0.50"]


def test_settle_json_shape():
    led = ledger("2026-03-01,Ann,10,Ann;Bob,")
    assert json.loads(run_ok(led, "settle", "{f}", "--format", "json")) == {
        "transfers": [{"from": "Bob", "to": "Ann", "cents": 500}]}


def test_settle_json_nothing_to_settle():
    led = ledger("2026-03-01,Ann,10,Ann,")
    assert json.loads(run_ok(led, "settle", "{f}", "--format", "json")) == {"transfers": []}


def test_settle_transfers_clear_all_balances():
    led = ledger("2026-03-01,Ann,10.01,Ann;Bob;Cy,",
                 "2026-03-02,Dee,7.77,dee;ann:3;Bob:2,",
                 "2026-03-03,Cy,100,Ann;Dee;Eve:4,")
    bal = {d["name"].casefold(): d["cents"]
           for d in json.loads(run_ok(led, "balances", "{f}", "--format", "json"))["balances"]}
    transfers = json.loads(run_ok(led, "settle", "{f}", "--format", "json"))["transfers"]
    assert transfers
    for t in transfers:
        assert t["cents"] > 0
        bal[t["from"].casefold()] += t["cents"]
        bal[t["to"].casefold()] -= t["cents"]
    assert all(v == 0 for v in bal.values())


# ---------------------------------------------------------------------------
# Options: --since / --until / --format
# ---------------------------------------------------------------------------

RANGE_LEDGER = ledger(
    "2026-03-01,Ann,10,Ann;Bob,",
    "2026-03-15,Bob,10,Bob;Cy,",
    "2026-03-31,Cy,10,Cy;Dee,",
    "2026-04-01,Dee,10,Dee;Eve,",
)

RANGE_ROWS = [
    {"date": "2026-03-01", "payer": "Ann", "cents": 1000, "parts": [("Ann", 1), ("Bob", 1)]},
    {"date": "2026-03-15", "payer": "Bob", "cents": 1000, "parts": [("Bob", 1), ("Cy", 1)]},
    {"date": "2026-03-31", "payer": "Cy", "cents": 1000, "parts": [("Cy", 1), ("Dee", 1)]},
    {"date": "2026-04-01", "payer": "Dee", "cents": 1000, "parts": [("Dee", 1), ("Eve", 1)]},
]


def _expected_text(rows, since=None, until=None):
    balances, _ = ref_model(rows, since, until)
    return [f"{n} {fmt_signed(c)}" for n, c in balances]


def test_since_is_inclusive():
    out = lines(run_ok(RANGE_LEDGER, "balances", "{f}", "--since", "2026-03-15"))
    assert out == _expected_text(RANGE_ROWS, since="2026-03-15")
    assert "Ann" not in [l.split(" ")[0] for l in out]


def test_until_is_inclusive():
    out = lines(run_ok(RANGE_LEDGER, "balances", "{f}", "--until", "2026-03-15"))
    assert out == _expected_text(RANGE_ROWS, until="2026-03-15")
    assert "Eve" not in [l.split(" ")[0] for l in out]


def test_since_and_until_together_inclusive():
    out = lines(run_ok(RANGE_LEDGER, "balances", "{f}",
                       "--since", "2026-03-15", "--until", "2026-03-31"))
    assert out == _expected_text(RANGE_ROWS, since="2026-03-15", until="2026-03-31")
    names = [l.split(" ")[0] for l in out]
    assert "Ann" not in names and "Eve" not in names


def test_people_only_in_excluded_rows_not_printed():
    out = lines(run_ok(RANGE_LEDGER, "balances", "{f}", "--since", "2026-03-31"))
    names = [l.split(" ")[0] for l in out]
    assert "Ann" not in names and "Bob" not in names
    assert names == ["Cy", "Dee", "Eve"]


def test_settle_respects_range():
    out = lines(run_ok(RANGE_LEDGER, "settle", "{f}", "--since", "2026-03-31"))
    _, transfers = ref_model(RANGE_ROWS, since="2026-03-31")
    assert out == [f"{a} -> {b} {fmt_unsigned(c)}" for a, b, c in transfers]


def test_range_with_no_rows_gives_empty_balances_and_nothing_to_settle():
    assert run_ok(RANGE_LEDGER, "balances", "{f}", "--since", "2027-01-01") == ""
    assert lines(run_ok(RANGE_LEDGER, "settle", "{f}", "--since", "2027-01-01")) == [
        "nothing to settle"]


def test_since_after_until_gives_no_rows():
    assert run_ok(RANGE_LEDGER, "balances", "{f}",
                  "--since", "2026-03-31", "--until", "2026-03-01") == ""


def test_options_may_come_before_positional_arguments():
    before = run_ok(RANGE_LEDGER, "balances", "--since", "2026-03-15", "{f}")
    after = run_ok(RANGE_LEDGER, "balances", "{f}", "--since", "2026-03-15")
    assert before == after != ""


def test_format_option_before_or_after_positional():
    before = run_ok(RANGE_LEDGER, "settle", "--format", "json", "{f}")
    after = run_ok(RANGE_LEDGER, "settle", "{f}", "--format", "json")
    assert before == after
    json.loads(before)


def test_format_text_is_default():
    assert run_ok(RANGE_LEDGER, "balances", "{f}") == \
        run_ok(RANGE_LEDGER, "balances", "{f}", "--format", "text")


def test_json_output_is_single_document_on_stdout():
    code, out, err = run(RANGE_LEDGER, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    json.loads(out)
    assert out.count("\n") <= 1


# ---------------------------------------------------------------------------
# Randomised comparison against the reference model of the spec
# ---------------------------------------------------------------------------

NAME_POOL = ["Ann", " ann ", "BOB", "bob", "Cy", "cY", "Dee", "eve", "Fay ", "gus"]


def _random_ledger(rng, n_rows):
    rows, text_rows = [], []
    distinct = list({_key(n): n for n in NAME_POOL}.values())
    for _ in range(n_rows):
        date = (dt.date(2026, 1, 1) + dt.timedelta(days=rng.randint(0, 90))).isoformat()
        payer = rng.choice(NAME_POOL)
        cents = rng.randint(1, 300000)
        if cents % 100 == 0:
            amt = str(cents // 100)
        elif cents % 10 == 0:
            amt = f"{cents // 100}.{(cents % 100) // 10}"
        else:
            amt = f"{cents // 100}.{cents % 100:02d}"
        pool = distinct[:]
        rng.shuffle(pool)
        chosen = pool[:rng.randint(1, 4)]  # distinct people by construction
        parts = [(n, rng.randint(1, 5)) for n in chosen]
        part_text = ";".join(
            (f" {n} " if rng.random() < 0.3 else n)
            + (f":{w}" if w != 1 or rng.random() < 0.3 else "")
            for n, w in parts)
        note = rng.choice(["", "lunch", "a, b", "x"])
        text_rows.append(f'{date},{payer},{amt},{part_text},"{note}"')
        rows.append({"date": date, "payer": payer.strip(), "cents": cents,
                     "parts": [(n.strip(), w) for n, w in parts]})
    return ledger(*text_rows), rows


@pytest.mark.parametrize("seed", range(30))
def test_random_ledgers_match_reference(seed):
    rng = random.Random(seed)
    led, rows = _random_ledger(rng, rng.randint(1, 25))
    extra = []
    since = until = None
    if seed % 3 == 0:
        since = "2026-02-01"
        extra += ["--since", since]
    if seed % 4 == 0:
        until = "2026-03-10"
        extra += ["--until", until]

    # Without a range, display spelling is fully specified and compared exactly.
    # With a range, SPEC.md's "first appearance in the file" is ambiguous about
    # rows outside the range, so names are compared ignoring case there.
    def fold(s):
        return s.casefold() if extra else s

    balances, transfers = ref_model(rows, since, until)

    out = run_ok(led, "balances", "{f}", *extra)
    assert fold(out).splitlines() == fold(
        "\n".join(f"{n} {fmt_signed(c)}" for n, c in balances)).splitlines()

    js = json.loads(run_ok(led, "balances", "{f}", "--format", "json", *extra))
    assert [b["cents"] for b in js["balances"]] == [c for _, c in balances]
    assert [fold(b["name"]) for b in js["balances"]] == [fold(n) for n, _ in balances]

    out = run_ok(led, "settle", "{f}", *extra)
    if not transfers:
        assert out == "nothing to settle\n"
    else:
        assert fold(out).splitlines() == fold(
            "\n".join(f"{a} -> {b} {fmt_unsigned(c)}" for a, b, c in transfers)).splitlines()

    js = json.loads(run_ok(led, "settle", "{f}", "--format", "json", *extra))
    assert [t["cents"] for t in js["transfers"]] == [c for _, _, c in transfers]
    assert [(fold(t["from"]), fold(t["to"])) for t in js["transfers"]] == \
        [(fold(a), fold(b)) for a, b, _ in transfers]
