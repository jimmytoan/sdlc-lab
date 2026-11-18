import csv
import io
import json
import random

import pytest

from helpers import run, lines


HEADER = "date,payer,amount,participants,note\n"


def invoke(ledger, command="balances", *options, raw=None):
    return run(ledger, command, "{f}", *options, raw=raw)


def success(ledger, command="balances", *options, raw=None):
    code, out, err = invoke(ledger, command, *options, raw=raw)
    assert (code, err) == (0, "")
    return out


def json_report(ledger, command="balances", *options):
    return json.loads(success(ledger, command, *options, "--format", "json"))


def test_csv_bom_comments_whitespace_crlf_and_column_order():
    raw = ("\ufeff  # comment before header\r\n\r\n"
           "note,participants,amount,payer,date,extra\r\n"
           '"dinner, and ""cake""",  bob ; ALICE ,12.50, Alice ,2024-02-29,ignored\r\n'
           "  # ignored\r\n   \r\n"
           " ,alice; BOB,1.50,bOb,2024-03-01,x\r\n").encode("utf-8")
    assert lines(success("", raw=raw)) == ["Alice +5.50", "bob -5.50"]
    assert json.loads(success("", "settle", "--format", "json", raw=raw)) == {
        "transfers": [{"from": "bob", "to": "Alice", "cents": 550}]}


def test_case_first_spelling_order_zero_and_payer_not_participant():
    ledger = HEADER + ("2024-01-01,zOE,1.00,BOB;alice,\n"
                       "2024-01-02,bob,1.00,ALICE;Zoe,\n"
                       "2024-01-03,ALICE,1.00,alice,\n"
                       "2024-01-04,Zero,0.01,zero,\n")
    assert lines(success(ledger)) == ["alice -1.00", "BOB +0.50", "Zero 0.00", "zOE +0.50"]
    assert lines(success(ledger, "settle")) == ["alice -> BOB 0.50", "alice -> zOE 0.50"]
    assert json_report(ledger) == {"balances": [
        {"name": "alice", "cents": -100}, {"name": "BOB", "cents": 50},
        {"name": "Zero", "cents": 0}, {"name": "zOE", "cents": 50}]}


def test_largest_remainders_not_participant_order_or_largest_weight():
    # 7 cents / 8: remainders are 5 for Amy (3), 6 for Bob (2), 7 for Cy (1), 6 for Dee (2).
    # Three extras go to Cy, Bob, Dee, not Amy.
    ledger = HEADER + "2024-01-01,Payer,0.07,Dee:2;Amy:3;Cy:1;Bob:2,\n"
    assert json_report(ledger) == {"balances": [
        {"name": "Amy", "cents": -2}, {"name": "Bob", "cents": -2},
        {"name": "Cy", "cents": -1}, {"name": "Dee", "cents": -2},
        {"name": "Payer", "cents": 7}]}


def test_remainder_ties_case_insensitive_and_zero_shares():
    ledger = HEADER + "2024-01-01,ZoE,0.02,zulu;bob;ALICE;Carl,\n"
    assert lines(success(ledger)) == ["ALICE -0.01", "bob -0.01", "Carl 0.00", "ZoE +0.02", "zulu 0.00"]


def test_settlement_reselects_extremes_and_breaks_both_ties():
    ledger = HEADER + ("2024-01-01,Carol,0.07,Bob:4;ann:3,\n"
                       "2024-01-02,Dave,0.07,Bob:3;ann:4,\n"
                       "2024-01-03,Ed,0.02,Bob;ann,\n")
    # Debtors both -8, creditors Carol/Dave +7, Ed +2.
    expected = ["ann -> Carol 0.07", "Bob -> Dave 0.07", "ann -> Ed 0.01", "Bob -> Ed 0.01"]
    assert lines(success(ledger, "settle")) == expected
    assert json_report(ledger, "settle") == {"transfers": [
        {"from": "ann", "to": "Carol", "cents": 7},
        {"from": "Bob", "to": "Dave", "cents": 7},
        {"from": "ann", "to": "Ed", "cents": 1},
        {"from": "Bob", "to": "Ed", "cents": 1}]}


def test_settlement_reorders_after_partial_payment():
    ledger = HEADER + ("2024-01-01,A,0.10,X,\n"
                       "2024-01-02,B,0.07,Y,\n"
                       "2024-01-03,C,0.04,X;Y,\n")
    assert lines(success(ledger, "settle")) == [
        "X -> A 0.10", "Y -> B 0.07", "X -> C 0.02", "Y -> C 0.02"]


def test_dates_filters_inclusive_and_only_considered_people():
    ledger = HEADER + ("2023-12-31,Old,2,Old;Before,\n"
                       "2024-01-01,Ann,1,Ann;Bob,\n"
                       "2024-01-02,Cy,2,Cy;Bob,\n"
                       "2024-01-03,Dan,3,Dan;Bob,\n"
                       "2024-01-04,Future,2,Future;After,\n")
    assert lines(success(ledger, "balances", "--since", "2024-01-01", "--until", "2024-01-03")) == [
        "Ann +0.50", "Bob -3.00", "Cy +1.00", "Dan +1.50"]
    assert lines(success(ledger, "settle", "--since", "2024-01-02", "--until", "2024-01-02")) == ["Bob -> Cy 1.00"]
    assert lines(success(ledger, "balances", "--until", "2023-12-31")) == ["Before -1.00", "Old +1.00"]
    assert lines(success(ledger, "balances", "--since", "2024-01-04")) == ["After -1.00", "Future +1.00"]
    assert success(ledger, "balances", "--since", "2025-01-01") == ""
    assert lines(success(ledger, "settle", "--until", "2020-01-01")) == ["nothing to settle"]
    assert json_report(ledger, "settle", "--since", "2025-01-01") == {"transfers": []}


def test_empty_and_all_zero_ledgers():
    assert success(HEADER) == ""
    assert json_report(HEADER) == {"balances": []}
    assert success(HEADER, "settle").strip() == "nothing to settle"
    assert json_report(HEADER, "settle") == {"transfers": []}
    ledger = HEADER + "2024-01-01,Ann,20,Ann,\n"
    assert lines(success(ledger)) == ["Ann 0.00"]
    assert json_report(ledger, "settle") == {"transfers": []}


def test_options_before_between_and_after_positionals():
    ledger = HEADER + "2024-01-01,Ann,2,Bob,\n"
    expected = {"balances": [{"name": "Ann", "cents": 200}, {"name": "Bob", "cents": -200}]}
    for args in [("--format", "json", "balances", "{f}"),
                 ("balances", "--format", "json", "{f}"),
                 ("balances", "{f}", "--format", "json"),
                 ("--since", "2024-01-01", "balances", "--until", "2024-01-01", "{f}", "--format", "json")]:
        code, out, err = run(ledger, *args)
        assert (code, err) == (0, "")
        assert json.loads(out) == expected


@pytest.mark.parametrize("missing", ["date", "payer", "amount", "participants", "note"])
def test_missing_header_columns(missing):
    cols = [c for c in ["date", "payer", "amount", "participants", "note"] if c != missing]
    ledger = ",".join(cols) + "\nnot,a,valid,row\n"
    code, out, err = invoke(ledger)
    assert (code, out, err.strip()) == (2, "", "header: missing " + missing)


def test_header_missing_first_in_spec_order_and_no_row_errors():
    code, out, err = invoke("note,participants\nhello,\n")
    assert (code, out, err.strip()) == (2, "", "header: missing date")


@pytest.mark.parametrize("bad_date", ["", "2024-02-30", "2023-02-29", "2024-13-01", "2024-00-01", "2024-01-00", "2024-1-01", "24-01-01", "2024/01/01", "not-a-date"])
def test_invalid_dates(bad_date):
    code, out, err = invoke(HEADER + f"{bad_date},Ann,1,Ann,\n")
    assert (code, out, err.strip()) == (2, "", "line 2: bad-date")


@pytest.mark.parametrize("amount", ["", "0", "0.00", "-1", "+1", "1.234", ".5", "1.", "1e2", "NaN", "inf", '"1,000"', "abc"])
def test_invalid_amounts(amount):
    code, out, err = invoke(HEADER + f"2024-01-01,Ann,{amount},Ann,\n")
    assert (code, out, err.strip()) == (2, "", "line 2: bad-amount")


@pytest.mark.parametrize("participants", ["", " ", ";Bob", "Bob;", "Bob;;Ann", "Bob; bob ", ":2", "Bob:0", "Bob:-1", "Bob:1.5", "Bob:abc", "Bob:", "Bob:1:2"])
def test_invalid_participants(participants):
    code, out, err = invoke(HEADER + f"2024-01-01,Ann,1,{participants},\n")
    assert (code, out, err.strip()) == (2, "", "line 2: bad-participants")


def test_error_order_physical_lines_and_validation_outside_filter():
    ledger = ("# heading\n\n" + HEADER +
              "bad, ,bad,,\n" +
              "   # ignored\n" +
              "2020-01-01, ,bad,,\n" +
              "\n2020-01-01,A,bad,,\n" +
              "2020-01-01,A,1,A;a,\n" +
              "2024-01-01,OK,1,OK,\n")
    for command in ("balances", "settle"):
        code, out, err = invoke(ledger, command, "--since", "2024-01-01")
        assert code == 2 and out == ""
        assert lines(err) == ["line 4: bad-date", "line 6: bad-payer", "line 8: bad-amount", "line 9: bad-participants"]


@pytest.mark.parametrize("args", [(), ("balances",), ("invalid", "{f}"), ("balances", "{f}", "--bogus"),
                                  ("balances", "{f}", "--format", "xml"), ("balances", "{f}", "--format"),
                                  ("balances", "{f}", "--since", "2023-02-29"),
                                  ("balances", "{f}", "--until", "bad"),
                                  ("balances", "{f}", "--since"),
                                  ("balances", "{f}", "extra")])
def test_usage_errors(args):
    code, out, err = run(HEADER, *args)
    assert code == 64 and out == "" and err


def test_unreadable_file(tmp_path):
    path = str(tmp_path / "missing.csv")
    code, out, err = run("", "balances", path)
    assert code == 3 and out == ""
    assert err.strip() == f"error: cannot read {path}"


def make_csv(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["date", "payer", "amount", "participants", "note"])
    writer.writerows(rows)
    return stream.getvalue()


def reference(rows):
    names = {}
    balances = {}
    for _, payer, cents, parts, _ in rows:
        def register(name):
            key = name.lower()
            if key not in names:
                names[key] = name
                balances[key] = 0
            return key
        payer_key = register(payer)
        balances[payer_key] += cents
        weights = [(register(name), weight) for name, weight in parts]
        total = sum(weight for _, weight in weights)
        shares = {key: cents * weight // total for key, weight in weights}
        left = cents - sum(shares.values())
        ranked = sorted(weights, key=lambda item: (-(cents * item[1] % total), item[0]))
        for key, _ in ranked[:left]:
            shares[key] += 1
        for key, share in shares.items():
            balances[key] -= share
    ordered = sorted(names)
    result = [{"name": names[k], "cents": balances[k]} for k in ordered]
    transfers = []
    while any(balances.values()):
        debt = min((k for k in ordered if balances[k] < 0), key=lambda k: (balances[k], k))
        credit = min((k for k in ordered if balances[k] > 0), key=lambda k: (-balances[k], k))
        amount = min(-balances[debt], balances[credit])
        transfers.append({"from": names[debt], "to": names[credit], "cents": amount})
        balances[debt] += amount
        balances[credit] -= amount
    return result, transfers


def test_deterministic_generated_ledgers_against_independent_model():
    rng = random.Random(20260211)
    people = ["Alice", "bob", "Cy", "Dora", "ed", "Fay", "Gus"]
    for _ in range(12):
        rows, csv_rows = [], []
        for i in range(rng.randint(3, 12)):
            payer = rng.choice(people)
            payer = rng.choice([payer, payer.upper(), payer.lower()])
            participants = rng.sample(people, rng.randint(1, len(people)))
            parts = [(rng.choice([p, p.upper(), p.lower()]), rng.randint(1, 7)) for p in participants]
            cents = rng.randint(1, 10003)
            day = f"2024-01-{i + 1:02d}"
            rows.append((day, payer, cents, parts, "quoted, note"))
            csv_rows.append((day, payer, f"{cents // 100}.{cents % 100:02d}",
                             ";".join(f"{n}:{w}" for n, w in parts), "quoted, note"))
        ledger = make_csv(csv_rows)
        balances, transfers = reference(rows)
        assert json_report(ledger) == {"balances": balances}
        assert json_report(ledger, "settle") == {"transfers": transfers}
        expected_text = [f"{x['name']} " + (f"{'+' if x['cents'] > 0 else '-'}{abs(x['cents']) // 100}.{abs(x['cents']) % 100:02d}" if x['cents'] else "0.00") for x in balances]
        assert lines(success(ledger)) == expected_text
        expected_transfers = [f"{x['from']} -> {x['to']} {x['cents'] // 100}.{x['cents'] % 100:02d}" for x in transfers]
        assert lines(success(ledger, "settle")) == (expected_transfers or ["nothing to settle"])


@pytest.mark.parametrize("amount,cents", [
    ("12", 1200), ("12.5", 1250), ("12.50", 1250), ("0.01", 1),
    ("000.01", 1), ("12345678901234567890.12", 1234567890123456789012),
])
def test_valid_amount_forms_and_exact_integer_arithmetic(amount, cents):
    ledger = HEADER + f"2024-02-29,A,{amount},B,\n"
    assert json_report(ledger) == {"balances": [
        {"name": "A", "cents": cents}, {"name": "B", "cents": -cents}]}
    assert json_report(ledger, "settle") == {"transfers": [
        {"from": "B", "to": "A", "cents": cents}]}


def test_equal_remainders_prefer_names_not_weights_or_file_order():
    ledger = HEADER + "2024-01-01,P,0.05,Z:3;A:3;B:3,\n"
    assert lines(success(ledger)) == ["A -0.02", "B -0.02", "P +0.05", "Z -0.01"]


def test_net_zero_after_multiple_rows_still_lists_all_people():
    ledger = HEADER + ("2024-01-01,A,1,B,\n"
                       "2024-01-02,B,1,A,\n")
    assert lines(success(ledger)) == ["A 0.00", "B 0.00"]
    assert success(ledger, "settle").strip() == "nothing to settle"
    assert json_report(ledger, "settle") == {"transfers": []}


def test_crlf_comments_count_as_physical_lines_in_errors():
    raw = ("# hi\r\n\r\n" + HEADER.replace("\n", "\r\n") +
           "  # ignore\r\n2024-02-30,A,1,A,\r\n"
           "\t\r\n2024-01-01,A,0,A,\r\n").encode()
    code, out, err = invoke("", raw=raw)
    assert (code, out, lines(err)) == (2, "", ["line 5: bad-date", "line 7: bad-amount"])


def test_utf8_names_csv_quoting_and_first_payer_spelling():
    ledger = make_csv([
        ("2000-02-29", "Élodie", "2.00", "éLODIE;李", 'text, "quoted"'),
        ("2000-03-01", "李", "1.00", "ÉLODIE;李", "# not a comment"),
        ("2000-03-02", "Smith, Jo", "1.00", "smith, jo", ""),
    ])
    assert json_report(ledger) == {"balances": [
        {"name": "Smith, Jo", "cents": 0},
        {"name": "Élodie", "cents": 50},
        {"name": "李", "cents": -50},
    ]}
    assert lines(success(ledger, "settle")) == ["李 -> Élodie 0.50"]


def test_exact_text_line_contents():
    ledger = HEADER + "2024-01-01,A,1,B,\n"
    assert success(ledger).splitlines() == ["A +1.00", "B -1.00"]
    assert success(ledger, "settle").splitlines() == ["B -> A 1.00"]
    assert success(HEADER, "settle").splitlines() == ["nothing to settle"]
