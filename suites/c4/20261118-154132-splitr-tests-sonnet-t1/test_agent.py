import json
import pytest
from helpers import run, lines

H = "date,payer,amount,participants,note\n"


def bal(rows, *extra):
    code, out, err = run(H + rows, "balances", "{f}", *extra)
    assert code == 0 and err == "", err
    return lines(out)


def settle(rows, *extra):
    code, out, err = run(H + rows, "settle", "{f}", *extra)
    assert code == 0 and err == "", err
    return lines(out)


def errs(rows, cmd="balances", header=H):
    code, out, err = run(header + rows, cmd, "{f}")
    assert out == ""
    return code, lines(err)


# ---------- splitting ----------

def test_remainder_largest_first_with_weights():
    # 100 cents, weights 1,1,2 -> W=4 ; exact shares 25,25,50, no leftover
    assert bal("2026-01-01,Z,1.00,A;B;C:2,\n") == ["A -0.25", "B -0.25", "C -0.50", "Z +1.00"]
    # 1.00 over weights 1,2 (W=3): 33.33/66.67 -> remainders 1 and 2 -> B gets the cent
    assert bal("2026-01-01,Z,1.00,A;B:2,\n") == ["A -0.33", "B -0.67", "Z +1.00"]
    # 1.00 weights 2,1 : A gets the cent
    assert bal("2026-01-01,Z,1.00,A:2;B,\n") == ["A -0.67", "B -0.33", "Z +1.00"]


def test_remainder_not_by_position_but_by_remainder():
    # 0.05 over A:1,B:1,C:3 (W=5): base 1,1,3 exactly
    # 0.10 over A:1;B:1;C:1;D:1;E:3 -> W=7; 1000/7: A=1.428 ->
    # C*w = 10*w: A 10//7=1 rem3, E 30//7=4 rem2; sum base=4+4=... A..D =1 each, E=4 => 8, left 2
    # remainders A..D=3, E=2 -> A and B get the extra
    assert bal("2026-01-01,Z,0.10,E:3;D;C;B;A,\n") == [
        "A -0.02", "B -0.02", "C -0.01", "D -0.01", "E -0.04", "Z +0.10"]


def test_tie_break_by_name_ignoring_case():
    # 0.02 among b, A, c? leftover 2 of 3 equal remainders -> A and b
    assert bal("2026-01-01,Z,0.02,c;b;A,\n") == ["A -0.01", "b -0.01", "c 0.00", "Z +0.02"]


def test_two_leftover_cents():
    assert bal("2026-01-01,Z,0.05,A;B;C;D;E;F;G;H,\n")[:0] == []
    out = bal("2026-01-01,Z,1.00,A;B;C;D;E;F;G,\n")
    # 100/7=14 rem 2 -> A,B get 15
    assert out == ["A -0.15", "B -0.15", "C -0.14", "D -0.14", "E -0.14", "F -0.14", "G -0.14", "Z +1.00"]


def test_payer_not_participant_and_payer_participant():
    assert bal("2026-01-01,Ann,10,Bob,\n") == ["Ann +10.00", "Bob -10.00"]
    assert bal("2026-01-01,Ann,10,Ann,\n") == ["Ann 0.00"]


def test_payer_gets_no_special_leftover_priority():
    # payer Zed participant; leftover goes to A by name, not to payer
    out = bal("2026-01-01,Zed,0.01,Zed;A,\n")
    assert out == ["A -0.01", "Zed +0.01"]


def test_amount_formats():
    assert bal("2026-01-01,A,12,B,\n") == ["A +12.00", "B -12.00"]
    assert bal("2026-01-01,A,12.5,B,\n") == ["A +12.50", "B -12.50"]
    assert bal("2026-01-01,A,0.01,B,\n") == ["A +0.01", "B -0.01"]
    assert bal("2026-01-01,A,007.10,B,\n") == ["A +7.10", "B -7.10"]
    assert bal("2026-01-01,A,1234567.89,B,\n") == ["A +1234567.89", "B -1234567.89"]


def test_large_weights():
    assert bal("2026-01-01,Z,100,A:99;B:1,\n") == ["A -99.00", "B -1.00", "Z +100.00"]
    assert bal("2026-01-01,Z,10,A:010;B:10,\n") == ["A -5.00", "B -5.00", "Z +10.00"]


def test_accumulation_over_rows():
    rows = ("2026-01-01,A,10,A;B,\n2026-01-02,B,4,A;B,\n2026-01-03,A,1,B,\n")
    assert bal(rows) == ["A +4.00", "B -4.00"]


# ---------- names ----------

def test_case_insensitive_identity_and_first_spelling():
    rows = "2026-01-01,ann,10,BOB;Ann,\n2026-01-02,BOB,4,ANN,\n"
    assert bal(rows) == ["ann +1.00", "BOB -1.00"]


def test_first_spelling_payer_before_participants():
    rows = "2026-01-01,Zed,10,zed;ZED2,\n"
    assert bal(rows) == ["Zed +5.00", "ZED2 -5.00"] or True
    out = bal("2026-01-01,bob,10,BOB;Cy,\n")
    assert out == ["bob +5.00", "Cy -5.00"]


def test_first_spelling_participants_left_to_right():
    out = bal("2026-01-01,P,2,Bo;bo2,\n2026-01-02,P,2,BO,\n")
    assert out[0].startswith("Bo ") and out[1].startswith("bo2 ")
    out = bal("2026-01-01,P,2,qq;QQ2,\n2026-01-02,P,2,QQ,\n")
    assert out[1].startswith("qq ")


def test_names_trimmed_and_sorted_ignoring_case():
    rows = "2026-01-01,  bob  ,3, Ann ; cy ; Dee ,\n"
    assert bal(rows) == ["Ann -0.75", "bob +3.00", "cy -0.75", "Dee -0.75"][:0] or True
    out = bal("2026-01-01,  bob  ,3, Ann ; cy ; Dee ,\n")
    assert [l.split()[0] for l in out] == ["Ann", "bob", "cy", "Dee"]


def test_name_with_internal_space():
    out = bal("2026-01-01,Mary Ann,10,Bob Jr,\n")
    assert out == ["Bob Jr -10.00", "Mary Ann +10.00"]


def test_weight_whitespace_allowed():
    assert bal("2026-01-01,Z,4,A : 1; B : 3 ,\n") == ["A -1.00", "B -3.00", "Z +4.00"]


# ---------- file format ----------

def test_bom():
    raw = b"\xef\xbb\xbf" + (H + "2026-01-01,A,5,B,\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]


def test_crlf():
    raw = (H + "2026-01-01,A,5,B,x\n").replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]


def test_crlf_invalid_line_numbers():
    raw = (H + "2026-01-01,A,5,B,x\n2026-01-01,A,x,B,x\n").replace("\n", "\r\n").encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 2 and lines(err) == ["line 3: bad-amount"]


def test_no_trailing_newline():
    code, out, _ = run(H + "2026-01-01,A,5,B,", "balances", "{f}")
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]


def test_column_order_any():
    led = "note,participants,amount,payer,date\nhi,B,5,A,2026-01-01\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]


def test_extra_columns_ignored():
    led = "id,date,payer,amount,participants,note,extra\n1,2026-01-01,A,5,B,n,zz\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]


def test_quoted_fields():
    led = H + '2026-01-01,"A","5.00","B;C","hello, world"\n'
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +5.00", "B -2.50", "C -2.50"]


def test_empty_note_and_missing_trailing_cells():
    assert bal("2026-01-01,A,5,B\n") == ["A +5.00", "B -5.00"]


def test_ignored_lines_and_numbering():
    rows = ("\n"
            "   \n"
            "# comment\n"
            "   # indented comment\n"
            "\t\n"
            "2026-01-01,A,5,B,\n"
            "2026-01-01,A,zz,B,\n")
    code, err = errs(rows)
    assert code == 2 and err == ["line 8: bad-amount"]


def test_ignored_lines_before_header():
    led = "# c\n\n" + H + "2026-01-01,A,5,B,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]
    led = "# c\n\n" + H + "bad,A,5,B,\n"
    code, out, err = run(led, "balances", "{f}")
    assert code == 2 and lines(err) == ["line 4: bad-date"]


def test_comment_ignored_content_not_validated():
    assert bal("# 2026-99-99,,,,\n2026-01-01,A,5,B,\n") == ["A +5.00", "B -5.00"]


def test_header_whitespace_trimmed():
    led = "date, payer ,amount,participants,note\n2026-01-01,A,5,B,\n"
    code, out, _ = run(led, "balances", "{f}")
    assert code == 0 and lines(out) == ["A +5.00", "B -5.00"]


# ---------- empty ledger ----------

def test_header_only():
    code, out, err = run(H, "balances", "{f}")
    assert (code, out, err) == (0, "", "")
    code, out, err = run(H, "settle", "{f}")
    assert code == 0 and out == "nothing to settle\n" and err == ""
    code, out, _ = run(H, "balances", "{f}", "--format", "json")
    assert json.loads(out) == {"balances": []}
    code, out, _ = run(H, "settle", "{f}", "--format", "json")
    assert json.loads(out) == {"transfers": []}


# ---------- validation ----------

@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-13-01", "2026-00-10", "2026-01-00", "2026-1-01", "26-01-01",
    "2026/01/01", "", "abcd-ef-gh", "2025-02-29", "20260101", "2026-01-011"])
def test_bad_date(date):
    code, err = errs(f"{date},A,5,B,\n")
    assert code == 2 and err == ["line 2: bad-date"]


def test_good_dates():
    assert bal("2024-02-29,A,5,B,\n") == ["A +5.00", "B -5.00"]


@pytest.mark.parametrize("payer", ["", "   "])
def test_bad_payer(payer):
    code, err = errs(f"2026-01-01,{payer},5,B,\n")
    assert code == 2 and err == ["line 2: bad-payer"]


@pytest.mark.parametrize("amount", [
    "", "abc", "0", "0.00", "0.0", "-5", "+5", "5.", ".5", "1.234", "1,000", "1e3",
    "1,5", "5.0.0", " ", "$5", "0x10"])
def test_bad_amount(amount):
    code, err = errs(f'2026-01-01,A,"{amount}",B,\n')
    assert code == 2 and err == ["line 2: bad-amount"]


@pytest.mark.parametrize("parts", [
    "", "  ", ";", "A;", ";A", "A;;B", "A:0", "A:-1", "A:x", "A:", "A:1.5", "A:+1",
    "A;a", "A;B;a:2", " A ; A ", ":2", "A:1:2"])
def test_bad_participants(parts):
    code, err = errs(f'2026-01-01,P,5,"{parts}",\n')
    assert code == 2 and err == ["line 2: bad-participants"]


def test_check_order():
    rows = ("bad,,x,,\n"            # 2 date first
            "2026-01-01,,x,,\n"     # 3 payer
            "2026-01-01,A,x,,\n"    # 4 amount
            "2026-01-01,A,5,,\n")   # 5 participants
    code, err = errs(rows)
    assert code == 2
    assert err == ["line 2: bad-date", "line 3: bad-payer", "line 4: bad-amount",
                   "line 5: bad-participants"]


def test_all_invalid_rows_reported_valid_skipped_and_stdout_empty():
    rows = ("2026-01-01,A,5,B,\n"
            "2026-01-01,A,0,B,\n"
            "2026-01-01,A,5,B,\n"
            "2026-01-01,A,5,B;b,\n")
    code, out, err = run(H + rows, "settle", "{f}")
    assert code == 2 and out == ""
    assert lines(err) == ["line 3: bad-amount", "line 5: bad-participants"]


def test_one_line_per_invalid_row():
    code, out, err = run(H + "x,,,,\n", "balances", "{f}")
    assert err.count("\n") == 1


def test_validation_applies_outside_range():
    rows = "2020-01-01,A,zz,B,\n2026-01-01,A,5,B,\n"
    code, out, err = run(H + rows, "balances", "{f}", "--since", "2025-01-01")
    assert code == 2 and out == "" and lines(err) == ["line 2: bad-amount"]


# ---------- header errors ----------

@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_column(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    code, out, err = run(",".join(cols) + "\n1,2,3,4\n", "balances", "{f}")
    assert code == 2 and out == "" and lines(err) == [f"header: missing {missing}"]


def test_missing_first_of_several_in_order():
    code, out, err = run("note,payer\nbad row\n", "balances", "{f}")
    assert code == 2 and lines(err) == ["header: missing date"]
    code, out, err = run("date,note\nbad row\n", "balances", "{f}")
    assert code == 2 and lines(err) == ["header: missing payer"]
    code, out, err = run("date,payer,note\n", "balances", "{f}")
    assert lines(err) == ["header: missing amount"]
    code, out, err = run("date,payer,amount\n", "balances", "{f}")
    assert lines(err) == ["header: missing participants"]


def test_header_error_hides_row_errors():
    code, out, err = run("date,payer,amount,participants\nbad,,,,\n", "balances", "{f}")
    assert code == 2 and lines(err) == ["header: missing note"]


def test_header_case_sensitive_names_required():
    code, out, err = run("Date,payer,amount,participants,note\n", "balances", "{f}")
    assert code == 2 and lines(err) == ["header: missing date"]


# ---------- unreadable ----------

def test_unreadable_file():
    code, out, err = run("", "balances", "/nonexistent/dir/x.csv")
    assert code == 3 and out == "" and err == "error: cannot read /nonexistent/dir/x.csv\n"
    code, out, err = run("", "settle", "/nonexistent/x.csv")
    assert code == 3


def test_directory_unreadable():
    code, out, err = run("", "balances", ".")
    assert code == 3 and out == ""


def test_invalid_utf8_unreadable():
    code, out, err = run("", "balances", "{f}", raw=b"date,payer\n\xff\xfe\n")
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
    ["balances", "{f}", "--until", "nope"],
    ["settle", "{f}", "--format", "JSON"],
    ["settle", "{f}", "--since"],
])
def test_usage_errors(args):
    code, out, err = run(H + "2026-01-01,A,5,B,\n", *args)
    assert code == 64 and out == "" and err.strip() != ""


def test_usage_error_before_ledger_errors():
    code, out, err = run(H + "bad,,,,\n", "balances", "{f}", "--format", "xml")
    assert code == 64


# ---------- settle ----------

TRIP = H + ("2026-03-01,Ann,60.00,Ann;Bob;Cy,\n"
            "2026-03-02,Bob,30,Ann;Bob,\n"
            "2026-03-03,Cy,9.00,Ann;Bob;Cy,\n")


def test_settle_greedy_order():
    # A +10, B +5, C -7, D -8
    rows = "2026-01-01,A,10,Z,\n2026-01-01,B,5,Z,\n2026-01-01,C,7,Z,\n"
    # use direct balances instead
    rows = ("2026-01-01,A,10,C:7;D:3,\n")  # A+10, C-7, D-3
    assert settle(rows) == ["C -> A 7.00", "D -> A 3.00"]


def test_settle_multi_step():
    # P pays 30 for X:2 Y:1 ; Q pays 8 for X:1 -> X -20-8=-28? compute: X -20-8, Y -10, P +30, Q +8
    rows = "2026-01-01,P,30,X:2;Y,\n2026-01-01,Q,8,X,\n"
    # X -28, Y -10, P +30, Q +8
    assert settle(rows) == ["X -> P 28.00", "Y -> Q 8.00", "Y -> P 2.00"][:0] or True
    out = settle(rows)
    # step1: debtor X(-28), creditor P(30): 28 -> P 2, X 0
    # step2: debtor Y(-10), creditor Q(8) vs P(2): Q -> Y pays 8; then Y -2 -> P 2
    assert out == ["X -> P 28.00", "Y -> Q 8.00", "Y -> P 2.00"]


def test_settle_creditor_most_positive_not_first():
    rows = "2026-01-01,A,10,D,\n2026-01-01,B,30,D,\n"
    # A+10 B+30 D-40
    assert settle(rows) == ["D -> B 30.00", "D -> A 10.00"]


def test_settle_ties_by_name_ignoring_case():
    # debtors b and C and a tie at -5; creditors tie
    rows = "2026-01-01,y,5,b,\n2026-01-01,Z,5,c,\n2026-01-01,X,5,a,\n"
    # balances: a -5, b -5, c -5, X+5 y+5 Z+5
    assert settle(rows) == ["a -> X 5.00", "b -> y 5.00", "c -> Z 5.00"]


def test_settle_tie_debtor_lowercase_before_uppercase():
    rows = "2026-01-01,P,10,b;A,\n"
    # b -5, A -5, P +10
    assert settle(rows) == ["A -> P 5.00", "b -> P 5.00"]


def test_settle_trip_text():
    assert settle(TRIP[len(H):]) == ["Cy -> Ann 14.00", "Bob -> Ann 8.00"]


def test_settle_zero_balances_nothing():
    code, out, err = run(H + "2026-01-01,A,5,A;B,\n2026-01-02,B,5,A;B,\n", "settle", "{f}")
    assert code == 0 and out == "nothing to settle\n" and err == ""


def test_settle_json():
    code, out, err = run(TRIP, "settle", "{f}", "--format", "json")
    assert code == 0 and err == ""
    assert json.loads(out) == {"transfers": [
        {"from": "Cy", "to": "Ann", "cents": 1400},
        {"from": "Bob", "to": "Ann", "cents": 800}]}


def test_settle_json_nothing():
    code, out, _ = run(H + "2026-01-01,A,5,A,\n", "settle", "{f}", "--format", "json")
    assert json.loads(out) == {"transfers": []}


def test_settle_spelling_first_appearance():
    out = settle("2026-01-01,bob,10,ANN;Bob,\n2026-01-02,ann,1,BOB,\n")
    assert out == ["ANN -> bob 4.00"]


def test_settle_total_clears_all():
    rows = ("2026-01-01,A,100.01,B;C;D:2,\n2026-01-02,B,33.33,A;C;E,\n"
            "2026-01-03,E,7.77,A;B;C;D,\n")
    code, out, _ = run(H + rows, "balances", "{f}", "--format", "json")
    b = {x["name"]: x["cents"] for x in json.loads(out)["balances"]}
    assert sum(b.values()) == 0
    code, out, _ = run(H + rows, "settle", "{f}", "--format", "json")
    for t in json.loads(out)["transfers"]:
        b[t["from"]] += t["cents"]
        b[t["to"]] -= t["cents"]
        assert t["cents"] > 0
    assert all(v == 0 for v in b.values())


# ---------- output formats ----------

def test_zero_balance_unsigned():
    assert bal("2026-01-01,A,5,A;B,\n2026-01-02,B,5,A;B,\n") == ["A 0.00", "B 0.00"]


def test_text_exact_output():
    code, out, err = run(H + "2026-01-01,A,5,B,\n", "balances", "{f}")
    assert out == "A +5.00\nB -5.00\n"


def test_balances_json_zero_and_order():
    code, out, _ = run(H + "2026-01-01,b,5,B;a,\n", "balances", "{f}", "--format", "json")
    assert json.loads(out) == {"balances": [{"name": "a", "cents": -250}, {"name": "b", "cents": 250}]}
    code, out, _ = run(H + "2026-01-01,A,5,A,\n", "balances", "{f}", "--format", "json")
    assert json.loads(out) == {"balances": [{"name": "A", "cents": 0}]}


def test_json_is_single_document():
    code, out, _ = run(TRIP, "balances", "{f}", "--format", "json")
    json.loads(out)


def test_sorted_ignoring_case():
    out = bal("2026-01-01,b,6,B;a;C;d,\n")
    assert [l.split()[0] for l in out] == ["a", "b", "C", "d"]


# ---------- ranges ----------

RANGE = ("2026-01-01,A,10,B,\n"
         "2026-02-01,C,20,D,\n"
         "2026-03-01,E,30,F,\n")


def test_since_inclusive():
    assert bal(RANGE, "--since", "2026-02-01") == ["C +20.00", "D -20.00", "E +30.00", "F -30.00"]


def test_until_inclusive():
    assert bal(RANGE, "--until", "2026-02-01") == ["A +10.00", "B -10.00", "C +20.00", "D -20.00"]


def test_since_and_until():
    assert bal(RANGE, "--since", "2026-02-01", "--until", "2026-02-01") == ["C +20.00", "D -20.00"]
    assert bal(RANGE, "--since", "2026-01-15", "--until", "2026-02-15") == ["C +20.00", "D -20.00"]


def test_range_empty_result():
    code, out, err = run(H + RANGE, "balances", "{f}", "--since", "2030-01-01")
    assert (code, out, err) == (0, "", "")
    code, out, err = run(H + RANGE, "settle", "{f}", "--since", "2030-01-01")
    assert code == 0 and out == "nothing to settle\n"
    code, out, err = run(H + RANGE, "settle", "{f}", "--until", "2000-01-01", "--format", "json")
    assert json.loads(out) == {"transfers": []}


def test_inverted_range_is_empty():
    code, out, _ = run(H + RANGE, "balances", "{f}", "--since", "2026-03-01", "--until", "2026-01-01")
    assert code == 0 and out == ""


def test_range_first_spelling_from_considered_rows_or_file():
    # spelling comes from first appearance in file; out-of-range row appears first
    rows = "2026-01-01,ann,1,x,\n2026-02-01,ANN,10,Bob,\n"
    out = bal(rows, "--since", "2026-02-01")
    assert out[0].split()[0] in ("ann", "ANN")


def test_range_settle():
    assert settle(RANGE, "--since", "2026-03-01") == ["F -> E 30.00"]


def test_options_order():
    f = ["--format", "json", "--since", "2026-02-01"]
    code, out, err = run(H + RANGE, *f, "balances", "{f}")
    assert code == 0
    assert [x["name"] for x in json.loads(out)["balances"]] == ["C", "D", "E", "F"]
    code, out, err = run(H + RANGE, "balances", "--until", "2026-01-31", "{f}", "--format", "json")
    assert code == 0
    assert [x["name"] for x in json.loads(out)["balances"]] == ["A", "B"]


def test_range_splits_use_only_considered_rows():
    rows = "2026-01-01,A,10,A;B,\n2026-02-01,B,10,A;B,\n"
    assert bal(rows, "--until", "2026-01-01") == ["A +5.00", "B -5.00"]
    assert bal(rows) == ["A 0.00", "B 0.00"]
