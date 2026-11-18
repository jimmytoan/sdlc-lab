"""Thorough pytest test suite for sessionize batch job (tests/test_agent.py)."""
import json
import os
import pytest
from helpers import ev, run_job, pick


def test_empty_input_and_always_all_files():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.daily == {
        "days": [],
        "duplicates_dropped": 0,
        "rejected": 0,
    }
    assert r.rejects == []


def test_whitespace_lines_skipped_and_line_numbers():
    lines = [
        "",
        "   ",
        ev("e1", "ann", "09:00:00"),
        "{invalid json",
        "",
        ev("e2", "bob", "09:05:00"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 4, "reason": "bad_json"}]
    assert r.daily["rejected"] == 1
    assert len(r.sessions) == 2


def test_reject_priority_order():
    # Priority:
    # 1. bad_json
    # 2. missing_field
    # 3. bad_ts
    # 4. bad_type
    # 5. bad_amount
    lines = [
        "{bad json",  # bad_json
        json.dumps({"event_id": "", "user": "ann", "ts": "09:00:00", "type": "view"}),  # missing_field
        ev("e3", "ann", "invalid-ts", "view"),  # bad_ts
        ev("e4", "ann", "09:00:00", "unknown_type"),  # bad_type
        ev("e5", "ann", "09:00:00", "purchase", amount="invalid-amount"),  # bad_amount
    ]
    r = run_job(lines)
    assert r.rejects == [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "missing_field"},
        {"line": 3, "reason": "bad_ts"},
        {"line": 4, "reason": "bad_type"},
        {"line": 5, "reason": "bad_amount"},
    ]


def test_bad_json_varieties():
    lines = [
        "[1, 2, 3]",
        '"just a string"',
        "123",
        "null",
    ]
    r = run_job(lines)
    assert r.rejects == [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "bad_json"},
        {"line": 3, "reason": "bad_json"},
        {"line": 4, "reason": "bad_json"},
    ]


def test_missing_field_varieties():
    base = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    lines = [
        json.dumps({**base, "event_id": ""}),
        json.dumps({**base, "user": "   "}),
        json.dumps({**base, "ts": None}),
        json.dumps({**base, "type": 123}),
        json.dumps({"user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}),
    ]
    r = run_job(lines)
    assert len(r.rejects) == 5
    for rej in r.rejects:
        assert rej["reason"] == "missing_field"


def test_bad_ts_varieties():
    lines = [
        ev("e1", "ann", "2026-03-01T09:00:00"),  # missing offset
        ev("e2", "ann", "2026-02-30T09:00:00Z"),  # invalid date
        ev("e3", "ann", "2026-03-01T25:00:00Z"),  # invalid hour
        ev("e4", "ann", "2026-03-01T09:00:00+25:00"),  # invalid offset
        ev("e5", "ann", "not-a-timestamp"),
    ]
    r = run_job(lines)
    assert all(rej["reason"] == "bad_ts" for rej in r.rejects)
    assert len(r.rejects) == 5


def test_valid_ts_varieties():
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00Z"),
        ev("e2", "ann", "2026-03-01T16:00:00+07:00"),  # 09:00:00 UTC
        ev("e3", "ann", "2026-03-01T03:30:00-05:30"),  # 09:00:00 UTC
        ev("e4", "ann", "2026-03-01T09:00:00.999999Z"),
    ])
    assert r.code == 0
    assert r.rejects == []
    assert len(r.sessions) == 1
    assert r.sessions[0]["events"] == "4"


def test_bad_amount_varieties():
    lines = [
        ev("e1", "ann", "09:00:00", "purchase"),
        ev("e2", "ann", "09:01:00", "purchase", amount=""),
        ev("e3", "ann", "09:02:00", "purchase", amount="-5.00"),
        ev("e4", "ann", "09:03:00", "purchase", amount="19.999"),
        ev("e5", "ann", "09:04:00", "purchase", amount=19.99),
    ]
    r = run_job(lines)
    assert all(rej["reason"] == "bad_amount" for rej in r.rejects)
    assert len(r.rejects) == 5


def test_amount_ignored_for_non_purchase():
    lines = [
        ev("e1", "ann", "09:00:00", "view"),
        ev("e2", "ann", "09:01:00", "click", amount="invalid"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == []
    assert len(r.sessions) == 1


def test_valid_amounts():
    r = run_job([
        ev("e1", "ann", "09:00:00", "purchase", amount="0"),
        ev("e2", "ann", "09:01:00", "purchase", amount="0.00"),
        ev("e3", "ann", "09:02:00", "purchase", amount="5"),
        ev("e4", "ann", "09:03:00", "purchase", amount="5.5"),
        ev("e5", "ann", "09:04:00", "purchase", amount="19.99"),
    ])
    assert r.code == 0
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "30.49"


def test_user_normalization_and_case():
    r = run_job([
        ev("e1", "  Ann  ", "09:00:00"),
        ev("e2", "ANN", "09:01:00"),
        ev("e3", "ann", "09:02:00"),
    ])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["events"] == "3"


def test_duplicates_tie_breaking():
    r = run_job([
        ev("e1", "ann", "09:10:00"),
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:05:00", amount="10"),
        ev("e2", "ann", "09:05:00", amount="20"),
    ])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "2"


def test_session_gap_boundary():
    r = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:00"),  # exactly 30 min gap -> same session
        ev("e3", "ann", "10:00:01"),  # 30 min 1 sec gap -> new session
    ])
    assert pick(r.sessions, "session_id", "events") == [
        ("ann-1", "2"),
        ("ann-2", "1"),
    ]


def test_session_ordering_start_then_user():
    r = run_job([
        ev("e1", "bob", "10:00:00"),
        ev("e2", "ann", "09:00:00"),
        ev("e3", "charlie", "09:00:00"),
    ])
    assert pick(r.sessions, "session_id", "user") == [
        ("ann-1", "ann"),
        ("charlie-1", "charlie"),
        ("bob-1", "bob"),
    ]


def test_fractional_seconds_precision_vs_output():
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00.500Z"),
        ev("e2", "ann", "2026-03-01T09:00:30.400Z"),
    ])
    assert len(r.sessions) == 1
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:00:30Z"
    assert r.sessions[0]["duration_s"] == "29"


def test_daily_summary_midnight_and_top_user():
    r = run_job([
        ev("e1", "ann", "2026-03-01T10:00:00Z", "purchase", amount="10.00"),
        ev("e2", "bob", "2026-03-01T11:00:00Z", "purchase", amount="20.00"),
        ev("e3", "ann", "2026-03-02T01:00:00Z", "purchase", amount="30.00"),
        ev("e4", "bob", "2026-03-02T01:05:00Z", "purchase", amount="30.00"),
    ])
    days = r.daily["days"]
    assert days[0]["date"] == "2026-03-01"
    assert days[0]["revenue"] == "30.00"
    assert days[0]["top_user"] == "bob"

    assert days[1]["date"] == "2026-03-02"
    assert days[1]["revenue"] == "60.00"
    assert days[1]["top_user"] == "ann"


def test_daily_summary_zero_revenue_top_user_null():
    r = run_job([
        ev("e1", "ann", "09:00:00", "view"),
        ev("e2", "bob", "09:05:00", "click"),
    ])
    assert r.daily["days"][0]["revenue"] == "0.00"
    assert r.daily["days"][0]["top_user"] is None


def test_missing_input_file_exit_code_2():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""
