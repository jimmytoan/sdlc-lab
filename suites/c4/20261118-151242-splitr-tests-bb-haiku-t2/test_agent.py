"""Black-box regression suite for splitr.py, written from SPEC.md.

Every test drives the CLI through tests/helpers.run (a subprocess), so the
product's internals are never imported or inspected.
"""
import json
import random
from pathlib import Path

import pytest

from helpers import run, lines

HEADER = "date,payer,amount,participants,note"


def ledger(*rows, header=HEADER):
    return "\n".join([header, *rows]) + "\n"


def fmt_signed(c):
    """Expected text for a signed balance, per SPEC: sign + two decimals, zero unsigned."""
    if c == 0:
        return "0.00"
    sign = "+" if c > 0 else "-"
    return f"{sign}{abs(c) // 100}.{abs(c) % 100:02d}"


def fmt_unsigned(c):
    return f"{c // 100}.{c % 100:02d}"


# ---------------------------------------------------------------------------
# Reference model (independent re-statement of SPEC.md's splitting and settle rules)
# ---------------------------------------------------------------------------

def ref_compute(rows):
    """rows: list of (payer, cents, [(name, weight), ...]) already in considered order.

    Returns (balances: list of (name, cents) sorted by name ignoring case,
             transfers: list of (debtor, creditor, cents))."""
    canon = {}

    def cn(name):
        key = name.casefold()
        if key not in canon:
            canon[key] = name
        return canon[key]

    bal = {}
    for payer, cents, parts in rows:
        p = cn(payer)
        bal.setdefault(p, 0)
        parts = [(cn(n), w) for n, w in parts]
        for n, _ in parts:
            bal.setdefault(n, 0)
        total_w = sum(w for _, w in parts)
        bases = [(cents * w) // total_w for _, w in parts]
        rems = [(cents * w) % total_w for _, w in parts]
        leftover = cents - sum(bases)
        order = sorted(range(len(parts)), key=lambda i: (-rems[i], parts[i][0].casefold()))
        shares = list(bases)
        for i in order[:leftover]:
            shares[i] += 1
        bal[p] += cents
        for (n, _), s in zip(parts, shares):
            bal[n] -= s
    assert sum(bal.values()) == 0
    balances = sorted(bal.items(), key=lambda kv: kv[0].casefold())

    work = dict(bal)
    transfers = []
    while True:
        nonzero = {k: v for k, v in work.items() if v != 0}
        if not nonzero:
            break
        debtor = min(nonzero, key=lambda k: (nonzero[k], k.casefold()))
        creditor = min(nonzero, key=lambda k: (-nonzero[k], k.casefold()))
        amt = min(-work[debtor], work[creditor])
        transfers.append((debtor, creditor, amt))
        work[debtor] += amt
        work[creditor] -= amt
    return balances, transfers


# ---------------------------------------------------------------------------
# Hand-computed examples
# ---------------------------------------------------------------------------

TRIP = ledger(
    "2026-03-01,Ann,60.00,Ann;Bob;Cy,dinner",
    "2026-03-02,Bob,30,Ann;Bob,taxi",
    '2026-03-03,Cy,9.00,Ann;Bob;Cy,"coffee, cake"',
)


def test_balances_text_example():
    code, out, err = run(TRIP, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +22.00\nBob -8.00\nCy -14.00\n"


def test_settle_text_example():
    code, out, err = run(TRIP, "settle", "{f}")
    assert code == 0 and err == ""
    assert out == "Cy -> Ann 14.00\nBob -> Ann 8.00\n"


def test_leftover_cent_goes_to_name_order_on_tie():
    # 10.00 over three equal weights: remainders all tie, so the extra cent goes to Ann.
    code, out, _ = run(ledger("2026-03-01,Zed,10.00,Ann;Bob;Cy,"), "balances", "{f}")
    assert code == 0
    assert out == "Ann -3.34\nBob -3.33\nCy -3.33\nZed +10.00\n"


def test_leftover_goes_to_largest_remainder_not_name_order():
    # 0.10 over Ann:1 and Bob:2 -> base 3 and 6, leftover 1 goes to Bob (remainder 2 > 1).
    code, out, _ = run(ledger("2026-03-01,Zed,0.10,Ann;Bob:2,"), "balances", "{f}")
    assert code == 0
    assert out == "Ann -0.03\nBob -0.07\nZed +0.10\n"


def test_weights_split():
    code, out, _ = run(ledger("2026-03-01,Ann,40.00,Ann:1;Bob:3,"), "balances", "{f}")
    assert code == 0
    assert out == "Ann +30.00\nBob -30.00\n"


def test_leftover_tie_broken_by_name_ignoring_case():
    # 0.01 split between "Banana" and "apple": equal remainders; ignoring case "apple" < "Banana".
    code, out, _ = run(ledger("2026-03-01,Zed,0.01,Banana;apple,"), "balances", "{f}")
    assert code == 0
    assert out == "apple -0.01\nBanana 0.00\nZed +0.01\n"


def test_settle_debtor_tie_broken_by_name_ignoring_case():
    # Zed is paid 10.00 by apple and Banana (5.00 each); ignoring case, apple is the debtor first.
    led = ledger(
        "2026-03-01,Zed,10.00,apple;Banana,",
    )
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert out == "apple -> Zed 5.00\nBanana -> Zed 5.00\n"


def test_settle_creditor_tie_broken_by_name_ignoring_case():
    # Dan owes 10.00; creditors Banana and apple both +5.00 -> apple is paid first.
    led = ledger(
        "2026-03-01,Banana,10.00,Banana;Dan,",
        "2026-03-02,apple,10.00,apple;Dan,",
    )
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert out == "Dan -> apple 5.00\nDan -> Banana 5.00\n"


def test_zero_balance_prints_unsigned_zero_and_person_is_listed():
    led = ledger("2026-03-01,Ann,10.00,Ann,", "2026-03-02,Bob,4.00,Bob;Ann,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Ann -2.00\nBob +2.00\n"
    solo = ledger("2026-03-01,Ann,10.00,Ann,")
    code, out, _ = run(solo, "balances", "{f}")
    assert code == 0
    assert out == "Ann 0.00\n"


def test_settle_nothing_to_settle_when_all_zero():
    code, out, err = run(ledger("2026-03-01,Ann,10.00,Ann,"), "settle", "{f}")
    assert code == 0 and err == ""
    assert out == "nothing to settle\n"


def test_amount_formats_are_equivalent():
    led = ledger(
        "2026-03-01,Ann,12,Ann;Bob,",
        "2026-03-02,Ann,12.5,Ann;Bob,",
        "2026-03-03,Ann,12.50,Ann;Bob,",
    )
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    # 12 + 12.5 + 12.50 = 37.00, split evenly: Ann paid 37.00 and owes 18.50.
    assert out == "Ann +18.50\nBob -18.50\n"


def test_large_amount_formatting():
    code, out, _ = run(ledger("2026-03-01,Ann,12345678.90,Ann;Bob,"), "balances", "{f}")
    assert code == 0
    assert out == "Ann +6172839.45\nBob -6172839.45\n"


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def test_balances_json_shape():
    code, out, err = run(TRIP, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    assert json.loads(out) == {"balances": [
        {"name": "Ann", "cents": 2200},
        {"name": "Bob", "cents": -800},
        {"name": "Cy", "cents": -1400},
    ]}


def test_settle_json_shape():
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 1400},
        {"from": "Bob", "to": "Ann", "cents": 800},
    ]}


def test_json_is_single_document_on_stdout():
    code, out, err = run(TRIP, "balances", "{f}", "--format", "json")
    assert code == 0 and err == ""
    assert out.count("\n") <= 1
    json.loads(out)  # must parse as a whole


def test_json_nothing_to_settle_has_empty_list():
    code, out, _ = run(ledger("2026-03-01,Ann,10.00,Ann,"), "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": []}


def test_json_empty_ledger_balances_has_empty_list():
    code, out, _ = run(ledger(), "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"balances": []}


def test_json_format_explicit_text_is_default():
    a = run(TRIP, "balances", "{f}")
    b = run(TRIP, "balances", "{f}", "--format", "text")
    assert a == b


# ---------------------------------------------------------------------------
# Empty ledgers and ignored lines
# ---------------------------------------------------------------------------

def test_header_only_is_valid_empty_ledger_text():
    code, out, err = run(ledger(), "balances", "{f}")
    assert code == 0 and out == "" and err == ""
    code, out, err = run(ledger(), "settle", "{f}")
    assert code == 0 and out == "nothing to settle\n" and err == ""


def test_comments_and_blank_lines_ignored_and_counted():
    led = (
        "# leading comment\n"
        "\n"
        "date,payer,amount,participants,note\n"
        "   \t  \n"
        "   # indented comment\n"
        "2026-03-01,Ann,10.00,Ann;Bob,\n"
        "# between\n"
        "2026-03-02,Ann,bad,Ann,\n"  # physical line 8
    )
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 8: bad-amount"]


def test_valid_rows_surrounded_by_comments_are_counted():
    led = (
        "# c\n"
        "date,payer,amount,participants,note\n"
        "# c\n"
        "2026-03-01,Ann,10.00,Ann;Bob,\n"
        "\n"
        "2026-03-02,Bob,4.00,Bob;Ann,\n"
    )
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +3.00\nBob -3.00\n"


def test_whitespace_padded_comment_is_ignored():
    led = ledger("   # not a row", "2026-03-01,Ann,1.00,Ann;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Ann +0.50\nBob -0.50\n"


# ---------------------------------------------------------------------------
# File format: BOM, CRLF, column order, quoting, extra columns
# ---------------------------------------------------------------------------

def test_bom_is_accepted():
    raw = b"\xef\xbb\xbf" + ledger("2026-03-01,Ann,10.00,Ann;Bob,").encode("utf-8")
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_crlf_line_endings_accepted():
    raw = ledger("2026-03-01,Ann,10.00,Ann;Bob,dinner", "2026-03-02,Bob,4.00,Bob,").replace("\n", "\r\n")
    code, out, err = run("", "balances", "{f}", raw=raw.encode())
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_crlf_and_bom_line_numbers_are_physical():
    raw = b"\xef\xbb\xbf" + ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-02,Ann,x,Ann,").replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_columns_in_any_order():
    led = "note,participants,amount,payer,date\ndinner,\"Ann;Bob\",10.00,Ann,2026-03-01\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_extra_columns_are_allowed():
    led = "date,category,payer,amount,participants,note\n2026-03-01,food,Ann,10.00,Ann;Bob,x\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Ann +5.00\nBob -5.00\n"


def test_quoted_note_with_comma_and_quotes():
    led = ledger('2026-03-01,Ann,10.00,Ann;Bob,"a, ""quoted"" note"')
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_empty_note_is_valid():
    code, out, err = run(ledger("2026-03-01,Ann,10.00,Ann;Bob,"), "balances", "{f}")
    assert code == 0 and err == ""
    code, out, err = run(ledger('2026-03-01,Ann,10.00,Ann;Bob,""'), "balances", "{f}")
    assert code == 0 and err == ""


def test_quoted_payer_and_participants():
    led = ledger('2026-03-01,"Ann","10.00","Ann;Bob",')
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_note_may_be_free_text_with_punctuation():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,Dinner @ 7pm: 2 bottles; tip & tax!")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_names_are_trimmed():
    led = ledger("2026-03-01, Ann , 10.00, Ann ; Bob ,")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""
    assert out == "Ann +5.00\nBob -5.00\n"


def test_names_match_ignoring_case_and_first_spelling_wins():
    led = ledger(
        "2026-03-01,ann,10.00,Ann;BOB,",
        "2026-03-02,BOB,4.00,bob;ANN,",
    )
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "ann +3.00\nBOB -3.00\n"


def test_payer_spelling_precedes_participant_spelling():
    # Reading order is payer first, then participants left to right.
    led = ledger("2026-03-01,bob,10.00,Bob;Ann,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Ann -5.00\nbob +5.00\n"


def test_payer_need_not_be_participant():
    led = ledger("2026-03-01,Zed,10.00,Ann;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Ann -5.00\nBob -5.00\nZed +10.00\n"


def test_names_with_internal_spaces_are_allowed():
    led = ledger("2026-03-01,Mary Ann,10.00,Mary Ann;Bob,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert out == "Bob -5.00\nMary Ann +5.00\n"


def test_balances_sorted_by_name_ignoring_case():
    led = ledger("2026-03-01,Banana,30.00,apple;Banana;Zed,")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0
    assert [l.split(" ")[0] for l in lines(out)] == ["apple", "Banana", "Zed"]


# ---------------------------------------------------------------------------
# Validation: invalid rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("date", [
    "2026-02-30",   # not a real day
    "2023-02-29",   # not a leap year
    "2026-13-01",   # month out of range
    "2026-00-10",   # month zero
    "2026-04-31",   # April has 30 days
    "2026/03/01",   # wrong separator
    "03-01-2026",   # wrong order
    "2026-3-1",     # not zero-padded
    "2026-03-01T10:00",
    "yesterday",
    "",
])
def test_bad_date_rejected(date):
    code, out, err = run(ledger(f"{date},Ann,10.00,Ann;Bob,"), "balances", "{f}")
    assert code == 2
    assert out == ""
    assert lines(err) == ["line 2: bad-date"]


def test_leap_day_in_leap_year_is_valid():
    code, out, err = run(ledger("2024-02-29,Ann,10.00,Ann;Bob,"), "balances", "{f}")
    assert code == 0 and err == ""


@pytest.mark.parametrize("payer", ["", "   ", "\t"])
def test_empty_payer_rejected(payer):
    code, out, err = run(ledger(f"2026-03-01,{payer},10.00,Ann;Bob,"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", [
    "",
    "0",
    "0.00",
    "-1",
    "-0.50",
    "+1",
    "1,000",
    "1 000",
    "1e2",
    "1E2",
    "1.234",
    "1.2.3",
    "abc",
    "$5",
    "5$",
    "12.5x",
])
def test_bad_amount_rejected(amount):
    if "," in amount:
        amount = f'"{amount}"'  # keep the comma inside one CSV field
    code, out, err = run(ledger(f"2026-03-01,Ann,{amount},Ann;Bob,"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-amount"]


@pytest.mark.parametrize("amount", ["12", "12.5", "12.50", "0.01", "0.5", "100.00", "999999.99"])
def test_good_amounts_accepted(amount):
    code, out, err = run(ledger(f"2026-03-01,Ann,{amount},Ann;Bob,"), "balances", "{f}")
    assert code == 0 and err == ""


@pytest.mark.parametrize("parts", [
    "",
    ";",
    "Ann;",
    ";Bob",
    "Ann;;Bob",
    "Ann:0",
    "Ann:-1",
    "Ann:+2",
    "Ann:1.5",
    "Ann:x",
    "Ann:",
    "Ann;ann",          # repeated person, case-insensitive
    "Ann;Bob;Ann:2",    # repeated with weight
    "Ann:2;Ann",
])
def test_bad_participants_rejected(parts):
    code, out, err = run(ledger(f"2026-03-01,Zed,10.00,{parts},"), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-participants"]


def test_weighted_and_spaced_participants_accepted():
    code, out, err = run(ledger("2026-03-01,Ann,10.00, Ann:2 ; Bob:3 ,"), "balances", "{f}")
    assert code == 0 and err == ""
    # 1000 split 2:3 -> 400 and 600
    assert out == "Ann +6.00\nBob -6.00\n"


def test_payer_listed_as_participant_is_fine():
    code, out, err = run(ledger("2026-03-01,Ann,10.00,Ann;Bob,"), "balances", "{f}")
    assert code == 0 and err == ""


# ---------------------------------------------------------------------------
# Validation precedence: first failing check wins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row, reason", [
    ("2026-02-30,,abc,;,", "bad-date"),
    ("2026-03-01,,abc,Ann;Bob,", "bad-payer"),
    ("2026-03-01,Ann,abc,;,", "bad-amount"),
    ("2026-03-01,Ann,10.00,;,", "bad-participants"),
])
def test_first_failing_check_is_the_only_reason(row, reason):
    code, out, err = run(ledger(row), "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"line 2: {reason}"]


def test_one_reason_per_row_even_with_several_problems():
    code, out, err = run(ledger("2026-13-40,Ann,-1,Ann;Ann,"), "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 2: bad-date"]


# ---------------------------------------------------------------------------
# Validation: reporting multiple rows and failing whole file
# ---------------------------------------------------------------------------

def test_every_invalid_row_reported_in_line_order_and_nothing_on_stdout():
    led = ledger(
        "2026-03-01,Ann,10.00,Ann;Bob,",       # line 2 ok
        "2026-03-01,Ann,oops,Ann;Bob,",        # line 3 bad-amount
        "2026-03-02,Bob,4.00,Bob;Ann,",        # line 4 ok
        "2026-02-31,Bob,4.00,Bob;Ann,",        # line 5 bad-date
        "2026-03-03,,4.00,Bob;Ann,",           # line 6 bad-payer
        "2026-03-04,Ann,4.00,Ann;Ann,",        # line 7 bad-participants
    )
    for cmd in ("balances", "settle"):
        code, out, err = run(led, cmd, "{f}")
        assert code == 2
        assert out == ""
        assert lines(err) == [
            "line 3: bad-amount",
            "line 5: bad-date",
            "line 6: bad-payer",
            "line 7: bad-participants",
        ]


def test_one_invalid_row_fails_whole_report_even_with_json():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-01,Ann,0,Ann;Bob,")
    code, out, err = run(led, "balances", "{f}", "--format", "json")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_header_row_is_not_validated_as_expense():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,")
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and err == ""


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header, missing", [
    ("payer,amount,participants,note", "date"),
    ("date,amount,participants,note", "payer"),
    ("date,payer,participants,note", "amount"),
    ("date,payer,amount,note", "participants"),
    ("date,payer,amount,participants", "note"),
])
def test_missing_header_column_reported(header, missing):
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", header=header)
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == [f"header: missing {missing}"]


def test_only_first_missing_column_is_reported_in_spec_order():
    code, out, err = run("payer,participants\n2026-03-01,Ann,Ann\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing date"]
    code, out, err = run("date,payer,participants\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing amount"]
    code, out, err = run("date\n", "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing payer"]


def test_missing_header_column_suppresses_row_errors():
    led = ledger("2026-99-99,,abc,;,", header="date,payer,amount,participants")
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing note"]


def test_missing_header_column_is_reported_for_settle_too():
    code, out, err = run("date,payer,amount\n", "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["header: missing participants"]


# ---------------------------------------------------------------------------
# Unreadable file
# ---------------------------------------------------------------------------

def test_missing_file_exit_3(tmp_path):
    missing = str(tmp_path / "does-not-exist.csv")
    code, out, err = run("", "balances", missing)
    assert code == 3
    assert out == ""
    assert lines(err) == [f"error: cannot read {missing}"]


def test_missing_file_exit_3_for_settle(tmp_path):
    missing = str(tmp_path / "nope.csv")
    code, out, err = run("", "settle", missing)
    assert code == 3 and out == ""
    assert lines(err) == [f"error: cannot read {missing}"]


# ---------------------------------------------------------------------------
# Usage errors (exit 64)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    [],
    ["balances"],
    ["settle"],
    ["explode", "{f}"],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["settle", "{f}", "--format", "csv"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "03/01/2026"],
    ["balances", "{f}", "--until", "tomorrow"],
    ["balances", "{f}", "--since"],
    ["balances", "{f}", "--until", "2026-3-1"],
])
def test_usage_errors_exit_64(args):
    code, out, err = run(TRIP, *args)
    assert code == 64
    assert out == ""


def test_unknown_command_exit_64():
    code, out, _ = run(TRIP, "explode", "{f}")
    assert code == 64 and out == ""


def test_command_names_are_case_sensitive_or_rejected():
    # Only the documented lower-case commands are defined.
    code, out, _ = run(TRIP, "Balances", "{f}")
    assert code == 64 and out == ""


# ---------------------------------------------------------------------------
# Options: position, --since/--until
# ---------------------------------------------------------------------------

def test_options_before_positional_arguments():
    code, out, _ = run(TRIP, "--format", "json", "balances", "{f}")
    assert code == 0
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": 2200}


def test_options_after_positional_arguments():
    code, out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": 2200}


def test_options_interleaved_with_positionals():
    code, out, _ = run(TRIP, "balances", "--format", "json", "{f}")
    assert code == 0
    json.loads(out)


SINCE_LEDGER = ledger(
    "2026-02-28,Old,10.00,Old;Ann,",       # outside range below
    "2026-03-01,Ann,10.00,Ann;Bob,",       # first day of range
    "2026-03-15,Bob,6.00,Bob;Ann,",
    "2026-03-31,Cy,4.00,Cy;Bob,",          # last day of range
    "2026-04-01,Dan,8.00,Dan;Bob,",        # outside range above
)


def test_since_is_inclusive_and_excludes_earlier_rows():
    code, out, err = run(SINCE_LEDGER, "balances", "{f}", "--since", "2026-03-01")
    assert code == 0 and err == ""
    names = [l.split(" ")[0] for l in lines(out)]
    assert "Old" not in names
    assert "Dan" in names


def test_until_is_inclusive_and_excludes_later_rows():
    code, out, _ = run(SINCE_LEDGER, "balances", "{f}", "--until", "2026-03-31")
    assert code == 0
    names = [l.split(" ")[0] for l in lines(out)]
    assert "Dan" not in names
    assert "Old" in names


def test_since_and_until_together_inclusive():
    code, out, _ = run(SINCE_LEDGER, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-03-31")
    assert code == 0
    # Only rows of 03-01, 03-15 and 03-31 count.
    # Only the three in-range rows count.
    expected = ref_compute([
        ("Ann", 1000, [("Ann", 1), ("Bob", 1)]),
        ("Bob", 600, [("Bob", 1), ("Ann", 1)]),
        ("Cy", 400, [("Cy", 1), ("Bob", 1)]),
    ])[0]
    assert lines(out) == [f"{n} {fmt_signed(c)}" for n, c in expected]


def test_since_with_no_matching_rows_prints_nothing_and_exit_0():
    code, out, err = run(SINCE_LEDGER, "balances", "{f}", "--since", "2030-01-01")
    assert code == 0 and err == "" and out == ""


def test_settle_nothing_to_settle_when_range_empty():
    code, out, err = run(SINCE_LEDGER, "settle", "{f}", "--since", "2030-01-01")
    assert code == 0 and err == ""
    assert out == "nothing to settle\n"


def test_out_of_range_people_not_printed():
    code, out, _ = run(SINCE_LEDGER, "balances", "{f}", "--since", "2026-04-01")
    assert code == 0
    expected = ref_compute([("Dan", 800, [("Dan", 1), ("Bob", 1)])])[0]
    assert lines(out) == [f"{n} {fmt_signed(c)}" for n, c in expected]
    assert "Ann" not in out and "Old" not in out and "Cy" not in out


def test_range_applies_to_settle_too():
    code, out, _ = run(SINCE_LEDGER, "settle", "{f}", "--until", "2026-02-28", "--format", "json")
    assert code == 0
    expected = ref_compute([("Old", 1000, [("Old", 1), ("Ann", 1)])])[1]
    assert json.loads(out) == {"transfers": [
        {"from": d, "to": c, "cents": a} for d, c, a in expected]}


def test_out_of_range_rows_still_validated():
    led = ledger(
        "2026-03-01,Ann,10.00,Ann;Bob,",
        "2025-01-01,Ann,abc,Ann;Bob,",   # outside range, still invalid
    )
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount"]


def test_out_of_range_bad_date_still_reported():
    led = ledger(
        "2026-03-01,Ann,10.00,Ann;Bob,",
        "2026-02-30,Ann,10.00,Ann;Bob,",  # invalid calendar date, outside range
    )
    code, out, err = run(led, "settle", "{f}", "--until", "2026-03-01")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-date"]


def test_out_of_range_bad_row_reported_in_json_mode_too():
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob,", "2026-03-05,,1.00,Ann,")
    code, out, err = run(led, "balances", "{f}", "--since", "2026-03-02", "--format", "json")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-payer"]


# ---------------------------------------------------------------------------
# Settle semantics
# ---------------------------------------------------------------------------

def test_settle_transfers_smaller_absolute_balance():
    # Ann +10, Bob -4, Cy -6: Cy pays Ann 6.00 first (most negative vs most positive), then Bob 4.00.
    led = ledger("2026-03-01,Ann,10.00,Ann;Bob;Cy,")
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    expected = ref_compute([("Ann", 1000, [("Ann", 1), ("Bob", 1), ("Cy", 1)])])[1]
    assert lines(out) == [f"{d} -> {c} {fmt_unsigned(a)}" for d, c, a in expected]


def test_settle_text_format_unsigned_two_decimals():
    led = ledger("2026-03-01,Ann,1.00,Ann;Bob,")
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    assert out == "Bob -> Ann 0.50\n"


def test_settle_creates_no_zero_transfers():
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0
    for t in json.loads(out)["transfers"]:
        assert t["cents"] > 0


def test_settle_clears_all_balances_in_json():
    code, out, _ = run(TRIP, "settle", "{f}", "--format", "json")
    bal = {"Ann": 2200, "Bob": -800, "Cy": -1400}
    for t in json.loads(out)["transfers"]:
        bal[t["from"]] += t["cents"]
        bal[t["to"]] -= t["cents"]
    assert all(v == 0 for v in bal.values())


def test_settle_chain_of_three():
    # Ann paid 20 for Bob, Bob paid 10 for Cy -> Ann +20 ... check against reference.
    led = ledger(
        "2026-03-01,Ann,20.00,Bob,",
        "2026-03-02,Bob,10.00,Cy,",
    )
    code, out, _ = run(led, "settle", "{f}")
    assert code == 0
    expected = ref_compute([("Ann", 2000, [("Bob", 1)]), ("Bob", 1000, [("Cy", 1)])])[1]
    assert lines(out) == [f"{d} -> {c} {fmt_unsigned(a)}" for d, c, a in expected]


# ---------------------------------------------------------------------------
# Randomized cross-check against the reference model
# ---------------------------------------------------------------------------

POOL = ["Ann", "bob", "CY", "dan", "Eve", "fay"]


def _random_ledger(rng, n_rows):
    rows, model = [], []
    for i in range(n_rows):
        day = f"2026-03-{rng.randint(1, 28):02d}"
        payer_name = rng.choice(POOL)
        payer = payer_name if rng.random() < 0.5 else payer_name.swapcase()
        k = rng.randint(1, 4)
        chosen = rng.sample(POOL, k)
        parts_txt, parts_model = [], []
        for n in chosen:
            spelled = n if rng.random() < 0.5 else n.upper()
            w = rng.randint(1, 4)
            parts_txt.append(spelled if w == 1 else f"{spelled}:{w}")
            parts_model.append((spelled, w))
        cents = rng.randint(1, 50000)
        amt = f"{cents // 100}.{cents % 100:02d}" if cents % 100 else str(cents // 100)
        rows.append(f"{day},{payer},{amt},{';'.join(parts_txt)},n{i}")
        model.append((payer, cents, parts_model))
    return ledger(*rows), model


@pytest.mark.parametrize("seed", range(30))
def test_random_ledgers_match_reference_balances(seed):
    rng = random.Random(seed)
    text, model = _random_ledger(rng, rng.randint(1, 8))
    expected_bal, expected_tr = ref_compute(model)
    code, out, err = run(text, "balances", "{f}")
    assert code == 0 and err == "", text
    assert lines(out) == [f"{n} {fmt_signed(c)}" for n, c in expected_bal], text


@pytest.mark.parametrize("seed", range(30))
def test_random_ledgers_match_reference_settle(seed):
    rng = random.Random(1000 + seed)
    text, model = _random_ledger(rng, rng.randint(1, 8))
    _, expected_tr = ref_compute(model)
    code, out, err = run(text, "settle", "{f}", "--format", "json")
    assert code == 0 and err == "", text
    got = json.loads(out)["transfers"]
    assert got == [{"from": d, "to": c, "cents": a} for d, c, a in expected_tr], text
    if not expected_tr:
        code, out, _ = run(text, "settle", "{f}")
        assert out == "nothing to settle\n"


@pytest.mark.parametrize("seed", range(10))
def test_random_balances_sum_to_zero_and_settle_clears(seed):
    rng = random.Random(2000 + seed)
    text, _ = _random_ledger(rng, rng.randint(1, 10))
    code, out, _ = run(text, "balances", "{f}", "--format", "json")
    assert code == 0
    bal = {b["name"].casefold(): b["cents"] for b in json.loads(out)["balances"]}
    assert sum(bal.values()) == 0
    code, out, _ = run(text, "settle", "{f}", "--format", "json")
    for t in json.loads(out)["transfers"]:
        assert t["cents"] > 0
        bal[t["from"].casefold()] += t["cents"]
        bal[t["to"].casefold()] -= t["cents"]
    assert all(v == 0 for v in bal.values())


def test_text_and_json_agree():
    rng = random.Random(42)
    text, _ = _random_ledger(rng, 7)
    _, tout, _ = run(text, "balances", "{f}")
    _, jout, _ = run(text, "balances", "{f}", "--format", "json")
    js = json.loads(jout)["balances"]
    assert lines(tout) == [f"{b['name']} {fmt_signed(b['cents'])}" for b in js]
    _, tout, _ = run(text, "settle", "{f}")
    _, jout, _ = run(text, "settle", "{f}", "--format", "json")
    js = json.loads(jout)["transfers"]
    if js:
        assert lines(tout) == [f"{t['from']} -> {t['to']} {fmt_unsigned(t['cents'])}" for t in js]
    else:
        assert lines(tout) == ["nothing to settle"]


# ---------------------------------------------------------------------------
# Output channel discipline
# ---------------------------------------------------------------------------

def test_success_has_empty_stderr():
    for cmd in (["balances"], ["settle"], ["balances", "--format", "json"], ["settle", "--format", "json"]):
        code, out, err = run(TRIP, *cmd, "{f}")
        assert code == 0 and err == ""


def test_failure_has_empty_stdout():
    cases = [
        (ledger("2026-02-30,Ann,1,Ann,"), ["balances"]),
        (ledger("2026-03-01,Ann,1,Ann,"), ["balances", "--format", "nope"]),
        ("date,payer,amount,participants\n", ["settle"]),
    ]
    for text, args in cases:
        code, out, err = run(text, *args, "{f}")
        assert code != 0
        assert out == ""
