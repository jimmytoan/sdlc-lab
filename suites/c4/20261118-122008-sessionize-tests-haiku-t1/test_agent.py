"""Regression suite for sessionize (job.py), written from SPEC.md.

Every test drives job.py as a black box through tests/helpers.py.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

from helpers import APP_DIR, ev, run_job, pick

HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


# ---------------------------------------------------------------- helpers

def raw(event_id, user, ts, type="view", **extra):
    """Like ev() but ts is always used verbatim (no expansion)."""
    return json.dumps({"event_id": event_id, "user": user, "ts": ts, "type": type, **extra})


def sessions_of(lines):
    r = run_job(lines)
    assert r.code == 0, r.stderr
    return r.sessions


def ids(lines):
    return [s["session_id"] for s in sessions_of(lines)]


def reject_reasons(lines):
    r = run_job(lines)
    assert r.code == 0, r.stderr
    return [(x["line"], x["reason"]) for x in r.rejects]


def only_reject(line):
    """Assert a single line is rejected and return its reason."""
    rej = reject_reasons([line])
    assert len(rej) == 1, f"expected one rejection, got {rej}"
    assert rej[0][0] == 1
    return rej[0][1]


def run_to_nested_out(lines):
    """Run job with an output directory that does not exist yet, several levels deep."""
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        out = os.path.join(d, "x", "y", "out")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out],
                           capture_output=True, text=True, timeout=30, cwd=APP_DIR)
        return p.returncode, os.path.isdir(out), sorted(os.listdir(out)) if os.path.isdir(out) else []


# ---------------------------------------------------------------- CLI and files

def test_outputs_always_written_with_exact_names():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_empty_input_still_writes_all_three_files():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_whitespace_only_input_writes_files_with_no_rows():
    r = run_job(["   ", "\t", ""])
    assert r.code == 0
    assert r.sessions == [] and r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_output_dir_created_when_missing_including_parents():
    code, is_dir, files = run_to_nested_out([ev("e1", "ann", "09:00:00")])
    assert code == 0
    assert is_dir
    assert files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_missing_input_exits_2_with_stderr_message():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""


def test_sessions_csv_header_exact():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.header == HEADER


def test_sessions_csv_has_no_rows_when_no_valid_events():
    r = run_job([raw("e1", "ann", "2026-03-01T09:00:00")])  # bad_ts only
    assert r.code == 0
    assert r.header == HEADER
    assert r.sessions == []


def test_exit_zero_when_some_lines_rejected():
    r = run_job(["{", ev("e1", "ann", "09:00:00")])
    assert r.code == 0


def test_daily_json_top_level_keys():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert set(r.daily.keys()) == {"days", "duplicates_dropped", "rejected"}


def test_daily_day_entry_keys():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1")])
    assert set(r.daily["days"][0].keys()) == {"date", "sessions", "users", "revenue", "top_user"}


# ---------------------------------------------------------------- rejects file

def test_rejects_file_is_jsonl_with_line_and_reason_only():
    r = run_job(["{", ev("e1", "ann", "09:00:00")])
    assert r.rejects == [{"line": 1, "reason": "bad_json"}]


def test_no_rejects_gives_empty_rejects_file():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.rejects == []
    assert r.daily["rejected"] == 0


def test_rejects_in_input_order():
    r = run_job([ev("e1", "ann", "09:00:00", "dance"), ev("e2", "ann", "09:01:00"), "[]",
                 raw("e3", "", "2026-03-01T09:00:00Z")])
    assert [(x["line"], x["reason"]) for x in r.rejects] == [(1, "bad_type"), (3, "bad_json"), (4, "missing_field")]


def test_line_numbers_are_physical_and_count_blank_lines():
    lines = ["", ev("e1", "ann", "09:00:00"), "   ", "{bad", "", ev("e2", "ann", "09:01:00"), "\t", "nope"]
    r = run_job(lines)
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 8, "reason": "bad_json"}]


def test_blank_and_whitespace_lines_are_not_rejects():
    r = run_job(["", "   ", ev("e1", "ann", "09:00:00"), "\t\t"])
    assert r.rejects == []
    assert r.daily["rejected"] == 0


def test_rejected_lines_do_not_affect_sessions():
    r = run_job([ev("e1", "ann", "09:00:00"), "{", ev("e2", "ann", "09:05:00"), "[1]"])
    assert len(r.sessions) == 1 and r.sessions[0]["events"] == "2"


def test_rejected_count_in_daily_matches_rejects_file():
    r = run_job(["{", "[]", "null", ev("e1", "ann", "09:00:00")])
    assert r.daily["rejected"] == len(r.rejects) == 3


# ---------------------------------------------------------------- bad_json

@pytest.mark.parametrize("line", [
    "{", "not json", '{"event_id": "e1",', "[]", "[1, 2]", "3", '"text"', "null", "true",
])
def test_bad_json_or_non_object(line):
    assert only_reject(line) == "bad_json"


# ---------------------------------------------------------------- missing_field

@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_missing_field_absent(field):
    obj = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    del obj[field]
    assert only_reject(json.dumps(obj)) == "missing_field"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", [5, None, True, ["x"], {"a": 1}])
def test_missing_field_not_a_string(field, value):
    obj = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    obj[field] = value
    assert only_reject(json.dumps(obj)) == "missing_field"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_missing_field_empty_after_trim(field, value):
    obj = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    obj[field] = value
    assert only_reject(json.dumps(obj)) == "missing_field"


def test_missing_field_wins_over_bad_ts():
    assert only_reject(raw("", "ann", "not-a-time")) == "missing_field"


def test_missing_field_wins_over_bad_type():
    assert only_reject(raw("e1", "  ", "2026-03-01T09:00:00Z", "dance")) == "missing_field"


def test_missing_field_wins_over_bad_amount():
    line = json.dumps({"event_id": "e1", "user": 7, "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": "x"})
    assert only_reject(line) == "missing_field"


def test_bad_json_wins_over_everything_else():
    assert only_reject('{"event_id": "e1", "user": "ann", "ts": "bad", "type": "dance",') == "bad_json"


# ---------------------------------------------------------------- bad_ts

VALID_TS = [
    "2026-03-01T09:00:00Z",
    "2026-03-01T09:00:00+07:00",
    "2026-03-01T09:00:00-05:30",
    "2026-03-01T09:00:00.250-05:30",
    "2026-03-01T09:00:00.5Z",
    "2026-03-01T09:00:00.123456Z",
    "2026-03-01T09:00:00+00:00",
    "2026-02-28T23:59:59Z",
    "2024-02-29T12:00:00Z",
]


@pytest.mark.parametrize("ts", VALID_TS)
def test_valid_timestamp_forms_accepted(ts):
    assert reject_reasons([raw("e1", "ann", ts)]) == []


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",          # no offset
    "2026-03-01 09:00:00Z",         # space instead of T
    "2026-03-01T09:00Z",            # no seconds
    "2026-03-01T09Z",               # no minutes
    "2026-03-01",                   # date only
    "09:00:00Z",                    # time only
    "2026-03-01T09:00:00 Z",        # space before offset
    "2026-03-01T09:00:00+0700",     # offset without colon
    "2026-03-01T09:00:00+07",       # short offset
    "2026-03-01T09:00:00UTC",       # named zone
    "2026-02-30T09:00:00Z",         # impossible date
    "2026-13-01T09:00:00Z",         # impossible month
    "2026-03-01T25:00:00Z",         # impossible hour
    "2026-03-01T09:60:00Z",         # impossible minute
    "not a time",
])
def test_invalid_timestamps_are_bad_ts(ts):
    assert only_reject(raw("e1", "ann", ts)) == "bad_ts"


def test_bad_ts_wins_over_bad_type():
    assert only_reject(raw("e1", "ann", "2026-03-01T09:00:00", "dance")) == "bad_ts"


def test_bad_ts_wins_over_bad_amount():
    assert only_reject(raw("e1", "ann", "2026-03-01T09:00:00", "purchase", amount="x")) == "bad_ts"


# ---------------------------------------------------------------- bad_type

@pytest.mark.parametrize("t", ["Purchase", "VIEW", "click ", "dance", "signup", "purchases"])
def test_bad_type(t):
    assert only_reject(raw("e1", "ann", "2026-03-01T09:00:00Z", t, amount="1")) == "bad_type"


def test_bad_type_wins_over_bad_amount():
    assert only_reject(raw("e1", "ann", "2026-03-01T09:00:00Z", "dance", amount="-1")) == "bad_type"


@pytest.mark.parametrize("t", ["view", "click", "purchase"])
def test_allowed_types_accepted(t):
    extra = {"amount": "1"} if t == "purchase" else {}
    assert reject_reasons([raw("e1", "ann", "2026-03-01T09:00:00Z", t, **extra)]) == []


# ---------------------------------------------------------------- bad_amount

@pytest.mark.parametrize("amount", ["5", "5.5", "19.99", "0", "0.00", "0.1", "123456789.99", "10.0"])
def test_valid_amounts_accepted(amount):
    assert reject_reasons([raw("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount=amount)]) == []


@pytest.mark.parametrize("amount", [
    "-1", "-0.01", "1.234", "1.001", "1e3", "abc", "", "5.", "1,50", "$5", "NaN", "Infinity", "0x10",
])
def test_invalid_amount_strings(amount):
    assert only_reject(raw("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount=amount)) == "bad_amount"


@pytest.mark.parametrize("amount", [5, 19.99, None, True, ["5"]])
def test_non_string_amount_is_bad(amount):
    line = json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": amount})
    assert only_reject(line) == "bad_amount"


def test_purchase_without_amount_is_bad():
    assert only_reject(raw("e1", "ann", "2026-03-01T09:00:00Z", "purchase")) == "bad_amount"


@pytest.mark.parametrize("t", ["view", "click"])
def test_amount_ignored_for_non_purchase(t):
    for amount in ["abc", "-5", 7, None, "1.2345"]:
        line = json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": t, "amount": amount})
        assert reject_reasons([line]) == [], amount


def test_non_purchase_without_amount_is_fine():
    assert reject_reasons([raw("e1", "ann", "2026-03-01T09:00:00Z", "click")]) == []


# ---------------------------------------------------------------- check order (first failing wins)

def test_first_failure_reported_only_once_per_line():
    r = run_job([raw("", "", "bad", "dance", amount="-1")])
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


# ---------------------------------------------------------------- user normalisation

def test_user_trimmed_and_lowercased_in_output():
    s = sessions_of([ev("e1", "  Ann  ", "09:00:00"), ev("e2", "ANN", "09:01:00")])
    assert len(s) == 1
    assert s[0]["user"] == "ann"
    assert s[0]["session_id"] == "ann-1"


def test_user_case_variants_are_one_user_for_sessions_numbering():
    assert ids([ev("e1", "Ann", "09:00:00"), ev("e2", "ann", "12:00:00")]) == ["ann-1", "ann-2"]


def test_users_with_different_names_are_separate():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "anna", "09:00:00")])
    assert sorted(x["user"] for x in s) == ["ann", "anna"]


def test_non_ascii_user_lowercased():
    s = sessions_of([ev("e1", "ÄNN", "09:00:00")])
    assert s[0]["user"] == "änn"
    assert s[0]["session_id"] == "änn-1"


def test_session_id_format():
    s = sessions_of([ev("e1", "bob", "09:00:00"), ev("e2", "bob", "10:00:00")])
    assert [x["session_id"] for x in s] == ["bob-1", "bob-2"]


# ---------------------------------------------------------------- event_id and duplicates

def test_duplicate_keeps_earliest_timestamp_even_if_later_in_file():
    r = run_job([ev("e1", "ann", "10:00:00", "purchase", amount="5"),
                 ev("e1", "ann", "09:00:00", "purchase", amount="7")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["revenue"] == "7.00"


def test_duplicate_timestamps_equal_keeps_first_in_file():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5"),
                 ev("e1", "ann", "09:00:00", "purchase", amount="7")])
    assert r.sessions[0]["revenue"] == "5.00"
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_equal_instant_different_offsets_keeps_first_in_file():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00+07:00", "purchase", amount="5"),
                 ev("e1", "ann", "2026-03-01T02:00:00Z", "purchase", amount="7")])
    assert r.sessions[0]["revenue"] == "5.00"


def test_duplicate_drop_counted_for_each_extra_copy():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00"), ev("e1", "ann", "09:02:00")])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "1"


def test_duplicates_are_not_rejects():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00")])
    assert r.rejects == []
    assert r.daily["rejected"] == 0


def test_invalid_duplicates_do_not_count_as_duplicates():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00", "dance")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.rejects == [{"line": 2, "reason": "bad_type"}]
    assert r.sessions[0]["events"] == "1"


def test_invalid_earlier_copy_does_not_shadow_valid_later_copy():
    r = run_job([ev("e1", "ann", "08:00:00", "dance"), ev("e1", "ann", "09:00:00")])
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.daily["duplicates_dropped"] == 0


def test_event_id_is_exact_match_case_sensitive():
    r = run_job([ev("E1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_event_id_exact_match_no_trimming():
    r = run_job([ev("e1", "ann", "09:00:00"), ev(" e1", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_event_id_is_global_across_users():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "bob", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert [s["user"] for s in r.sessions] == ["ann"]


def test_duplicate_removed_before_gap_computation():
    # e1 at 09:50 is a later duplicate; it must not split ann's session.
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:20:00"), ev("e1", "ann", "09:50:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[0]["end"] == "2026-03-01T09:20:00Z"


# ---------------------------------------------------------------- sessions: gaps and boundaries

def test_gap_exactly_30_minutes_same_session():
    assert ids([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00")]) == ["ann-1"]


def test_gap_just_over_30_minutes_new_session():
    assert ids([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01")]) == ["ann-1", "ann-2"]


def test_gap_30_minutes_with_fractional_seconds_same_session():
    assert ids([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:30:00.000Z")]) == ["ann-1"]


def test_gap_30_min_plus_half_second_new_session_full_precision():
    lines = [raw("e1", "ann", "2026-03-01T09:00:00.000Z"), raw("e2", "ann", "2026-03-01T09:30:00.500Z")]
    assert ids(lines) == ["ann-1", "ann-2"]


def test_gap_30_min_minus_half_second_same_session():
    lines = [raw("e1", "ann", "2026-03-01T09:00:00.900Z"), raw("e2", "ann", "2026-03-01T09:30:00.300Z")]
    assert ids(lines) == ["ann-1"]


def test_gap_measured_between_consecutive_events_not_session_start():
    # 09:00, 09:25, 09:50: each gap is 25 min, so one session spanning 50 min.
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:25:00"), ev("e3", "ann", "09:50:00")])
    assert len(s) == 1
    assert s[0]["duration_s"] == "3000"


def test_gap_is_per_user_other_users_do_not_split():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"), ev("e3", "ann", "09:20:00")])
    assert [x["session_id"] for x in s] == ["ann-1", "bob-1"]
    ann = [x for x in s if x["user"] == "ann"][0]
    assert ann["events"] == "2"


def test_other_user_activity_does_not_bridge_gap():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:20:00"), ev("e3", "ann", "09:40:00")])
    ann = sorted([x for x in s if x["user"] == "ann"], key=lambda x: x["session_id"])
    assert [x["session_id"] for x in ann] == ["ann-1", "ann-2"]


def test_session_numbering_restarts_per_user():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "11:00:00"),
                     ev("e3", "bob", "09:00:00"), ev("e4", "bob", "11:00:00"), ev("e5", "bob", "15:00:00")])
    got = sorted(x["session_id"] for x in s)
    assert got == ["ann-1", "ann-2", "bob-1", "bob-2", "bob-3"]


def test_session_ids_follow_time_order_not_file_order():
    s = sessions_of([ev("e1", "ann", "11:00:00"), ev("e2", "ann", "09:00:00")])
    by_start = {x["start"]: x["session_id"] for x in s}
    assert by_start["2026-03-01T09:00:00Z"] == "ann-1"
    assert by_start["2026-03-01T11:00:00Z"] == "ann-2"


def test_events_out_of_file_order_are_sorted_for_gaps():
    s = sessions_of([ev("e1", "ann", "09:40:00"), ev("e2", "ann", "09:00:00"), ev("e3", "ann", "09:20:00")])
    assert len(s) == 1
    assert s[0]["start"] == "2026-03-01T09:00:00Z" and s[0]["end"] == "2026-03-01T09:40:00Z"


def test_gap_check_uses_utc_instants_across_offsets():
    # 09:00+07:00 == 02:00Z; 02:45Z is 45 min later -> new session
    s = sessions_of([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T02:45:00Z")])
    assert len(s) == 2


def test_same_instant_different_offsets_same_session():
    s = sessions_of([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T02:05:00Z")])
    assert len(s) == 1


def test_identical_timestamps_same_session_no_gap():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:00:00", "click")])
    assert len(s) == 1 and s[0]["events"] == "2"


def test_single_event_session_duration_zero():
    s = sessions_of([ev("e1", "ann", "09:00:00")])
    assert s[0]["duration_s"] == "0" and s[0]["start"] == s[0]["end"]


# ---------------------------------------------------------------- sessions: row content

def test_session_row_values_exact():
    r = run_job([ev("e1", "ann", "2026-03-01T02:00:00Z"),
                 ev("e2", "ann", "2026-03-01T02:05:00Z", "click"),
                 ev("e3", "ann", "2026-03-01T02:10:30Z", "purchase", amount="19.99")])
    assert r.sessions == [{"session_id": "ann-1", "user": "ann", "start": "2026-03-01T02:00:00Z",
                           "end": "2026-03-01T02:10:30Z", "duration_s": "630", "events": "3",
                           "purchases": "1", "revenue": "19.99"}]


def test_session_row_example_from_spec_shape():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00+07:00"),
                 ev("e2", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99")])
    s = r.sessions[0]
    assert s["session_id"] == "ann-1" and s["start"] == "2026-03-01T02:00:00Z"
    assert s["end"] == "2026-03-01T02:10:30Z" and s["duration_s"] == "630"


def test_start_and_end_are_utc_z_format_without_fraction():
    r = run_job([raw("e1", "ann", "2026-03-01T09:00:00.987+07:00"),
                 raw("e2", "ann", "2026-03-01T09:00:05.999+07:00")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T02:00:00Z"
    assert s["end"] == "2026-03-01T02:00:05Z"


def test_duration_rounded_down_from_fractional():
    r = run_job([raw("e1", "ann", "2026-03-01T09:00:00.000Z"), raw("e2", "ann", "2026-03-01T09:00:01.999Z")])
    assert r.sessions[0]["duration_s"] == "1"


def test_duration_sub_second_is_zero():
    r = run_job([raw("e1", "ann", "2026-03-01T09:00:00.100Z"), raw("e2", "ann", "2026-03-01T09:00:00.900Z")])
    assert r.sessions[0]["duration_s"] == "0"


def test_duration_across_midnight():
    r = run_job([ev("e1", "ann", "2026-03-01T23:50:00Z"), ev("e2", "ann", "2026-03-02T00:10:00Z")])
    assert r.sessions[0]["duration_s"] == "1200"


def test_event_and_purchase_counts():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1"),
                 ev("e2", "ann", "09:01:00", "purchase", amount="2"),
                 ev("e3", "ann", "09:02:00"),
                 ev("e4", "ann", "09:03:00", "click")])
    s = r.sessions[0]
    assert s["events"] == "4" and s["purchases"] == "2" and s["revenue"] == "3.00"


def test_revenue_two_decimals_when_no_purchase():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


def test_revenue_formatted_with_two_decimals():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5")])
    assert r.sessions[0]["revenue"] == "5.00"


def test_revenue_exact_decimal_no_float_error():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0.10"),
                 ev("e2", "ann", "09:01:00", "purchase", amount="0.20")])
    assert r.sessions[0]["revenue"] == "0.30"


def test_revenue_many_small_amounts_exact():
    lines = [ev(f"e{i}", "ann", f"09:{i:02d}:00", "purchase", amount="0.01") for i in range(10)]
    r = run_job(lines)
    assert r.sessions[0]["revenue"] == "0.10"


def test_revenue_large_amount_exact():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="123456789012345.67"),
                 ev("e2", "ann", "09:01:00", "purchase", amount="0.33")])
    assert r.sessions[0]["revenue"] == "123456789012346.00"


def test_zero_purchase_revenue_is_0_00():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0")])
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "1"


def test_amount_ignored_for_views_in_revenue():
    r = run_job([ev("e1", "ann", "09:00:00", "view", amount="99")])
    assert r.sessions[0]["revenue"] == "0.00"


# ---------------------------------------------------------------- sessions: ordering

def test_rows_ordered_by_start_ascending():
    s = sessions_of([ev("e1", "ann", "12:00:00"), ev("e2", "bob", "08:00:00"), ev("e3", "cid", "10:00:00")])
    assert [x["user"] for x in s] == ["bob", "cid", "ann"]


def test_rows_tie_on_start_ordered_by_user():
    s = sessions_of([ev("e1", "zed", "09:00:00"), ev("e2", "amy", "09:00:00"), ev("e3", "max", "09:00:00")])
    assert [x["user"] for x in s] == ["amy", "max", "zed"]


def test_rows_ordered_by_full_precision_start():
    lines = [raw("e1", "amy", "2026-03-01T09:00:00.900Z"), raw("e2", "zed", "2026-03-01T09:00:00.100Z")]
    s = sessions_of(lines)
    assert [x["user"] for x in s] == ["zed", "amy"]


def test_rows_order_not_affected_by_file_order():
    a = sessions_of([ev("e1", "bob", "10:00:00"), ev("e2", "ann", "09:00:00")])
    b = sessions_of([ev("e2", "ann", "09:00:00"), ev("e1", "bob", "10:00:00")])
    assert pick(a, "session_id") == pick(b, "session_id")


def test_csv_file_row_order_matches_parsed_rows():
    r = run_job([ev("e1", "bob", "10:00:00"), ev("e2", "ann", "09:00:00")])
    assert pick(r.sessions, "user") == [("ann",), ("bob",)]


# ---------------------------------------------------------------- daily summary

def test_days_listed_ascending_by_date():
    r = run_job([ev("e1", "ann", "2026-03-03T09:00:00Z"), ev("e2", "ann", "2026-03-01T09:00:00Z"),
                 ev("e3", "ann", "2026-03-02T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-02", "2026-03-03"]


def test_days_without_sessions_are_omitted():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e2", "ann", "2026-03-05T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-05"]


def test_day_with_only_rejected_lines_not_listed():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), raw("e2", "ann", "2026-03-02T09:00:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]


def test_day_with_only_dropped_duplicates_not_listed():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e1", "ann", "2026-03-02T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]


def test_empty_days_when_no_sessions():
    r = run_job([raw("e1", "ann", "bad")])
    assert r.daily["days"] == []
    assert r.daily["rejected"] == 1


def test_session_belongs_to_utc_date_of_start_across_midnight():
    r = run_job([ev("e1", "ann", "2026-03-01T23:50:00Z"), ev("e2", "ann", "2026-03-02T00:10:00Z", "purchase", amount="4")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "4.00", "top_user": "ann"}]


def test_date_uses_utc_not_local_offset():
    # 2026-03-01T01:00+07:00 is 2026-02-28T18:00Z
    r = run_job([ev("e1", "ann", "2026-03-01T01:00:00+07:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-02-28"]


def test_session_starting_late_previous_day_counted_on_its_start_date():
    r = run_job([ev("e1", "ann", "2026-03-01T23:59:00+00:00"), ev("e2", "ann", "2026-03-02T00:20:00+00:00")])
    assert r.daily["days"][0]["date"] == "2026-03-01"
    assert r.daily["days"][0]["sessions"] == 1


def test_day_sessions_and_users_counts():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "12:00:00"), ev("e3", "bob", "09:00:00")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3
    assert d["users"] == 2


def test_users_counts_distinct_users_not_sessions():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "13:00:00"), ev("e3", "ann", "17:00:00")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3 and d["users"] == 1


def test_day_revenue_sums_session_revenue():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5.00"),
                 ev("e2", "bob", "09:00:00", "purchase", amount="20"),
                 ev("e3", "ann", "15:00:00", "purchase", amount="0.01")])
    d = r.daily["days"][0]
    assert d["revenue"] == "25.01"


def test_day_revenue_formatted_two_decimals_string():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="24")])
    assert r.daily["days"][0]["revenue"] == "24.00"


def test_day_with_no_revenue_has_zero_and_null_top_user():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:00:00")])
    d = r.daily["days"][0]
    assert d["revenue"] == "0.00" and d["top_user"] is None


def test_day_with_only_zero_amount_purchase_has_null_top_user():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0")])
    d = r.daily["days"][0]
    assert d["revenue"] == "0.00" and d["top_user"] is None


def test_top_user_is_highest_revenue_user():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5.00"),
                 ev("e2", "bob", "09:00:00", "purchase", amount="20")])
    assert r.daily["days"][0]["top_user"] == "bob"


def test_top_user_tie_goes_to_name_sorting_first():
    r = run_job([ev("e1", "zed", "09:00:00", "purchase", amount="5"),
                 ev("e2", "amy", "09:00:00", "purchase", amount="5")])
    assert r.daily["days"][0]["top_user"] == "amy"


def test_top_user_sums_revenue_across_sessions_of_same_day():
    # ann: 9 + 9 = 18 across two sessions; bob: 10 in one session -> ann
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="9"),
                 ev("e2", "ann", "15:00:00", "purchase", amount="9"),
                 ev("e3", "bob", "10:00:00", "purchase", amount="10")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_top_user_ignores_other_days():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="1"),
                 ev("e2", "bob", "2026-03-02T09:00:00Z", "purchase", amount="1"),
                 ev("e3", "ann", "2026-03-02T10:00:00Z", "view")])
    by_date = {d["date"]: d for d in r.daily["days"]}
    assert by_date["2026-03-01"]["top_user"] == "ann"
    assert by_date["2026-03-02"]["top_user"] == "bob"


def test_top_user_is_lowercased_user():
    r = run_job([ev("e1", "ANN", "09:00:00", "purchase", amount="1")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_daily_counts_duplicates_and_rejects():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), "{", ev("e2", "bob", "09:00:00", "dance")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2


def test_daily_json_counts_are_integers():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1")])
    d = r.daily["days"][0]
    assert isinstance(d["sessions"], int) and isinstance(d["users"], int)
    assert isinstance(r.daily["rejected"], int) and isinstance(r.daily["duplicates_dropped"], int)


def test_daily_revenue_consistent_with_sessions_csv():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1.10"),
                 ev("e2", "bob", "09:00:00", "purchase", amount="2.20"),
                 ev("e3", "ann", "20:00:00", "purchase", amount="3.30")])
    total_from_csv = sum(float(s["revenue"]) for s in r.sessions)
    assert r.daily["days"][0]["revenue"] == f"{total_from_csv:.2f}"


def test_every_session_counted_on_exactly_one_day():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e2", "ann", "2026-03-01T15:00:00Z"),
                 ev("e3", "bob", "2026-03-02T09:00:00Z")])
    assert sum(d["sessions"] for d in r.daily["days"]) == len(r.sessions)


# ---------------------------------------------------------------- end-to-end scenario

def test_combined_scenario_matches_spec_rules():
    lines = [
        "",                                                                    # line 1: blank, skipped
        ev("e1", "Ann", "2026-03-01T09:00:00+07:00", "view"),                  # line 2 -> 02:00Z
        "{broken",                                                             # line 3: bad_json
        ev("e2", " ann ", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99"),  # line 4
        ev("e2", "ann", "2026-03-01T09:30:00+07:00", "purchase", amount="19.99"),    # line 5: dup, later
        raw("e3", "ann", "2026-03-01T09:50:00"),                               # line 6: bad_ts
        ev("e4", "bob", "2026-03-01T23:50:00Z", "purchase", amount="5"),       # line 7
        ev("e5", "bob", "2026-03-02T00:10:00Z", "purchase", amount="0.5"),     # line 8
        ev("e6", "ann", "2026-03-01T12:00:00Z", "dance"),                      # line 9: bad_type
        ev("e7", "ann", "2026-03-01T12:00:00Z", "purchase"),                   # line 10: bad_amount
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 3, "reason": "bad_json"}, {"line": 6, "reason": "bad_ts"},
                         {"line": 9, "reason": "bad_type"}, {"line": 10, "reason": "bad_amount"}]
    assert pick(r.sessions, "session_id", "start", "end", "duration_s", "events", "purchases", "revenue") == [
        ("ann-1", "2026-03-01T02:00:00Z", "2026-03-01T02:10:30Z", "630", "2", "1", "19.99"),
        ("bob-1", "2026-03-01T23:50:00Z", "2026-03-02T00:10:00Z", "1200", "2", "2", "5.50"),
    ]
    assert r.daily == {
        "days": [{"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "25.49", "top_user": "ann"}],
        "duplicates_dropped": 1,
        "rejected": 4,
    }
