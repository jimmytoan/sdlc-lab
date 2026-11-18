"""Regression suite for splitr, written from SPEC.md and driven as a black box."""
import json
import os
import random
import subprocess
import sys

import pytest

from helpers import APP_DIR, run, lines

HEADER = "date,payer,amount,participants,note\n"


# ---------- helpers (local to this file) ----------

def ledger(*rows, header=HEADER):
    return header + "".join(r + "\n" for r in rows)


def row(date="2026-03-01", payer="Ann", amount="10.00", parts="Bob", note=""):
    return f"{date},{payer},{amount},{parts},{note}"


def ok(text, *args):
    """Run and assert success with empty stderr. Returns stdout."""
    code, out, err = run(text, *args)
    assert code == 0, (code, out, err)
    assert err == ""
    return out


def fail(text, *args, code=2):
    """Run and assert failure with the given exit code and empty stdout."""
    c, out, err = run(text, *args)
    assert c == code, (c, out, err)
    assert out == ""
    return err


def run_path(*argv):
    """Run splitr with arbitrary argv (no temp-file substitution)."""
    p = subprocess.run([sys.executable, os.path.join(APP_DIR, "splitr.py"), *argv],
                       capture_output=True, text=True, timeout=20, cwd=APP_DIR)
    return p.returncode, p.stdout, p.stderr


def bal_json(text, *extra):
    out = ok(text, "balances", "{f}", "--format", "json", *extra)
    return json.loads(out)["balances"]


def settle_json(text, *extra):
    out = ok(text, "settle", "{f}", "--format", "json", *extra)
    return json.loads(out)["transfers"]


def balances_map(text, *extra):
    return {b["name"]: b["cents"] for b in bal_json(text, *extra)}


# ---------- file format ----------

def test_bom_is_accepted():
    raw = b"\xef\xbb\xbf" + ledger(row(payer="Ann", amount="5", parts="Bob")).encode()
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_crlf_line_endings_are_accepted():
    text = ledger(row(payer="Ann", amount="5", parts="Bob")).replace("\n", "\r\n")
    code, out, err = run(text, "balances", "{f}")
    assert code == 0 and err == ""
    assert lines(out) == ["Ann +5.00", "Bob -5.00"]


def test_columns_in_any_order():
    text = ("participants,note,amount,payer,date\n"
            "Bob;Cy,lunch,9.00,Ann,2026-03-01\n")
    assert lines(ok(text, "balances", "{f}")) == ["Ann +9.00", "Bob -4.50", "Cy -4.50"]


def test_quoted_note_may_contain_commas_and_quotes():
    text = ledger('2026-03-01,Ann,10.00,Bob,"pizza, beer ""large"""')
    assert lines(ok(text, "balances", "{f}")) == ["Ann +10.00", "Bob -10.00"]


def test_empty_note_is_valid():
    text = ledger("2026-03-01,Ann,10.00,Bob,")
    assert lines(ok(text, "balances", "{f}")) == ["Ann +10.00", "Bob -10.00"]


def test_hash_inside_fields_is_not_a_comment():
    text = ledger(row(payer="Ann", amount="4", parts="Bob", note="# not a comment"))
    assert lines(ok(text, "balances", "{f}")) == ["Ann +4.00", "Bob -4.00"]


def test_comment_and_blank_lines_are_ignored():
    text = ("# leading comment\n"
            "\n"
            "   \t \n"
            "   # indented comment\n"
            + HEADER
            + "# between rows\n"
            + row(payer="Ann", amount="6", parts="Bob") + "\n"
            + "    \n")
    assert lines(ok(text, "balances", "{f}")) == ["Ann +6.00", "Bob -6.00"]


def test_header_only_is_valid_and_empty():
    text = HEADER
    assert ok(text, "balances", "{f}") == ""
    assert lines(ok(text, "settle", "{f}")) == ["nothing to settle"]


def test_empty_ledger_with_only_comments_after_header_is_valid():
    text = HEADER + "# nothing here\n\n"
    assert ok(text, "balances", "{f}") == ""
    assert lines(ok(text, "settle", "{f}")) == ["nothing to settle"]


def test_header_only_json_outputs():
    assert bal_json(HEADER) == []
    assert settle_json(HEADER) == []


# ---------- header validation ----------

@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_required_column_reports_that_column(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    text = ",".join(cols) + "\n" + ",".join(["x"] * len(cols)) + "\n"
    err = fail(text, "balances", "{f}")
    assert err.strip() == f"header: missing {missing}"


def test_only_first_missing_column_is_reported():
    text = "date,amount,participants\n2026-03-01,1,Bob\n"
    err = fail(text, "balances", "{f}")
    assert err.strip() == "header: missing payer"
    assert len(err.strip().split("\n")) == 1


def test_missing_note_with_other_errors_only_reports_header():
    text = "date,payer,amount,participants\n2026-03-01,,abc,\n"
    err = fail(text, "balances", "{f}")
    assert err.strip() == "header: missing note"


# ---------- row validation ----------

@pytest.mark.parametrize("bad", ["2026-02-30", "2026-13-01", "2026/03/01", "03-01-2026",
                                 "2026-3-1", "2026-03-01x", "yesterday", "", "2026-00-10"])
def test_bad_date(bad):
    err = fail(ledger(row(date=bad)), "balances", "{f}")
    assert err.strip() == "line 2: bad-date"


def test_leap_day_is_a_real_date():
    text = ledger(row(date="2024-02-29", payer="Ann", amount="2", parts="Bob"))
    assert lines(ok(text, "balances", "{f}")) == ["Ann +2.00", "Bob -2.00"]


@pytest.mark.parametrize("payer", ["", "   "])
def test_empty_payer(payer):
    err = fail(ledger(row(payer=payer)), "balances", "{f}")
    assert err.strip() == "line 2: bad-payer"


@pytest.mark.parametrize("good", ["12", "12.5", "12.50", "0.01", "1234567.89", "0.10"])
def test_valid_amounts(good):
    text = ledger(row(payer="Ann", amount=good, parts="Bob"))
    cents = round(float(good) * 100)
    assert balances_map(text) == {"Ann": cents, "Bob": -cents}


@pytest.mark.parametrize("bad", ["0", "0.00", "-5", "+5", "\"1,000\"", "1e3", "12.505",
                                 "", "abc", ".5", "5.", "1 000", "12.5.0", "$12", "\"12,50\""])
def test_invalid_amounts(bad):
    err = fail(ledger(row(amount=bad)), "balances", "{f}")
    assert err.strip() == "line 2: bad-amount"


def test_amount_with_surrounding_spaces_is_trimmed():
    text = ledger(row(payer="Ann", amount=" 7.00 ", parts="Bob"))
    assert balances_map(text) == {"Ann": 700, "Bob": -700}


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "Ann;",
    ";Bob",
    "Ann;;Bob",
    "Ann:0",
    "Ann:-1",
    "Ann:1.5",
    "Ann:",
    "Ann:x",
    "Ann;Ann",
    "Bob;bob",
    "Ann:2;ANN:1",
])
def test_invalid_participants(bad):
    err = fail(ledger(row(parts=bad)), "balances", "{f}")
    assert err.strip() == "line 2: bad-participants"


# ---------- error ordering and reporting ----------

def test_first_failing_check_wins_date_before_others():
    err = fail(ledger(row(date="nope", payer="", amount="x", parts="")), "balances", "{f}")
    assert err.strip() == "line 2: bad-date"


def test_payer_checked_before_amount_and_participants():
    err = fail(ledger(row(payer="", amount="x", parts="")), "balances", "{f}")
    assert err.strip() == "line 2: bad-payer"


def test_amount_checked_before_participants():
    err = fail(ledger(row(amount="x", parts="")), "balances", "{f}")
    assert err.strip() == "line 2: bad-amount"


def test_each_invalid_row_reported_once_in_line_order():
    text = ledger(
        row(date="bad"),                 # line 2
        row(payer="Ann", parts="Bob"),   # line 3, valid
        row(parts=""),                   # line 4
        row(amount="0"),                 # line 5
        row(payer=""),                   # line 6
    )
    err = fail(text, "settle", "{f}")
    assert err.strip().split("\n") == [
        "line 2: bad-date", "line 4: bad-participants", "line 5: bad-amount", "line 6: bad-payer"]


def test_invalid_row_suppresses_all_stdout_even_for_valid_rows():
    text = ledger(row(payer="Ann", parts="Bob"), row(amount="x"))
    code, out, err = run(text, "balances", "{f}", "--format", "json")
    assert code == 2 and out == ""
    assert err.strip() == "line 3: bad-amount"


def test_line_numbers_count_comments_blank_lines_and_bom():
    raw = ("\ufeff# comment\n"
           "\n"
           + HEADER
           + "   \n"
           + row(payer="Ann", amount="x", parts="Bob") + "\n").encode("utf-8")
    code, out, err = run("", "balances", "{f}", raw=raw)
    assert code == 2 and out == ""
    assert err.strip() == "line 5: bad-amount"


def test_crlf_does_not_shift_line_numbers():
    text = (HEADER + row(payer="Ann", parts="Bob") + "\n" + row(date="x")).replace("\n", "\r\n")
    code, out, err = run(text, "balances", "{f}")
    assert code == 2 and out == ""
    assert err.strip() == "line 3: bad-date"


def test_invalid_row_outside_date_range_still_fails():
    text = ledger(row(date="2026-01-01", payer="Ann", parts="Bob"),
                  row(date="2026-03-05", amount="oops"))
    err = fail(text, "balances", "{f}", "--since", "2026-03-01")
    assert err.strip() == "line 3: bad-amount"


def test_invalid_row_outside_until_range_still_fails():
    text = ledger(row(date="2026-06-01", payer="Ann", parts="Bob"),
                  row(date="2026-03-05", parts=""))
    err = fail(text, "balances", "{f}", "--until", "2026-03-31")
    assert err.strip() == "line 3: bad-participants"


# ---------- unreadable ledger / usage errors ----------

def test_missing_file_exit_3(tmp_path):
    path = str(tmp_path / "does-not-exist.csv")
    code, out, err = run_path("balances", path)
    assert code == 3
    assert out == ""
    assert err.strip() == f"error: cannot read {path}"


def test_directory_as_ledger_exit_3():
    code, out, err = run_path("balances", APP_DIR)
    assert code == 3
    assert out == ""
    assert err.strip().startswith("error: cannot read ")


@pytest.mark.parametrize("argv", [
    [],
    ["balances"],
    ["explode", "{f}"],
    ["balances", "{f}", "--verbose"],
    ["balances", "{f}", "--format", "xml"],
    ["balances", "{f}", "--format"],
    ["balances", "{f}", "--since", "2026-02-30"],
    ["balances", "{f}", "--since", "junk"],
    ["balances", "{f}", "--until", "2026/03/01"],
    ["balances", "{f}", "--until"],
    ["settle", "{f}", "--format", "yaml"],
])
def test_usage_errors_exit_64(argv):
    c, out, err = run(ledger(row()), *argv)
    assert c == 64
    assert out == ""


# ---------- splitting rule ----------

def test_weighted_split_basic():
    text = ledger(row(payer="Ann", amount="40.00", parts="Ann:1;Bob:3"))
    assert lines(ok(text, "balances", "{f}")) == ["Ann +30.00", "Bob -30.00"]


def test_default_weight_is_one_and_equals_explicit_one():
    a = ledger(row(payer="Zed", amount="9.00", parts="Ann;Bob;Cy"))
    b = ledger(row(payer="Zed", amount="9.00", parts="Ann:1;Bob:1;Cy:1"))
    assert balances_map(a) == balances_map(b)


def test_leftover_cent_goes_to_largest_remainder_not_alphabetical():
    # 1000 cents over weights 1:2 -> base 333 and 666, remainders 1 and 2.
    # Bob (remainder 2) gets the leftover cent, not Ann.
    text = ledger(row(payer="Zed", amount="10.00", parts="Ann:1;Bob:2"))
    assert lines(ok(text, "balances", "{f}")) == ["Ann -3.33", "Bob -6.67", "Zed +10.00"]


def test_leftover_remainder_beats_list_order():
    # 1001 cents over Cy:1, Bob:2, Ann:1 (W=4): base Cy 250 (rem 1),
    # Bob 500 (rem 2), Ann 250 (rem 1). The leftover cent goes to Bob, the
    # largest remainder, even though Cy is listed first.
    text = ledger(row(payer="Zed", amount="10.01", parts="Cy;Bob:2;Ann"))
    m = balances_map(text)
    assert m == {"Ann": -250, "Bob": -501, "Cy": -250, "Zed": 1001}


def test_leftover_ties_broken_by_name_ignoring_case():
    # 10.00 split three ways: 333 each, one leftover cent, all remainders equal.
    # Names ascending ignoring case: ann < Bob < cy -> ann gets the cent.
    text = ledger(row(payer="Zed", amount="10.00", parts="cy;Bob;ann"))
    assert lines(ok(text, "balances", "{f}")) == [
        "ann -3.34", "Bob -3.33", "cy -3.33", "Zed +10.00"]


def test_two_leftover_cents_go_to_first_two_by_name():
    text = ledger(row(payer="Zed", amount="0.02", parts="Cy;Bob;Ann"))
    assert lines(ok(text, "balances", "{f}")) == [
        "Ann -0.01", "Bob -0.01", "Cy 0.00", "Zed +0.02"]


def test_payer_who_is_participant_gets_own_share_back():
    text = ledger(row(payer="Ann", amount="10.00", parts="Ann;Bob"))
    assert balances_map(text) == {"Ann": 500, "Bob": -500}


def test_payer_not_participant_gets_full_amount():
    text = ledger(row(payer="Zed", amount="12.00", parts="Ann;Bob;Cy"))
    assert balances_map(text) == {"Ann": -400, "Bob": -400, "Cy": -400, "Zed": 1200}


def test_balances_always_sum_to_zero_over_many_rows():
    rng = random.Random(1234)
    names = ["Ann", "bob", "CY", "dee", "Eve", "fay"]
    rows = []
    for _ in range(60):
        payer = rng.choice(names)
        k = rng.randint(1, 4)
        people = rng.sample(names, k)
        parts = ";".join(f"{p}:{rng.randint(1, 5)}" if rng.random() < 0.5 else p for p in people)
        amount = f"{rng.randint(1, 99999) / 100:.2f}"
        rows.append(row(date="2026-03-01", payer=payer, amount=amount, parts=parts))
    bals = bal_json(ledger(*rows))
    assert sum(b["cents"] for b in bals) == 0


# ---------- names ----------

def test_names_match_ignoring_case_and_show_first_spelling():
    text = ledger(
        row(payer="Ann", amount="6.00", parts="Bob"),
        row(payer="ANN", amount="3.00", parts="bob"),
    )
    assert lines(ok(text, "balances", "{f}")) == ["Ann +9.00", "Bob -9.00"]


def test_names_are_trimmed():
    text = ledger(row(payer="  Ann  ", amount="4.00", parts=" Bob ; Cy "))
    assert lines(ok(text, "balances", "{f}")) == ["Ann +4.00", "Bob -2.00", "Cy -2.00"]


def test_first_spelling_follows_payer_before_participants():
    text = ledger(row(payer="Zed", amount="10.00", parts="zed;Ann"))
    assert lines(ok(text, "balances", "{f}")) == ["Ann -5.00", "Zed +5.00"]


def test_first_spelling_follows_row_order_across_rows():
    text = ledger(
        row(payer="Ann", amount="2.00", parts="Cy;Bob"),
        row(payer="bob", amount="2.00", parts="Ann"),
    )
    assert lines(ok(text, "balances", "{f}")) == ["Ann 0.00", "Bob +1.00", "Cy -1.00"]


def test_balances_sorted_by_name_ignoring_case():
    text = ledger(row(payer="charlie", amount="3.00", parts="Alice;bob"))
    names = [l.split(" ")[0] for l in lines(ok(text, "balances", "{f}"))]
    assert names == ["Alice", "bob", "charlie"]


def test_zero_balance_prints_without_sign():
    text = ledger(
        row(payer="Ann", amount="10.00", parts="Ann;Bob"),
        row(payer="Bob", amount="10.00", parts="Ann;Bob"),
    )
    assert lines(ok(text, "balances", "{f}")) == ["Ann 0.00", "Bob 0.00"]


def test_text_amount_format_signs_and_two_decimals():
    text = ledger(row(payer="Ann", amount="0.50", parts="Bob"),
                  row(payer="Cy", amount="1200", parts="Ann"))
    assert lines(ok(text, "balances", "{f}")) == ["Ann -1199.50", "Bob -0.50", "Cy +1200.00"]


def test_person_only_appearing_as_payer_is_listed():
    text = ledger(row(payer="Ann", amount="5.00", parts="Bob"))
    assert [l.split(" ")[0] for l in lines(ok(text, "balances", "{f}"))] == ["Ann", "Bob"]


# ---------- settle ----------

def _settle_example():
    # balances: A +10, B +5, C -7, D -8
    return ledger(
        row(payer="Ann", amount="15.00", parts="C:7;D:8"),
        row(payer="Bob", amount="5.00", parts="Ann"),
    )


def test_settle_follows_exact_procedure():
    # D pays A 8 (most negative vs most positive), then C pays B 5, then C pays A 2.
    assert lines(ok(_settle_example(), "settle", "{f}")) == [
        "D -> Ann 8.00", "C -> Bob 5.00", "C -> Ann 2.00"]


def test_settle_json_shape_and_values():
    assert settle_json(_settle_example()) == [
        {"from": "D", "to": "Ann", "cents": 800},
        {"from": "C", "to": "Bob", "cents": 500},
        {"from": "C", "to": "Ann", "cents": 200},
    ]


def test_settle_json_keys_are_exact():
    out = json.loads(ok(_settle_example(), "settle", "{f}", "--format", "json"))
    assert list(out.keys()) == ["transfers"]
    assert all(set(t.keys()) == {"from", "to", "cents"} for t in out["transfers"])


def test_settle_ties_broken_by_name_ignoring_case():
    # Debtors "cy" and "Dan" both -5; creditors "ann" and "Bob" both +5.
    # Ignoring case: cy before Dan, ann before Bob (case-sensitive order would
    # put Bob before ann and Dan before cy).
    text = ledger(
        row(payer="ann", amount="5.00", parts="cy"),
        row(payer="Bob", amount="5.00", parts="Dan"),
    )
    assert lines(ok(text, "settle", "{f}")) == ["cy -> ann 5.00", "Dan -> Bob 5.00"]


def test_settle_nothing_to_settle_text():
    text = ledger(row(payer="Ann", amount="10.00", parts="Ann;Bob"),
                  row(payer="Bob", amount="10.00", parts="Ann;Bob"))
    code, out, err = run(text, "settle", "{f}")
    assert code == 0 and err == ""
    assert out == "nothing to settle\n"


def test_settle_nothing_to_settle_json():
    text = ledger(row(payer="Ann", amount="10.00", parts="Ann;Bob"),
                  row(payer="Bob", amount="10.00", parts="Ann;Bob"))
    assert settle_json(text) == []


def test_settle_with_no_rows_text():
    code, out, _ = run(HEADER, "settle", "{f}")
    assert code == 0
    assert out == "nothing to settle\n"


def test_settle_single_cent_transfer():
    text = ledger(row(payer="Ann", amount="0.01", parts="Bob"))
    assert lines(ok(text, "settle", "{f}")) == ["Bob -> Ann 0.01"]


def test_settle_transfers_clear_all_balances_on_random_ledger():
    rng = random.Random(99)
    names = ["Ann", "bob", "Cy", "dan", "Eve", "fay", "Gus"]
    rows = []
    for _ in range(40):
        payer = rng.choice(names)
        people = rng.sample(names, rng.randint(1, 5))
        parts = ";".join(f"{p}:{rng.randint(1, 4)}" for p in people)
        rows.append(row(payer=payer, amount=f"{rng.randint(1, 50000) / 100:.2f}", parts=parts))
    text = ledger(*rows)
    bals = {b["name"]: b["cents"] for b in bal_json(text)}
    transfers = settle_json(text)
    for t in transfers:
        assert t["cents"] > 0
        bals[t["from"]] += t["cents"]
        bals[t["to"]] -= t["cents"]
    assert all(v == 0 for v in bals.values())
    # Each transfer must be from a debtor to a creditor.
    for t in transfers:
        assert t["from"] != t["to"]


# ---------- output formats ----------

def test_balances_json_exact_document():
    text = ledger(row(payer="Ann", amount="12.50", parts="Bob"))
    out = ok(text, "balances", "{f}", "--format", "json")
    assert json.loads(out) == {"balances": [
        {"name": "Ann", "cents": 1250}, {"name": "Bob", "cents": -1250}]}


def test_balances_json_zero_is_plain_zero_and_ordered():
    text = ledger(row(payer="Ann", amount="10.00", parts="Ann;Bob"),
                  row(payer="Bob", amount="10.00", parts="Ann;Bob"))
    assert bal_json(text) == [{"name": "Ann", "cents": 0}, {"name": "Bob", "cents": 0}]


def test_json_stdout_is_a_single_document():
    text = ledger(row(payer="Ann", amount="1.00", parts="Bob"))
    out = ok(text, "settle", "{f}", "--format", "json")
    assert out.count("\n") == 1 and out.endswith("\n")
    json.loads(out)


def test_text_and_json_agree_for_balances():
    text = ledger(row(payer="Zed", amount="10.00", parts="Ann;Bob;Cy"),
                  row(payer="Ann", amount="3.33", parts="Bob:2;Cy"))
    text_lines = lines(ok(text, "balances", "{f}"))
    js = bal_json(text)
    assert text_lines == [
        f"{b['name']} {('+' if b['cents'] > 0 else '-' if b['cents'] < 0 else '')}"
        f"{abs(b['cents']) // 100}.{abs(b['cents']) % 100:02d}"
        for b in js]


def test_text_and_json_agree_for_settle():
    text = _settle_example()
    text_lines = lines(ok(text, "settle", "{f}"))
    js = settle_json(text)
    assert text_lines == [
        f"{t['from']} -> {t['to']} {t['cents'] // 100}.{t['cents'] % 100:02d}" for t in js]


def test_settle_text_amount_two_decimals():
    text = ledger(row(payer="Ann", amount="12.5", parts="Bob;Cy"))
    assert lines(ok(text, "settle", "{f}")) == ["Bob -> Ann 6.25", "Cy -> Ann 6.25"]


# ---------- options ----------

def test_options_may_precede_positional_arguments():
    text = _settle_example()
    before = ok(text, "--format", "json", "settle", "{f}")
    after = ok(text, "settle", "{f}", "--format", "json")
    assert before == after
    assert json.loads(before)["transfers"][0] == {"from": "D", "to": "Ann", "cents": 800}


def test_format_equals_syntax():
    text = _settle_example()
    assert ok(text, "settle", "{f}", "--format=json") == ok(text, "settle", "{f}", "--format", "json")


def test_default_format_is_text():
    text = ledger(row(payer="Ann", amount="5.00", parts="Bob"))
    assert ok(text, "balances", "{f}") == "Ann +5.00\nBob -5.00\n"


DATED = ledger(
    row(date="2026-02-28", payer="Ann", amount="10.00", parts="Bob"),
    row(date="2026-03-01", payer="Bob", amount="4.00", parts="Ann"),
    row(date="2026-03-31", payer="Ann", amount="6.00", parts="Bob"),
    row(date="2026-04-01", payer="Cy", amount="8.00", parts="Cy;Ann"),
)


def test_no_range_considers_all_rows():
    assert balances_map(DATED) == {"Ann": 800, "Bob": -1200, "Cy": 400}


def test_since_is_inclusive_and_people_outside_range_are_not_printed():
    assert balances_map(DATED, "--since", "2026-03-01", "--until", "2026-03-31") == {
        "Ann": 200, "Bob": -200}


def test_until_alone_is_inclusive():
    assert balances_map(DATED, "--until", "2026-03-01") == {"Ann": 600, "Bob": -600}


def test_since_alone_is_inclusive():
    assert balances_map(DATED, "--since", "2026-04-01") == {"Cy": 400, "Ann": -400}


def test_since_and_until_same_day():
    assert balances_map(DATED, "--since", "2026-03-31", "--until", "2026-03-31") == {
        "Ann": 600, "Bob": -600}


def test_range_with_no_rows_gives_empty_outputs():
    assert ok(DATED, "balances", "{f}", "--since", "2030-01-01") == ""
    assert lines(ok(DATED, "settle", "{f}", "--since", "2030-01-01")) == ["nothing to settle"]
    assert bal_json(DATED, "--since", "2030-01-01") == []


def test_settle_respects_range():
    assert lines(ok(DATED, "settle", "{f}", "--since", "2026-04-01")) == ["Ann -> Cy 4.00"]


def test_range_applies_to_json_outputs():
    assert settle_json(DATED, "--since", "2026-04-01") == [
        {"from": "Ann", "to": "Cy", "cents": 400}]


def test_range_options_work_before_positional_arguments():
    out = ok(DATED, "--until", "2026-03-01", "balances", "{f}", "--format", "json")
    assert json.loads(out)["balances"] == [{"name": "Ann", "cents": 600}, {"name": "Bob", "cents": -600}]


def test_empty_range_since_after_until():
    assert ok(DATED, "balances", "{f}", "--since", "2026-03-31", "--until", "2026-03-01") == ""
