"""Public acceptance examples for sessionize. The coding agent may read and run these."""
from helpers import ev, run_job, pick


def test_single_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:05:00", "click"),
                 ev("e3", "ann", "09:10:30", "purchase", amount="19.99")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert len(r.sessions) == 1
    s = r.sessions[0]
    assert s["session_id"] == "ann-1" and s["user"] == "ann"
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:10:30Z"
    assert s["duration_s"] == "630" and s["events"] == "3" and s["purchases"] == "1" and s["revenue"] == "19.99"


def test_gap_splits_sessions():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:45:00")])
    assert pick(r.sessions, "session_id", "start") == [("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T09:45:00Z")]


def test_two_users_sorted_by_start():
    r = run_job([ev("e1", "bob", "10:00:00"), ev("e2", "ann", "09:00:00")])
    assert pick(r.sessions, "session_id") == [("ann-1",), ("bob-1",)]


def test_timezone_converted_to_utc():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00+07:00")])
    assert r.sessions[0]["start"] == "2026-03-01T02:00:00Z"


def test_duplicate_event_dropped():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:01:00")])
    assert r.sessions[0]["events"] == "2"
    assert r.daily["duplicates_dropped"] == 1


def test_rejects():
    r = run_job([ev("e1", "ann", "09:00:00"), "{not json", ev("e3", "ann", "09:01:00", "dance")])
    assert r.code == 0
    assert r.rejects == [{"line": 2, "reason": "bad_json"}, {"line": 3, "reason": "bad_type"}]
    assert r.daily["rejected"] == 2 and r.sessions[0]["events"] == "1"


def test_daily_summary():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5.00"), ev("e2", "bob", "09:00:00", "purchase", amount="20"),
                 ev("e3", "ann", "2026-03-02T09:00:00Z")])
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "25.00", "top_user": "bob"},
        {"date": "2026-03-02", "sessions": 1, "users": 1, "revenue": "0.00", "top_user": None}]


def test_missing_input_file():
    r = run_job([], make_input=False)
    assert r.code == 2 and r.stderr.strip() != ""
