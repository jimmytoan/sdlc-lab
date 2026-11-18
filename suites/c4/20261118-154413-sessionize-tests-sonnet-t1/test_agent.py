import json
import os
import subprocess
import sys
import tempfile

from helpers import ev, run_job, pick, APP_DIR


def raw(**kw):
    return json.dumps(kw)


def one(line):
    return run_job([line])


def reason(line):
    r = one(line)
    assert r.code == 0
    return [x["reason"] for x in r.rejects]


# ---------- basic files / exit codes ----------

def test_empty_input_all_files_written():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_only_rejects_still_all_files():
    r = run_job(["garbage"])
    assert r.code == 0 and r.sessions == [] and r.daily["days"] == []
    assert r.daily["rejected"] == 1


def test_missing_input_exit2_no_crash():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip()
    assert "Traceback" not in r.stderr


def test_output_dir_created_nested():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "in"))
        with open(os.path.join(d, "in", "events.jsonl"), "w") as f:
            f.write(ev("e1", "a", "09:00:00") + "\n")
        out = os.path.join(d, "x", "y", "out")
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", os.path.join(d, "in"), "--out", out],
                           capture_output=True, text=True, cwd=APP_DIR)
        assert p.returncode == 0
        assert sorted(os.listdir(out)) == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_no_trailing_newline_last_line_read():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "in"))
        with open(os.path.join(d, "in", "events.jsonl"), "w") as f:
            f.write(ev("e1", "a", "09:00:00") + "\n" + ev("e2", "a", "09:01:00"))
        out = os.path.join(d, "out")
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", os.path.join(d, "in"), "--out", out],
                           capture_output=True, text=True, cwd=APP_DIR)
        assert p.returncode == 0
        with open(os.path.join(out, "sessions.csv")) as f:
            assert f.read().splitlines()[1].split(",")[5] == "2"


def test_unicode_user_utf8():
    r = run_job([ev("e1", "ÉLodie", "09:00:00")])
    assert r.sessions[0]["user"] == "élodie"
    assert r.sessions[0]["session_id"] == "élodie-1"


# ---------- rejects ----------

def test_blank_lines_skipped_but_counted():
    r = run_job([ev("e1", "a", "09:00:00"), "", "   ", "\t", "bad", ev("e2", "a", "09:01:00"), "{}"])
    assert r.rejects == [{"line": 5, "reason": "bad_json"}, {"line": 7, "reason": "missing_field"}]
    assert r.daily["rejected"] == 2
    assert r.sessions[0]["events"] == "2"


def test_bad_json_variants():
    for line in ["{not json", "[1,2]", "42", '"str"', "null", "true", '{"a":1} trailing', "{'a': 1}"]:
        assert reason(line) == ["bad_json"], line


def test_missing_field_variants():
    good = dict(event_id="e1", user="a", ts="2026-03-01T09:00:00Z", type="view")
    for k in good:
        d = dict(good)
        del d[k]
        assert reason(json.dumps(d)) == ["missing_field"], k
        for bad in ["", "   ", None, 5, True, [], {}]:
            d = dict(good)
            d[k] = bad
            assert reason(json.dumps(d)) == ["missing_field"], (k, bad)


def test_bad_ts_variants():
    bads = [
        "2026-03-01T09:00:00",        # no offset
        "2026-03-01T09:00:00.5",
        "2026-03-01 09:00:00Z",
        "2026-03-01T09:00Z",
        "2026-03-01",
        "yesterday",
        "2026-02-30T09:00:00Z",
        "2026-13-01T09:00:00Z",
        "2026-03-01T25:00:00Z",
        "2026-03-01T09:60:00Z",
        "2026-03-01T09:00:61Z",
        "2026-03-01T09:00:00+0700",
        "2026-03-01T09:00:00+07",
        "2026-03-01T09:00:00+24:00",
        "2026-03-01T09:00:00+07:60",
        "2026-03-01T09:00:00.Z",
        "26-03-01T09:00:00Z",
    ]
    for ts in bads:
        assert reason(raw(event_id="e", user="a", ts=ts, type="view")) == ["bad_ts"], ts


def test_good_ts_variants():
    goods = {
        "2026-03-01T09:00:00Z": "2026-03-01T09:00:00Z",
        "2026-03-01T09:00:00+07:00": "2026-03-01T02:00:00Z",
        "2026-03-01T09:00:00-05:30": "2026-03-01T14:30:00Z",
        "2026-03-01T09:00:00.250-05:30": "2026-03-01T14:30:00Z",
        "2026-03-01T09:00:00.123456789Z": "2026-03-01T09:00:00Z",
        "2026-03-01T00:30:00+01:00": "2026-02-28T23:30:00Z",
        "2026-12-31T23:59:59-02:00": "2027-01-01T01:59:59Z",
        "2026-03-01T09:00:00+00:00": "2026-03-01T09:00:00Z",
        "2024-02-29T12:00:00Z": "2024-02-29T12:00:00Z",
    }
    for ts, exp in goods.items():
        r = one(raw(event_id="e", user="a", ts=ts, type="view"))
        assert r.rejects == [], ts
        assert r.sessions[0]["start"] == exp, ts


def test_bad_type():
    for t in ["dance", "VIEW", "View", "logout", "refund", "view "]:
        assert reason(raw(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type=t)) == ["bad_type"], t


def test_bad_amount_variants():
    base = dict(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type="purchase")
    for amt in ["", "abc", "-1", "-0.50", "1.234", "1.", ".5", "1e3", "1,5", "+5", "NaN", "Infinity", "0x10", "1 2"]:
        assert reason(json.dumps({**base, "amount": amt})) == ["bad_amount"], amt
    for amt in [5, 5.5, None, True, [], {}]:
        assert reason(json.dumps({**base, "amount": amt})) == ["bad_amount"], amt
    assert reason(json.dumps(base)) == ["bad_amount"]


def test_valid_amounts():
    for amt, exp in [("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"), ("0", "0.00"), ("0.00", "0.00"),
                     ("007.10", "7.10"), ("1000000.01", "1000000.01")]:
        r = one(raw(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type="purchase", amount=amt))
        assert r.rejects == [], amt
        assert r.sessions[0]["revenue"] == exp, amt
        assert r.sessions[0]["purchases"] == "1"


def test_amount_ignored_for_non_purchase():
    for t in ["view", "click"]:
        for amt in ["garbage", 5, None, "-3"]:
            r = one(raw(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type=t, amount=amt))
            assert r.rejects == []
            assert r.sessions[0]["revenue"] == "0.00" and r.sessions[0]["purchases"] == "0"


def test_check_order():
    # missing_field beats bad_ts / bad_type / bad_amount
    assert reason(raw(event_id="", user="a", ts="nope", type="zzz")) == ["missing_field"]
    assert reason(raw(user="a", ts="2026-03-01T09:00:00Z", type="purchase")) == ["missing_field"]
    # bad_ts beats bad_type
    assert reason(raw(event_id="e", user="a", ts="nope", type="zzz")) == ["bad_ts"]
    # bad_ts beats bad_amount
    assert reason(raw(event_id="e", user="a", ts="nope", type="purchase", amount="x")) == ["bad_ts"]
    # bad_type beats bad_amount
    assert reason(raw(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type="zzz", amount="x")) == ["bad_type"]


def test_rejects_file_format_and_order():
    r = run_job([ev("e1", "a", "09:00:00"), "x", raw(event_id="e"), "", raw(event_id="e", user="a", ts="bad", type="view"),
                 raw(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type="q"),
                 raw(event_id="e", user="a", ts="2026-03-01T09:00:00Z", type="purchase")])
    assert r.rejects == [{"line": 2, "reason": "bad_json"}, {"line": 3, "reason": "missing_field"},
                         {"line": 5, "reason": "bad_ts"}, {"line": 6, "reason": "bad_type"},
                         {"line": 7, "reason": "bad_amount"}]
    assert r.daily["rejected"] == 5
    assert all(set(x) == {"line", "reason"} and isinstance(x["line"], int) for x in r.rejects)


def test_rejected_events_do_not_take_part_in_dedupe():
    # the rejected line has the same id and earlier ts; the valid one must survive
    r = run_job([raw(event_id="e1", user="a", ts="2026-03-01T08:00:00Z", type="zzz"), ev("e1", "a", "09:00:00")])
    assert r.sessions[0]["events"] == "1"
    assert r.daily["duplicates_dropped"] == 0 and r.daily["rejected"] == 1


# ---------- users ----------

def test_user_trim_lowercase_merge():
    r = run_job([ev("e1", " ANN ", "09:00:00"), ev("e2", "ann", "09:01:00"), ev("e3", "Ann", "09:02:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann" and r.sessions[0]["events"] == "3"


def test_event_id_is_trimmed_for_validity_only_but_exact_for_dupes():
    # exact string match: different case / spacing => different events
    r = run_job([ev("E1", "a", "09:00:00"), ev("e1", "a", "09:01:00")])
    assert r.sessions[0]["events"] == "2" and r.daily["duplicates_dropped"] == 0


# ---------- duplicates ----------

def test_duplicate_earliest_ts_wins_even_if_later_in_file():
    r = run_job([ev("e1", "a", "10:00:00", "purchase", amount="9"),
                 ev("e1", "a", "09:00:00", "purchase", amount="4"),
                 ])
    assert len(r.sessions) == 1
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T09:00:00Z" and s["revenue"] == "4.00" and s["events"] == "1"
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_equal_ts_first_in_file_wins():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="4"),
                 ev("e1", "b", "09:00:00", "purchase", amount="7")])
    assert pick(r.sessions, "user", "revenue") == [("a", "4.00")]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_equal_instant_different_offsets():
    r = run_job([ev("e1", "a", "2026-03-01T10:00:00+01:00", "purchase", amount="4"),
                 ev("e1", "b", "2026-03-01T09:00:00Z", "purchase", amount="7")])
    assert pick(r.sessions, "user", "revenue") == [("a", "4.00")]


def test_duplicate_compared_by_instant_not_text():
    r = run_job([ev("e1", "a", "2026-03-01T09:30:00+02:00", "purchase", amount="4"),  # 07:30Z, earlier
                 ev("e1", "b", "2026-03-01T08:00:00Z", "purchase", amount="7")])
    assert pick(r.sessions, "user", "start") == [("a", "2026-03-01T07:30:00Z")]


def test_duplicate_fraction_precision():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z", "purchase", amount="4"),
                 ev("e1", "b", "2026-03-01T09:00:00.100Z", "purchase", amount="7")])
    assert pick(r.sessions, "user") == [("b",)]


def test_duplicate_dropped_does_not_bridge_gap():
    # dropped duplicate at 09:20 must not keep session alive
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:40:00"), ev("e1", "a", "09:50:00")])
    # e1 at 09:50 is dropped (09:00 earlier). sessions: 09:00 | 09:40
    assert pick(r.sessions, "session_id", "events") == [("a-1", "1"), ("a-2", "1")]
    assert r.daily["duplicates_dropped"] == 1


def test_many_duplicates_count():
    r = run_job([ev("e1", "a", "09:00:00")] * 4 + [ev("e2", "a", "09:01:00")] * 2)
    assert r.daily["duplicates_dropped"] == 4
    assert r.sessions[0]["events"] == "2"


# ---------- sessions ----------

def test_gap_exactly_30_min_same_session():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:30:00")])
    assert len(r.sessions) == 1 and r.sessions[0]["duration_s"] == "1800"


def test_gap_just_over_30_min_splits():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:30:01")])
    assert len(r.sessions) == 2


def test_gap_uses_fractional_precision():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.000Z"), ev("e2", "a", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 2
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.600Z"), ev("e2", "a", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 1


def test_gap_measured_from_previous_event_not_session_start():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:25:00"), ev("e3", "a", "09:50:00"),
                 ev("e4", "a", "10:15:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == str(75 * 60) and r.sessions[0]["events"] == "4"


def test_gap_across_users_independent():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "b", "09:10:00"), ev("e3", "a", "09:20:00"),
                 ev("e4", "b", "09:50:00")])
    assert pick(r.sessions, "session_id", "events") == [("a-1", "2"), ("b-1", "1"), ("b-2", "1")] or \
        sorted(pick(r.sessions, "session_id", "events")) == [("a-1", "2"), ("b-1", "1"), ("b-2", "1")]


def test_unsorted_input_ordered_by_ts():
    r = run_job([ev("e3", "a", "09:20:00"), ev("e1", "a", "09:00:00"), ev("e2", "a", "09:10:00")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:20:00Z" and s["events"] == "3"


def test_unsorted_input_session_numbers_in_time_order():
    r = run_job([ev("e2", "a", "11:00:00"), ev("e1", "a", "09:00:00"), ev("e3", "a", "13:00:00")])
    assert pick(r.sessions, "session_id", "start") == [
        ("a-1", "2026-03-01T09:00:00Z"), ("a-2", "2026-03-01T11:00:00Z"), ("a-3", "2026-03-01T13:00:00Z")]


def test_timezone_ordering_uses_utc():
    # local text order differs from UTC order
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00+07:00"),   # 02:00Z
                 ev("e2", "a", "2026-03-01T02:10:00Z"),
                 ev("e3", "a", "2026-03-01T03:00:00-05:00")])  # 08:00Z
    assert pick(r.sessions, "session_id", "start", "end", "events") == [
        ("a-1", "2026-03-01T02:00:00Z", "2026-03-01T02:10:00Z", "2"),
        ("a-2", "2026-03-01T08:00:00Z", "2026-03-01T08:00:00Z", "1")]


def test_fraction_cut_off_in_output_and_duration_floored():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.900Z"), ev("e2", "a", "2026-03-01T09:00:02.100Z")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:00:02Z"
    assert s["duration_s"] == "1"  # 1.2s floored


def test_duration_floor_vs_rounding():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00.000Z"), ev("e2", "a", "2026-03-01T09:00:05.999Z")])
    assert r.sessions[0]["duration_s"] == "5"


def test_single_event_session_duration_zero():
    r = run_job([ev("e1", "a", "09:00:00")])
    assert r.sessions[0]["duration_s"] == "0" and r.sessions[0]["start"] == r.sessions[0]["end"]


def test_same_timestamp_events_same_session():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:00:00", "click")])
    assert len(r.sessions) == 1 and r.sessions[0]["events"] == "2"


def test_counts_and_types():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "09:01:00", "click"),
                 ev("e3", "a", "09:02:00", "purchase", amount="1.10"),
                 ev("e4", "a", "09:03:00", "purchase", amount="2.20")])
    s = r.sessions[0]
    assert (s["events"], s["purchases"], s["revenue"]) == ("4", "2", "3.30")


def test_revenue_exact_decimal():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="0.1"),
                 ev("e2", "a", "09:01:00", "purchase", amount="0.2"),
                 ev("e3", "a", "09:02:00", "purchase", amount="0.01")])
    assert r.sessions[0]["revenue"] == "0.31"
    r = run_job([ev(f"e{i}", "a", f"09:{i:02d}:00", "purchase", amount="0.10") for i in range(10)])
    assert r.sessions[0]["revenue"] == "1.00"


def test_revenue_zero_formatting():
    r = run_job([ev("e1", "a", "09:00:00")])
    assert r.sessions[0]["revenue"] == "0.00"
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="0")])
    assert r.sessions[0]["revenue"] == "0.00" and r.sessions[0]["purchases"] == "1"


def test_session_over_midnight_utc():
    r = run_job([ev("e1", "a", "2026-03-01T23:50:00Z"), ev("e2", "a", "2026-03-02T00:10:00Z")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T23:50:00Z" and s["end"] == "2026-03-02T00:10:00Z" and s["duration_s"] == "1200"


def test_session_order_by_start_then_user():
    r = run_job([ev("e1", "carl", "09:00:00"), ev("e2", "bob", "09:00:00"), ev("e3", "ann", "09:30:00"),
                 ev("e4", "zed", "08:59:59"), ev("e5", "bob", "2026-03-01T09:00:00.5Z")])
    assert pick(r.sessions, "session_id") == [("zed-1",), ("bob-1",), ("carl-1",), ("ann-1",)]


def test_session_order_uses_full_precision():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "bob", "2026-03-01T09:00:00.100Z")])
    assert pick(r.sessions, "user") == [("bob",), ("ann",)]


def test_session_order_by_start_not_session_id_or_end():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "12:00:00"), ev("e3", "b", "10:00:00"),
                 ev("e4", "b", "10:10:00")])
    assert pick(r.sessions, "session_id") == [("a-1",), ("b-1",), ("a-2",)]


def test_user_sort_is_lexicographic_on_lowercased():
    r = run_job([ev("e1", "B", "09:00:00"), ev("e2", "a", "09:00:00"), ev("e3", "10", "09:00:00")])
    assert pick(r.sessions, "user") == [("10",), ("a",), ("b",)]


def test_csv_raw_format():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "in"))
        with open(os.path.join(d, "in", "events.jsonl"), "w") as f:
            f.write("\n".join([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99")]) + "\n")
        out = os.path.join(d, "out")
        subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", os.path.join(d, "in"), "--out", out],
                       check=True, cwd=APP_DIR)
        with open(os.path.join(out, "sessions.csv"), newline="") as f:
            text = f.read()
        assert text.replace("\r\n", "\n") == ("session_id,user,start,end,duration_s,events,purchases,revenue\n"
                                              "ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:10:30Z,630,2,1,19.99\n")


# ---------- daily ----------

def test_daily_session_belongs_to_start_date():
    r = run_job([ev("e1", "a", "2026-03-01T23:50:00Z", "purchase", amount="3"),
                 ev("e2", "a", "2026-03-02T00:10:00Z", "purchase", amount="4")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "7.00", "top_user": "a"}]


def test_daily_date_is_utc_not_local():
    r = run_job([ev("e1", "a", "2026-03-02T03:00:00+07:00")])  # 2026-03-01T20:00Z
    assert r.daily["days"][0]["date"] == "2026-03-01"
    r = run_job([ev("e1", "a", "2026-03-01T22:00:00-05:00")])  # 03-02T03:00Z
    assert r.daily["days"][0]["date"] == "2026-03-02"


def test_daily_users_distinct_and_sessions_counted():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e2", "a", "11:00:00"), ev("e3", "b", "09:00:00")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3 and d["users"] == 2


def test_daily_days_ascending_and_skip_empty_dates():
    r = run_job([ev("e1", "a", "2026-03-05T09:00:00Z"), ev("e2", "a", "2026-03-01T09:00:00Z"),
                 ev("e3", "a", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_daily_top_user_highest_revenue():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="5"), ev("e2", "b", "09:00:00", "purchase", amount="6"),
                 ev("e3", "a", "12:00:00", "purchase", amount="2")])
    d = r.daily["days"][0]
    assert d["top_user"] == "a" and d["revenue"] == "13.00"  # a=7 across two sessions


def test_daily_top_user_tie_sorts_first():
    r = run_job([ev("e1", "bob", "09:00:00", "purchase", amount="5"), ev("e2", "ann", "10:00:00", "purchase", amount="5"),
                 ev("e3", "cy", "09:00:00", "purchase", amount="1")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_daily_top_user_tie_when_zero_revenue_is_null():
    r = run_job([ev("e1", "bob", "09:00:00"), ev("e2", "ann", "09:00:00", "purchase", amount="0")])
    d = r.daily["days"][0]
    assert d["top_user"] is None and d["revenue"] == "0.00"


def test_daily_top_user_with_one_paying_user_among_zero():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "zed", "09:00:00", "purchase", amount="0.01")])
    assert r.daily["days"][0]["top_user"] == "zed"


def test_daily_revenue_string_two_decimals():
    r = run_job([ev("e1", "a", "09:00:00", "purchase", amount="0.1"), ev("e2", "b", "09:00:00", "purchase", amount="0.2")])
    assert r.daily["days"][0]["revenue"] == "0.30"
    assert isinstance(r.daily["days"][0]["revenue"], str)


def test_daily_counters_and_keys():
    r = run_job([ev("e1", "a", "09:00:00"), ev("e1", "a", "09:00:00"), ev("e1", "a", "09:00:00"), "bad", "{}"])
    assert r.daily["duplicates_dropped"] == 2 and r.daily["rejected"] == 2
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}


def test_daily_top_user_per_day_independent():
    r = run_job([ev("e1", "a", "2026-03-01T09:00:00Z", "purchase", amount="9"),
                 ev("e2", "b", "2026-03-01T09:00:00Z", "purchase", amount="1"),
                 ev("e3", "a", "2026-03-02T09:00:00Z", "purchase", amount="1"),
                 ev("e4", "b", "2026-03-02T09:00:00Z", "purchase", amount="9")])
    assert [d["top_user"] for d in r.daily["days"]] == ["a", "b"]
