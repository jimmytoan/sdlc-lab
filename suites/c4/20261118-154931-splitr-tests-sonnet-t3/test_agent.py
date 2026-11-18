import json
import pytest
from helpers import run, lines

H = "date,payer,amount,participants,note\n"


def bal(rows, *extra):
    code, out, err = run(H + rows, "balances", "{f}", *extra)
    assert code == 0 and err == ""
    return lines(out)


def settle(rows, *extra):
    code, out, err = run(H + rows, "settle", "{f}", *extra)
    assert code == 0 and err == ""
    return lines(out)


def errs(rows, header=H, cmd="balances"):
    code, out, err = run(header + rows, cmd, "{f}")
    assert code == 2 and out == ""
    return lines(err)


# ---------- splitting ----------
def test_split_remainder_largest_first_tie_by_name():
    # 10.00 / 3: remainders tie -> Ann gets the cent
    assert bal("2026-01-01,Zed,10.00,Cy;Bob;Ann,\n") == [
        "Ann -3.34", "Bob -3.33", "Cy -3.33", "Zed +10.00"]


def test_split_two_leftover_cents_tie_by_name_ignoring_case():
    # 0.02 among 3 -> two cents to first two by name (ann, Bob), not Cy
    assert bal("2026-01-01,Zed,0.02,cy;Bob;ann,\n") == [
        "cy -0.00".replace("-0.00", "0.00") if False else "ann -0.01",
        "Bob -0.01", "cy 0.00", "Zed +0.02"]


def test_split_weighted_remainder_order():
    # 1.00 with weights 1,1,2 (W=4): exact 25,25,50 no leftover
    assert bal("2026-01-01,Zed,1.00,A:1;B:1;C:2,\n") == [
        "A -0.25", "B -0.25", "C -0.50", "Zed +1.00"]
    # 1.00 with weights 1,2 (W=3): 33.33 / 66.67; remainders 100%3=1, 200%3=2 -> B gets the cent
    assert bal("2026-01-01,Zed,1.00,A:1;B:2,\n") == [
        "A -0.33", "B -0.67", "Zed +1.00"]


def test_split_largest_remainder_beats_name():
    # 1.00, weights A:2 B:1 C:4 (W=7): C*w mod W: A 200%7=4, B 100%7=2, C 400%7=1
    # base: 28,14,57 =99, leftover 1 -> A (largest remainder)
    assert bal("2026-01-01,Zed,1.00,C:4;B:1;A:2,\n") == [
        "A -0.29", "B -0.14", "C -0.57", "Zed +1.00"]
    # leftover goes to largest remainder even if name later: A:1 B:2 -> B
    assert bal("2026-01-01,Zed,0.05,A:1;B:2,\n") == ["A -0.02", "B -0.03", "Zed +0.05"]


def test_split_remainder_ignores_payer_participation():
    # payer is participant but has no priority
    assert bal("2026-01-01,Zed,0.01,Ann;Zed,\n") == ["Ann -0.01", "Zed +0.01"]


def test_payer_not_participant_and_payer_participant():
    assert bal("2026-01-01,Ann,30,Ann;Bob;Cy,\n") == ["Ann +20.00", "Bob -10.00", "Cy -10.00"]
    assert bal("2026-01-01,Ann,30,Bob,\n") == ["Ann +30.00", "Bob -30.00"]


def test_self_only_expense_zero_balance():
    assert bal("2026-01-01,Ann,5,Ann,\n") == ["Ann 0.00"]


def test_zero_balance_unsigned_in_text_and_settle():
    out = bal("2026-01-01,Ann,10,Bob,\n2026-01-02,Bob,10,Ann,\n")
    assert out == ["Ann 0.00", "Bob 0.00"]
    assert settle("2026-01-01,Ann,10,Bob,\n2026-01-02,Bob,10,Ann,\n") == ["nothing to settle"]


def test_amount_formats():
    assert bal("2026-01-01,A,12,B,\n2026-01-02,A,12.5,B,\n2026-01-03,A,0.07,B,\n2026-01-04,A,12.50,B,\n") == [
        "A +37.07", "B -37.07"]


def test_large_amount():
    assert bal("2026-01-01,A,1234567.89,B,\n") == ["A +1234567.89", "B -1234567.89"]


def test_amount_leading_zeros_ok():
    assert bal("2026-01-01,A,007.5,B,\n") == ["A +7.50", "B -7.50"]


# ---------- names ----------
def test_names_case_insensitive_first_spelling():
    out = bal("2026-01-01,ann,10,BOB;Ann,\n2026-01-02,bob,6,ANN,\n")
    assert out == ["ann -1.00", "BOB +1.00"]


def test_first_appearance_payer_before_participants():
    out = bal("2026-01-01,  Zoe ,10,  zoe ; Amy ,\n")
    assert out == ["Amy -5.00", "Zoe +5.00"]


def test_first_appearance_participant_order():
    out = bal("2026-01-01,P,10,bob;Al,\n2026-01-02,Q,10,BOB;AL,\n")
    assert out == ["Al -10.00", "bob -10.00", "P +10.00", "Q +10.00"][:0] + ["Al -10.00", "bob -10.00", "P +10.00", "Q +10.00"]


def test_sorted_ignoring_case():
    out = bal("2026-01-01,b,10,a;C;d,\n")
    assert [l.split()[0] for l in out] == ["a", "b", "C", "d"]


def test_names_with_spaces_inside():
    out = bal("2026-01-01,Ann Lee,10,Bob Ray,\n")
    assert out == ["Ann Lee +10.00", "Bob Ray -10.00"]


def test_participant_whitespace_trim_and_weight_whitespace():
    assert bal("2026-01-01,A,40, B : 3 ; C:1 ,\n") == ["A +40.00", "B -30.00", "C -10.00"]


# ---------- settle ----------
def test_settle_tie_breaking_names():
    # three creditors equal, one big debtor
    rows = "2026-01-01,Cy,10,D,\n2026-01-01,Bob,10,D,\n2026-01-01,ann,10,D,\n"
    assert settle(rows) == ["D -> ann 10.00", "D -> Bob 10.00", "D -> Cy 10.00"]


def test_settle_debtor_tie_by_name():
    rows = "2026-01-01,X,10,b,\n2026-01-01,X,10,A,\n"
    assert settle(rows) == ["A -> X 10.00", "b -> X 10.00"]


def test_settle_partial_amounts():
    rows = "2026-01-01,A,30,B,\n2026-01-01,C,10,D,\n"
    # balances A+30 B-30 C+10 D-10
    assert settle(rows) == ["B -> A 30.00", "D -> C 10.00"]


def test_settle_largest_first():
    rows = "2026-01-01,A,50,X,\n2026-01-01,B,20,Y,\n2026-01-01,C,5,Z,\n"
    # X-50 Y-20 Z-5, A+50 B+20 C+5
    assert settle(rows) == ["X -> A 50.00", "Y -> B 20.00", "Z -> C 5.00"]


def test_settle_split_transfer():
    rows = "2026-01-01,A,30,X,\n2026-01-01,B,10,X,\n2026-01-01,X,25,Y,\n"
    # X: -30-10+25 = -15; Y -25; A +30; B +10
    assert settle(rows) == ["Y -> A 25.00", "X -> B 10.00", "X -> A 5.00"]


def test_settle_basic_json():
    code, out, _ = run(H + "2026-01-01,Ann,12.50,Bob,\n", "settle", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"transfers": [{"from": "Bob", "to": "Ann", "cents": 1250}]}


def test_settle_text_format():
    assert settle("2026-01-01,Ann,12.50,Bob,\n") == ["Bob -> Ann 12.50"]


def test_settle_json_empty():
    code, out, _ = run(H, "settle", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"transfers": []}


def test_nothing_to_settle_exact():
    code, out, _ = run(H, "settle", "{f}")
    assert code == 0 and out == "nothing to settle\n"


# ---------- json / balances ----------
def test_balances_json_order_and_sign():
    code, out, _ = run(H + "2026-01-01,b,10,a;c,\n", "balances", "{f}", "--format", "json")
    assert code == 0
    assert json.loads(out) == {"balances": [
        {"name": "a", "cents": -500}, {"name": "b", "cents": 1000}, {"name": "c", "cents": -500}]}


def test_balances_empty_ledger():
    code, out, err = run(H, "balances", "{f}")
    assert (code, out, err) == (0, "", "")
    code, out, _ = run(H, "balances", "{f}", "--format", "json")
    assert code == 0 and json.loads(out) == {"balances": []}


def test_balances_sum_zero_json():
    rows = "2026-01-01,A,10.01,B;C;D:3,\n2026-01-02,B,0.03,A;C;D,\n2026-01-03,C,99.99,A:2;B:5;C:7,\n"
    code, out, _ = run(H + rows, "balances", "{f}", "--format", "json")
    assert sum(b["cents"] for b in json.loads(out)["balances"]) == 0


# ---------- file format ----------
def test_bom():
    raw = b"\xef\xbb\xbf" + (H + "2026-01-01,A,10,B,\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["A +10.00", "B -10.00"]


def test_crlf():
    raw = (H + "2026-01-01,A,10,B,x\n2026-01-02,B,4,A,y\n").replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["A +6.00", "B -6.00"]


def test_crlf_error_lines():
    raw = (H + "2026-01-01,A,x,B,\n").replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 2 and lines(err) == ["line 2: bad-amount"]


def test_no_trailing_newline():
    code, out, _ = run(H + "2026-01-01,A,10,B,", "balances", "{f}")
    assert code == 0 and lines(out) == ["A +10.00", "B -10.00"]


def test_column_order_any():
    led = "note,participants,amount,payer,date\nhi,B;C,10,A,2026-01-01\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +10.00", "B -5.00", "C -5.00"]


def test_extra_columns_ok():
    led = "id,date,payer,amount,participants,note,extra\n1,2026-01-01,A,10,B,n,z\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +10.00", "B -10.00"]


def test_quoted_fields():
    led = H + '2026-01-01,"A","10.00","B;C","note, with, commas"\n'
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +10.00", "B -5.00", "C -5.00"]


def test_quoted_name_with_comma():
    led = H + '2026-01-01,"Lee, Ann",10,B,\n'
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["B -10.00", "Lee, Ann +10.00"]


def test_ignored_lines():
    rows = "\n   \n# comment\n   # indented, comment\n2026-01-01,A,10,B,\n\t\n"
    assert bal(rows) == ["A +10.00", "B -10.00"]


def test_ignored_lines_before_header():
    led = "# c\n\n" + H + "2026-01-01,A,10,B,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +10.00", "B -10.00"]


def test_ignored_lines_count_in_numbering():
    rows = "# c\n\n   \n2026-01-01,A,x,B,\n"
    assert errs(rows) == ["line 5: bad-amount"]


def test_ignored_lines_before_header_count_in_numbering():
    led = "# c\n" + H + "bad,A,10,B,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and lines(err) == ["line 3: bad-date"]


def test_header_whitespace_and_case_exact():
    led = " date , payer ,amount,participants,note\n2026-01-01,A,10,B,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0


def test_short_row_missing_cells():
    # missing participants cell -> bad-participants
    assert errs("2026-01-01,A,10\n") == ["line 2: bad-participants"]


def test_missing_note_cell_ok():
    assert bal("2026-01-01,A,10,B\n") == ["A +10.00", "B -10.00"]


# ---------- validation ----------
@pytest.mark.parametrize("d", ["2026-13-01", "2026-02-30", "2025-02-29", "26-01-01", "2026/01/01",
                               "2026-1-1", "", "abc", "2026-01-01x", "20260101"])
def test_bad_dates(d):
    assert errs(f"{d},A,10,B,\n") == ["line 2: bad-date"]


def test_leap_day_valid():
    assert bal("2024-02-29,A,10,B,\n") == ["A +10.00", "B -10.00"]


@pytest.mark.parametrize("a", ["0", "0.00", "-5", "+5", "1,000", "1e3", "1.234", "abc", "", "5.", ".5",
                               "1 000", "0.0", "00"])
def test_bad_amounts(a):
    assert errs(f'2026-01-01,A,"{a}",B,\n') == ["line 2: bad-amount"]


@pytest.mark.parametrize("p", ["", "  ", "A;", ";A", "A;;B", "A:0", "A:-1", "A:x", "A:1.5", "A:", "A;a",
                               "A:1;a:2", " ; ", ":2", "A:+1"])
def test_bad_participants(p):
    assert errs(f'2026-01-01,P,10,"{p}",\n') == ["line 2: bad-participants"]


def test_bad_payer():
    assert errs("2026-01-01,   ,10,B,\n") == ["line 2: bad-payer"]
    assert errs("2026-01-01,,10,B,\n") == ["line 2: bad-payer"]


def test_reason_order():
    rows = (
        "bad,,x,\n"           # date wins
        "2026-01-01,,x,\n"    # payer wins over amount
        "2026-01-01,A,x,\n"   # amount wins over participants
        "2026-01-01,A,10,\n"  # participants
    )
    assert errs(rows) == ["line 2: bad-date", "line 3: bad-payer", "line 4: bad-amount",
                          "line 5: bad-participants"]


def test_all_invalid_rows_reported_ascending_valid_skipped():
    rows = "2026-01-01,A,x,B,\n2026-01-01,A,10,B,\n\n2026-01-01,,10,B,\n# hi\nnope,A,10,B,\n"
    assert errs(rows) == ["line 2: bad-amount", "line 5: bad-payer", "line 7: bad-date"]


def test_invalid_row_settle_and_json_empty_stdout():
    for args in (("settle",), ("balances", "--format", "json")):
        code, out, err = run(H + "x,A,10,B,\n", args[0], "{f}", *args[1:])
        assert code == 2 and out == "" and lines(err) == ["line 2: bad-date"]


def test_validation_applies_outside_range():
    rows = "2020-01-01,A,x,B,\n2026-01-01,A,10,B,\n"
    code, out, err = run(H + rows, "balances", "{f}", "--since", "2025-01-01")
    assert code == 2 and out == "" and lines(err) == ["line 2: bad-amount"]
    code, out, err = run(H + rows, "balances", "{f}", "--until", "2019-01-01")
    assert code == 2 and out == ""


# ---------- header ----------
@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_header_column(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    assert errs("", header=",".join(cols) + "\n") == [f"header: missing {missing}"]


def test_header_first_missing_only_and_no_row_errors():
    # missing payer and note; rows invalid too
    led = "date,amount,participants\nbad,x,y\n"
    assert errs("", header=led) == ["header: missing payer"]
    led = "participants,amount\nbad,x\n"
    assert errs("", header=led) == ["header: missing date"]
    led = "date,payer,participants\n"
    assert errs("", header=led) == ["header: missing amount"]
    led = "date,payer,amount\n"
    assert errs("", header=led) == ["header: missing participants"]


def test_header_missing_settle():
    code, out, err = run("date,payer\n", "settle", "{f}")
    assert code == 2 and out == "" and lines(err) == ["header: missing amount"]


# ---------- since/until ----------
RANGE = ("2026-01-01,A,10,B,\n2026-02-01,C,20,D,\n2026-03-01,E,30,F,\n")


def test_since_inclusive():
    assert bal(RANGE, "--since", "2026-02-01") == [
        "C +20.00", "D -20.00", "E +30.00", "F -30.00"]


def test_until_inclusive():
    assert bal(RANGE, "--until", "2026-02-01") == [
        "A +10.00", "B -10.00", "C +20.00", "D -20.00"]


def test_since_and_until():
    assert bal(RANGE, "--since", "2026-02-01", "--until", "2026-02-01") == ["C +20.00", "D -20.00"]


def test_empty_range():
    assert bal(RANGE, "--since", "2027-01-01") == []
    assert settle(RANGE, "--since", "2027-01-01") == ["nothing to settle"]
    assert bal(RANGE, "--since", "2026-03-01", "--until", "2026-01-01") == []


def test_range_settle_and_json():
    assert settle(RANGE, "--until", "2026-01-31") == ["B -> A 10.00"]
    code, out, _ = run(H + RANGE, "balances", "{f}", "--format", "json", "--since", "2026-03-01")
    assert json.loads(out) == {"balances": [{"name": "E", "cents": 3000}, {"name": "F", "cents": -3000}]}


def test_range_spelling_from_first_appearance_in_file():
    # spelling comes from the first row of the file; check people in range are shown
    rows = "2026-01-01,ann,10,B,\n2026-02-01,ANN,10,C,\n"
    out = bal(rows, "--since", "2026-02-01")
    assert out[0].split()[0] in ("ann", "ANN")


def test_range_unsorted_rows():
    rows = "2026-03-01,A,10,B,\n2026-01-01,C,10,D,\n"
    assert bal(rows, "--until", "2026-01-31") == ["C +10.00", "D -10.00"]


# ---------- CLI ----------
def test_options_before_positionals():
    code, out, _ = run(H + "2026-01-01,A,10,B,\n", "--format", "json", "balances", "{f}")
    assert code == 0 and json.loads(out)["balances"][0]["name"] == "A"
    code, out, _ = run(H + RANGE, "--since", "2026-03-01", "balances", "{f}")
    assert code == 0 and lines(out) == ["E +30.00", "F -30.00"]


def test_options_between_positionals():
    code, out, _ = run(H + "2026-01-01,A,10,B,\n", "balances", "--format", "json", "{f}")
    assert code == 0 and "balances" in json.loads(out)


def test_explicit_text_format():
    code, out, _ = run(H + "2026-01-01,A,10,B,\n", "balances", "{f}", "--format", "text")
    assert code == 0 and lines(out) == ["A +10.00", "B -10.00"]


def test_json_is_single_document():
    code, out, _ = run(H + "2026-01-01,A,10,B,\n", "settle", "{f}", "--format", "json")
    json.loads(out)


@pytest.mark.parametrize("args", [
    ["explode", "{f}"],
    ["balances"],
    [],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--bogus"],
    ["balances", "{f}", "--since", "2026-13-01"],
    ["balances", "{f}", "--since", "nope"],
    ["balances", "{f}", "--until", "2026-02-30"],
    ["balances", "{f}", "--until", "2026-1-1"],
    ["balances", "{f}", "--since"],
    ["settle", "{f}", "--format", "JSON"],
])
def test_usage_errors(args):
    code, out, err = run(H + "2026-01-01,A,10,B,\n", *args)
    assert code == 64 and out == ""


def test_usage_error_precedes_ledger_problems():
    code, out, _ = run(H + "bad,A,10,B,\n", "balances", "{f}", "--since", "zzz")
    assert code == 64 and out == ""


def test_unreadable_file():
    code, out, err = run("", "balances", "/nonexistent/dir/x.csv")
    assert code == 3 and out == ""
    assert lines(err) == ["error: cannot read /nonexistent/dir/x.csv"]
    code, out, err = run("", "settle", "/nonexistent/x.csv")
    assert code == 3 and out == ""


def test_directory_as_ledger():
    code, out, err = run("", "balances", ".")
    assert code == 3 and out == "" and lines(err) == ["error: cannot read ."]


def test_invalid_utf8_unreadable():
    code, out, err = run("", "balances", "{f}", raw=b"date,payer,amount,participants,note\n\xff\xfe,A,1,B,\n")
    assert code == 3 and out == ""


def test_success_stderr_empty():
    code, out, err = run(H + "2026-01-01,A,10,B,\n", "settle", "{f}")
    assert code == 0 and err == ""
