import json
import os
import subprocess
import sys
import tempfile

import pytest

from helpers import APP_DIR, ev, pick, run_job


def raw(**kw):
    return json.dumps(kw)


def reasons(lines):
    r = run_job(lines)
    assert r.code == 0
    return r.rejects


# ---------- basics / files ----------

def test_empty_input_writes_all_files():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_blank_lines_only():
    r = run_job(["", "   ", "\t"])
    assert r.code == 0 and r.sessions == [] and r.rejects == []


def test_out_dir_created_nested():
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w") as f:
            f.write(ev("e1", "ann", "09:00:00") + "\n")
        out = os.path.join(d, "a", "b")
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out],
                           capture_output=True, text=True, cwd=APP_DIR)
        assert p.returncode == 0
        assert sorted(os.listdir(out)) == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_missing_input_exit_2_stderr():
    r = run_job([], make_input=False)
    assert r.code == 2 and r.stderr.strip()


def test_csv_header_and_exact_format():
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "i"), os.path.join(d, "o")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w") as f:
            f.write("\n".join([ev("e1", "Ann", "2026-03-01T09:00:00+07:00"),
                               ev("e2", "ann", "2026-03-01T09:05:00+07:00", "click"),
                               ev("e3", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99")]) + "\n")
        subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out], cwd=APP_DIR)
        txt = open(os.path.join(out, "sessions.csv")).read()
        assert txt.splitlines() == [
            "session_id,user,start,end,duration_s,events,purchases,revenue",
            "ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:10:30Z,630,3,1,19.99"]


# ---------- rejects ----------

def test_bad_json_variants():
    r = reasons(["{oops", "[1,2]", '"str"', "123", "null", '{"a":1} extra'])
    assert r == [{"line": i, "reason": "bad_json"} for i in range(1, 7)]


def test_missing_field_variants():
    good = dict(event_id="e", user="u", ts="2026-03-01T09:00:00Z", type="view")
    lines = []
    for k in ("event_id", "user", "ts", "type"):
        d = dict(good)
        del d[k]
        lines.append(json.dumps(d))
        d = dict(good)
        d[k] = "   "
        lines.append(json.dumps(d))
        d = dict(good)
        d[k] = ""
        lines.append(json.dumps(d))
        d = dict(good)
        d[k] = 5
        lines.append(json.dumps(d))
        d = dict(good)
        d[k] = None
        lines.append(json.dumps(d))
    r = reasons(lines)
    assert r == [{"line": i, "reason": "missing_field"} for i in range(1, len(lines) + 1)]


def test_empty_object_missing_field():
    assert reasons(["{}"]) == [{"line": 1, "reason": "missing_field"}]


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00", "2026-03-01 09:00:00Z", "2026-03-01", "garbage", "2026-03-01T09:00Z",
    "2026-13-01T09:00:00Z", "2026-02-30T09:00:00Z", "2026-03-01T25:00:00Z", "2026-03-01T09:60:00Z",
    "2026-03-01T09:00:00+0700", "2026-03-01T09:00:00+07", "2026-03-01T09:00:00.Z", "2026-03-01T09:00:00 Z",
])
def test_bad_ts(ts):
    r = run_job([raw(event_id="e", user="u", ts=ts, type="view")])
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]
    assert r.sessions == []


@pytest.mark.parametrize("ts,start", [
    ("2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T09:00:00+07:00", "2026-03-01T02:00:00Z"),
    ("2026-03-01T09:00:00.250-05:30", "2026-03-01T14:30:00Z"),
    ("2026-03-01T00:30:00+01:00", "2026-02-28T23:30:00Z"),
    ("2026-03-01T23:30:00-02:00", "2026-03-02T01:30:00Z"),
    ("2026-03-01T09:00:00.123456789Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T09:00:00.9Z", "2026-03-01T09:00:00Z"),
])
def test_valid_ts(ts, start):
    r = run_job([raw(event_id="e", user="u", ts=ts, type="view")])
    assert r.rejects == []
    assert r.sessions[0]["start"] == start


def test_bad_type():
    r = reasons([ev("e1", "a", "09:00:00", "dance"), ev("e2", "a", "09:00:00", "View"), ev("e3", "a", "09:00:00", "")])
    assert r[0] == {"line": 1, "reason": "bad_type"}
    assert r[1] == {"line": 2, "reason": "bad_type"}
    assert r[2] == {"line": 3, "reason": "missing_field"}


def test_non_string_type_is_missing_field():
    assert reasons([raw(event_id="e", user="u", ts="2026-03-01T09:00:00Z", type=["view"])]) == [
        {"line": 1, "reason": "missing_field"}]


@pytest.mark.parametrize("extra", [
    {}, {"amount": 5}, {"amount": 5.5}, {"amount": None}, {"amount": "-1"}, {"amount": "1.234"},
    {"amount": "abc"}, {"amount": ""}, {"amount": "1."}, {"amount": ".5"}, {"amount": "1e3"},
    {"amount": "+5"}, {"amount": "1,5"}, {"amount": "-0"},
])
def test_bad_amount(extra):
    r = run_job([ev("e", "u", "09:00:00", "purchase", **extra)])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]
    assert r.sessions == []


@pytest.mark.parametrize("amount,rev", [("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"), ("0", "0.00"),
                                         ("0.00", "0.00"), ("007.10", "7.10")])
def test_good_amount(amount, rev):
    r = run_job([ev("e", "u", "09:00:00", "purchase", amount=amount)])
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == rev and r.sessions[0]["purchases"] == "1"


@pytest.mark.parametrize("t", ["view", "click"])
def test_amount_ignored_for_non_purchase(t):
    r = run_job([ev("e", "u", "09:00:00", t, amount="garbage"), ev("f", "u", "09:01:00", t, amount=7)])
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00" and r.sessions[0]["purchases"] == "0"
    assert r.sessions[0]["events"] == "2"


def test_reject_order_precedence():
    # missing_field before bad_ts before bad_type before bad_amount
    lines = [
        raw(event_id="e", user="", ts="bad", type="nope"),
        raw(event_id="e", user="u", ts="bad", type="nope"),
        raw(event_id="e", user="u", ts="2026-03-01T09:00:00Z", type="nope", amount="x"),
        raw(event_id="e", user="u", ts="bad", type="purchase", amount="x"),
        raw(event_id="e", user="u", ts="2026-03-01T09:00:00Z", type="purchase"),
    ]
    assert [x["reason"] for x in reasons(lines)] == ["missing_field", "bad_ts", "bad_type", "bad_ts", "bad_amount"]


def test_line_numbers_count_blank_lines_and_rejects_in_order():
    lines = ["", ev("e1", "a", "09:00:00"), "   ", "{bad", "", ev("e2", "a", "09:01:00", "x"), "[]"]
    r = run_job(lines)
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 6, "reason": "bad_type"},
                         {"line": 7, "reason": "bad_json"}]
    assert r.daily["rejected"] == 3


def test_rejected_event_does_not_affect_duplicates_or_sessions():
    lines = [ev("e1", "a", "09:00:00", "purchase", amount="bad"), ev("e1", "a", "09:10:00")]
    r = run_job(lines)
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["start"] == "2026-03-01T09:10:00Z"


def test_unicode_input():
    r = run_job([ev("é1", "Ünï", "09:00:00")])
    assert r.sessions[0]["user"] == "ünï"


# ---------- users ----------

def test_user_trim_and_lowercase():
    r = run_job([ev("e1", " ANN ", "09:00:00"), ev("e2", "ann", "09:05:00"), ev("e3", "Ann", "09:06:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann" and r.sessions[0]["events"] == "3"
    assert r.sessions[0]["session_id"] == "ann-1"


def test_event_id_exact_match_case_sensitive_and_untrimmed_compare():
    r = run_job([ev("E1", "a", "09:00:00"), ev("e1", "a", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


# ---------- duplicates ----------

def test_duplicate_keeps_earliest_timestamp_even_if_later_in_file():
    r = run_job([ev("e1", "ann", "10:00:00"), ev("e1", "ann", "09:00:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert len(r.sessions) == 1
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["events"] == "1"


def test_duplicate_tie_keeps_first_in_file():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1"),
                 ev("e1", "ann", "2026-03-01T16:00:00+07:00", "purchase", amount="2")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["revenue"] == "1.00"


def test_duplicate_tie_first_in_file_type_differs():
    r = run_job([ev("e1", "ann", "09:00:00", "view"), ev("e1", "ann", "09:00:00", "purchase", amount="3")])
    assert r.sessions[0]["purchases"] == "0"


def test_duplicate_across_users_and_fractional_compare():
    r = run_job([ev("e1", "bob", "2026-03-01T09:00:00.500Z"), ev("e1", "ann", "2026-03-01T09:00:00.100Z")])
    assert r.daily["duplicates_dropped"] == 1
    assert pick(r.sessions, "user") == [("ann",)]


def test_many_duplicates_counted():
    r = run_job([ev("e1", "a", "09:00:00")] * 4 + [ev("e2", "a", "09:01:00")] * 2)
    assert r.daily["duplicates_dropped"] == 4
    assert r.sessions[0]["events"] == "2"


def test_duplicate_removal_changes_sessions():
    # dropped later duplicate must not bridge a gap
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:25:00"), ev("e2", "a", "09:50:00"),
                 ev("e3", "a", "10:10:00")])
    assert pick(r.sessions, "session_id", "events") == [("a-1", "2"), ("a-2", "1")]


# ---------- sessions ----------

def test_gap_exactly_30_min_same_session():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:30:00")])
    assert len(r.sessions) == 1 and r.sessions[0]["duration_s"] == "1800"


def test_gap_just_over_30_min_splits():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:30:01")])
    assert len(r.sessions) == 2


def test_gap_subsecond_precision_used():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.000Z"), ev("e2", "a", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 2
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z"), ev("e2", "a", "2026-03-01T09:30:00.900Z")])
    assert len(r.sessions) == 1


def test_gap_measured_from_previous_event_not_session_start():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:25:00"), ev("e3", "a", "09:50:00"),
                 ev("e4", "a", "10:15:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == str(75 * 60) and r.sessions[0]["events"] == "4"


def test_input_unordered_sorted_by_ts():
    r = run_job([ev("e3", "a", "09:50:00"), ev("e1", "a", "09:00:00"), ev("e2", "a", "09:25:00")])
    assert len(r.sessions) == 1 and r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:50:00Z"


def test_session_numbering_per_user_in_time_order():
    r = run_job([ev("e1", "a", "12:00:00"), ev("e2", "a", "09:00:00"), ev("e3", "b", "10:00:00"),
                 ev("e4", "a", "15:00:00"), ev("e5", "b", "13:00:00")])
    assert pick(r.sessions, "session_id", "start") == [
        ("a-1", "2026-03-01T09:00:00Z"), ("b-1", "2026-03-01T10:00:00Z"),
        ("a-2", "2026-03-01T12:00:00Z"), ("b-2", "2026-03-01T13:00:00Z"), ("a-3", "2026-03-01T15:00:00Z")]


def test_users_interleaved_independent_gaps():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "b", "09:10:00"), ev("e3", "a", "09:20:00"),
                 ev("e4", "b", "09:40:00")])
    assert len(r.sessions) == 2
    assert {s["user"]: s["events"] for s in r.sessions} == {"a": "2", "b": "2"}


def test_duration_floor_and_fraction_cut():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z"), ev("e2", "a", "2026-03-01T09:00:02.100Z")])
    s = r.sessions[0]
    assert s["duration_s"] == "1"  # 1.2s floored
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:00:02Z"


def test_duration_zero_single_event():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.700Z")])
    assert r.sessions[0]["duration_s"] == "0" and r.sessions[0]["events"] == "1"


def test_same_timestamp_events_same_session():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:00:00")])
    assert len(r.sessions) == 1 and r.sessions[0]["events"] == "2"


def test_rows_sorted_by_start_then_user():
    r = run_job([ev("e1", "carl", "09:00:00"), ev("e2", "bob", "09:00:00"), ev("e3", "ann", "09:00:01"),
                 ev("e4", "zed", "08:59:59")])
    assert pick(r.sessions, "user") == [("zed",), ("bob",), ("carl",), ("ann",)]


def test_rows_sorted_by_full_precision_start():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "bob", "2026-03-01T09:00:00.100Z")])
    assert pick(r.sessions, "user") == [("bob",), ("ann",)]


def test_rows_sorted_by_utc_not_local():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "bob", "2026-03-01T03:00:00Z")])
    assert pick(r.sessions, "user") == [("ann",), ("bob",)]


def test_sessions_across_offsets_merge_in_utc():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00+07:00"), ev("e2", "a", "2026-03-01T02:20:00Z")])
    assert len(r.sessions) == 1


def test_purchase_counts_and_revenue_exact_decimal():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="0.10"), ev("e2", "a", "09:01:00", "purchase", amount="0.20"),
                 ev("e3", "a", "09:02:00", "purchase", amount="0.1"), ev("e4", "a", "09:03:00", "click"),
                 ev("e5", "a", "09:04:00", "view")])
    s = r.sessions[0]
    assert s["revenue"] == "0.40" and s["purchases"] == "3" and s["events"] == "5"


def test_revenue_large_exact():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="12345678901234567.89"),
                 ev("e2", "a", "09:01:00", "purchase", amount="0.01")])
    assert r.sessions[0]["revenue"] == "12345678901234567.90"


def test_revenue_per_session_separate():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="2"), ev("e2", "a", "11:00:00", "purchase", amount="3.5")])
    assert pick(r.sessions, "revenue") == [("2.00",), ("3.50",)]


# ---------- daily ----------

def test_daily_session_belongs_to_start_date_utc():
    r = run_job([ev("e1", "a", "2026-03-01T23:50:00Z"), ev("e2", "a", "2026-03-02T00:10:00Z", "purchase", amount="4")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "4.00", "top_user": "a"}]


def test_daily_date_uses_utc_conversion():
    r = run_job([ev("e1", "a", "2026-03-02T03:00:00+07:00")])
    assert r.daily["days"][0]["date"] == "2026-03-01"


def test_daily_users_distinct_and_sessions_count():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "12:00:00"), ev("e3", "b", "09:00:00")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3 and d["users"] == 2


def test_daily_top_user_highest_revenue_summed_across_sessions():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="6"), ev("e2", "a", "12:00:00", "purchase", amount="6"),
                 ev("e3", "b", "09:00:00", "purchase", amount="10")])
    d = r.daily["days"][0]
    assert d["top_user"] == "a" and d["revenue"] == "22.00"


def test_daily_top_user_tie_sorts_first():
    r = run_job([ev("e1", "zed", "09:00:00", "purchase", amount="5"), ev("e2", "amy", "10:00:00", "purchase", amount="5"),
                 ev("e3", "bob", "10:00:00", "purchase", amount="1")])
    assert r.daily["days"][0]["top_user"] == "amy"


def test_daily_top_user_null_when_zero_revenue():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "b", "09:00:00", "purchase", amount="0")])
    d = r.daily["days"][0]
    assert d["top_user"] is None and d["revenue"] == "0.00"


def test_daily_days_ascending_and_skip_empty_dates():
    r = run_job([ev("e1", "a", "2026-03-05T09:00:00Z"), ev("e2", "a", "2026-03-01T09:00:00Z"),
                 ev("e3", "a", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_daily_revenue_per_day_and_rejected_dups_counts():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="1.5"), ev("e1", "a", "09:00:00", "purchase", amount="1.5"),
                 "bad", ev("e2", "a", "2026-03-02T09:00:00Z", "purchase", amount="2.25"), ""])
    assert r.daily["duplicates_dropped"] == 1 and r.daily["rejected"] == 1
    assert [(d["date"], d["revenue"]) for d in r.daily["days"]] == [("2026-03-01", "1.50"), ("2026-03-02", "2.25")]


def test_daily_json_keys():
    r = run_job([ev("e1", "a", "09:00:00")])
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}


def test_rejects_file_empty_when_no_rejects():
    r = run_job([ev("e1", "a", "09:00:00")])
    assert r.rejects == [] and r.daily["rejected"] == 0
