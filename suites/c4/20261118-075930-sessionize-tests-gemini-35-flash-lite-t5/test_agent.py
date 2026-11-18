"""Thorough test suite for sessionize batch job (test_agent.py)."""
import json
import os
import tempfile
import subprocess
import sys
from helpers import ev, run_job, pick


def test_blank_lines_and_line_numbering():
    """Blank lines are skipped but count towards 1-based line numbers in rejects."""
    lines = [
        "",
        "   ",
        ev("e1", "", "09:00:00"),  # line 3: missing_field
        "",
        "{invalid json",  # line 5: bad_json
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        {"line": 3, "reason": "missing_field"},
        {"line": 5, "reason": "bad_json"},
    ]
    assert r.daily["rejected"] == 2


def test_validation_order_and_rules():
    """Tests all rejection reasons in correct priority order per line."""
    # Priority: 1. bad_json, 2. missing_field, 3. bad_ts, 4. bad_type, 5. bad_amount
    lines = [
        "not-json-object",
        json.dumps({"user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}),
        json.dumps({"event_id": "e2", "user": "ann", "ts": "2026-03-01T09:00:00", "type": "view"}),
        json.dumps({"event_id": "e3", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "dance"}),
        json.dumps({"event_id": "e4", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": "-5"}),
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


def test_amount_rules_and_non_purchase_ignored():
    """Amount rules apply to purchases; non-purchases ignore invalid or missing amounts."""
    lines = [
        ev("e1", "ann", "09:00:00", "view", amount="invalid"),
        ev("e2", "ann", "09:01:00", "click"),
        ev("e3", "ann", "09:02:00", "purchase", amount="0"),
        ev("e4", "ann", "09:03:00", "purchase", amount="5.5"),
        ev("e5", "ann", "09:04:00", "purchase", amount="19.99"),
        ev("e6", "ann", "09:05:00", "purchase", amount="19.999"),  # > 2 fraction digits
        ev("e7", "ann", "09:06:00", "purchase", amount="-1.00"),  # negative
        ev("e8", "ann", "09:07:00", "purchase"),                  # missing amount
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.sessions[0]["events"] == "5"  # e1, e2, e3, e4, e5 kept
    assert r.daily["rejected"] == 3
    assert r.sessions[0]["revenue"] == "25.49"


def test_timestamp_parsing_variants_and_offsets():
    """Tests various valid ISO 8601 timestamps with offsets and fractional seconds, plus invalid offsets."""
    lines = [
        ev("e1", "ann", "2026-03-01T09:00:00Z"),
        ev("e2", "ann", "2026-03-01T09:05:00+00:00"),
        ev("e3", "ann", "2026-03-01T04:30:00.250-04:30"),  # 04:30 - (-04:30) = 09:00:00 UTC
        ev("e4", "ann", "2026-03-01T09:00:00+25:00"),
        ev("e5", "ann", "2026-03-01T09:00:00+07:60"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.daily["rejected"] == 2
    assert sum(int(s["events"]) for s in r.sessions) == 3


def test_user_trimming_and_lowercasing():
    """Users are trimmed and lower-cased across input and output."""
    r = run_job([
        ev("e1", "  Ann  ", "09:00:00"),
        ev("e2", "ANN", "09:01:00"),
    ])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["events"] == "2"


def test_duplicate_events_tie_breaking():
    """Duplicates: keep earliest timestamp; if equal ts, keep first in file (file order)."""
    lines = [
        ev("e1", "ann", "09:05:00"),  # line 1
        ev("e1", "ann", "09:00:00"),  # line 2 (earlier ts -> kept)
        ev("e2", "ann", "09:02:00"),  # line 3
        ev("e2", "ann", "09:02:00"),  # line 4 (equal ts -> keep line 3, drop line 4)
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:02:00Z"
    assert r.sessions[0]["events"] == "2"


def test_session_gap_boundaries():
    """Gap > 30 mins starts new session; gap <= 30 mins keeps same session."""
    r1 = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00")])
    assert len(r1.sessions) == 1
    assert r1.sessions[0]["events"] == "2"

    r2 = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01")])
    assert len(r2.sessions) == 2
    assert r2.sessions[0]["session_id"] == "ann-1"
    assert r2.sessions[1]["session_id"] == "ann-2"


def test_fractional_seconds_precision_in_gaps_and_ordering():
    """Fractional seconds are cut off in output start/end, but used in ordering and gap calculations."""
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00.900Z"),
        ev("e2", "ann", "2026-03-01T09:30:00.800Z"),
    ])
    assert len(r.sessions) == 1
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:30:00Z"
    assert r.sessions[0]["duration_s"] == "1799"


def test_sessions_ordering_by_start_and_user():
    """Sessions ordered by start ascending, then user ascending."""
    r = run_job([
        ev("e1", "bob", "09:00:00"),
        ev("e2", "ann", "09:00:00"),
        ev("e3", "charlie", "08:00:00"),
    ])
    assert pick(r.sessions, "session_id", "user", "start") == [
        ("charlie-1", "charlie", "2026-03-01T08:00:00Z"),
        ("ann-1", "ann", "2026-03-01T09:00:00Z"),
        ("bob-1", "bob", "2026-03-01T09:00:00Z"),
    ]


def test_daily_summary_midnight_crossing_and_top_user():
    """Daily summary handles sessions running past midnight, correct date assignment, and top user tie-breaking."""
    lines = [
        # Session starting March 1 at 23:45:00, ending March 2 at 00:10:00 (gap 25 mins -> same session belonging to March 1)
        ev("e1", "bob", "2026-03-01T23:45:00Z", "purchase", amount="10.00"),
        ev("e2", "bob", "2026-03-02T00:10:00Z"),
        # March 1 session with ann (revenue 10.00, tie with bob revenue 10.00 -> alphabetical 'ann' wins top_user)
        ev("e3", "ann", "2026-03-01T10:00:00Z", "purchase", amount="10.00"),
        # March 2 session with charlie (revenue 0.00 -> top_user is null)
        ev("e4", "charlie", "2026-03-02T10:00:00Z"),
    ]
    r = run_job(lines)
    days = r.daily["days"]
    assert days[0]["date"] == "2026-03-01"
    assert days[0]["sessions"] == 2
    assert days[0]["users"] == 2
    assert days[0]["revenue"] == "20.00"
    assert days[0]["top_user"] == "ann"

    assert days[1]["date"] == "2026-03-02"
    assert days[1]["sessions"] == 1
    assert days[1]["users"] == 1
    assert days[1]["revenue"] == "0.00"
    assert days[1]["top_user"] is None


def test_empty_files_always_written():
    """All three output files are always written even when there are no rows."""
    r = run_job([], make_input=True)
    assert r.code == 0
    assert set(r.files) == {"daily.json", "rejects.jsonl", "sessions.csv"}
    assert r.daily["days"] == []
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 0
    assert r.sessions == []
    assert r.rejects == []
