"""Regression suite for sessionize (see SPEC.md). Black-box: every test runs job.py as a subprocess."""
import json
import os
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from helpers import APP_DIR, ev, pick, run_job

JOB = os.path.join(APP_DIR, "job.py")
HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
OUT_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DAY = "2026-03-01"


# ---------------------------------------------------------------- helpers

def execute(lines, out_parts=("out",), pre_files=None, in_dir_exists=True, extra=()):
    """Run the job with raw events.jsonl lines. Returns code, stderr and the text of every output file."""
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        out = os.path.join(d, *out_parts)
        if in_dir_exists:
            os.makedirs(inp)
            with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        if pre_files:
            os.makedirs(out, exist_ok=True)
            for name, text in pre_files.items():
                with open(os.path.join(out, name), "w", encoding="utf-8") as f:
                    f.write(text)
        p = subprocess.run([sys.executable, JOB, "--in", inp, "--out", out, *extra],
                           capture_output=True, text=True, timeout=30, cwd=APP_DIR)
        texts = {}
        if os.path.isdir(out):
            for name in sorted(os.listdir(out)):
                with open(os.path.join(out, name), encoding="utf-8", newline="") as f:
                    texts[name] = f.read()
        return SimpleNamespace(code=p.returncode, stderr=p.stderr, texts=texts)


def raw_event(event_id="e1", user="ann", ts="2026-03-01T09:00:00Z", type="view", **extra):
    """Build an event line; any field (named or keyword) may be _DROP to omit it, or any value to set it raw."""
    obj = {"event_id": event_id, "user": user, "ts": ts, "type": type, **extra}
    return json.dumps({k: v for k, v in obj.items() if v is not _DROP})


_DROP = object()


def purchase(event_id, user, ts, amount):
    return ev(event_id, user, ts, "purchase", amount=amount)


def rejects_of(r):
    return [(x["line"], x["reason"]) for x in r.rejects]


def only_session(r):
    assert len(r.sessions) == 1, r.sessions
    return r.sessions[0]


# ---------------------------------------------------------------- output files and exit codes

def test_writes_exactly_three_files():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_sessions_csv_header_is_exact():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.header == HEADER


def test_sessions_csv_raw_row_format():
    res = execute([ev("e1", "ann", "2026-03-01T09:00:00+07:00"),
                   ev("e2", "ann", "2026-03-01T02:05:30Z", "purchase", amount="19.9")])
    assert res.code == 0
    lines = res.texts["sessions.csv"].split("\n")
    assert lines[0] == ",".join(HEADER)
    assert lines[1] == "ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:05:30Z,330,2,1,19.90"
    assert lines[2] == "" and lines[-1] == ""  # file ends with exactly one newline


def test_empty_input_still_writes_all_files():
    res = execute([])
    assert res.code == 0
    assert sorted(res.texts) == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert res.texts["sessions.csv"].strip() == ",".join(HEADER)
    assert json.loads(res.texts["daily.json"]) == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    assert res.texts["rejects.jsonl"] == ""


def test_whitespace_only_input_still_writes_all_files():
    r = run_job(["", "   ", "\t"])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == [] and r.rejects == [] and r.daily["rejected"] == 0


def test_creates_nested_output_directory():
    res = execute([ev("e1", "ann", "09:00:00")], out_parts=("a", "b", "out"))
    assert res.code == 0
    assert sorted(res.texts) == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_rerun_overwrites_stale_output():
    stale = {"sessions.csv": "stale\n", "daily.json": "{}", "rejects.jsonl": "{\"line\": 99, \"reason\": \"x\"}\n"}
    res = execute([ev("e1", "ann", "09:00:00")], pre_files=stale)
    assert res.code == 0
    assert "stale" not in res.texts["sessions.csv"]
    assert json.loads(res.texts["daily.json"])["rejected"] == 0
    assert res.texts["rejects.jsonl"] == ""


def test_rerun_is_deterministic():
    lines = [ev("e1", "bob", "10:00:00", "purchase", amount="1.5"), ev("e2", "ann", "09:00:00"),
             "{bad", ev("e1", "bob", "10:00:00", "purchase", amount="1.5")]
    a = execute(lines)
    b = execute(lines)
    assert a.texts == b.texts


def test_exit_zero_when_lines_rejected():
    r = run_job([ev("e1", "ann", "09:00:00"), "not json"])
    assert r.code == 0
    assert r.rejects == [{"line": 2, "reason": "bad_json"}]


def test_missing_events_file_exits_2_with_stderr_message():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""
    assert "Traceback" not in r.stderr


def test_missing_input_directory_exits_2():
    res = execute([], in_dir_exists=False)
    assert res.code == 2
    assert res.stderr.strip() != ""


def test_daily_json_has_spec_keys():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1")])
    assert {"days", "duplicates_dropped", "rejected"} <= set(r.daily)
    day = r.daily["days"][0]
    assert {"date", "sessions", "users", "revenue", "top_user"} <= set(day)


# ---------------------------------------------------------------- timestamps

@pytest.mark.parametrize("ts, expected", [
    ("2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T09:00:00+07:00", "2026-03-01T02:00:00Z"),
    ("2026-03-01T09:00:00-05:30", "2026-03-01T14:30:00Z"),
    ("2026-03-01T09:00:00+00:00", "2026-03-01T09:00:00Z"),
    ("2026-03-01T09:00:00.250-05:30", "2026-03-01T14:30:00Z"),
    ("2026-03-01T09:00:00.5Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T09:00:00.123456Z", "2026-03-01T09:00:00Z"),
    ("2026-03-01T23:30:00-05:00", "2026-03-02T04:30:00Z"),
])
def test_timestamp_forms_converted_to_utc(ts, expected):
    r = run_job([raw_event("e1", "ann", ts)])
    assert r.code == 0 and r.rejects == []
    assert only_session(r)["start"] == expected


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",           # no offset
    "2026-03-01T09:00Z",             # no seconds
    "2026-03-01",                    # date only
    "2026-03-01T09:00:00 Z",         # garbage before offset
    "2026-02-30T09:00:00Z",          # impossible date
    "2026-13-01T09:00:00Z",          # impossible month
    "2026-03-01T09:60:00Z",          # impossible minute
    "yesterday",
])
def test_invalid_timestamps_rejected(ts):
    r = run_job([raw_event("e1", "ann", ts), ev("e2", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "bad_ts")]
    assert [s["user"] for s in r.sessions] == ["bob"]


def test_fractional_seconds_do_not_appear_in_start_or_end():
    r = run_job([raw_event("e1", "ann", "2026-03-01T09:00:00.750Z"),
                 raw_event("e2", "ann", "2026-03-01T09:00:10.999Z")])
    s = only_session(r)
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:00:10Z"
    assert s["duration_s"] == "10"


def test_output_timestamps_have_strict_format():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.9+01:00"), ev("e2", "bob", "2026-03-02T00:00:01Z")])
    for s in r.sessions:
        assert OUT_TS.match(s["start"]) and OUT_TS.match(s["end"])


# ---------------------------------------------------------------- event_type and amount rules

@pytest.mark.parametrize("etype", ["view", "click"])
def test_view_and_click_are_valid(etype):
    r = run_job([ev("e1", "ann", "09:00:00", etype)])
    assert r.rejects == [] and only_session(r)["purchases"] == "0"


@pytest.mark.parametrize("etype", ["logout", "refund", "Purchase", "PURCHASE", "buy", "view "])
def test_unknown_types_rejected(etype):
    r = run_job([ev("e1", "ann", "09:00:00", etype), ev("e2", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "bad_type")]


@pytest.mark.parametrize("amount", ["0", "5", "5.5", "19.99", "0.01", "1000000.00", "123456789012345678.99"])
def test_valid_purchase_amounts(amount):
    r = run_job([purchase("e1", "ann", "09:00:00", amount)])
    assert r.rejects == []
    assert only_session(r)["purchases"] == "1"


@pytest.mark.parametrize("amount", [
    "-1", "-0.50", "1.234", "abc", "", "1,50", "1e3", "$5", "5.555", "--5",
    5, 5.5, None,
])
def test_invalid_purchase_amounts_rejected(amount):
    line = raw_event("e1", "ann", type="purchase", amount=amount)
    r = run_job([line, ev("e2", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "bad_amount")]


def test_purchase_without_amount_rejected():
    line = raw_event("e1", "ann", type="purchase", amount=_DROP)
    r = run_job([line])
    assert rejects_of(r) == [(1, "bad_amount")]


def test_amount_ignored_for_non_purchase_types():
    r = run_job([raw_event("e1", "ann", type="view", amount="abc"),
                 raw_event("e2", "ann", "2026-03-01T09:01:00Z", type="click", amount=7),
                 raw_event("e3", "ann", "2026-03-01T09:02:00Z", type="click", amount=_DROP)])
    assert r.rejects == []
    s = only_session(r)
    assert s["events"] == "3" and s["purchases"] == "0" and s["revenue"] == "0.00"


def test_zero_amount_purchase_counts_as_purchase():
    r = run_job([purchase("e1", "ann", "09:00:00", "0")])
    s = only_session(r)
    assert s["purchases"] == "1" and s["revenue"] == "0.00"


# ---------------------------------------------------------------- required fields

@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_missing_required_field(field):
    r = run_job([raw_event(**{field: _DROP}), ev("e9", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "missing_field")]


@pytest.mark.parametrize("field, value", [
    ("event_id", 7), ("event_id", None), ("event_id", ["e1"]),
    ("user", 7), ("user", None), ("user", {"name": "ann"}),
    ("ts", 1772326800), ("ts", None), ("ts", ["2026-03-01T09:00:00Z"]),
    ("type", None), ("type", 1),
])
def test_non_string_required_field(field, value):
    r = run_job([raw_event(**{field: value}), ev("e9", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "missing_field")]


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("blank", ["", " ", "   ", "\t"])
def test_blank_required_field(field, blank):
    r = run_job([raw_event(**{field: blank}), ev("e9", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "missing_field")]


# ---------------------------------------------------------------- json validity

@pytest.mark.parametrize("line", [
    "{not json", '{"event_id": "e1"', "[]", '["e1"]', '"text"', "42", "true", "null",
    "{'event_id': 'e1'}",
])
def test_bad_json_lines(line):
    r = run_job([line, ev("e9", "bob", "09:00:00")])
    assert rejects_of(r) == [(1, "bad_json")]


# ---------------------------------------------------------------- rejection order

def test_missing_field_checked_before_bad_ts():
    r = run_job([raw_event(user=_DROP, ts="nope", type="dance")])
    assert rejects_of(r) == [(1, "missing_field")]


def test_bad_ts_checked_before_bad_type():
    r = run_job([raw_event(ts="nope", type="dance")])
    assert rejects_of(r) == [(1, "bad_ts")]


def test_bad_type_checked_before_bad_amount():
    r = run_job([raw_event(type="dance", amount="abc")])
    assert rejects_of(r) == [(1, "bad_type")]


def test_bad_ts_checked_before_bad_amount():
    r = run_job([raw_event(ts="nope", type="purchase", amount="abc")])
    assert rejects_of(r) == [(1, "bad_ts")]


def test_bad_json_checked_before_everything():
    r = run_job(["[1, 2"])
    assert rejects_of(r) == [(1, "bad_json")]


# ---------------------------------------------------------------- rejects file and line numbers

def test_reject_line_numbers_count_blank_lines():
    lines = ["", ev("e1", "ann", "09:00:00"), "   ", "{oops", "", ev("e2", "ann", "09:01:00", "dance")]
    r = run_job(lines)
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 6, "reason": "bad_type"}]


def test_rejects_in_input_order_with_exact_format():
    res = execute([ev("e1", "ann", "09:00:00"), "{x", ev("e2", "ann", "09:01:00", "nope")])
    assert res.texts["rejects.jsonl"] == (
        '{"line": 2, "reason": "bad_json"}\n{"line": 3, "reason": "bad_type"}\n')


def test_rejected_count_in_daily_summary():
    r = run_job(["{x", "[]", ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:00:01", "zzz")])
    assert r.daily["rejected"] == 3
    assert r.daily["duplicates_dropped"] == 0


def test_rejected_lines_do_not_split_or_create_sessions():
    r = run_job([ev("e1", "ann", "09:00:00"),
                 raw_event("e2", "ann", "09:10:00", type="dance"),
                 ev("e3", "ann", "09:20:00"),
                 raw_event("e4", "ghost", "bad")])
    assert [s["user"] for s in r.sessions] == ["ann"]
    s = only_session(r)
    assert s["events"] == "2" and s["duration_s"] == "1200"


def test_rejected_duplicate_does_not_take_part_in_dedupe():
    r = run_job([ev("e1", "ann", "09:00:00"),
                 raw_event("e1", "bob", "2026-03-01T08:00:00Z", type="dance")])  # earlier but invalid
    assert rejects_of(r) == [(2, "bad_type")]
    assert r.daily["duplicates_dropped"] == 0
    assert [s["user"] for s in r.sessions] == ["ann"]


# ---------------------------------------------------------------- duplicates

def test_duplicate_keeps_earliest_timestamp_regardless_of_file_order():
    r = run_job([purchase("e1", "ann", "09:05:00", "5"), purchase("e1", "bob", "09:00:00", "7")])
    assert [s["user"] for s in r.sessions] == ["bob"]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_with_equal_timestamp_keeps_first_in_file():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "bob", "09:00:00")])
    assert [s["user"] for s in r.sessions] == ["ann"]
    r = run_job([ev("e1", "bob", "09:00:00"), ev("e1", "ann", "09:00:00")])
    assert [s["user"] for s in r.sessions] == ["bob"]


def test_duplicate_comparison_uses_instant_not_text():
    # 10:00+01:00 is 09:00Z, earlier than 09:30Z even though its text is later.
    r = run_job([ev("e1", "bob", "2026-03-01T09:30:00Z"), ev("e1", "ann", "2026-03-01T10:00:00+01:00")])
    assert [s["user"] for s in r.sessions] == ["ann"]


def test_duplicate_counted_in_daily_summary():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:05"),
                 ev("e2", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 2
    assert only_session(r)["events"] == "2"


def test_duplicate_ids_are_exact_string_match():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("E1", "ann", "09:00:01"), ev("e1 ", "ann", "09:00:02")])
    assert r.daily["duplicates_dropped"] == 0
    assert only_session(r)["events"] == "3"


def test_duplicate_removed_before_sessionizing():
    # Without dedupe, the 09:50 copy of e2 would be the last event and split the session.
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:50:00"), ev("e2", "ann", "09:20:00")])
    s = only_session(r)
    assert s["events"] == "2" and s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:20:00Z"


def test_duplicate_of_one_event_does_not_alter_other_counts():
    r = run_job([purchase("e1", "ann", "09:00:00", "5"), purchase("e1", "ann", "09:00:00", "5")])
    s = only_session(r)
    assert s["purchases"] == "1" and s["revenue"] == "5.00"


# ---------------------------------------------------------------- user normalisation

def test_user_trimmed_and_lowercased():
    r = run_job([ev("e1", " Ann ", "09:00:00"), ev("e2", "ANN", "09:01:00"), ev("e3", "ann", "09:02:00")])
    assert len(r.sessions) == 1
    s = only_session(r)
    assert s["user"] == "ann" and s["session_id"] == "ann-1" and s["events"] == "3"


def test_unicode_user_lowercased():
    r = run_job([ev("e1", "Zoë", "09:00:00")])
    assert only_session(r)["user"] == "zoë"
    assert only_session(r)["session_id"] == "zoë-1"


def test_users_with_different_case_are_one_user_in_daily():
    r = run_job([ev("e1", "Ann", "09:00:00"), ev("e2", "ann", "12:00:00")])
    assert r.daily["days"] == [{"date": DAY, "sessions": 2, "users": 1, "revenue": "0.00", "top_user": None}]


# ---------------------------------------------------------------- sessions: gaps

def test_gap_of_exactly_30_minutes_same_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00")])
    assert len(r.sessions) == 1 and only_session(r)["events"] == "2"


def test_gap_just_over_30_minutes_new_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01")])
    assert pick(r.sessions, "session_id") == [("ann-1",), ("ann-2",)]


def test_gap_of_30_minutes_measured_with_fractions():
    same = run_job([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:29:59.999Z")])
    assert len(same.sessions) == 1
    split = run_job([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:30:00.001Z")])
    assert len(split.sessions) == 2


def test_gap_measured_across_offsets():
    # 09:00Z and 10:31+01:00 (09:31Z) are 31 minutes apart.
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e2", "ann", "2026-03-01T10:31:00+01:00")])
    assert len(r.sessions) == 2


def test_gap_is_to_previous_event_not_session_start():
    lines = [ev(f"e{i}", "ann", f"2026-03-01T{9 + (i * 20) // 60:02d}:{(i * 20) % 60:02d}:00Z") for i in range(7)]
    r = run_job(lines)  # 09:00 .. 11:00 in 20-minute steps: one session
    s = only_session(r)
    assert s["events"] == "7" and s["duration_s"] == "7200"


def test_gap_uses_time_order_not_file_order():
    # File order 09:00, 10:00, 09:30: time order gives gaps of 30 and 30 -> one session.
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "10:00:00"), ev("e3", "ann", "09:30:00")])
    s = only_session(r)
    assert s["events"] == "3" and s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T10:00:00Z"


def test_gap_checked_per_user():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"), ev("e3", "ann", "09:20:00"),
                 ev("e4", "bob", "10:00:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("bob-1", "1"), ("bob-2", "1")]


def test_session_numbering_is_per_user_in_time_order():
    r = run_job([ev("e1", "ann", "12:00:00"), ev("e2", "ann", "08:00:00"), ev("e3", "ann", "10:00:00")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T08:00:00Z"), ("ann-2", "2026-03-01T10:00:00Z"), ("ann-3", "2026-03-01T12:00:00Z")]


def test_out_of_order_lines_form_correct_session_bounds():
    r = run_job([ev("e2", "ann", "09:10:00"), ev("e1", "ann", "09:00:00")])
    s = only_session(r)
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:10:00Z"


# ---------------------------------------------------------------- sessions: values

def test_duration_rounds_down_to_whole_seconds():
    r = run_job([raw_event("e1", "ann", "2026-03-01T09:00:00.000Z"),
                 raw_event("e2", "ann", "2026-03-01T09:00:59.999Z")])
    assert only_session(r)["duration_s"] == "59"


def test_single_event_session_has_zero_duration():
    r = run_job([ev("e1", "ann", "09:00:00")])
    s = only_session(r)
    assert s["duration_s"] == "0" and s["start"] == s["end"] and s["events"] == "1"


def test_duration_example_from_spec_shape():
    r = run_job([ev("e1", "ann", "2026-03-01T02:00:00Z"), ev("e2", "ann", "2026-03-01T02:05:00Z"),
                 ev("e3", "ann", "2026-03-01T02:10:30Z", "purchase", amount="19.99")])
    s = only_session(r)
    assert s["session_id"] == "ann-1" and s["duration_s"] == "630"
    assert s["events"] == "3" and s["purchases"] == "1" and s["revenue"] == "19.99"


def test_events_count_all_types_and_purchases_only_purchase_type():
    r = run_job([ev("e1", "ann", "09:00:00", "view"), ev("e2", "ann", "09:01:00", "click"),
                 ev("e3", "ann", "09:02:00", "purchase", amount="1"),
                 ev("e4", "ann", "09:03:00", "purchase", amount="2")])
    s = only_session(r)
    assert s["events"] == "4" and s["purchases"] == "2" and s["revenue"] == "3.00"


def test_revenue_is_exact_decimal():
    r = run_job([purchase("e1", "ann", "09:00:00", "0.1"), purchase("e2", "ann", "09:01:00", "0.2")])
    assert only_session(r)["revenue"] == "0.30"


def test_revenue_two_decimals_always():
    r = run_job([purchase("e1", "ann", "09:00:00", "5"), purchase("e2", "ann", "09:01:00", "19.9")])
    assert only_session(r)["revenue"] == "24.90"
    r = run_job([ev("e1", "bob", "09:00:00")])
    assert only_session(r)["revenue"] == "0.00"


def test_revenue_large_amounts_exact():
    r = run_job([purchase("e1", "ann", "09:00:00", "12345678901234567.89"),
                 purchase("e2", "ann", "09:01:00", "0.11")])
    assert only_session(r)["revenue"] == "12345678901234568.00"


def test_revenue_sums_only_purchases_in_session():
    r = run_job([purchase("e1", "ann", "09:00:00", "10"), purchase("e2", "ann", "10:00:00", "3")])
    assert pick(r.sessions, "session_id", "revenue") == [("ann-1", "10.00"), ("ann-2", "3.00")]


def test_session_row_order_by_full_precision_start_then_user():
    # Truncated starts are equal (09:00:00Z); full precision puts bob first even though ann sorts first.
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "bob", "2026-03-01T09:00:00.100Z")])
    assert pick(r.sessions, "user") == [("bob",), ("ann",)]


def test_same_start_sorted_by_user_ascending():
    r = run_job([ev("e1", "zed", "09:00:00"), ev("e2", "amy", "09:00:00"), ev("e3", "kim", "09:00:00")])
    assert pick(r.sessions, "user") == [("amy",), ("kim",), ("zed",)]


def test_sessions_sorted_globally_by_start():
    r = run_job([ev("e1", "ann", "11:00:00"), ev("e2", "bob", "09:00:00"), ev("e3", "ann", "10:00:00"),
                 ev("e4", "bob", "12:00:00")])
    starts = [s["start"] for s in r.sessions]
    assert starts == sorted(starts)
    assert len(starts) == 4


def test_session_start_end_truncate_fraction_but_gaps_do_not():
    r = run_job([raw_event("e1", "ann", "2026-03-01T09:00:00.900Z"),
                 raw_event("e2", "ann", "2026-03-01T09:30:00.950Z")])  # gap 30.05 min -> split
    assert [s["start"] for s in r.sessions] == ["2026-03-01T09:00:00Z", "2026-03-01T09:30:00Z"]


# ---------------------------------------------------------------- daily summary

def test_days_listed_ascending_and_skip_empty_dates():
    r = run_job([ev("e1", "ann", "2026-03-05T09:00:00Z"), ev("e2", "bob", "2026-03-01T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-05"]


def test_session_belongs_to_utc_date_of_start_across_midnight():
    r = run_job([purchase("e1", "ann", "2026-03-01T23:50:00Z", "5"),
                 purchase("e2", "ann", "2026-03-02T00:20:00Z", "7")])
    assert len(r.sessions) == 1
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "12.00",
                                "top_user": "ann"}]


def test_session_date_uses_utc_not_local_offset():
    r = run_job([purchase("e1", "ann", "2026-03-01T23:30:00-05:00", "4")])  # 2026-03-02 04:30Z
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-02"]


def test_next_day_session_after_midnight_split_counts_on_new_day():
    r = run_job([purchase("e1", "ann", "2026-03-01T23:50:00Z", "5"),
                 purchase("e2", "ann", "2026-03-02T01:00:00Z", "7")])
    assert pick(r.sessions, "session_id", "start") == [("ann-1", "2026-03-01T23:50:00Z"),
                                                       ("ann-2", "2026-03-02T01:00:00Z")]
    assert [(d["date"], d["sessions"], d["revenue"]) for d in r.daily["days"]] == [
        ("2026-03-01", 1, "5.00"), ("2026-03-02", 1, "7.00")]


def test_day_counts_sessions_and_distinct_users():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "12:00:00"), ev("e3", "bob", "09:00:00")])
    assert r.daily["days"] == [{"date": DAY, "sessions": 3, "users": 2, "revenue": "0.00", "top_user": None}]


def test_day_revenue_is_exact_sum_of_session_revenue():
    r = run_job([purchase("e1", "ann", "09:00:00", "0.10"), purchase("e2", "bob", "09:00:00", "0.20"),
                 purchase("e3", "cy", "09:00:00", "0.30")])
    assert r.daily["days"][0]["revenue"] == "0.60"


def test_top_user_is_highest_revenue_summed_over_day():
    r = run_job([purchase("e1", "ann", "09:00:00", "5"), purchase("e2", "ann", "12:00:00", "5"),
                 purchase("e3", "bob", "09:00:00", "8")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3 and d["users"] == 2
    assert d["revenue"] == "18.00" and d["top_user"] == "ann"


def test_top_user_tie_goes_to_name_sorting_first():
    r = run_job([purchase("e1", "zoe", "09:00:00", "10"), purchase("e2", "amy", "09:00:00", "10")])
    assert r.daily["days"][0]["top_user"] == "amy"


def test_top_user_is_null_when_day_revenue_zero():
    r = run_job([ev("e1", "ann", "09:00:00"), purchase("e2", "bob", "09:00:00", "0")])
    d = r.daily["days"][0]
    assert d["revenue"] == "0.00" and d["top_user"] is None


def test_top_user_ignores_users_with_zero_revenue():
    r = run_job([ev("e1", "aaa", "09:00:00"), purchase("e2", "bob", "09:00:00", "0.01")])
    assert r.daily["days"][0]["top_user"] == "bob"


def test_top_user_is_per_day():
    r = run_job([purchase("e1", "ann", "2026-03-01T09:00:00Z", "9"),
                 purchase("e2", "bob", "2026-03-01T10:00:00Z", "1"),
                 purchase("e3", "bob", "2026-03-02T09:00:00Z", "4"),
                 purchase("e4", "ann", "2026-03-02T10:00:00Z", "2")])
    assert [(d["date"], d["top_user"]) for d in r.daily["days"]] == [("2026-03-01", "ann"), ("2026-03-02", "bob")]


def test_daily_revenue_string_has_two_decimals():
    r = run_job([purchase("e1", "ann", "09:00:00", "3")])
    assert r.daily["days"][0]["revenue"] == "3.00"
    assert isinstance(r.daily["days"][0]["revenue"], str)


def test_daily_summary_counts_duplicates_and_rejects_separately():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), "{", ev("e2", "ann", "09:00:10")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 1


def test_days_have_expected_types():
    r = run_job([purchase("e1", "ann", "09:00:00", "1.5")])
    d = r.daily["days"][0]
    assert isinstance(d["sessions"], int) and isinstance(d["users"], int)
    assert d["date"] == DAY and d["top_user"] == "ann"


def test_empty_day_list_when_all_rejected():
    r = run_job(["{", "[]"])
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 2}
    assert r.sessions == []
