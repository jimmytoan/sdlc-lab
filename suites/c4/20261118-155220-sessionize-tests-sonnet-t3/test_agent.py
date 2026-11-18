import json
import os
import subprocess
import sys
import tempfile

import pytest

from helpers import ev, run_job, pick, APP_DIR


def one(line):
    return run_job([line])


def reasons(r):
    return [x["reason"] for x in r.rejects]


def base(**over):
    d = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    d.update(over)
    return {k: v for k, v in d.items() if v is not ...}


# ---------- IO / files ----------

def test_empty_input_writes_all_files():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_only_rejects_still_all_files():
    r = run_job(["junk"])
    assert r.code == 0
    assert r.sessions == [] and r.daily["days"] == [] and r.daily["rejected"] == 1


def test_out_dir_created_nested():
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w") as f:
            f.write(ev("e1", "a", "09:00:00") + "\n")
        out = os.path.join(d, "x", "y")
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out],
                           capture_output=True, text=True)
        assert p.returncode == 0
        assert sorted(os.listdir(out)) == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_missing_input_file_exit2():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip()


def test_csv_text_exact():
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "i"), os.path.join(d, "o")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w") as f:
            f.write(ev("e1", "ann", "09:00:00", "purchase", amount="5") + "\n")
        subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out], check=True)
        txt = open(os.path.join(out, "sessions.csv"), newline="").read()
        assert txt.replace("\r\n", "\n") == (
            "session_id,user,start,end,duration_s,events,purchases,revenue\n"
            "ann-1,ann,2026-03-01T09:00:00Z,2026-03-01T09:00:00Z,0,1,1,5.00\n")


def test_utf8_input():
    r = run_job([ev("e1", "Zoë", "09:00:00")])
    assert r.sessions[0]["user"] == "zoë"


# ---------- blank lines / numbering ----------

def test_blank_lines_skipped_but_counted():
    r = run_job(["", "   ", ev("e1", "a", "09:00:00"), "\t", "bad"])
    assert r.rejects == [{"line": 5, "reason": "bad_json"}]
    assert len(r.sessions) == 1
    assert r.daily["rejected"] == 1


def test_reject_line_numbers_in_order():
    r = run_job(["x", ev("e1", "a", "09:00:00"), "[]", ev("e2", "a", "09:00:00", "zzz"), "{}"])
    assert r.rejects == [{"line": 1, "reason": "bad_json"}, {"line": 3, "reason": "bad_json"},
                         {"line": 4, "reason": "bad_type"}, {"line": 5, "reason": "missing_field"}]


# ---------- rejection reasons ----------

@pytest.mark.parametrize("line", ["{not json", "[1,2]", '"str"', "42", "null", "true", '{"a":1',
                                  '{"event_id": "e1",}'])
def test_bad_json(line):
    r = one(line)
    assert r.rejects == [{"line": 1, "reason": "bad_json"}]


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("val", [..., None, 5, "", "   ", ["a"], {"a": 1}, True])
def test_missing_field(field, val):
    r = one(json.dumps(base(**{field: val})))
    assert reasons(r) == ["missing_field"]


def test_missing_field_beats_later_checks():
    r = one(json.dumps(base(user="", ts="bad", type="zzz")))
    assert reasons(r) == ["missing_field"]


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00", "2026-03-01 09:00:00Z", "2026-03-01", "2026-03-01T09:00Z",
    "2026-03-01T09:00:00+0700", "2026-03-01T09:00:00+07", "2026-13-01T09:00:00Z",
    "2026-02-30T09:00:00Z", "2026-03-01T25:00:00Z", "2026-03-01T09:60:00Z", "2026-03-01T09:00:61Z",
    "garbage", "2026-03-01T09:00:00.Z", "2026-03-01T09:00:00+24:00", "2026-03-01T09:00:00+07:60",
    "1772355600",
])
def test_bad_ts(ts):
    r = one(json.dumps(base(ts=ts)))
    assert reasons(r) == ["bad_ts"]


def test_bad_ts_before_bad_type():
    r = one(json.dumps(base(ts="x", type="zzz")))
    assert reasons(r) == ["bad_ts"]


@pytest.mark.parametrize("ty", ["dance", "View", "PURCHASE", "refund", "logout", "views"])
def test_bad_type(ty):
    r = one(json.dumps(base(type=ty)))
    assert reasons(r) == ["bad_type"]


def test_bad_type_before_bad_amount():
    r = one(json.dumps(base(type="zzz", amount="x")))
    assert reasons(r) == ["bad_type"]


@pytest.mark.parametrize("amt", [..., None, 5, 5.5, "", "abc", "-1", "-0", "1.234", "1.", ".5", "1,5", "1e3",
                                 "+5", "0x10", "5 0", [], True])
def test_bad_amount(amt):
    r = one(json.dumps(base(type="purchase", amount=amt)))
    assert reasons(r) == ["bad_amount"]


@pytest.mark.parametrize("amt,exp", [("0", "0.00"), ("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"),
                                     ("0.00", "0.00"), ("1000000.01", "1000000.01")])
def test_good_amount(amt, exp):
    r = one(json.dumps(base(type="purchase", amount=amt)))
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == exp
    assert r.sessions[0]["purchases"] == "1"


@pytest.mark.parametrize("ty", ["view", "click"])
@pytest.mark.parametrize("amt", ["junk", -5, None, "-1"])
def test_amount_ignored_for_non_purchase(ty, amt):
    r = one(json.dumps(base(type=ty, amount=amt)))
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


def test_extra_fields_ignored():
    r = one(json.dumps(base(foo="bar", n=1)))
    assert r.rejects == [] and len(r.sessions) == 1


# ---------- timestamps ----------

@pytest.mark.parametrize("ts,exp", [
    ("2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T09:00:00+07:00", "2026-03-01T02:00:00Z"),
    ("2026-03-01T09:00:00-05:30", "2026-03-01T14:30:00Z"),
    ("2026-03-01T09:00:00.250-05:30", "2026-03-01T14:30:00Z"),
    ("2026-03-01T09:00:00.999999Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T01:00:00+03:00", "2026-02-28T22:00:00Z"),
    ("2026-03-01T23:00:00-02:00", "2026-03-02T01:00:00Z"),
    ("2026-03-01T09:00:00+00:00", "2026-03-01T09:00:00Z"),
    ("2028-02-29T00:00:00Z", "2028-02-29T00:00:00Z"),
    ("2026-12-31T23:59:59-01:00", "2027-01-01T00:59:59Z"),
])
def test_ts_conversion(ts, exp):
    r = one(json.dumps(base(ts=ts)))
    assert r.rejects == []
    assert r.sessions[0]["start"] == exp
    assert r.sessions[0]["end"] == exp


def test_date_uses_utc_start():
    r = run_job([ev("e1", "a", "2026-03-02T01:00:00+03:00")])
    assert r.daily["days"][0]["date"] == "2026-03-01"


# ---------- users ----------

def test_user_normalised():
    r = run_job([ev("e1", " Ann ", "09:00:00"), ev("e2", "ANN", "09:01:00"), ev("e3", "ann", "09:02:00")])
    assert pick(r.sessions, "session_id", "user", "events") == [("ann-1", "ann", "3")]


def test_distinct_users_separate_sessions():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:01:00"), ev("e3", "ann", "09:02:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("bob-1", "1")]


# ---------- duplicates ----------

def test_duplicate_keeps_earliest_ts_not_first_in_file():
    r = run_job([ev("e1", "ann", "10:00:00"), ev("e1", "ann", "09:00:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert pick(r.sessions, "start", "events") == [("2026-03-01T09:00:00Z", "1")]


def test_duplicate_compares_instants_across_offsets():
    r = run_job([ev("e1", "ann", "05:00:00"), ev("e1", "ann", "2026-03-01T09:00:00+07:00")])
    assert r.sessions[0]["start"] == "2026-03-01T02:00:00Z"


def test_duplicate_tie_first_in_file_wins():
    r = run_job([ev("e1", "ann", "09:00:00", "view"),
                 ev("e1", "bob", "09:00:00", "purchase", amount="9")])
    assert pick(r.sessions, "user", "purchases") == [("ann", "0")]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_subsecond_earliest():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.500Z"), ev("e1", "bob", "2026-03-01T09:00:00.100Z")])
    assert pick(r.sessions, "user") == [("bob",)]


def test_duplicate_across_users_and_case_sensitive_id():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("E1", "ann", "09:01:00"), ev("e1", "bob", "09:02:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2")]


def test_event_id_exact_match():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1 ", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_many_duplicates_count():
    r = run_job([ev("e1", "ann", "09:00:00")] * 4)
    assert r.daily["duplicates_dropped"] == 3
    assert r.sessions[0]["events"] == "1"


def test_rejected_lines_are_not_dedupe_candidates():
    r = run_job([ev("e1", "ann", "08:00:00", "dance"), ev("e1", "ann", "09:00:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 1
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"


def test_duplicate_removal_changes_session_split():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "10:00:00"), ev("e2", "ann", "09:10:00")])
    assert len(r.sessions) == 1 and r.sessions[0]["end"] == "2026-03-01T09:10:00Z"


def test_duplicate_purchase_counted_once():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5"),
                 ev("e1", "ann", "09:00:05", "purchase", amount="5")])
    assert r.sessions[0]["revenue"] == "5.00" and r.sessions[0]["purchases"] == "1"


# ---------- sessions ----------

def test_gap_exactly_30_min_same_session():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:30:00")])
    assert len(r.sessions) == 1 and r.sessions[0]["duration_s"] == "1800"


def test_gap_just_over_30_min_splits():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:30:01")])
    assert len(r.sessions) == 2


def test_gap_subsecond_over_30_splits():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "2026-03-01T09:30:00.001Z")])
    assert len(r.sessions) == 2


def test_gap_subsecond_exactly_30_same():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.700Z"), ev("e2", "a", "2026-03-01T09:30:00.700Z")])
    assert len(r.sessions) == 1


def test_gap_measured_from_previous_event():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:25:00"), ev("e3", "a", "09:50:00"),
                 ev("e4", "a", "10:15:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == str(75 * 60)
    assert r.sessions[0]["events"] == "4"


def test_input_order_irrelevant_for_sessions():
    r = run_job([ev("e3", "a", "10:30:00"), ev("e1", "a", "09:00:00"), ev("e2", "a", "09:10:00")])
    assert pick(r.sessions, "session_id", "start", "end", "events") == [
        ("a-1", "2026-03-01T09:00:00Z", "2026-03-01T09:10:00Z", "2"),
        ("a-2", "2026-03-01T10:30:00Z", "2026-03-01T10:30:00Z", "1")]


def test_session_numbering_per_user_in_time_order():
    r = run_job([ev("e1", "a", "12:00:00"), ev("e2", "a", "09:00:00"), ev("e3", "b", "10:00:00"),
                 ev("e4", "a", "10:00:00"), ev("e5", "b", "15:00:00")])
    got = {(s["session_id"], s["start"][11:16]) for s in r.sessions}
    assert got == {("a-1", "09:00"), ("a-2", "10:00"), ("a-3", "12:00"), ("b-1", "10:00"), ("b-2", "15:00")}


def test_duration_floor():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z"), ev("e2", "a", "2026-03-01T09:00:02.100Z")])
    s = r.sessions[0]
    assert s["duration_s"] == "1"
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:00:02Z"


def test_duration_floor_other_case():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.100Z"), ev("e2", "a", "2026-03-01T09:00:01.900Z")])
    assert r.sessions[0]["duration_s"] == "1"


def test_ordering_uses_full_precision():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z"), ev("e2", "b", "2026-03-01T09:00:00.100Z")])
    assert pick(r.sessions, "user") == [("b",), ("a",)]


def test_same_second_events_one_session():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z"), ev("e2", "a", "2026-03-01T09:00:00.100Z")])
    assert r.sessions[0]["events"] == "2" and r.sessions[0]["duration_s"] == "0"


def test_rows_sorted_by_start_then_user():
    r = run_job([ev("e1", "carl", "09:00:00"), ev("e2", "bob", "09:00:00"), ev("e3", "ann", "09:00:00"),
                 ev("e4", "zed", "08:00:00"), ev("e5", "ann", "12:00:00")])
    assert pick(r.sessions, "session_id") == [("zed-1",), ("ann-1",), ("bob-1",), ("carl-1",), ("ann-2",)]


def test_rows_sorted_by_instant_not_text():
    r = run_job([ev("e1", "a", "2026-03-01T10:00:00+05:00"),
                 ev("e2", "b", "2026-03-01T06:00:00Z")])
    assert pick(r.sessions, "user") == [("a",), ("b",)]


def test_revenue_exact_decimal():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="0.10"),
                 ev("e2", "a", "09:01:00", "purchase", amount="0.20"),
                 ev("e3", "a", "09:02:00", "purchase", amount="0.1"),
                 ev("e4", "a", "09:03:00", "click"), ev("e5", "a", "09:04:00", "view")])
    s = r.sessions[0]
    assert s["revenue"] == "0.40" and s["purchases"] == "3" and s["events"] == "5"


def test_revenue_zero_formatting():
    r = run_job([ev("e1", "a", "09:00:00")])
    assert r.sessions[0]["revenue"] == "0.00"


def test_revenue_per_session():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="3"), ev("e2", "a", "11:00:00", "purchase", amount="4.5")])
    assert pick(r.sessions, "revenue") == [("3.00",), ("4.50",)]


def test_session_crossing_midnight_belongs_to_start_date():
    r = run_job([ev("e1", "a", "2026-03-01T23:50:00Z"),
                 ev("e2", "a", "2026-03-02T00:10:00Z", "purchase", amount="7")])
    assert len(r.sessions) == 1
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "7.00", "top_user": "a"}]


# ---------- daily ----------

def test_days_ascending_and_only_nonempty():
    r = run_job([ev("e1", "a", "2026-03-05T09:00:00Z"), ev("e2", "a", "2026-03-01T09:00:00Z"),
                 ev("e3", "a", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_daily_users_distinct_and_sessions_count():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "11:00:00"), ev("e3", "b", "09:00:00")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3 and d["users"] == 2


def test_daily_revenue_sum_and_top_user_across_sessions():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="5"),
                 ev("e2", "a", "11:00:00", "purchase", amount="6"),
                 ev("e3", "b", "09:00:00", "purchase", amount="10.50")])
    d = r.daily["days"][0]
    assert d["revenue"] == "21.50"
    assert d["top_user"] == "a"


def test_top_user_tie_sorts_first():
    r = run_job([ev("e1", "bob", "09:00:00", "purchase", amount="5"),
                 ev("e2", "ann", "09:00:00", "purchase", amount="5.00")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_top_user_null_when_zero_revenue():
    r = run_job([ev("e1", "bob", "09:00:00", "purchase", amount="0"), ev("e2", "ann", "09:00:00")])
    d = r.daily["days"][0]
    assert d["top_user"] is None and d["revenue"] == "0.00"


def test_top_user_per_day():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00Z", "purchase", amount="1"),
                 ev("e2", "b", "2026-03-01T09:00:00Z", "purchase", amount="2"),
                 ev("e3", "a", "2026-03-02T09:00:00Z", "purchase", amount="3"),
                 ev("e4", "b", "2026-03-02T09:00:00Z", "purchase", amount="2")])
    assert [d["top_user"] for d in r.daily["days"]] == ["b", "a"]


def test_daily_revenue_string_two_decimals():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="1.5")])
    assert r.daily["days"][0]["revenue"] == "1.50"


def test_daily_counters_and_keys():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e1", "a", "09:00:00"), ev("e1", "a", "09:00:00"),
                 "bad", ev("e2", "a", "09:00:00", "x"), ""])
    assert r.daily["duplicates_dropped"] == 2
    assert r.daily["rejected"] == 2
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}


def test_daily_day_by_utc_not_local():
    r = run_job([ev("e1", "a", "2026-03-01T00:30:00+07:00"), ev("e2", "b", "2026-03-01T23:30:00-05:00")])
    assert [(d["date"], d["sessions"]) for d in r.daily["days"]] == [("2026-02-28", 1), ("2026-03-02", 1)]


def test_user_two_sessions_same_day():
    r = run_job([ev("e1", "a", "01:00:00", "purchase", amount="2"), ev("e2", "a", "05:00:00", "purchase", amount="2")])
    assert r.daily["days"][0] == {"date": "2026-03-01", "sessions": 2, "users": 1, "revenue": "4.00", "top_user": "a"}
