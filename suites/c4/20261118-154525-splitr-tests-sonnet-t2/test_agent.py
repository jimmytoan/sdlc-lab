import json
import pytest
from helpers import run, lines

H = "date,payer,amount,participants,note\n"


def bal(rows, *extra):
    code, out, err = run(H + rows, "balances", "{f}", *extra)
    assert code == 0, err
    assert err == ""
    return lines(out)


def settle(rows, *extra):
    code, out, err = run(H + rows, "settle", "{f}", *extra)
    assert code == 0, err
    return lines(out)


def errs(text, cmd="balances", *extra, raw=None):
    code, out, err = run(text, cmd, "{f}", *extra, raw=raw)
    assert out == ""
    return code, lines(err)


# ---------- splitting ----------

def test_even_split_leftover_tie_by_name():
    assert bal("2026-01-01,Zed,0.01,Bob;Ann,\n") == ["Ann -0.01", "Bob 0.00", "Zed +0.01"]


def test_leftover_two_cents_tie_ignoring_case():
    # 3 participants, 0.05 -> 1 each + 2 leftover to the first two by name (case-insens.)
    out = bal("2026-01-01,Zed,0.05,cy;Bob;ann,\n")
    assert out == ["ann -0.02", "Bob -0.02", "cy -0.01", "Zed +0.05"]


def test_leftover_by_largest_remainder_not_name():
    # 100 cents, weights Ann:1 Bob:2 -> shares 33/66, remainders 1/2 -> Bob gets the cent
    out = bal("2026-01-01,Zed,1.00,Ann:1;Bob:2,\n")
    assert out == ["Ann -0.33", "Bob -0.67", "Zed +1.00"]


def test_leftover_remainder_order_multiple():
    # 10 cents, weights 1,2,4 (W=7): shares 1,2,5 (sum 8) ; rem 10%7=3, 20%7=6, 40%7=5
    # leftover 2 -> Bob(6), Cy(5)
    out = bal("2026-01-01,Zed,0.10,Ann:1;Bob:2;Cy:4,\n")
    assert out == ["Ann -0.01", "Bob -0.03", "Cy -0.06", "Zed +0.10"]


def test_weights_basic_and_default():
    out = bal("2026-01-01,Ann,40.00,Ann;Bob:3,\n")
    assert out == ["Ann +30.00", "Bob -30.00"]


def test_weight_larger_than_one_digit_and_spaces():
    out = bal("2026-01-01,Ann,11.00,Ann : 10 ; Bob : 1 ,\n")
    assert out == ["Ann +1.00", "Bob -1.00"]


def test_payer_not_participant():
    assert bal("2026-01-01,Ann,10,Bob,\n") == ["Ann +10.00", "Bob -10.00"]


def test_payer_participant_gets_no_priority_for_cent():
    # payer Zed among participants: Ann gets the extra cent by name
    out = bal("2026-01-01,Zed,0.01,Zed;Ann,\n")
    assert out == ["Ann -0.01", "Zed +0.01"]


def test_balances_sum_zero_many():
    rows = "".join(f"2026-01-{d:02d},P{d%3},{d}.{d:02d},P0;P1;P2:{d},\n" for d in range(1, 20))
    total = 0
    code, out, _ = run(H + rows, "balances", "{f}", "--format", "json")
    assert code == 0
    for b in json.loads(out)["balances"]:
        total += b["cents"]
    assert total == 0


def test_amount_formats():
    assert bal("2026-01-01,Ann,12,Bob,\n2026-01-02,Ann,12.5,Bob,\n2026-01-03,Ann,12.50,Bob,\n"
               "2026-01-04,Ann,0.07,Bob,\n") == ["Ann +37.07", "Bob -37.07"]


def test_large_amount():
    assert bal("2026-01-01,Ann,1234567890.99,Bob,\n") == ["Ann +1234567890.99", "Bob -1234567890.99"]


# ---------- names ----------

def test_names_case_insensitive_first_spelling():
    out = bal("2026-01-01,ann,10,BOB;Ann,\n2026-01-02,BOB,10,bob;ANN,\n")
    assert out == ["ann 0.00", "BOB 0.00"]


def test_first_spelling_payer_before_participants():
    out = bal("2026-01-01,Bob,10,ANN;bob,\n2026-01-02,ann,5,Ann,\n")
    assert out == ["ANN -5.00", "Bob +5.00"]


def test_first_spelling_participants_left_to_right():
    out = bal("2026-01-01,Zed,10,bob;BOB2;Ann,\n2026-01-02,Zed,3,BOB;ANN,\n")
    names = [l.rsplit(" ", 1)[0] for l in out]
    assert names == ["Ann", "bob", "BOB2", "Zed"]


def test_names_trimmed():
    out = bal("2026-01-01,  Ann  ,10,  Bob ; Ann ,\n")
    assert out == ["Ann +5.00", "Bob -5.00"]


def test_sorted_ignoring_case():
    out = bal("2026-01-01,bob,6,Ann;Cy;bob,\n")
    assert [l.split()[0] for l in out] == ["Ann", "bob", "Cy"]
    out = bal("2026-01-01,Zed,6,b;A;C,\n")
    assert [l.split()[0] for l in out] == ["A", "b", "C", "Zed"]


def test_names_with_spaces():
    out = bal("2026-01-01,Ann Lee,10,Bob Ray,\n")
    assert out == ["Ann Lee +10.00", "Bob Ray -10.00"]


# ---------- balances output ----------

def test_zero_balance_no_sign():
    out = bal("2026-01-01,Ann,10,Bob,\n2026-01-02,Bob,10,Ann,\n")
    assert out == ["Ann 0.00", "Bob 0.00"]


def test_self_only_expense_zero():
    assert bal("2026-01-01,Ann,10,Ann,\n") == ["Ann 0.00"]


def test_json_balances_exact():
    code, out, _ = run(H + "2026-01-01,Ann,12.50,Bob,\n", "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"balances": [{"name": "Ann", "cents": 1250}, {"name": "Bob", "cents": -1250}]}
    assert out.count("\n") == 1


def test_json_balances_empty():
    code, out, _ = run(H, "balances", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"balances": []}


def test_empty_ledger_balances_prints_nothing():
    code, out, err = run(H, "balances", "{f}")
    assert (code, out, err) == (0, "", "")


# ---------- settle ----------

def test_settle_simple():
    assert settle("2026-01-01,Ann,10,Bob,\n") == ["Bob -> Ann 10.00"]


def test_settle_nothing():
    assert settle("2026-01-01,Ann,10,Ann,\n") == ["nothing to settle"]
    code, out, _ = run(H, "settle", "{f}")
    assert out == "nothing to settle\n"


def test_settle_json_empty():
    code, out, _ = run(H, "settle", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"transfers": []}


def test_settle_json():
    code, out, _ = run(H + "2026-01-01,Ann,12.50,Bob,\n", "settle", "{f}", "--format", "json")
    assert json.loads(out) == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 1250}]}


def test_settle_most_negative_first_and_partial():
    # Ann +20, Bob -5, Cy -15
    rows = "2026-01-01,Ann,20,Bob:1;Cy:3,\n"
    assert settle(rows) == ["Cy -> Ann 15.00", "Bob -> Ann 5.00"]


def test_settle_multiple_creditors():
    # Ann paid 30 for Ann,Bob,Cy -> Ann+20,Bob-10,Cy-10; Dee paid 10 for Dee,Bob -> Dee+5,Bob-5
    rows = "2026-01-01,Ann,30,Ann;Bob;Cy,\n2026-01-02,Dee,10,Dee;Bob,\n"
    assert bal(rows) == ["Ann +20.00", "Bob -15.00", "Cy -10.00", "Dee +5.00"]
    assert settle(rows) == ["Bob -> Ann 15.00", "Cy -> Ann 5.00", "Cy -> Dee 5.00"]


def test_settle_debtor_tie_by_name():
    # Ann +20; Bob -10, Cy -10 -> Bob first
    rows = "2026-01-01,Ann,30,Ann;Bob;Cy,\n"
    assert settle(rows) == ["Bob -> Ann 10.00", "Cy -> Ann 10.00"]
    # debtor tie ignoring case
    rows = "2026-01-01,Zed,20,bob;Cy,\n"
    assert settle(rows) == ["bob -> Zed 10.00", "Cy -> Zed 10.00"]


def test_settle_creditor_tie_by_name():
    # Bob and Ann both +10, Zed -20
    rows = "2026-01-01,Bob,10,Zed,\n2026-01-02,Ann,10,Zed,\n"
    assert settle(rows) == ["Zed -> Ann 10.00", "Zed -> Bob 10.00"]
    rows = "2026-01-01,bob,10,Zed,\n2026-01-02,Ann,10,Zed,\n"
    assert settle(rows) == ["Zed -> Ann 10.00", "Zed -> bob 10.00"]


def test_settle_creditor_tie_case_mixed():
    rows = "2026-01-01,Cy,10,Zed,\n2026-01-02,anna,10,Zed,\n2026-01-03,Bob,10,Zed,\n"
    assert settle(rows) == ["Zed -> anna 10.00", "Zed -> Bob 10.00", "Zed -> Cy 10.00"]


def test_settle_greedy_not_minimal():
    # balances: A +5, B +5, C -3, D -7 -> D most negative pays largest creditor (A, tie by name)
    rows = ("2026-01-01,A,5,D,\n2026-01-02,B,5,D,\n2026-01-03,C,0.01,C,\n")
    # simpler explicit: build with exact amounts
    rows = "2026-01-01,A,5,D,\n2026-01-02,B,5,D,\n"
    rows += "2026-01-03,D,3,C,\n"
    # D: -10+3 = -7, C: -3, A +5, B +5
    assert bal(rows) == ["A +5.00", "B +5.00", "C -3.00", "D -7.00"]
    assert settle(rows) == ["D -> A 5.00", "C -> B 3.00", "D -> B 2.00"]


def test_settle_amount_format_cents():
    assert settle("2026-01-01,Ann,0.05,Bob,\n") == ["Bob -> Ann 0.05"]
    assert settle("2026-01-01,Ann,3.1,Bob,\n") == ["Bob -> Ann 3.10"]


# ---------- since / until ----------

RANGE = ("2026-01-01,Ann,10,Bob,\n2026-02-01,Cy,20,Dee,\n2026-03-01,Eve,30,Fay,\n")


def test_since_only():
    assert bal(RANGE, "--since", "2026-02-01") == ["Cy +20.00", "Dee -20.00", "Eve +30.00", "Fay -30.00"]


def test_until_only():
    assert bal(RANGE, "--until", "2026-02-01") == ["Ann +10.00", "Bob -10.00", "Cy +20.00", "Dee -20.00"]


def test_since_until_inclusive_both():
    assert bal(RANGE, "--since", "2026-02-01", "--until", "2026-02-01") == ["Cy +20.00", "Dee -20.00"]


def test_range_empty_result():
    assert bal(RANGE, "--since", "2027-01-01") == []
    assert settle(RANGE, "--since", "2027-01-01") == ["nothing to settle"]


def test_range_excludes_people_outside():
    out = bal(RANGE, "--since", "2026-03-01")
    assert out == ["Eve +30.00", "Fay -30.00"]


def test_range_first_spelling_from_considered_rows_or_file():
    # spelling is from first appearance in the file
    rows = "2025-01-01,x,1,y,\n2026-01-01,ANN,10,bob,\n2026-02-01,ann,10,Bob,\n"
    out = bal(rows, "--since", "2026-02-01")
    assert [l.split()[0].lower() for l in out] == ["ann", "bob"]


def test_range_settle_and_json():
    code, out, _ = run(H + RANGE, "settle", "{f}", "--until", "2026-01-31", "--format", "json")
    assert json.loads(out) == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 1000}]}


def test_validation_applies_outside_range():
    code, e = errs(H + "2020-01-01,Ann,abc,Bob,\n2026-01-01,Ann,1,Bob,\n", "balances", "--since", "2026-01-01")
    assert code == 2 and e == ["line 2: bad-amount"]


def test_range_unordered_rows():
    rows = "2026-03-01,Ann,10,Bob,\n2026-01-01,Cy,10,Dee,\n"
    assert bal(rows, "--until", "2026-02-01") == ["Cy +10.00", "Dee -10.00"]


# ---------- options placement ----------

def test_options_before_and_after():
    r1 = run(H + RANGE, "balances", "{f}", "--format", "json", "--since", "2026-02-01")
    r2 = run(H + RANGE, "--format", "json", "--since", "2026-02-01", "balances", "{f}")
    r3 = run(H + RANGE, "--since", "2026-02-01", "balances", "--format", "json", "{f}")
    assert r1[0] == 0 and r1 == r2 == r3


def test_format_text_explicit():
    code, out, _ = run(H + "2026-01-01,Ann,1,Bob,\n", "balances", "{f}", "--format", "text")
    assert code == 0 and lines(out) == ["Ann +1.00", "Bob -1.00"]


# ---------- file format ----------

def test_bom():
    raw = b"\xef\xbb\xbf" + (H + "2026-01-01,Ann,10,Bob,\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_crlf():
    raw = (H + "2026-01-01,Ann,10,Bob,hi\n2026-01-02,Bob,4,Ann,\n").replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["Ann +6.00", "Bob -6.00"], err


def test_crlf_with_last_column_not_note():
    raw = "note,amount,participants,payer,date\r\nx,10,Bob,Ann,2026-01-01\r\n".encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"], err


def test_no_trailing_newline():
    code, out, _ = run(H + "2026-01-01,Ann,10,Bob,", "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_column_order_any():
    led = "note,participants,amount,payer,date\nhello,Bob;Cy,10,Ann,2026-01-01\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -5.00", "Cy -5.00"]


def test_extra_columns_allowed():
    led = "id,date,payer,amount,participants,note,zzz\n1,2026-01-01,Ann,10,Bob,n,q\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_quoted_fields():
    led = H + '2026-01-01,"Ann","10.00","Bob;Cy","a, b, c"\n'
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -5.00", "Cy -5.00"]


def test_quoted_note_with_escaped_quote():
    led = H + '2026-01-01,Ann,10,Bob,"say ""hi"", ok"\n'
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_ignored_lines():
    led = (H + "\n   \n# comment\n   # indented, comment\n\t\n2026-01-01,Ann,10,Bob,\n\n")
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_comment_before_header():
    led = "# generated\n\n" + H + "2026-01-01,Ann,10,Bob,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


def test_line_numbers_count_ignored_lines():
    led = H + "\n# c\n   \n2026-01-01,Ann,x,Bob,\n"
    code, e = errs(led)
    assert code == 2 and e == ["line 5: bad-amount"]


def test_line_numbers_with_comment_before_header():
    led = "# c\n" + H + "2026-13-01,Ann,1,Bob,\n"
    code, e = errs(led)
    assert code == 2 and e == ["line 3: bad-date"]


def test_line_numbers_crlf():
    raw = (H + "\n2026-01-01,Ann,x,Bob,\n").replace("\n", "\r\n").encode()
    code, e = errs("", raw=raw)
    assert code == 2 and e == ["line 3: bad-amount"]


def test_header_whitespace_tolerated_and_case_sensitive_names():
    led = " date , payer ,amount, participants ,note\n2026-01-01,Ann,10,Bob,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["Ann +10.00", "Bob -10.00"]


# ---------- validation ----------

@pytest.mark.parametrize("d", ["2026-02-30", "2026-13-01", "2026-00-10", "2026-1-01", "26-01-01",
                               "2026/01/01", "", "abc", "2026-01-32", "2025-02-29", "2026-01-01x"])
def test_bad_date(d):
    code, e = errs(H + f"{d},Ann,10,Bob,\n")
    assert code == 2 and e == ["line 2: bad-date"]


def test_good_leap_date():
    assert bal("2024-02-29,Ann,10,Bob,\n") == ["Ann +10.00", "Bob -10.00"]


@pytest.mark.parametrize("p", ["", "   "])
def test_bad_payer(p):
    code, e = errs(H + f"2026-01-01,{p},10,Bob,\n")
    assert code == 2 and e == ["line 2: bad-payer"]


@pytest.mark.parametrize("a", ["", "0", "0.00", "-5", "+5", "1,000", "1e3", "1.234", "1.", ".5", "abc",
                               "1 000", "0.0", "00.00", "$5", "5.5.5", "1_000"])
def test_bad_amount(a):
    q = '"' + a + '"'
    code, e = errs(H + f"2026-01-01,Ann,{q},Bob,\n")
    assert code == 2 and e == ["line 2: bad-amount"]


@pytest.mark.parametrize("a,c", [("0.01", 1), ("007", 700), ("5.0", 500), ("0.10", 10), (" 5 ", 500)])
def test_good_amount(a, c):
    code, out, _ = run(H + f'2026-01-01,Ann,"{a}",Bob,\n', "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out)["balances"][0] == {"name": "Ann", "cents": c}


@pytest.mark.parametrize("p", ["", "  ", ";", "Bob;", ";Bob", "Bob;;Cy", "Bob:0", "Bob:-1", "Bob:x", "Bob:",
                               "Bob:1.5", "Bob;bob", "Bob;BOB:2", "Bob; bob ", ":2", "Bob:+1", "Bob:1:2"])
def test_bad_participants(p):
    code, e = errs(H + f'2026-01-01,Ann,10,"{p}",\n')
    assert code == 2 and e == ["line 2: bad-participants"], p


def test_payer_listed_twice_as_participant_bad():
    code, e = errs(H + "2026-01-01,Ann,10,Ann;ann,\n")
    assert code == 2 and e == ["line 2: bad-participants"]


def test_check_order():
    rows = [
        "bad,,x,,",            # date first
        "2026-01-01,,x,,",     # payer next
        "2026-01-01,Ann,x,,",  # amount next
        "2026-01-01,Ann,1,,",  # participants
    ]
    code, e = errs(H + "\n".join(rows) + "\n")
    assert code == 2
    assert e == ["line 2: bad-date", "line 3: bad-payer", "line 4: bad-amount", "line 5: bad-participants"]


def test_multiple_errors_one_per_row_ascending_and_valid_rows_skipped():
    led = H + "2026-01-01,Ann,10,Bob,\nbad,Ann,10,Bob,\n2026-01-01,Ann,10,Bob,\n2026-01-01,,0,,\n"
    code, e = errs(led)
    assert code == 2 and e == ["line 3: bad-date", "line 5: bad-payer"]


def test_errors_for_settle_too():
    code, e = errs(H + "2026-01-01,Ann,0,Bob,\n", "settle")
    assert code == 2 and e == ["line 2: bad-amount"]


def test_error_json_format_stdout_empty():
    code, e = errs(H + "2026-01-01,Ann,0,Bob,\n", "balances", "--format", "json")
    assert code == 2 and e == ["line 2: bad-amount"]


def test_short_row_missing_cells():
    code, e = errs(H + "2026-01-01,Ann\n")
    assert code == 2 and e == ["line 2: bad-amount"]


# ---------- header ----------

@pytest.mark.parametrize("col", ["date", "payer", "amount", "participants", "note"])
def test_missing_header_column(col):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != col]
    code, e = errs(",".join(cols) + "\n2026-01-01,Ann,10,Bob\n")
    assert code == 2 and e == [f"header: missing {col}"]


def test_missing_multiple_reports_first_only():
    code, e = errs("note,payer\nx,y\nbad row\n")
    assert code == 2 and e == ["header: missing date"]
    code, e = errs("date,note\n")
    assert code == 2 and e == ["header: missing payer"]
    code, e = errs("date,payer,note\n")
    assert code == 2 and e == ["header: missing amount"]
    code, e = errs("date,payer,amount\n")
    assert code == 2 and e == ["header: missing participants"]


def test_header_error_hides_row_errors():
    code, e = errs("date,payer,amount,participants\nbad,,x,\n")
    assert code == 2 and e == ["header: missing note"]


def test_header_case_exact():
    code, e = errs("Date,payer,amount,participants,note\n")
    assert code == 2 and e == ["header: missing date"]


def test_header_only_valid():
    code, out, err = run(H, "settle", "{f}")
    assert (code, out, err) == (0, "nothing to settle\n", "")


def test_header_only_with_bom_and_crlf():
    code, out, _ = run("", "settle", "{f}", raw=b"\xef\xbb\xbf" + H.replace("\n", "\r\n").encode())
    assert code == 0 and out == "nothing to settle\n"


# ---------- unreadable ----------

def test_missing_file():
    code, out, err = run("", "balances", "/nonexistent/dir/x.csv")
    assert code == 3 and out == "" and lines(err) == ["error: cannot read /nonexistent/dir/x.csv"]


def test_directory_as_ledger():
    code, out, err = run("", "balances", ".")
    assert code == 3 and out == "" and lines(err) == ["error: cannot read ."]


def test_invalid_utf8_unreadable():
    code, out, err = run("", "balances", "{f}", raw=b"date,payer,amount,participants,note\n\xff\xfe,a,1,b,\n")
    assert code == 3 and out == "" and lines(err) == ["error: cannot read {f}"]


# ---------- usage ----------

@pytest.mark.parametrize("args", [
    ["explode", "{f}"],
    ["balances"],
    [],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "yesterday"],
    ["balances", "{f}", "--until", "2026-1-1"],
    ["balances", "{f}", "--until", "2026-13-01"],
    ["settle", "{f}", "--format", "JSON"],
    ["settle", "{f}", "--since"],
    ["Balances", "{f}"],
])
def test_usage_errors(args):
    code, out, err = run(H + "2026-01-01,Ann,10,Bob,\n", *args)
    assert code == 64 and out == ""
    assert err.strip() != ""


def test_usage_error_wins_over_bad_ledger():
    code, out, _ = run(H + "bad,,,,\n", "explode", "{f}")
    assert code == 64 and out == ""
    code, out, _ = run(H + "bad,,,,\n", "balances", "{f}", "--since", "nope")
    assert code == 64 and out == ""


def test_usage_error_before_missing_file():
    code, out, _ = run("", "balances", "/nonexistent/x.csv", "--format", "xml")
    assert code == 64


def test_since_after_until_gives_empty():
    assert bal(RANGE, "--since", "2026-03-01", "--until", "2026-01-01") == []
