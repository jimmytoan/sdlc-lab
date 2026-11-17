# DFL-HOLDOUT-CANARY-7c1e4b9a2f6d — held-out scenarios for sessionize change request 1.
from helpers import ev, run_job, pick


def test_default_gap_reported_and_unchanged():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00"), ev("e3", "ann", "10:00:01")])
    assert r.daily["gap_minutes"] == 30
    assert pick(r.sessions, "events") == [("2",), ("1",)]


def test_gap_boundary_with_custom_value():
    lines = [ev("e1", "ann", "09:00:00"), ev("e2", "ann", "10:00:00"), ev("e3", "ann", "2026-03-01T11:00:00.001Z")]
    r = run_job(lines, "--gap-minutes", "60")
    assert pick(r.sessions, "events") == [("2",), ("1",)] and r.daily["gap_minutes"] == 60
    r = run_job(lines, "--gap-minutes", "1")
    assert len(r.sessions) == 3


def test_gap_minutes_invalid():
    for v in ("0", "-5", "abc", "2.5", ""):
        r = run_job([ev("e1", "ann", "09:00:00")], "--gap-minutes", v)
        assert r.code == 64 and r.stderr.strip() != "" and r.files == [], v


def test_logout_counts_and_duration():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:10:00", "logout")])
    assert pick(r.sessions, "events", "end", "duration_s", "purchases") == [("2", "2026-03-01T09:10:00Z", "600", "0")]


def test_logout_then_immediate_event_is_new_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:01:00", "logout"),
                 ev("e3", "ann", "09:01:00"), ev("e4", "ann", "09:02:00")])
    assert pick(r.sessions, "session_id", "start", "events") == [("ann-1", "2026-03-01T09:00:00Z", "2"),
                                                                 ("ann-2", "2026-03-01T09:01:00Z", "2")]


def test_logout_after_gap_is_its_own_closed_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "10:00:00", "logout"), ev("e3", "ann", "10:01:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "1"), ("ann-2", "1"), ("ann-3", "1")]


def test_two_logouts_in_a_row():
    r = run_job([ev("e1", "ann", "09:00:00", "logout"), ev("e2", "ann", "09:00:30", "logout")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "1"), ("ann-2", "1")]


def test_logout_only_affects_that_user():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:00:10"), ev("e3", "ann", "09:01:00", "logout"),
                 ev("e4", "bob", "09:02:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("bob-1", "2")]


def test_logout_out_of_order_in_file():
    r = run_job([ev("e3", "ann", "09:02:00"), ev("e2", "ann", "09:01:00", "logout"), ev("e1", "ann", "09:00:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("ann-2", "1")]


def test_refund_amount_validation():
    r = run_job([ev("e1", "ann", "09:00:00", "refund"), ev("e2", "ann", "09:00:00", "refund", amount="-1"),
                 ev("e3", "ann", "09:00:00", "refund", amount=3), ev("e4", "ann", "09:00:00", "logout", amount="junk")])
    assert [x["reason"] for x in r.rejects] == ["bad_amount"] * 3
    assert pick(r.sessions, "events", "refunds", "revenue") == [("1", "0", "0.00")]


def test_negative_revenue_format():
    r = run_job([ev("e1", "ann", "09:00:00", "refund", amount="5"), ev("e2", "ann", "09:01:00", "refund", amount="0.25")])
    assert pick(r.sessions, "purchases", "refunds", "revenue") == [("0", "2", "-5.25")]
    assert r.daily["days"][0]["revenue"] == "-5.25" and r.daily["days"][0]["top_user"] is None


def test_top_user_uses_net_revenue():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="50.00"), ev("e2", "ann", "09:01:00", "refund", amount="45.00"),
                 ev("e3", "bob", "09:00:00", "purchase", amount="10.00")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "15.00", "top_user": "bob"}]


def test_top_user_when_day_total_not_positive_but_one_user_is():
    r = run_job([ev("e1", "ann", "09:00:00", "refund", amount="30.00"), ev("e2", "bob", "09:00:00", "purchase", amount="10.00")])
    d = r.daily["days"][0]
    assert d["revenue"] == "-20.00" and d["top_user"] == "bob"


def test_top_user_null_when_best_is_zero():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5.00"), ev("e2", "ann", "09:01:00", "refund", amount="5.00"),
                 ev("e3", "bob", "09:00:00", "refund", amount="1.00")])
    d = r.daily["days"][0]
    assert d["revenue"] == "-1.00" and d["top_user"] is None


def test_refund_across_sessions_same_day():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="20.00"), ev("e2", "ann", "12:00:00", "refund", amount="20.00"),
                 ev("e3", "bob", "09:00:00", "purchase", amount="0.01")])
    assert pick(r.sessions, "session_id", "revenue") == [("ann-1", "20.00"), ("bob-1", "0.01"), ("ann-2", "-20.00")]
    assert r.daily["days"][0]["top_user"] == "bob"


def test_new_types_still_reject_unknown():
    r = run_job([ev("e1", "ann", "09:00:00", "login"), ev("e2", "ann", "09:00:00", "Refund", amount="1")])
    assert [x["reason"] for x in r.rejects] == ["bad_type", "bad_type"]
