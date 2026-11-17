# DFL-HOLDOUT-CANARY-7c1e4b9a2f6d — held-out scenarios for sessionize v1. Never mounted into a coder sandbox.
import json
import pytest
from helpers import ev, run_job, pick


def test_header_exact():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.header == ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def test_empty_input_writes_all_files():
    r = run_job([])
    assert r.code == 0 and r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == [] and r.rejects == []
    assert r.daily["days"] == [] and r.daily["duplicates_dropped"] == 0 and r.daily["rejected"] == 0


def test_gap_exactly_30_minutes_same_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00"), ev("e3", "ann", "10:00:01")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("ann-2", "1")]


def test_gap_uses_fractional_precision():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 2


def test_gap_measured_from_previous_event_not_session_start():
    r = run_job([ev(f"e{i}", "ann", f"09:{m:02d}:00") for i, m in enumerate((0, 25, 50))] + [ev("e9", "ann", "10:15:00")])
    assert pick(r.sessions, "events", "duration_s") == [("4", "4500")]


def test_out_of_order_input():
    r = run_job([ev("e3", "ann", "09:20:00", "purchase", amount="1.10"), ev("e1", "ann", "09:00:00"),
                 ev("e4", "ann", "11:00:00"), ev("e2", "ann", "09:10:00")])
    assert pick(r.sessions, "session_id", "start", "end", "events") == [
        ("ann-1", "2026-03-01T09:00:00Z", "2026-03-01T09:20:00Z", "3"),
        ("ann-2", "2026-03-01T11:00:00Z", "2026-03-01T11:00:00Z", "1")]


def test_user_normalised():
    r = run_job([ev("e1", "  Ann ", "09:00:00"), ev("e2", "ANN", "09:05:00")])
    assert pick(r.sessions, "session_id", "user", "events") == [("ann-1", "ann", "2")]


def test_offsets_and_fraction_truncated_in_output():
    # 09:00:00.999-05:30 is 14:30:00.999Z; 20:10:01.250+05:45 is 14:25:01.250Z
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.999-05:30"), ev("e2", "ann", "2026-03-01T20:10:01.250+05:45")])
    assert pick(r.sessions, "start", "end", "duration_s") == [("2026-03-01T14:25:01Z", "2026-03-01T14:30:00Z", "299")]


def test_duration_floor():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "ann", "2026-03-01T09:00:02.100Z")])
    assert pick(r.sessions, "start", "end", "duration_s") == [("2026-03-01T09:00:00Z", "2026-03-01T09:00:02Z", "1")]


def test_sort_by_start_full_precision_then_user():
    r = run_job([ev("e1", "zed", "2026-03-01T09:00:00.100Z"), ev("e2", "bob", "2026-03-01T09:00:00.700Z"),
                 ev("e3", "ann", "2026-03-01T09:00:00.700Z"), ev("e4", "cy", "2026-03-01T08:59:59.999Z")])
    assert pick(r.sessions, "user") == [("cy",), ("zed",), ("ann",), ("bob",)]


def test_dedupe_keeps_earliest_timestamp():
    r = run_job([ev("e1", "ann", "09:40:00"), ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:20:00")])
    assert pick(r.sessions, "start", "end", "events") == [("2026-03-01T09:00:00Z", "2026-03-01T09:20:00Z", "2")]
    assert r.daily["duplicates_dropped"] == 1


def test_dedupe_tie_keeps_first_in_file():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="3.00"), ev("e1", "ann", "09:00:00", "purchase", amount="9.00"),
                 ev("e1", "bob", "2026-03-01T16:00:00+07:00")])
    assert pick(r.sessions, "user", "revenue") == [("ann", "3.00")]
    assert r.daily["duplicates_dropped"] == 2


def test_dedupe_is_exact_string_and_ignores_rejected():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("E1", "ann", "09:01:00"), ev("e1", "ann", "nonsense"),
                 ev("e2", "ann", "09:02:00", "purchase", amount="x"), ev("e2", "ann", "09:03:00")])
    assert r.sessions[0]["events"] == "3"
    assert r.daily["duplicates_dropped"] == 0 and r.daily["rejected"] == 2


def test_reject_reasons_and_order_of_checks():
    lines = [
        "[1, 2]",                                                      # 1 bad_json (not an object)
        "",                                                            # 2 skipped
        json.dumps({"event_id": "a", "user": "u", "type": "view"}),   # 3 missing_field
        json.dumps({"event_id": 5, "user": "u", "ts": "bad", "type": "nope"}),  # 4 missing_field wins
        json.dumps({"event_id": "b", "user": "  ", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # 5 missing_field
        ev("c", "u", "2026-03-01T09:00:00", "nope"),                  # 6 bad_ts wins over bad_type
        ev("d", "u", "09:00:00", "VIEW"),                             # 7 bad_type
        ev("e", "u", "09:00:00", "purchase"),                         # 8 bad_amount
        "   ",                                                         # 9 skipped
        ev("f", "u", "09:00:00", "purchase", amount=5),               # 10 bad_amount (not a string)
        ev("g", "u", "09:00:00"),                                     # 11 valid
        "null",                                                        # 12 bad_json
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 1, "reason": "bad_json"}, {"line": 3, "reason": "missing_field"},
                         {"line": 4, "reason": "missing_field"}, {"line": 5, "reason": "missing_field"},
                         {"line": 6, "reason": "bad_ts"}, {"line": 7, "reason": "bad_type"},
                         {"line": 8, "reason": "bad_amount"}, {"line": 10, "reason": "bad_amount"},
                         {"line": 12, "reason": "bad_json"}]
    assert r.daily["rejected"] == 9 and len(r.sessions) == 1


@pytest.mark.parametrize("t", [pytest.param("2026-03-01T09:00:00", id="stated:no_offset"), pytest.param("2026-03-01 09:00:00Z", id="arbitrary:space_separator"), pytest.param("2026-03-01T09:00Z", id="arbitrary:no_seconds"), pytest.param("2026-02-30T09:00:00Z", id="implied:feb_30"), pytest.param("2026-03-01T25:00:00Z", id="implied:hour_25"), pytest.param("2026-03-01", id="implied:date_only"), pytest.param("1772355600", id="implied:epoch"), pytest.param("2026-03-01T09:00:00+0700x", id="implied:junk_offset")])
def test_bad_timestamp(t):
    r = run_job([ev("e1", "ann", t)])
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}] and r.sessions == []


@pytest.mark.parametrize("a", [pytest.param("-1", id="implied:negative"), pytest.param("1.999", id="arbitrary:three_decimals"), pytest.param("abc", id="implied:text"), pytest.param("", id="implied:empty"), pytest.param("1,5", id="implied:comma"), pytest.param("1e3", id="arbitrary:exponent")])
def test_bad_amount(a):
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount=a)])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]


def test_amount_ignored_for_non_purchase():
    r = run_job([ev("e1", "ann", "09:00:00", "view", amount="oops"), ev("e2", "ann", "09:01:00", "click", amount="7.00")])
    assert r.rejects == [] and pick(r.sessions, "events", "purchases", "revenue") == [("2", "0", "0.00")]


def test_revenue_exact_decimal():
    amounts = ["0.10", "0.20", "0.1", "5", "19.99", "0"]
    r = run_job([ev(f"e{i}", "ann", f"09:0{i}:00", "purchase", amount=a) for i, a in enumerate(amounts)])
    assert pick(r.sessions, "purchases", "revenue") == [("6", "25.39")]
    assert r.daily["days"][0]["revenue"] == "25.39"


def test_session_across_midnight_counts_on_start_date():
    r = run_job([ev("e1", "ann", "2026-03-01T23:50:00Z", "purchase", amount="4.00"),
                 ev("e2", "ann", "2026-03-02T00:10:00Z", "purchase", amount="6.00")])
    assert len(r.sessions) == 1
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "10.00", "top_user": "ann"}]


def test_date_is_utc_date_not_local():
    r = run_job([ev("e1", "ann", "2026-03-02T03:00:00+07:00")])
    assert r.daily["days"][0]["date"] == "2026-03-01"


def test_daily_users_distinct_and_top_user_sums_sessions():
    r = run_job([ev("e1", "ann", "08:00:00", "purchase", amount="6.00"), ev("e2", "ann", "12:00:00", "purchase", amount="6.00"),
                 ev("e3", "bob", "09:00:00", "purchase", amount="10.00"), ev("e4", "cy", "09:00:00")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 4, "users": 3, "revenue": "22.00", "top_user": "ann"}]


def test_top_user_tie_sorts_first():
    r = run_job([ev("e1", "bob", "08:00:00", "purchase", amount="5.00"), ev("e2", "ann", "12:00:00", "purchase", amount="5.00")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_session_numbering_per_user_across_days():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e2", "bob", "2026-03-01T10:00:00Z"),
                 ev("e3", "ann", "2026-03-02T09:00:00Z"), ev("e4", "ann", "2026-03-03T09:00:00Z")])
    assert pick(r.sessions, "session_id") == [("ann-1",), ("bob-1",), ("ann-2",), ("ann-3",)]
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-02", "2026-03-03"]


def test_output_dir_created_nested():
    import os, subprocess, sys, tempfile
    from helpers import APP_DIR
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "in"))
        with open(os.path.join(d, "in", "events.jsonl"), "w") as f:
            f.write(ev("e1", "ann", "09:00:00") + "\n")
        out = os.path.join(d, "a", "b", "out")
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", os.path.join(d, "in"), "--out", out],
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 0 and sorted(os.listdir(out)) == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_unicode_user_and_extra_fields():
    r = run_job([json.dumps({"event_id": "e1", "user": "Zoë", "ts": "2026-03-01T09:00:00Z", "type": "view", "page": "/x", "n": 3},
                            ensure_ascii=False)])
    assert pick(r.sessions, "session_id", "user") == [("zoë-1", "zoë")]
