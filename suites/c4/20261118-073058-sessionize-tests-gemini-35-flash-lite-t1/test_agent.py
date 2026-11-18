"""Thorough test suite for sessionize job to guard against regressions."""
import json
import os
from helpers import ev, run_job, pick


def test_empty_input_files_created():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    assert r.rejects == []


def test_whitespace_and_blank_lines():
    r = run_job(["", "   ", ev("e1", "ann", "09:00:00"), "\n", ev("e2", "bob", "09:01:00")])
    assert r.code == 0
    assert len(r.sessions) == 2
    assert r.daily["rejected"] == 0


def test_rejection_order_and_reasons():
    lines = [
        "not json",  # line 1: bad_json
        json.dumps({"event_id": "", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # line 2: missing_field
        json.dumps({"event_id": "e2", "user": "ann", "ts": "bad-ts", "type": "view"}),  # line 3: bad_ts
        json.dumps({"event_id": "e3", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "dance"}),  # line 4: bad_type
        json.dumps({"event_id": "e4", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": "-5"}),  # line 5: bad_amount
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "missing_field"},
        {"line": 3, "reason": "bad_ts"},
        {"line": 4, "reason": "bad_type"},
        {"line": 5, "reason": "bad_amount"},
    ]


def test_missing_field_variants():
    r = run_job([
        json.dumps({"user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # missing event_id
        json.dumps({"event_id": "e1", "user": "   ", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # empty after trim
        json.dumps({"event_id": "e1", "user": "ann", "ts": 123, "type": "view"}),  # ts not string
        json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": None}),  # type null
    ])
    assert len(r.rejects) == 4
    for rej in r.rejects:
        assert rej["reason"] == "missing_field"


def test_bad_ts_variants():
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00"),  # missing offset
        ev("e2", "ann", "2026-02-30T09:00:00Z"),  # invalid date
        ev("e3", "ann", "2026-03-01T25:00:00Z"),  # invalid hour
        ev("e4", "ann", "2026-03-01T09:00:00+25:00"),  # invalid offset hour
    ])
    assert len(r.rejects) == 4
    for rej in r.rejects:
        assert rej["reason"] == "bad_ts"


def test_bad_amount_variants():
    r = run_job([
        ev("e1", "ann", "09:00:00", "purchase"),  # missing amount
        ev("e2", "ann", "09:00:00", "purchase", amount=19.99),  # amount not string
        ev("e3", "ann", "09:00:00", "purchase", amount="abc"),  # non-decimal
        ev("e4", "ann", "09:00:00", "purchase", amount="19.999"),  # >2 fraction digits
        ev("e5", "ann", "09:00:00", "purchase", amount="-1.00"),  # negative
    ])
    assert len(r.rejects) == 5
    for rej in r.rejects:
        assert rej["reason"] == "bad_amount"


def test_amount_ignored_for_non_purchase():
    r = run_job([
        ev("e1", "ann", "09:00:00", "view", amount="invalid-amount"),
        ev("e2", "ann", "09:05:00", "click", amount="-99"),
    ])
    assert r.code == 0
    assert r.rejects == []
    assert len(r.sessions) == 1


def test_user_normalization_and_comparison():
    r = run_job([
        ev("e1", "  Ann  ", "09:00:00"),
        ev("e2", "ANN", "09:05:00"),
        ev("e3", "ann", "09:10:00"),
    ])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["events"] == "3"


def test_timestamp_offsets_and_fractional():
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00.250Z"),
        ev("e2", "bob", "2026-03-01T09:00:00+02:00"),
        ev("e3", "charlie", "2026-03-01T09:00:00-05:30"),
    ])
    assert r.code == 0
    assert len(r.sessions) == 3
    sessions_by_user = {s["user"]: s["start"] for s in r.sessions}
    assert sessions_by_user["ann"] == "2026-03-01T09:00:00Z"
    assert sessions_by_user["bob"] == "2026-03-01T07:00:00Z"
    assert sessions_by_user["charlie"] == "2026-03-01T14:30:00Z"


def test_duplicate_resolution_rules():
    r = run_job([
        ev("e1", "ann", "09:05:00", "view"),
        ev("e1", "ann", "09:00:00", "click"),
        ev("e1", "ann", "09:05:00", "purchase", amount="10.00"),
        ev("e2", "ann", "09:10:00", "view"),
        ev("e2", "ann", "09:10:00", "purchase", amount="5.00"),
    ])
    assert r.daily["duplicates_dropped"] == 3
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[0]["purchases"] == "0"


def test_session_gap_boundary():
    r = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:00"),
        ev("e3", "ann", "10:00:01"),
    ])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("ann-2", "1")]


def test_session_duration_and_revenue():
    r = run_job([
        ev("e1", "ann", "09:00:00", "purchase", amount="10.5"),
        ev("e2", "ann", "09:00:45", "purchase", amount="9.49"),
    ])
    s = r.sessions[0]
    assert s["duration_s"] == "45"
    assert s["purchases"] == "2"
    assert s["revenue"] == "19.99"


def test_session_sorting():
    r = run_job([
        ev("e1", "bob", "09:00:00"),
        ev("e2", "ann", "09:00:00"),
        ev("e3", "charlie", "08:00:00"),
    ])
    assert pick(r.sessions, "session_id", "user") == [
        ("charlie-1", "charlie"),
        ("ann-1", "ann"),
        ("bob-1", "bob"),
    ]


def test_daily_summary_midnight_and_top_user_ties():
    r = run_job([
        ev("e1", "ann", "2026-03-01T23:50:00Z", "purchase", amount="10.00"),
        ev("e2", "ann", "2026-03-02T00:10:00Z", "purchase", amount="5.00"),
        ev("e3", "bob", "2026-03-02T05:00:00Z", "purchase", amount="10.00"),
        ev("e4", "alice", "2026-03-02T05:00:00Z", "purchase", amount="10.00"),
    ])
    days = {d["date"]: d for d in r.daily["days"]}
    assert days["2026-03-01"]["revenue"] == "15.00"
    assert days["2026-03-01"]["top_user"] == "ann"
    assert days["2026-03-02"]["revenue"] == "20.00"
    assert days["2026-03-02"]["top_user"] == "alice"


def test_top_user_null_when_revenue_zero():
    r = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "bob", "09:00:00"),
    ])
    assert r.daily["days"][0]["revenue"] == "0.00"
    assert r.daily["days"][0]["top_user"] is None


def test_missing_input_file_exit_code():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""
