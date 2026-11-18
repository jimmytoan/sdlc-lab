"""Thorough pytest test suite for sessionize job."""
import json
import os
import tempfile
import subprocess
import sys
from helpers import ev, run_job, pick


def test_empty_input_file():
    r = run_job([])
    assert r.code == 0
    assert sorted(r.files) == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_blank_and_whitespace_lines():
    # Blank lines and whitespace-only lines are skipped for events but count for line numbering in rejects
    lines = [
        "",
        "   ",
        ev("e1", "ann", "09:00:00"),
        "",
        "{bad json",
    ]
    r = run_job(lines)
    assert r.code == 0
    assert len(r.sessions) == 1
    assert r.rejects == [{"line": 5, "reason": "bad_json"}]
    assert r.daily["rejected"] == 1


def test_user_normalization_trimming_and_lowercase():
    r = run_job([ev("e1", "  ANN  ", "09:00:00"), ev("e2", "ann", "09:05:00")])
    assert r.code == 0
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["session_id"] == "ann-1"


def test_timestamp_variations_and_invalid():
    # Valid timestamps: Z, offsets, fractional seconds
    r1 = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00Z"),
        ev("e2", "ann", "2026-03-01T09:01:00+02:00"),  # 07:01:00 UTC
        ev("e3", "ann", "2026-03-01T07:02:00.500Z"),  # within 30 min of 07:01:00 UTC
    ])
    assert r1.code == 0
    assert len(r1.sessions) == 2  # 07:01/07:02 in session 1, 09:00 in session 2 (gap > 30m)

    # All three in same session if close in UTC
    r1_single = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00Z"),
        ev("e2", "ann", "2026-03-01T09:01:00Z"),
        ev("e3", "ann", "2026-03-01T09:02:00.500Z"),
    ])
    assert r1_single.code == 0
    assert len(r1_single.sessions) == 1
    assert r1_single.sessions[0]["events"] == "3"

    # Invalid timestamps: missing offset, invalid hour/minute offset
    r2 = run_job([
        ev("e1", "ann", "2026-03-01T09:00:00"),  # missing offset -> bad_ts
        ev("e2", "ann", "2026-03-01T09:00:00+25:00"),  # invalid hour -> bad_ts
        ev("e3", "ann", "not-a-timestamp"),
    ])
    assert r2.code == 0
    assert r2.rejects == [
        {"line": 1, "reason": "bad_ts"},
        {"line": 2, "reason": "bad_ts"},
        {"line": 3, "reason": "bad_ts"},
    ]


def test_rejection_priority_order():
    # 1. bad_json vs missing_field vs bad_ts vs bad_type vs bad_amount
    lines = [
        "not json",  # bad_json syntax error
        "[1, 2, 3]",  # bad_json not a dict
        "42",  # bad_json not a dict
        json.dumps({"event_id": "", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # missing_field: empty string after trim
        json.dumps({"user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # missing_field: absent event_id
        json.dumps({"event_id": "e2", "user": 123, "ts": "2026-03-01T09:00:00Z", "type": "view"}),  # missing_field: non-string user
        json.dumps({"event_id": "e3", "user": "ann", "ts": "bad-ts", "type": "view"}),  # bad_ts
        json.dumps({"event_id": "e4", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "dance"}),  # bad_type
        json.dumps({"event_id": "e5", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase"}),  # bad_amount: missing
        json.dumps({"event_id": "e6", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": "-5"}),  # bad_amount: negative
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        {"line": 1, "reason": "bad_json"},
        {"line": 2, "reason": "bad_json"},
        {"line": 3, "reason": "bad_json"},
        {"line": 4, "reason": "missing_field"},
        {"line": 5, "reason": "missing_field"},
        {"line": 6, "reason": "missing_field"},
        {"line": 7, "reason": "bad_ts"},
        {"line": 8, "reason": "bad_type"},
        {"line": 9, "reason": "bad_amount"},
        {"line": 10, "reason": "bad_amount"},
    ]


def test_amount_rules_for_purchase_and_others():
    # Non-purchase with invalid amount should not be rejected
    r1 = run_job([ev("e1", "ann", "09:00:00", "view", amount="invalid")])
    assert r1.code == 0
    assert r1.rejects == []

    # Purchase with valid amounts: integer string, one decimal, two decimals
    r2 = run_job([
        ev("e1", "ann", "09:00:00", "purchase", amount="5"),
        ev("e2", "ann", "09:01:00", "purchase", amount="5.5"),
        ev("e3", "ann", "09:02:00", "purchase", amount="19.99"),
    ])
    assert r2.code == 0
    assert r2.sessions[0]["revenue"] == "30.49"

    # Purchase with invalid amounts: >2 fraction digits, non-numeric
    r3 = run_job([
        ev("e1", "ann", "09:00:00", "purchase", amount="19.999"),
        ev("e2", "ann", "09:01:00", "purchase", amount="abc"),
    ])
    assert r3.code == 0
    assert len(r3.rejects) == 2
    assert r3.rejects[0]["reason"] == "bad_amount"
    assert r3.rejects[1]["reason"] == "bad_amount"


def test_duplicate_handling():
    # Same event_id, keep earliest ts; if ts equal, keep first in file.
    lines = [
        ev("e1", "ann", "09:05:00"),
        ev("e1", "ann", "09:00:00"),  # earlier ts -> kept
        ev("e1", "ann", "09:00:00"),  # duplicate of 09:00:00, comes later -> dropped
        ev("e2", "ann", "09:10:00"),
        ev("e2", "ann", "09:10:00"),  # same ts, comes later -> dropped (first in file kept)
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.sessions[0]["events"] == "2"  # e1 (at 09:00) and e2 (at 09:10)
    assert r.daily["duplicates_dropped"] == 3
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:10:00Z"


def test_session_gap_boundaries():
    # Exactly 30 minutes gap stays in same session
    r1 = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:00"),
    ])
    assert len(r1.sessions) == 1
    assert r1.sessions[0]["session_id"] == "ann-1"
    assert r1.sessions[0]["events"] == "2"

    # 30 minutes and 1 second gap starts new session
    r2 = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:30:01"),
    ])
    assert len(r2.sessions) == 2
    assert pick(r2.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"),
        ("ann-2", "2026-03-01T09:30:01Z"),
    ]


def test_session_duration_and_rounding():
    r = run_job([
        ev("e1", "ann", "09:00:00"),
        ev("e2", "ann", "09:10:30"),
    ])
    s = r.sessions[0]
    assert s["duration_s"] == "630"
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:10:30Z"


def test_session_sorting_order():
    # Sessions ordered by start ascending, then user ascending
    r = run_job([
        ev("e1", "bob", "09:10:00"),
        ev("e2", "ann", "09:00:00"),
        ev("e3", "charlie", "09:00:00"),
    ])
    assert pick(r.sessions, "session_id", "user") == [
        ("ann-1", "ann"),
        ("charlie-1", "charlie"),
        ("bob-1", "bob"),
    ]


def test_daily_summary_midnight_and_top_user_ties():
    # Session running past midnight belongs to UTC date of start
    # Tie-breaking for top_user (alphabetical sorting)
    # Zero revenue day -> top_user is null
    lines = [
        ev("e1", "bob", "2026-03-01T23:50:00Z", "purchase", amount="10.00"),
        ev("e2", "ann", "2026-03-01T23:55:00Z", "purchase", amount="10.00"),  # tie with bob, ann sorts first -> top_user "ann"
        ev("e3", "charlie", "2026-03-02T01:00:00Z"),  # zero revenue session on Mar 2
    ]
    r = run_job(lines)
    assert r.daily["days"] == [
        {
            "date": "2026-03-01",
            "sessions": 2,
            "users": 2,
            "revenue": "20.00",
            "top_user": "ann",
        },
        {
            "date": "2026-03-02",
            "sessions": 1,
            "users": 1,
            "revenue": "0.00",
            "top_user": None,
        },
    ]


def test_output_directory_creation():
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        out = os.path.join(d, "nonexistent", "sub", "out")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(ev("e1", "ann", "09:00:00") + "\n")
        app_dir = os.environ.get("APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, os.path.join(app_dir, "job.py"),
                           "--in", inp, "--out", out], capture_output=True, text=True)
        assert p.returncode == 0
        assert os.path.isdir(out)
        assert "sessions.csv" in os.listdir(out)


def test_missing_input_file():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""
