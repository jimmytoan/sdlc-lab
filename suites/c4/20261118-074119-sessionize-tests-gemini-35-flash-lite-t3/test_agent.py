"""Thorough pytest test suite for sessionize job batch job."""
from helpers import ev, run_job, pick
import json


def test_empty_input_file():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {
        "days": [],
        "duplicates_dropped": 0,
        "rejected": 0
    }


def test_whitespace_lines_and_line_numbers():
    lines = [
        "",
        "   ",
        ev("e1", "ann", "09:00:00"),
        '{"invalid json}',
        ev("e2", "ann", "09:05:00")
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 4, "reason": "bad_json"}]
    assert r.daily["rejected"] == 1
    assert len(r.sessions) == 1
    assert r.sessions[0]["events"] == "2"


def test_rejection_priority_order():
    # 1. bad_json vs missing_field: bad_json wins if not valid JSON
    # 2. missing_field vs bad_ts: missing field wins
    # 3. bad_ts vs bad_type: bad_ts wins
    # 4. bad_type vs bad_amount: bad_type wins
    lines = [
        '{bad json',  # bad_json (line 1)
        json.dumps({"event_id": "e1", "ts": "09:00:00", "type": "view"}),  # missing_field 'user' (line 2)
        json.dumps({"event_id": "e1", "user": "ann", "ts": "not-a-ts", "type": "view"}),  # bad_ts (line 3)
        json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "dance"}),  # bad_type (line 4)
        json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": "invalid"}),  # bad_amount (line 5)
    ]
    r = run_job(lines)
    assert r.rejects == [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "missing_field"},
        {"line": 3, "reason": "bad_ts"},
        {"line": 4, "reason": "bad_type"},
        {"line": 5, "reason": "bad_amount"},
    ]


def test_missing_field_variants():
    # empty string, whitespace string, missing key, non-string value (null, number)
    lines = [
        json.dumps({"event_id": "", "user": "ann", "ts": "09:00:00", "type": "view"}),
        json.dumps({"event_id": "e1", "user": "   ", "ts": "09:00:00", "type": "view"}),
        json.dumps({"event_id": "e1", "user": "ann", "ts": None, "type": "view"}),
        json.dumps({"event_id": "e1", "user": "ann", "ts": "09:00:00", "type": 123}),
    ]
    r = run_job(lines)
    assert len(r.rejects) == 4
    for item in r.rejects:
        assert item["reason"] == "missing_field"


def test_bad_ts_variants():
    # missing offset, invalid offset format, invalid date/time
    lines = [
        ev("e1", "ann", "2026-03-01T09:00:00"),  # no offset
        ev("e2", "ann", "2026-02-30T09:00:00Z"),  # invalid calendar date
        ev("e3", "ann", "2026-03-01T25:00:00Z"),  # invalid hour
        ev("e4", "ann", "2026-03-01T09:00:00+25:00"),  # invalid offset hour
        ev("e5", "ann", "2026-03-01T09:00:00+00:60"),  # invalid offset minute
        ev("e6", "ann", "not-a-timestamp-at-all"),
    ]
    r = run_job(lines)
    assert len(r.rejects) == 6
    assert all(item["reason"] == "bad_ts" for item in r.rejects)


def test_amount_ignored_for_non_purchase():
    # view and click with bad or missing amount should not be rejected
    lines = [
        ev("e1", "ann", "09:00:00", "view", amount="invalid"),
        ev("e2", "ann", "09:01:00", "click"),
    ]
    r = run_job(lines)
    assert r.rejects == []
    assert len(r.sessions) == 1
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[0]["purchases"] == "0"
    assert r.sessions[0]["revenue"] == "0.00"


def test_bad_amount_variants():
    # negative, >2 fraction digits, non-string, missing amount for purchase
    lines = [
        ev("e1", "ann", "09:00:00", "purchase", amount="-5.00"),
        ev("e2", "ann", "09:01:00", "purchase", amount="19.999"),
        ev("e3", "ann", "09:02:00", "purchase", amount=19.99),
        ev("e4", "ann", "09:03:00", "purchase"),  # missing amount
    ]
    r = run_job(lines)
    assert len(r.rejects) == 4
    assert all(item["reason"] == "bad_amount" for item in r.rejects)


def test_duplicate_events_tie_breaking():
    # Same event_id: keep earliest timestamp. If timestamps equal, keep first in file.
    lines = [
        ev("e1", "ann", "09:10:00"),  # later ts
        ev("e1", "ann", "09:00:00"),  # earliest ts (wins)
        ev("e1", "ann", "09:00:00"),  # duplicate of 09:00:00 but later in file
        ev("e2", "ann", "09:02:00"),
        ev("e2", "ann", "09:02:00"),  # exact same ts and id, second is dropped
    ]
    r = run_job(lines)
    assert r.daily["duplicates_dropped"] == 3
    assert r.rejects == []
    assert r.sessions[0]["events"] == "2"  # e1 (at 09:00:00) and e2 (at 09:02:00)
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"


def test_user_normalization():
    lines = [
        ev("e1", "  Ann  ", "09:00:00"),
        ev("e2", "ANN", "09:05:00"),
        ev("e3", "ann", "09:10:00"),
    ]
    r = run_job(lines)
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["events"] == "3"


def test_session_gap_thresholds():
    # Gap > 30 minutes splits session. Gap <= 30 minutes stays in same session.
    # 30 minutes = 1800 seconds.
    lines = [
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:00"),  # exactly 30 min gap -> same session
        ev("e3", "ann", "10:00:01"),  # 30 min 1 sec gap -> new session
    ]
    r = run_job(lines)
    assert len(r.sessions) == 2
    assert r.sessions[0]["session_id"] == "ann-1"
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[1]["session_id"] == "ann-2"
    assert r.sessions[1]["events"] == "1"


def test_session_sorting_by_start_and_user():
    # Tie in start timestamp: sorted by user ascending
    lines = [
        ev("e1", "bob", "10:00:00"),
        ev("e2", "ann", "10:00:00"),
        ev("e3", "charlie", "09:00:00"),
    ]
    r = run_job(lines)
    assert pick(r.sessions, "session_id", "user") == [
        ("charlie-1", "charlie"),
        ("ann-1", "ann"),
        ("bob-1", "bob"),
    ]


def test_timestamp_fractional_seconds_precision():
    # Fractional seconds cut off in output start/end, but used in duration and gaps
    lines = [
        ev("e1", "ann", "2026-03-01T09:00:00.100Z"),
        ev("e2", "ann", "2026-03-01T09:00:30.900Z"),
    ]
    r = run_job(lines)
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:00:30Z"
    # Duration: from 00.100 to 30.900 is 30.8 seconds -> rounded down to 30 seconds
    assert r.sessions[0]["duration_s"] == "30"


def test_session_running_past_midnight():
    # Gap <= 30 minutes so events stay in the same session, even when crossing midnight
    lines = [
        ev("e1", "ann", "2026-03-01T23:45:00Z"),
        ev("e2", "ann", "2026-03-02T00:10:00Z"),
    ]
    r = run_job(lines)
    assert len(r.sessions) == 1
    assert r.sessions[0]["start"] == "2026-03-01T23:45:00Z"
    assert r.sessions[0]["end"] == "2026-03-02T00:10:00Z"
    # Belongs to date 2026-03-01 (date of its start)
    assert r.daily["days"][0]["date"] == "2026-03-01"
    assert r.daily["days"][0]["sessions"] == 1


def test_daily_summary_top_user_ties_and_zero_revenue():
    lines = [
        ev("e1", "bob", "09:00:00", "purchase", amount="10.00"),
        ev("e2", "ann", "09:05:00", "purchase", amount="10.00"),  # tie in revenue with bob, 'ann' sorts first alphabetically
        ev("e3", "charlie", "10:00:00"),  # zero revenue session
    ]
    r = run_job(lines)
    day = r.daily["days"][0]
    assert day["revenue"] == "20.00"
    assert day["top_user"] == "ann"  # tie broken by alphabetical user sort

    # Test zero revenue day top_user is null
    r2 = run_job([ev("e1", "ann", "09:00:00")])
    assert r2.daily["days"][0]["top_user"] is None
