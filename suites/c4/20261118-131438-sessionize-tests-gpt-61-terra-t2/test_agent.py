import csv
import json
import os
import subprocess
import sys

import pytest

from helpers import APP_DIR, ev, pick, run_job


HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def event(**overrides):
    obj = {"event_id": "id", "user": "Ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    obj.update(overrides)
    return json.dumps(obj)


def check_success(r):
    assert r.code == 0, r.stderr
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER


def test_empty_and_whitespace_input():
    for lines in ([], ["", "   ", "\t", "  \r"]):
        r = run_job(lines)
        check_success(r)
        assert r.sessions == []
        assert r.rejects == []
        assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_missing_input_and_output_directory_creation_and_overwrite(tmp_path):
    inp = tmp_path / "input dir"
    inp.mkdir()
    out = tmp_path / "nested" / "output dir"
    cmd = [sys.executable, os.path.join(APP_DIR, "job.py"), "--in", str(inp), "--out", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=APP_DIR)
    assert p.returncode == 2
    assert "events.jsonl" in p.stderr
    (inp / "events.jsonl").write_text(ev("1", "x", "09:00:00") + "\n", encoding="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=APP_DIR)
    assert p.returncode == 0, p.stderr
    assert sorted(x.name for x in out.iterdir()) == ["daily.json", "rejects.jsonl", "sessions.csv"]
    (inp / "events.jsonl").write_text("bad json\n", encoding="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=APP_DIR)
    assert p.returncode == 0, p.stderr
    with (out / "sessions.csv").open(newline="", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [HEADER]
    assert json.loads((out / "daily.json").read_text(encoding="utf-8")) == {
        "days": [], "duplicates_dropped": 0, "rejected": 1}
    assert [json.loads(x) for x in (out / "rejects.jsonl").read_text(encoding="utf-8").splitlines()] == [
        {"line": 1, "reason": "bad_json"}]


def test_reject_order_line_numbers_and_precedence():
    missing = json.loads(event())
    del missing["event_id"]
    lines = [
        " ", "{", "null", "[]", '"hello"', "42",
        json.dumps(missing),
        event(event_id=" \t", ts="bad", type="bad"),
        event(user=None, ts="bad"),
        event(ts="bad", type="bad"),
        event(type="bad", amount="bad"),
        event(type="purchase", amount="bad"),
        "\t", event(event_id="valid", type="click", amount={"ignored": True}),
    ]
    r = run_job(lines)
    check_success(r)
    reasons = ["bad_json"] * 5 + ["missing_field"] * 3 + ["bad_ts", "bad_type", "bad_amount"]
    assert r.rejects == [{"line": i, "reason": reason} for i, reason in zip(range(2, 13), reasons)]
    assert r.daily == {"days": [{"date": "2026-03-01", "sessions": 1, "users": 1,
                                 "revenue": "0.00", "top_user": None}],
                       "duplicates_dropped": 0, "rejected": 11}
    assert pick(r.sessions, "events", "purchases", "revenue") == [("1", "0", "0.00")]


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_each_required_field(field):
    base = json.loads(event())
    lines = []
    missing = dict(base)
    del missing[field]
    lines.append(json.dumps(missing))
    for value in (None, 0, True, [], {}, "", "  \t"):
        obj = dict(base, **{field: value})
        lines.append(json.dumps(obj))
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{"line": i, "reason": "missing_field"} for i in range(1, len(lines) + 1)]
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(lines)}


def test_type_exactness_and_amount_ignored_for_nonpurchase():
    lines = [event(event_id=str(i), type=t, amount="-100")
             for i, t in enumerate(["view", "click"])]
    lines += [event(event_id="bad" + str(i), type=t, amount="1")
              for i, t in enumerate(["VIEW", "Click", " purchase ", "logout", "refund", "other"])]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{"line": i, "reason": "bad_type"} for i in range(3, 9)]
    assert pick(r.sessions, "events", "purchases", "revenue") == [("2", "0", "0.00")]


def test_purchase_amount_validation_and_exact_decimal_sums():
    good = ["0", "0.00", "5", "5.5", "19.99", "0.01", "0.1", "0.2"]
    bad = [None, 1, True, [], "", " ", "-1", "+1", ".5", "1.", "1.001", "1e2", "NaN", "inf", "1,00", "1 0"]
    lines = [event(event_id="g" + str(i), type="purchase", amount=a) for i, a in enumerate(good)]
    lines += [event(event_id="b" + str(i), type="purchase", amount=a) for i, a in enumerate(bad)]
    lines += [event(event_id="missing", type="purchase")]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{"line": i, "reason": "bad_amount"} for i in range(len(good) + 1, len(lines) + 1)]
    assert pick(r.sessions, "events", "purchases", "revenue") == [("8", "8", "30.80")]
    assert r.daily["days"][0]["revenue"] == "30.80"
    assert r.daily["days"][0]["top_user"] == "ann"


def test_timestamp_formats_validation_and_utc_conversion():
    valid = [
        ("2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z"),
        ("2026-03-01T09:00:00+07:00", "2026-03-01T02:00:00Z"),
        ("2026-03-01T00:00:00.250-05:30", "2026-03-01T05:30:00Z"),
        ("2026-03-01T00:15:00+01:00", "2026-02-28T23:15:00Z"),
        ("2026-03-01T23:15:00-02:00", "2026-03-02T01:15:00Z"),
    ]
    invalid = ["2026-03-01T09:00:00", "2026-03-01", "2026-03-01T09:00Z",
               "2026-03-01 09:00:00Z", "2026-03-01T09:00:00z", "2026-03-01T09:00:00+0700",
               "2026-03-01T09:00:00+24:00", "2026-03-01T09:00:00-05:60",
               "2026-02-30T09:00:00Z", "2026-03-01T24:00:00Z", "2026-03-01T09:00:60Z",
               "2026-03-01T09:00:00.Z", "not a time"]
    lines = [event(event_id=str(i), user="u" + str(i), ts=t) for i, (t, _) in enumerate(valid)]
    lines += [event(event_id="bad" + str(i), ts=t) for i, t in enumerate(invalid)]
    r = run_job(lines)
    check_success(r)
    assert {s["user"]: s["start"] for s in r.sessions} == {"u" + str(i): expected for i, (_, expected) in enumerate(valid)}
    assert r.rejects == [{"line": i, "reason": "bad_ts"} for i in range(len(valid) + 1, len(lines) + 1)]
    assert [d["date"] for d in r.daily["days"]] == ["2026-02-28", "2026-03-01", "2026-03-02"]


def test_duplicates_global_earliest_tie_input_order_and_exact_id():
    lines = [
        ev("same", "later", "10:00:00", "purchase", amount="99"),
        ev("same", " FIRST ", "2026-03-01T11:00:00+02:00", "purchase", amount="2"),
        ev("same", "tie", "09:00:00", "purchase", amount="20"),
        ev("same", "invalid", "08:00:00", "purchase", amount="-1"),
        ev(" same ", "first", "09:00:01", "purchase", amount="0.10"),
        ev("SAME", "first", "09:00:02"),
        ev("only-invalid", "none", "08:00:00", "bad"),
        ev("only-invalid", "first", "09:00:03"),
    ]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{"line": 4, "reason": "bad_amount"}, {"line": 7, "reason": "bad_type"}]
    assert r.daily == {"days": [{"date": "2026-03-01", "sessions": 1, "users": 1,
                                 "revenue": "2.10", "top_user": "first"}],
                       "duplicates_dropped": 2, "rejected": 2}
    assert pick(r.sessions, "session_id", "events", "purchases", "revenue") == [("first-1", "4", "2", "2.10")]


def test_session_gap_full_precision_order_and_duration():
    lines = [
        ev("4", "ANN", "2026-03-01T10:00:00.300001Z"),
        ev("2", " ann ", "2026-03-01T09:30:00.300000Z", "purchase", amount="1.25"),
        ev("1", "Ann", "2026-03-01T09:00:00.300000Z"),
        ev("3", "ann", "2026-03-01T09:30:01.200000Z", "click"),
        ev("5", "ann", "2026-03-01T10:30:00.300002Z"),
        ev("6", "ann", "2026-03-01T10:30:01.100001Z"),
    ]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == []
    assert pick(r.sessions, *HEADER) == [
        ("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z", "3600", "4", "1", "1.25"),
        ("ann-2", "ann", "2026-03-01T10:30:00Z", "2026-03-01T10:30:01Z", "0", "2", "0", "0.00"),
    ]
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 2, "users": 1, "revenue": "1.25", "top_user": "ann"}]


def test_rows_sorted_by_precise_start_then_normalized_user():
    lines = [ev("1", "Zed", "2026-03-01T09:00:00.100Z"),
             ev("2", "Bob", "2026-03-01T09:00:00.200Z"),
             ev("3", "Ann", "2026-03-01T09:00:00.200Z"),
             ev("4", "ZED", "2026-03-01T11:00:00Z"),
             ev("5", "ann", "2026-03-01T10:00:00Z")]
    r = run_job(lines)
    check_success(r)
    assert pick(r.sessions, "session_id") == [("zed-1",), ("ann-1",), ("bob-1",), ("ann-2",), ("zed-2",)]
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 5, "users": 3, "revenue": "0.00", "top_user": None}]


def test_daily_start_date_aggregate_user_revenue_ties_and_zero():
    lines = [
        ev("1", "Bob", "2026-03-01T23:50:00Z", "purchase", amount="2"),
        ev("2", "bob", "2026-03-02T00:10:00Z", "purchase", amount="3"),
        ev("3", " Ann ", "2026-03-01T08:00:00Z", "purchase", amount="2"),
        ev("4", "ANN", "2026-03-01T09:00:00Z", "purchase", amount="3"),
        ev("5", "zoe", "2026-03-01T10:00:00Z", "purchase", amount="4.99"),
        ev("6", "ann", "2026-03-02T11:00:00Z", "purchase", amount="0"),
        ev("7", "bob", "2026-03-02T12:00:00Z"),
        ev("8", "bob", "2026-03-03T00:30:00+02:00", "purchase", amount="0.01"),
    ]
    r = run_job(lines)
    check_success(r)
    assert r.daily == {"days": [
        {"date": "2026-03-01", "sessions": 4, "users": 3, "revenue": "14.99", "top_user": "ann"},
        {"date": "2026-03-02", "sessions": 3, "users": 2, "revenue": "0.01", "top_user": "bob"},
    ], "duplicates_dropped": 0, "rejected": 0}
    bob = next(s for s in r.sessions if s["session_id"] == "bob-1")
    assert (bob["start"], bob["end"], bob["duration_s"], bob["revenue"]) == (
        "2026-03-01T23:50:00Z", "2026-03-02T00:10:00Z", "1200", "5.00")


def test_csv_special_characters_and_unicode_users():
    r = run_job([ev("id", '  ÉVE, "Team"  ', "09:00:00", "purchase", amount="1.00")])
    check_success(r)
    assert pick(r.sessions, "session_id", "user", "revenue") == [('éve, "team"-1', 'éve, "team"', "1.00")]
    assert r.daily["days"][0]["top_user"] == 'éve, "team"'


def test_duplicate_selection_uses_subsecond_precision_and_utc():
    r = run_job([
        ev("x", "late", "2026-03-01T09:00:00.800Z", "purchase", amount="9"),
        ev("x", "early", "2026-03-01T10:00:00.100+01:00", "purchase", amount="1"),
        ev("x", "equal", "2026-03-01T09:00:00.100Z", "purchase", amount="5"),
        ev("y", "early", "2026-03-01T09:30:00.100Z"),
    ])
    check_success(r)
    assert r.rejects == []
    assert pick(r.sessions, *HEADER) == [
        ("early-1", "early", "2026-03-01T09:00:00Z", "2026-03-01T09:30:00Z", "1800", "2", "1", "1.00")]
    assert r.daily == {"days": [{"date": "2026-03-01", "sessions": 1, "users": 1,
                                 "revenue": "1.00", "top_user": "early"}],
                       "duplicates_dropped": 2, "rejected": 0}
