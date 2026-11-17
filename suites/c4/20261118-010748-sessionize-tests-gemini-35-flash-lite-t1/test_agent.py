"""Comprehensive test suite for sessionize batch job.
Guards against regressions in future implementations.
"""
import json
import os
import pytest
from helpers import ev, run_job, pick


def test_empty_and_whitespace_input_files_created():
    """Empty or whitespace-only inputs result in output files always being written."""
    r = run_job(["", "   ", "\t"], make_input=True)
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.daily["days"] == []
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 0


def test_user_trimming_and_case_insensitivity():
    """Users are compared after trimming and lower-casing; lower-cased form in output."""
    r = run_job([
        ev("e1", "  Ann  ", "09:00:00"),
        ev("e2", "ANN", "09:05:00")
    ])
    assert r.code == 0
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["session_id"] == "ann-1"
    assert r.sessions[0]["events"] == "2"


def test_timestamp_parsing_variants():
    """Supports fractional seconds, Z, and various UTC offsets, rejects missing offset."""
    # Valid timestamps: bob is at 02:00 UTC (+07:00), ann at 09:00 UTC (Z), charlie at 14:30 UTC (-05:30)
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00.250Z"),
        ev("e2", "bob", "2026-03-01T09:00:00+07:00"),
        ev("e3", "charlie", "2026-03-01T09:00:00.123456-05:30")
    ])
    assert r.code == 0
    assert len(r.sessions) == 3
    # Ordered by start ascending: bob (02:00:00Z), ann (09:00:00Z), charlie (14:30:00Z)
    assert r.sessions[0]["user"] == "bob" and r.sessions[0]["start"] == "2026-03-01T02:00:00Z"
    assert r.sessions[1]["user"] == "ann" and r.sessions[1]["start"] == "2026-03-01T09:00:00Z" # fractional seconds cut off
    assert r.sessions[2]["user"] == "charlie" and r.sessions[2]["start"] == "2026-03-01T14:30:00Z"


def test_rejection_order_and_reasons():
    """Lines are rejected in exact order: bad_json, missing_field, bad_ts, bad_type, bad_amount."""
    lines = [
        "{bad json",                                    # 1: bad_json
        json.dumps({"event_id": "e2", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # 2: missing_field (user absent)
        ev("e3", "ann", "2026-03-01T09:00:00"),          # 3: bad_ts (missing offset)
        ev("e4", "ann", "09:00:00", "unknown_type"),     # 4: bad_type
        ev("e5", "ann", "09:00:00", "purchase", amount="-1"), # 5: bad_amount (negative)
        ev("e6", "ann", "09:00:00", "purchase"),           # 5: bad_amount (missing amount)
        ev("e7", "ann", "09:00:00", "purchase", amount="19.999"), # 5: bad_amount (>2 decimals)
        ev("e8", "ann", "09:00:00", "purchase", amount="abc"),    # 5: bad_amount (not decimal)
    ]
    r = run_job(lines)
    assert r.code == 0
    expected_rejects = [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "missing_field"},
        {"line": 3, "reason": "bad_ts"},
        {"line": 4, "reason": "bad_type"},
        {"line": 5, "reason": "bad_amount"},
        {"line": 6, "reason": "bad_amount"},
        {"line": 7, "reason": "bad_amount"},
        {"line": 8, "reason": "bad_amount"},
    ]
    assert r.rejects == expected_rejects
    assert r.daily["rejected"] == len(expected_rejects)


def test_non_dict_json_is_bad_json():
    """JSON arrays or literals are rejected as bad_json."""
    r = run_job(["[1, 2, 3]", '"just a string"', "123", "true", "null"])
    assert r.code == 0
    assert r.rejects == [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "bad_json"},
        {"line": 3, "reason": "bad_json"},
        {"line": 4, "reason": "bad_json"},
        {"line": 5, "reason": "bad_json"},
    ]


def test_rejects_line_numbering_with_blank_lines():
    """Blank and whitespace-only lines are skipped but count towards physical line numbering."""
    lines = [
        ev("e1", "ann", "09:00:00"), # 1
        "",                            # 2
        "   ",                         # 3
        "{bad_json"                    # 4
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 4, "reason": "bad_json"}]


def test_amount_ignored_for_non_purchase():
    """Amount field is ignored for view/click even if invalid or missing."""
    r = run_job([
        ev("e1", "ann", "09:00:00", "view", amount="invalid"),
        ev("e2", "ann", "09:01:00", "click")
    ])
    assert r.code == 0
    assert r.sessions[0]["purchases"] == "0"
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.daily["rejected"] == 0


def test_duplicate_tie_breaking():
    """Duplicates: keep earliest timestamp; if timestamps equal, keep first in file."""
    lines = [
        ev("e1", "ann", "09:05:00"), # later
        ev("e1", "ann", "09:00:00"), # earlier (kept)
        ev("e2", "ann", "09:02:00", type="view"), # first of e2
        ev("e2", "ann", "09:02:00", type="click"), # duplicate e2 with equal ts -> keep first in file
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "2" # e1 at 09:00 and first e2 at 09:02
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:02:00Z"


def test_session_gap_boundary():
    """Gap of exactly 30 minutes stays in same session; > 30 minutes starts new session."""
    r_exact = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:00")
    ])
    assert len(r_exact.sessions) == 1
    assert r_exact.sessions[0]["session_id"] == "ann-1"

    r_over = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:01")
    ])
    assert len(r_over.sessions) == 2
    assert pick(r_over.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"),
        ("ann-2", "2026-03-01T09:30:01Z")
    ]


def test_session_ordering_rules():
    """Sessions ordered by start ascending, then user ascending."""
    r = run_job([
        ev("e1", "bob", "10:00:00"),
        ev("e2", "ann", "09:00:00"),
        ev("e3", "cara", "09:00:00"),
    ])
    assert pick(r.sessions, "session_id", "user", "start") == [
        ("ann-1", "ann", "2026-03-01T09:00:00Z"),
        ("cara-1", "cara", "2026-03-01T09:00:00Z"),
        ("bob-1", "bob", "2026-03-01T10:00:00Z"),
    ]


def test_daily_summary_midnight_and_top_user_ties():
    """Sessions across midnight belong to start date; top_user tie-breaks alphabetically."""
    r = run_job([
        ev("e1", "bob", "2026-03-01T23:50:00Z", "purchase", amount="10.00"),
        ev("e2", "ann", "2026-03-01T23:55:00Z", "purchase", amount="10.00"),
        # session running past midnight
        ev("e3", "ann", "2026-03-01T23:58:00Z", "purchase", amount="5.00"),
        ev("e4", "ann", "2026-03-02T00:15:00Z", "purchase", amount="5.00"),
    ])
    assert r.code == 0
    # ann session 1: start 23:55, end 00:15 (runs past midnight, belongs to 2026-03-01 date of start).
    # ann revenue on 03-01: 10.00 + 5.00 = 15.00.
    # bob revenue on 03-01: 10.00.
    # top_user for 03-01: ann (rev 15.00 > bob 10.00).
    assert r.daily["days"][0]["top_user"] == "ann"

    # Test top_user alphabetical tie break:
    r_tie = run_job([
        ev("e1", "bob", "2026-03-01T09:00:00Z", "purchase", amount="10.00"),
        ev("e2", "ann", "2026-03-01T09:00:00Z", "purchase", amount="10.00"),
    ])
    assert r_tie.code == 0
    # ann and bob both have 10.00 revenue on 2026-03-01. Tie-break: user that sorts first -> 'ann'.
    assert r_tie.daily["days"][0]["top_user"] == "ann"


def test_top_user_null_when_zero_revenue():
    """top_user is null when day's revenue is zero."""
    r = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00Z", "view"),
        ev("e2", "bob", "2026-03-01T10:00:00Z", "click"),
    ])
    assert r.code == 0
    assert r.daily["days"][0]["revenue"] == "0.00"
    assert r.daily["days"][0]["top_user"] is None


def test_missing_input_file_exit_code():
    """If events.jsonl does not exist: exit code 2 and a message on stderr."""
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""
