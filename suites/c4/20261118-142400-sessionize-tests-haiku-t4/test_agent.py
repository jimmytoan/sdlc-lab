"""Regression suite for the sessionize batch job (SPEC.md).

Everything is driven as a black box through the CLI: `python job.py --in DIR --out DIR`.
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from decimal import Decimal
from types import SimpleNamespace

import pytest

from helpers import APP_DIR, ev

JOB = os.path.join(APP_DIR, "job.py")
HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DAY_KEYS = {"date", "sessions", "users", "revenue", "top_user"}
TOP_KEYS = {"days", "duplicates_dropped", "rejected"}
DAY = "2026-03-01"


# ---------------------------------------------------------------- helpers

def content_of(lines):
    """Lines -> file content, one line per entry, with a trailing newline."""
    return "".join(line + "\n" for line in lines)


def run_raw(content, *extra_args, out_parts=("out",)):
    """Write `content` as events.jsonl (None = no file), run the job, collect every output."""
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        if content is not None:
            with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8", newline="") as f:
                f.write(content)
        out = os.path.join(d, *out_parts)
        p = subprocess.run([sys.executable, JOB, "--in", inp, "--out", out, *extra_args],
                           capture_output=True, text=True, timeout=30, cwd=d)
        return collect(p, out)


def collect(p, out):
    files = sorted(os.listdir(out)) if os.path.isdir(out) else []
    res = SimpleNamespace(code=p.returncode, stdout=p.stdout, stderr=p.stderr, files=files,
                          csv_text=None, header=None, sessions=[], daily=None, daily_text=None,
                          rejects=None, rejects_text=None)
    if "sessions.csv" in files:
        with open(os.path.join(out, "sessions.csv"), newline="", encoding="utf-8") as f:
            res.csv_text = f.read()
        rows = list(csv.reader(io.StringIO(res.csv_text)))
        res.header = rows[0] if rows else None
        res.sessions = [dict(zip(res.header, r)) for r in rows[1:]] if rows else []
    if "daily.json" in files:
        with open(os.path.join(out, "daily.json"), encoding="utf-8") as f:
            res.daily_text = f.read()
        res.daily = json.loads(res.daily_text)
    if "rejects.jsonl" in files:
        with open(os.path.join(out, "rejects.jsonl"), encoding="utf-8") as f:
            res.rejects_text = f.read()
        res.rejects = [json.loads(x) for x in res.rejects_text.split("\n") if x.strip()]
    return res


def run_lines(lines):
    return run_raw(content_of(lines))


def sessions_of(lines):
    r = run_lines(lines)
    assert r.code == 0, r.stderr
    return r.sessions


def ids(sessions):
    return [s["session_id"] for s in sessions]


def reasons_for(lines):
    r = run_lines(lines)
    assert r.code == 0, r.stderr
    return [(x["line"], x["reason"]) for x in r.rejects]


def only_reject(line):
    """Feed one line alongside a valid anchor event and return the single reject reason."""
    r = run_lines([line])
    assert r.code == 0, r.stderr
    assert len(r.rejects) == 1, r.rejects_text
    assert r.rejects[0]["line"] == 1
    return r.rejects[0]["reason"]


def purchase(eid, user, ts, amount):
    return ev(eid, user, ts, "purchase", amount=amount)


# ---------------------------------------------------------------- outputs and CLI contract

def test_all_three_files_written_for_normal_input():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_all_three_files_written_for_empty_input():
    r = run_raw("")
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.rejects == []


def test_all_three_files_written_when_everything_rejected():
    r = run_lines(["{nope", ev("e1", "ann", "2026-03-01T09:00:00")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.daily["days"] == []


def test_empty_input_produces_header_only_csv_and_empty_summary():
    r = run_raw("")
    assert r.csv_text == ",".join(HEADER) + "\n"
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    assert r.rejects_text == ""


def test_whitespace_only_input_is_treated_as_empty():
    r = run_raw("\n   \n\t\n")
    assert r.code == 0
    assert r.sessions == []
    assert r.rejects == []


def test_header_is_exact():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert r.csv_text.split("\n")[0] == "session_id,user,start,end,duration_s,events,purchases,revenue"


def test_output_directory_is_created_including_parents():
    r = run_raw(content_of([ev("e1", "ann", "09:00:00")]), out_parts=("a", "b", "c"))
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_existing_output_directory_is_overwritten():
    with tempfile.TemporaryDirectory() as d:
        inp, out = os.path.join(d, "in"), os.path.join(d, "out")
        os.makedirs(inp)
        os.makedirs(out)
        with open(os.path.join(out, "stale.txt"), "w") as f:
            f.write("old")
        with open(os.path.join(out, "sessions.csv"), "w") as f:
            f.write("stale garbage\n")
        with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write(content_of([ev("e1", "ann", "09:00:00")]))
        p = subprocess.run([sys.executable, JOB, "--in", inp, "--out", out],
                           capture_output=True, text=True, timeout=30, cwd=d)
        assert p.returncode == 0, p.stderr
        with open(os.path.join(out, "sessions.csv"), encoding="utf-8") as f:
            assert f.read().startswith("session_id,")
        with open(os.path.join(out, "daily.json"), encoding="utf-8") as f:
            assert json.load(f)["days"][0]["sessions"] == 1


def test_output_is_deterministic():
    lines = [ev("e1", "bob", "10:00:00", "purchase", amount="3.5"), ev("e2", "ann", "09:00:00"),
             "{bad", ev("e1", "bob", "10:00:00", "purchase", amount="3.5")]
    a, b = run_raw(content_of(lines)), run_raw(content_of(lines))
    assert a.csv_text == b.csv_text
    assert a.daily_text == b.daily_text
    assert a.rejects_text == b.rejects_text


def test_missing_input_file_exits_2_with_stderr_message():
    r = run_raw(None)
    assert r.code == 2
    assert r.stderr.strip() != ""
    assert "Traceback" not in r.stderr


def test_exit_zero_even_when_lines_are_rejected():
    r = run_lines(["{", "[]", ev("e1", "ann", "2026-03-01T09:00:00Z")])
    assert r.code == 0
    assert len(r.rejects) == 2


def test_file_without_trailing_newline_is_read_fully():
    r = run_raw(ev("e1", "ann", "09:00:00") + "\n" + ev("e2", "ann", "09:05:00"))
    assert r.code == 0
    assert r.sessions[0]["events"] == "2"


def test_crlf_line_endings_are_accepted():
    r = run_raw(content_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:05:00")]).replace("\n", "\r\n"))
    assert r.code == 0
    assert r.sessions[0]["events"] == "2"
    assert r.rejects == []


def test_utf8_input_with_non_ascii_names_and_ids():
    r = run_lines([ev("é-1", "Éva", "09:00:00", "purchase", amount="2.50")])
    assert r.code == 0
    assert r.sessions[0]["user"] == "éva"
    assert r.sessions[0]["session_id"] == "éva-1"
    assert r.sessions[0]["revenue"] == "2.50"


# ---------------------------------------------------------------- sessions.csv format

def test_full_session_row_values():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:05:00", "click"),
                   ev("e3", "ann", "09:10:30", "purchase", amount="19.99")])
    assert r.csv_text.split("\n")[1] == "ann-1,ann,2026-03-01T09:00:00Z,2026-03-01T09:10:30Z,630,3,1,19.99"


def test_csv_rows_have_no_quoting_for_plain_values():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert '"' not in r.csv_text


def test_timestamps_are_utc_z_format():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "bob", "2026-03-01T09:05:00-05:30")])
    assert [x["start"] for x in r.sessions] == ["2026-03-01T02:00:00Z", "2026-03-01T14:35:00Z"]
    assert [x["end"] for x in r.sessions] == ["2026-03-01T02:00:00Z", "2026-03-01T14:35:00Z"]
    for s in r.sessions:
        assert UTC_RE.match(s["start"]) and UTC_RE.match(s["end"])


def test_timezone_offset_is_applied_with_sign():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00-05:30")])
    assert r.sessions[0]["start"] == "2026-03-01T14:30:00Z"


def test_z_suffix_is_utc():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00Z")])
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"


def test_fractional_seconds_are_cut_from_output():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00.750Z"), ev("e2", "ann", "2026-03-01T09:00:01.999Z")])
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:00:01Z"


def test_fractional_seconds_with_offset_accepted():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00.250-05:30")])
    assert r.code == 0
    assert r.sessions[0]["start"] == "2026-03-01T14:30:00Z"
    assert r.rejects == []


def test_duration_is_whole_seconds_rounded_down():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:00:59.999Z")])
    assert r.sessions[0]["duration_s"] == "59"


def test_duration_of_single_event_session_is_zero():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert r.sessions[0]["duration_s"] == "0"
    assert r.sessions[0]["start"] == r.sessions[0]["end"]


def test_duration_uses_full_precision_difference():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "ann", "2026-03-01T09:00:01.100Z")])
    assert r.sessions[0]["duration_s"] == "0"


def test_events_and_purchases_counts():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:01:00", "click"),
                   purchase("e3", "ann", "09:02:00", "1"), purchase("e4", "ann", "09:03:00", "2")])
    s = r.sessions[0]
    assert s["events"] == "4"
    assert s["purchases"] == "2"
    assert s["revenue"] == "3.00"


def test_session_without_purchases_has_zero_revenue():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:01:00", "click")])
    assert r.sessions[0]["purchases"] == "0"
    assert r.sessions[0]["revenue"] == "0.00"


def test_revenue_is_exact_decimal():
    r = run_lines([purchase("e1", "ann", "09:00:00", "0.10"), purchase("e2", "ann", "09:01:00", "0.20")])
    assert r.sessions[0]["revenue"] == "0.30"


def test_revenue_has_no_float_drift_on_large_amounts():
    r = run_lines([purchase("e1", "ann", "09:00:00", "12345678901234567.99"),
                   purchase("e2", "ann", "09:01:00", "0.01")])
    assert r.sessions[0]["revenue"] == "12345678901234568.00"


def test_revenue_exact_beyond_float_precision():
    r = run_lines([purchase("e1", "ann", "09:00:00", "9007199254740993")])
    assert r.sessions[0]["revenue"] == "9007199254740993.00"
    assert r.daily["days"][0]["revenue"] == "9007199254740993.00"


@pytest.mark.parametrize("amount, expected", [
    ("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"), ("0", "0.00"), ("0.5", "0.50"), ("100.10", "100.10"),
])
def test_revenue_always_two_decimals(amount, expected):
    r = run_lines([purchase("e1", "ann", "09:00:00", amount)])
    assert r.sessions[0]["revenue"] == expected


def test_zero_amount_purchase_is_valid():
    r = run_lines([purchase("e1", "ann", "09:00:00", "0")])
    assert r.rejects == []
    assert r.sessions[0]["purchases"] == "1"
    assert r.sessions[0]["revenue"] == "0.00"


def test_csv_quotes_user_names_containing_commas():
    r = run_lines([ev("e1", "O,Neil", "09:00:00")])
    assert r.sessions[0]["user"] == "o,neil"
    assert r.sessions[0]["session_id"] == "o,neil-1"
    assert len(r.header) == 8


def test_rows_ordered_by_start_then_user():
    r = run_lines([ev("e1", "zed", "09:00:00"), ev("e2", "ann", "09:00:00"), ev("e3", "mia", "08:00:00")])
    assert [s["user"] for s in r.sessions] == ["mia", "ann", "zed"]


def test_rows_ordered_by_full_precision_start_not_truncated_start():
    # Both truncate to 09:00:00Z; full precision decides the order, so bob (…00.100) precedes ann (…00.900).
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "bob", "2026-03-01T09:00:00.100Z")])
    assert [s["user"] for s in r.sessions] == ["bob", "ann"]


def test_rows_ordered_across_users_by_start():
    r = run_lines([ev("e1", "ann", "10:00:00"), ev("e2", "bob", "09:00:00"), ev("e3", "ann", "08:00:00")])
    assert [s["session_id"] for s in r.sessions] == ["ann-1", "bob-1", "ann-2"]


# ---------------------------------------------------------------- sessionization

def test_gap_of_exactly_30_minutes_stays_in_session():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00")])
    assert len(s) == 1
    assert s[0]["events"] == "2"


def test_gap_over_30_minutes_starts_new_session():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01")])
    assert ids(s) == ["ann-1", "ann-2"]
    assert s[1]["start"] == "2026-03-01T09:30:01Z"


def test_gap_is_measured_to_previous_event_not_session_start():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:25:00"), ev("e3", "ann", "09:50:00")])
    assert len(s) == 1
    assert s[0]["duration_s"] == "3000"


def test_gap_exactly_30_minutes_with_fractions():
    s = sessions_of([ev("e1", "ann", "2026-03-01T09:00:00.500Z"), ev("e2", "ann", "2026-03-01T09:30:00.000Z")])
    assert len(s) == 1


def test_gap_just_over_30_minutes_with_fractions_splits():
    s = sessions_of([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:30:00.001Z")])
    assert len(s) == 2


def test_gap_is_computed_in_utc_across_offsets():
    # 09:00+07:00 == 02:00Z; 02:30Z is exactly 30 minutes later -> same session.
    same = sessions_of([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T02:30:00Z")])
    assert len(same) == 1
    split = sessions_of([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T02:31:00Z")])
    assert len(split) == 2


def test_gaps_are_per_user():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"), ev("e3", "ann", "09:20:00")])
    assert ids(s) == ["ann-1", "bob-1"]
    assert s[0]["events"] == "2"


def test_session_numbering_per_user_in_time_order():
    s = sessions_of([
        ev("e1", "ann", "12:00:00"), ev("e2", "bob", "08:00:00"), ev("e3", "ann", "08:00:00"),
        ev("e4", "ann", "10:00:00"), ev("e5", "bob", "09:00:00"),
    ])
    by_user = {}
    for x in s:
        by_user.setdefault(x["user"], []).append(x["session_id"])
    assert by_user == {"ann": ["ann-1", "ann-2", "ann-3"], "bob": ["bob-1", "bob-2"]}


def test_session_ids_follow_time_order_not_file_order():
    s = sessions_of([ev("e1", "ann", "12:00:00"), ev("e2", "ann", "08:00:00")])
    assert [(x["session_id"], x["start"]) for x in s] == [
        ("ann-1", "2026-03-01T08:00:00Z"), ("ann-2", "2026-03-01T12:00:00Z")]


def test_events_sorted_by_timestamp_before_sessionizing():
    s = sessions_of([ev("e1", "ann", "09:50:00"), ev("e2", "ann", "09:00:00"), ev("e3", "ann", "09:25:00")])
    assert len(s) == 1
    assert s[0]["start"] == "2026-03-01T09:00:00Z"
    assert s[0]["end"] == "2026-03-01T09:50:00Z"


def test_user_names_trimmed_and_lowercased_to_one_user():
    s = sessions_of([ev("e1", "  Ann ", "09:00:00"), ev("e2", "ann", "09:05:00"), ev("e3", "ANN", "09:10:00")])
    assert len(s) == 1
    assert s[0]["user"] == "ann"
    assert s[0]["session_id"] == "ann-1"
    assert s[0]["events"] == "3"


def test_output_user_is_lowercase_form():
    s = sessions_of([ev("e1", "BOB", "09:00:00")])
    assert s[0]["user"] == "bob"
    assert s[0]["session_id"] == "bob-1"


def test_users_with_different_names_are_not_merged():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "anna", "09:00:00")])
    assert sorted(x["user"] for x in s) == ["ann", "anna"]


def test_session_end_is_last_event_by_timestamp():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:20:00"), ev("e3", "ann", "09:10:00")])
    assert s[0]["end"] == "2026-03-01T09:20:00Z"


# ---------------------------------------------------------------- duplicates

def test_duplicate_keeps_earliest_timestamp_even_when_later_in_file():
    r = run_lines([purchase("e1", "ann", "09:05:00", "5"), purchase("e1", "ann", "09:00:00", "7")])
    assert r.sessions[0]["revenue"] == "7.00"
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_with_equal_timestamp_keeps_first_in_file():
    r = run_lines([purchase("e1", "bob", "09:00:00", "3"), purchase("e1", "ann", "09:00:00", "9")])
    assert [s["user"] for s in r.sessions] == ["bob"]
    assert r.sessions[0]["revenue"] == "3.00"


def test_kept_duplicate_determines_user():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e1", "bob", "08:00:00")])
    assert [s["user"] for s in r.sessions] == ["bob"]


def test_duplicates_dropped_counts_every_extra_copy():
    r = run_lines([ev("e1", "ann", "09:00:00")] * 3 + [ev("e2", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "2"


def test_duplicate_is_not_a_reject():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00")])
    assert r.rejects == []
    assert r.rejects_text == ""


def test_event_id_match_is_case_sensitive():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("E1", "ann", "09:00:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_invalid_copy_is_not_a_duplicate_and_is_rejected():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00", "dance")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 1
    assert r.rejects == [{"line": 2, "reason": "bad_type"}]
    assert r.sessions[0]["events"] == "1"


def test_duplicates_are_counted_only_among_valid_events():
    # Line 2 is rejected (bad_ts), so the valid e1 on line 3 is the only kept copy.
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00"), ev("e1", "ann", "09:01:00"),
                   ev("e1", "ann", "09:02:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]


def test_duplicate_that_is_dropped_does_not_extend_session():
    # Dropped copy at 12:00 must not count as a session event.
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "12:00:00")])
    assert len(s) == 1
    assert s[0]["events"] == "1"
    assert s[0]["end"] == "2026-03-01T09:00:00Z"


# ---------------------------------------------------------------- rejects: basics

def test_reject_line_numbers_are_physical_and_count_blank_lines():
    r = run_raw("\n" + ev("e1", "ann", "09:00:00") + "\n   \n{bad\n\n" + ev("e2", "ann", "09:01:00") + "\n[]\n")
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 7, "reason": "bad_json"}]


def test_blank_lines_are_skipped_not_rejected():
    r = run_raw("\n\n" + ev("e1", "ann", "09:00:00") + "\n\t\n")
    assert r.rejects == []
    assert r.daily["rejected"] == 0


def test_rejects_are_listed_in_input_order():
    r = run_lines(["{x", ev("e1", "ann", "2026-03-01T09:00:00"), "[]", ev("e2", "ann", "09:00:00", "dance")])
    assert [x["line"] for x in r.rejects] == [1, 2, 3, 4]
    assert [x["reason"] for x in r.rejects] == ["bad_json", "bad_ts", "bad_json", "bad_type"]


def test_reject_objects_have_exactly_line_and_reason_keys():
    r = run_lines(["{x"])
    assert r.rejects_text == '{"line": 1, "reason": "bad_json"}\n'


def test_rejected_lines_take_no_further_part_in_sessions():
    # The rejected middle line must not split or extend ann's session.
    s = sessions_of([ev("e1", "ann", "09:00:00"), "{broken", ev("e2", "ann", "09:20:00")])
    assert len(s) == 1
    assert s[0]["events"] == "2"


def test_rejected_event_does_not_create_user_session():
    s = sessions_of([ev("e1", "ghost", "2026-03-01T09:00:00"), ev("e2", "ann", "09:00:00")])
    assert [x["user"] for x in s] == ["ann"]


def test_rejected_line_does_not_count_as_duplicate_source_for_later_valid_copy():
    r = run_lines([ev("e1", "ann", "2026-03-01T09:00:00"), ev("e1", "bob", "10:00:00")])
    assert [x["user"] for x in r.sessions] == ["bob"]
    assert r.daily["duplicates_dropped"] == 0


def test_daily_rejected_equals_number_of_rejects():
    r = run_lines(["{", "[]", "null", ev("e1", "ann", "09:00:00", "dance")])
    assert r.daily["rejected"] == 4 == len(r.rejects)


def test_clean_input_has_empty_rejects_file():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert r.rejects_text == ""
    assert r.daily["rejected"] == 0


# ---------------------------------------------------------------- rejects: bad_json

@pytest.mark.parametrize("line", [
    "{", "{not json}", "not json at all", '{"event_id": "e1"', "[]", "[1, 2]", '"text"', "42", "null",
    "true", '["event_id", "e1"]',
])
def test_bad_json_or_non_object(line):
    assert only_reject(line) == "bad_json"


# ---------------------------------------------------------------- rejects: missing_field

@pytest.mark.parametrize("obj", [
    {},
    {"user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "ann", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z"},
    {"event_id": None, "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": 5, "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": ["ann"], "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": 1772355600, "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": None},
    {"event_id": "   ", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": " \t ", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": ""},
])
def test_missing_or_non_string_or_empty_fields(obj):
    assert only_reject(json.dumps(obj)) == "missing_field"


# ---------------------------------------------------------------- rejects: bad_ts

@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",              # no offset
    "2026-03-01T09:00",                 # no seconds
    "2026-03-01T09:00Z",                # no seconds, with Z
    "2026-03-01 09:00:00Z",             # space instead of T
    "2026-03-01",                       # date only
    "09:00:00Z",                        # time only
    "yesterday",
    "2026-02-30T09:00:00Z",             # impossible date
    "2026-13-01T09:00:00Z",             # impossible month
    "2026-03-01T24:00:00Z",             # impossible hour
    "2026-03-01T09:60:00Z",             # impossible minute
    "2026-03-01T09:00:00+25:00",        # impossible offset
    "2026-03-01T09:00:00+07",           # offset without minutes
    "2026-03-01T09:00:00.Z",            # dot without digits
])
def test_bad_timestamps(ts):
    assert only_reject(json.dumps({"event_id": "e1", "user": "ann", "ts": ts, "type": "view"})) == "bad_ts"


# ---------------------------------------------------------------- rejects: bad_type

@pytest.mark.parametrize("typ", ["dance", "Purchase", "VIEW", "refund", "logout", "page_view", "buy"])
def test_bad_types(typ):
    assert only_reject(ev("e1", "ann", "09:00:00", typ)) == "bad_type"


def test_allowed_types_accepted():
    r = run_lines([ev("e1", "ann", "09:00:00", "view"), ev("e2", "ann", "09:01:00", "click"),
                   purchase("e3", "ann", "09:02:00", "1")])
    assert r.rejects == []
    assert r.sessions[0]["events"] == "3"


# ---------------------------------------------------------------- rejects: bad_amount

@pytest.mark.parametrize("amount", [
    None, 5, 5.5, ["5"], "", "-1", "-0.01", "1.234", "1.", ".5", "abc", "1e3", "1,00", "$5", "5.00.1",
    " ", "NaN", "Infinity",
])
def test_bad_purchase_amounts(amount):
    line = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase", "amount": amount}
    assert only_reject(json.dumps(line)) == "bad_amount"


def test_purchase_without_amount_key_is_bad_amount():
    assert only_reject(ev("e1", "ann", "09:00:00", "purchase")) == "bad_amount"


@pytest.mark.parametrize("amount", ["5", "5.5", "19.99", "0", "0.01", "123456789"])
def test_valid_purchase_amounts(amount):
    r = run_lines([purchase("e1", "ann", "09:00:00", amount)])
    assert r.rejects == []


def test_amount_is_ignored_for_non_purchase_events():
    r = run_lines([ev("e1", "ann", "09:00:00", "view", amount="abc"),
                   ev("e2", "ann", "09:01:00", "click", amount=5),
                   ev("e3", "ann", "09:02:00", "view", amount="-7")])
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00"


# ---------------------------------------------------------------- rejects: precedence

@pytest.mark.parametrize("obj, expected", [
    # missing_field beats bad_ts
    ({"event_id": "e1", "ts": "bogus", "type": "view"}, "missing_field"),
    # missing_field beats bad_type
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": 7}, "missing_field"),
    # bad_ts beats bad_type
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00", "type": "dance"}, "bad_ts"),
    # bad_ts beats bad_amount
    ({"event_id": "e1", "user": "ann", "ts": "nope", "type": "purchase", "amount": "x"}, "bad_ts"),
    # bad_type beats bad_amount
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "dance", "amount": "x"}, "bad_type"),
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "purchase"}, "bad_amount"),
])
def test_first_failing_check_is_reported(obj, expected):
    assert only_reject(json.dumps(obj)) == expected


def test_each_rejected_line_gets_one_reason():
    r = run_lines([json.dumps({"event_id": "", "user": "", "ts": "bad", "type": "x"})])
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


# ---------------------------------------------------------------- daily.json

def test_daily_top_level_keys_exact():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert set(r.daily) == TOP_KEYS


def test_day_entry_keys_exact_and_types():
    r = run_lines([purchase("e1", "ann", "09:00:00", "5")])
    day = r.daily["days"][0]
    assert set(day) == DAY_KEYS
    assert isinstance(day["sessions"], int)
    assert isinstance(day["users"], int)
    assert isinstance(day["revenue"], str)
    assert day["revenue"] == "5.00"


def test_days_list_ascending_and_only_days_with_sessions():
    r = run_lines([ev("e1", "ann", "2026-03-05T09:00:00Z"), ev("e2", "ann", "2026-03-01T09:00:00Z"),
                   ev("e3", "bob", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_day_with_sessions_and_zero_revenue():
    r = run_lines([ev("e1", "ann", "09:00:00")])
    assert r.daily["days"] == [{"date": DAY, "sessions": 1, "users": 1, "revenue": "0.00", "top_user": None}]


def test_day_counts_sessions_and_distinct_users():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "12:00:00"), ev("e3", "bob", "09:00:00")])
    day = r.daily["days"][0]
    assert day["sessions"] == 3
    assert day["users"] == 2


def test_day_revenue_sums_session_revenue():
    r = run_lines([purchase("e1", "ann", "09:00:00", "5.00"), purchase("e2", "bob", "09:00:00", "19.99")])
    assert r.daily["days"][0]["revenue"] == "24.99"


def test_day_revenue_exact_decimal():
    r = run_lines([purchase("e1", "ann", "09:00:00", "0.10"), purchase("e2", "bob", "09:00:00", "0.20")])
    assert r.daily["days"][0]["revenue"] == "0.30"


def test_session_belongs_to_utc_date_of_start_even_past_midnight():
    r = run_lines([purchase("e1", "ann", "2026-03-01T23:50:00Z", "4"),
                   purchase("e2", "ann", "2026-03-02T00:10:00Z", "6")])
    assert len(r.sessions) == 1
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]
    assert r.daily["days"][0]["sessions"] == 1
    assert r.daily["days"][0]["revenue"] == "10.00"


def test_session_past_midnight_does_not_create_next_day_entry():
    r = run_lines([ev("e1", "ann", "2026-03-01T23:50:00Z"), ev("e2", "ann", "2026-03-02T00:10:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]


def test_date_uses_utc_not_local_offset():
    # 06:30+07:00 is 23:30Z on the previous day.
    r = run_lines([ev("e1", "ann", "2026-03-01T06:30:00+07:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-02-28"]
    assert r.sessions[0]["start"] == "2026-02-28T23:30:00Z"


def test_second_session_after_midnight_starts_new_day_entry():
    r = run_lines([ev("e1", "ann", "2026-03-01T23:00:00Z"), ev("e2", "ann", "2026-03-02T01:00:00Z")])
    assert [(d["date"], d["sessions"]) for d in r.daily["days"]] == [("2026-03-01", 1), ("2026-03-02", 1)]


def test_top_user_is_highest_revenue_user():
    r = run_lines([purchase("e1", "ann", "09:00:00", "5.00"), purchase("e2", "bob", "09:00:00", "20")])
    assert r.daily["days"][0]["top_user"] == "bob"


def test_top_user_ties_broken_by_name_ascending():
    r = run_lines([purchase("e1", "zed", "09:00:00", "10"), purchase("e2", "ann", "09:00:00", "10")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_top_user_revenue_aggregated_across_sessions_of_the_day():
    r = run_lines([
        purchase("e1", "ann", "09:00:00", "6"), purchase("e2", "ann", "12:00:00", "6"),
        purchase("e3", "bob", "10:00:00", "10"),
    ])
    assert r.daily["days"][0]["top_user"] == "ann"
    assert r.daily["days"][0]["revenue"] == "22.00"


def test_top_user_ignores_users_with_no_revenue():
    r = run_lines([ev("e1", "aaa", "09:00:00"), purchase("e2", "zed", "09:00:00", "0.01")])
    assert r.daily["days"][0]["top_user"] == "zed"


def test_top_user_null_when_day_revenue_is_zero_with_purchases():
    r = run_lines([purchase("e1", "ann", "09:00:00", "0"), purchase("e2", "bob", "09:00:00", "0.00")])
    assert r.daily["days"][0]["top_user"] is None
    assert r.daily["days"][0]["revenue"] == "0.00"


def test_top_user_is_per_day():
    r = run_lines([purchase("e1", "ann", "09:00:00", "5"), purchase("e2", "bob", "2026-03-02T09:00:00Z", "7")])
    assert [(d["date"], d["top_user"]) for d in r.daily["days"]] == [("2026-03-01", "ann"), ("2026-03-02", "bob")]


def test_daily_duplicates_dropped_reported():
    r = run_lines([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00")])
    assert r.daily["duplicates_dropped"] == 2


def test_daily_summary_is_empty_for_empty_input():
    r = run_raw("")
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_daily_revenue_matches_sessions_csv_total():
    r = run_lines([purchase("e1", "ann", "09:00:00", "1.25"), purchase("e2", "bob", "09:00:00", "2.50"),
                   purchase("e3", "ann", "15:00:00", "3.10")])
    csv_total = sum(Decimal(s["revenue"]) for s in r.sessions)
    day_total = sum(Decimal(d["revenue"]) for d in r.daily["days"])
    assert csv_total == day_total == Decimal("6.85")


# ---------------------------------------------------------------- public-facing spec examples (extra)

def test_spec_example_event_line_parses():
    line = '{"event_id": "e1", "user": "Ann", "ts": "2026-03-01T09:00:00+07:00", "type": "purchase", "amount": "19.99"}'
    r = run_raw(line + "\n")
    assert r.rejects == []
    assert r.sessions[0] == {"session_id": "ann-1", "user": "ann", "start": "2026-03-01T02:00:00Z",
                             "end": "2026-03-01T02:00:00Z", "duration_s": "0", "events": "1",
                             "purchases": "1", "revenue": "19.99"}


def test_spec_style_daily_example_shape():
    r = run_lines([purchase("e1", "ann", "09:00:00", "5"), ev("e2", "ann", "09:00:00", "view"),
                   ev("e3", "bob", "10:00:00"), "{bad"])
    assert r.daily["rejected"] == 1
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["days"] == [{"date": DAY, "sessions": 2, "users": 2, "revenue": "5.00", "top_user": "ann"}]
