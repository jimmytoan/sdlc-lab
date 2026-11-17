"""Public acceptance examples for sessionize change request 1."""
from helpers import ev, run_job, pick


def test_gap_minutes_option():
    lines = [ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:10:00")]
    r = run_job(lines, "--gap-minutes", "5")
    assert r.code == 0 and len(r.sessions) == 2 and r.daily["gap_minutes"] == 5
    assert len(run_job(lines).sessions) == 1


def test_logout_closes_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:01:00", "logout"), ev("e3", "ann", "09:02:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("ann-2", "1")]


def test_refund_reduces_revenue():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="20.00"), ev("e2", "ann", "09:05:00", "refund", amount="5.50")])
    assert r.header == ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "refunds", "revenue"]
    assert pick(r.sessions, "purchases", "refunds", "revenue") == [("1", "1", "14.50")]
    assert r.daily["days"][0]["revenue"] == "14.50"
