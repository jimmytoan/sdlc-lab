"""Black-box test suite for the sessionize batch job, written from SPEC.md.

The job is driven only through its command line; outputs are read from disk.
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

from helpers import APP_DIR, ev, pick, run_job

HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DAY = "2026-03-01"


# ---------------------------------------------------------------- helpers

def full(ts_text, day="2026-03-01"):
    """Return a full ISO timestamp for a bare 'HH:MM:SS[.fff]' time (UTC)."""
    return f"{day}T{ts_text}Z"


def E(event_id, user, ts, type="view", **extra):
    """Event line with a full timestamp already formatted by the caller."""
    return json.dumps({"event_id": event_id, "user": user, "ts": ts, "type": type, **extra})


def P(event_id, user, ts, amount, **extra):
    """Purchase line with a full timestamp."""
    return E(event_id, user, ts, "purchase", amount=amount, **extra)


def run_cli(input_text, out_rel="out", create_input=True):
    """Run job.py on the given raw events.jsonl bytes-as-text. Returns (proc, out_dir, tmpdir)."""
    tmp = tempfile.mkdtemp()
    inp = os.path.join(tmp, "in")
    os.makedirs(inp)
    if create_input:
        with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8", newline="") as f:
            f.write(input_text)
    out = os.path.join(tmp, out_rel)
    p = subprocess.run([sys.executable, os.path.join(APP_DIR, "job.py"), "--in", inp, "--out", out],
                       capture_output=True, text=True, timeout=30, cwd=APP_DIR)
    return p, out


def read_sessions(out):
    with open(os.path.join(out, "sessions.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return rows


def read_rejects(out):
    with open(os.path.join(out, "rejects.jsonl"), encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().split("\n") if x.strip()]


def read_daily(out):
    with open(os.path.join(out, "daily.json"), encoding="utf-8") as f:
        return json.load(f)


def one_session(lines):
    """Run with lines and return the single session row (asserting there is exactly one)."""
    r = run_job(lines)
    assert r.code == 0
    assert len(r.sessions) == 1, r.sessions
    return r.sessions[0]


def sessions_of(lines):
    r = run_job(lines)
    assert r.code == 0
    return r.sessions


def day(r, date):
    matches = [d for d in r.daily["days"] if d["date"] == date]
    assert len(matches) == 1, f"no unique day {date}: {r.daily}"
    return matches[0]


# ---------------------------------------------------------------- CLI and files

def test_exit_zero_and_three_output_files_written():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_missing_input_file_exits_2_with_stderr_message():
    p, out = run_cli("", create_input=False)
    assert p.returncode == 2
    assert p.stderr.strip() != ""


def test_output_dir_is_created_including_parents():
    p, out = run_cli(E("e1", "ann", full("09:00:00")) + "\n", out_rel=os.path.join("a", "b", "out"))
    assert p.returncode == 0, p.stderr
    assert sorted(os.listdir(out)) == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_empty_input_file_still_writes_all_three_files():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_whitespace_only_lines_are_skipped_not_rejected():
    r = run_job(["", "   ", "\t", ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.rejects == []
    assert r.daily["rejected"] == 0
    assert len(r.sessions) == 1


def test_all_lines_rejected_still_writes_all_files():
    r = run_job(["{", "[]", ev("e1", "ann", "09:00:00", "dance")])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == []
    assert r.daily["days"] == []
    assert r.daily["rejected"] == 3


def test_run_is_deterministic():
    lines = [ev("e1", "bob", "10:00:00"), ev("e2", "ann", "09:00:00", "purchase", amount="1.5"),
             ev("e3", "ann", "09:50:00"), "{bad"]
    a, b = run_job(lines), run_job(lines)
    assert (a.header, a.sessions, a.daily, a.rejects) == (b.header, b.sessions, b.daily, b.rejects)


def test_sessions_csv_header_exact():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.header == HEADER


def test_sessions_csv_has_header_only_when_no_sessions():
    r = run_job(["{bad"])
    assert r.code == 0
    assert r.header == HEADER
    assert r.sessions == []


def test_rejects_file_is_jsonl_with_only_line_and_reason_keys():
    r = run_job([ev("e1", "ann", "09:00:00"), "{bad", ev("e3", "ann", "09:01:00", "dance")])
    assert r.rejects == [{"line": 2, "reason": "bad_json"}, {"line": 3, "reason": "bad_type"}]
    for item in r.rejects:
        assert set(item) == {"line", "reason"}


def test_rejects_file_empty_when_nothing_rejected():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.rejects == []


def test_events_jsonl_read_as_utf8():
    p, out = run_cli(E("e1", "Zoë", full("09:00:00")) + "\n")
    assert p.returncode == 0, p.stderr
    rows = read_sessions(out)
    assert rows[1][:2] == ["zoë-1", "zoë"]


# ---------------------------------------------------------------- input parsing

def test_line_numbers_count_skipped_blank_lines():
    r = run_job(["", ev("e1", "ann", "09:00:00"), "   ", "{bad"])
    assert r.rejects == [{"line": 4, "reason": "bad_json"}]


def test_rejects_listed_in_input_order_with_physical_line_numbers():
    r = run_job(["{a", ev("e1", "ann", "09:00:00"), ev("e2", "", "09:00:00"), "", "[]"])
    assert r.rejects == [{"line": 1, "reason": "bad_json"},
                         {"line": 3, "reason": "missing_field"},
                         {"line": 5, "reason": "bad_json"}]


@pytest.mark.parametrize("line", [
    "{not json",
    "[1, 2]",
    '"just a string"',
    "42",
    "null",
    "true",
    '{"event_id": }',
    '{"event_id": "e1",}',
])
def test_bad_json_or_non_object_rejected(line):
    r = run_job([line])
    assert r.code == 0
    assert r.rejects == [{"line": 1, "reason": "bad_json"}]


@pytest.mark.parametrize("drop", ["event_id", "user", "ts", "type"])
def test_missing_required_field_rejected(drop):
    obj = {"event_id": "e1", "user": "ann", "ts": full("09:00:00"), "type": "view"}
    del obj[drop]
    r = run_job([json.dumps(obj)])
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


@pytest.mark.parametrize("field,value", [
    ("event_id", None), ("event_id", 7), ("event_id", ["e1"]), ("event_id", ""), ("event_id", "   "),
    ("user", None), ("user", 3), ("user", ""), ("user", "  \t "),
    ("ts", None), ("ts", 1772355600), ("type", None), ("type", 1), ("type", ""),
])
def test_non_string_or_blank_required_field_is_missing_field(field, value):
    obj = {"event_id": "e1", "user": "ann", "ts": full("09:00:00"), "type": "view"}
    obj[field] = value
    r = run_job([json.dumps(obj)])
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",              # no offset
    "2026-03-01T09:00:00.250",          # no offset with fraction
    "2026-03-01",                       # date only
    "2026-03-01T09:00Z",                # seconds missing
    "2026-02-30T09:00:00Z",             # impossible date
    "2026-13-01T09:00:00Z",             # impossible month
    "2026-03-01T25:00:00Z",             # impossible hour
    "2026-03-01T09:00:00+25:00",        # impossible offset
    "yesterday",
    "09:00:00Z",                        # no date
])
def test_bad_timestamp_rejected(ts):
    r = run_job([E("e1", "ann", ts)])
    assert r.code == 0
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]
    assert r.sessions == []


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00Z",
    "2026-03-01T09:00:00+07:00",
    "2026-03-01T09:00:00-05:30",
    "2026-03-01T09:00:00.250-05:30",
    "2026-03-01T09:00:00.1Z",
    "2026-03-01T09:00:00.123456Z",
    "2026-03-01T09:00:00+00:00",
])
def test_valid_timestamp_forms_accepted(ts):
    r = run_job([E("e1", "ann", ts)])
    assert r.code == 0
    assert r.rejects == []
    assert len(r.sessions) == 1


@pytest.mark.parametrize("typ", ["VIEW", "Purchase", "scroll", "view "])
def test_bad_type_rejected(typ):
    r = run_job([E("e1", "ann", full("09:00:00"), typ)])
    assert r.rejects == [{"line": 1, "reason": "bad_type"}]


@pytest.mark.parametrize("typ", ["view", "click", "purchase"])
def test_allowed_types_accepted(typ):
    extra = {"amount": "1"} if typ == "purchase" else {}
    r = run_job([E("e1", "ann", full("09:00:00"), typ, **extra)])
    assert r.rejects == []
    assert r.sessions[0]["events"] == "1"


@pytest.mark.parametrize("amount", ["-1", "-0.01", "1.234", "abc", "", "1,50", "1e3", "1.2.3", "$5"])
def test_invalid_purchase_amount_rejected(amount):
    r = run_job([P("e1", "ann", full("09:00:00"), amount)])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]


@pytest.mark.parametrize("amount", [None, 5, 19.99, ["5"], True])
def test_non_string_purchase_amount_rejected(amount):
    r = run_job([json.dumps({"event_id": "e1", "user": "ann", "ts": full("09:00:00"),
                             "type": "purchase", "amount": amount})])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]


def test_purchase_without_amount_field_rejected():
    r = run_job([E("e1", "ann", full("09:00:00"), "purchase")])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]


@pytest.mark.parametrize("amount", ["0", "0.00", "5", "5.5", "19.99", "100.10"])
def test_valid_purchase_amounts_accepted(amount):
    r = run_job([P("e1", "ann", full("09:00:00"), amount)])
    assert r.rejects == []
    assert r.sessions[0]["purchases"] == "1"


@pytest.mark.parametrize("typ", ["view", "click"])
def test_amount_ignored_for_non_purchase(typ):
    # Even an invalid amount is ignored for non-purchase events.
    r = run_job([E("e1", "ann", full("09:00:00"), typ, amount="garbage")])
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00"


# ---------------------------------------------------------------- rejection ordering

@pytest.mark.parametrize("line,reason", [
    # bad_json beats everything
    ("{bad", "bad_json"),
    # missing_field beats bad_ts, bad_type, bad_amount
    (json.dumps({"event_id": "", "user": "ann", "ts": "nope", "type": "dance"}), "missing_field"),
    (json.dumps({"event_id": "e1", "user": "ann", "type": "dance"}), "missing_field"),
    # bad_ts beats bad_type and bad_amount
    (json.dumps({"event_id": "e1", "user": "ann", "ts": "nope", "type": "dance"}), "bad_ts"),
    (json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00", "type": "purchase"}), "bad_ts"),
    # bad_type beats bad_amount
    (json.dumps({"event_id": "e1", "user": "ann", "ts": full("09:00:00"), "type": "dance",
                 "amount": "x"}), "bad_type"),
    (json.dumps({"event_id": "e1", "user": "ann", "ts": full("09:00:00"), "type": "refund"}), "bad_type"),
    # bad_amount is the last check
    (json.dumps({"event_id": "e1", "user": "ann", "ts": full("09:00:00"), "type": "purchase",
                 "amount": "x"}), "bad_amount"),
])
def test_first_failing_check_is_reported(line, reason):
    r = run_job([line])
    assert r.rejects == [{"line": 1, "reason": reason}]


def test_rejected_event_does_not_affect_session_gap():
    # The rejected line at 09:20 must not count as the user's previous event:
    # 09:00 -> 09:50 is a 50 minute gap, so two sessions.
    r = run_job([ev("e1", "ann", "09:00:00"),
                 E("e2", "ann", full("09:20:00"), "dance"),
                 ev("e3", "ann", "09:50:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "1"), ("ann-2", "1")]
    assert r.daily["rejected"] == 1


def test_rejected_event_does_not_extend_session_end():
    r = run_job([ev("e1", "ann", "09:00:00"),
                 E("e2", "ann", "2026-03-01T09:05:00", "view")])  # no offset: bad_ts
    assert r.rejects == [{"line": 2, "reason": "bad_ts"}]
    s = r.sessions[0]
    assert s["end"] == "2026-03-01T09:00:00Z" and s["events"] == "1"


def test_rejected_event_does_not_count_as_duplicate():
    r = run_job([E("e1", "ann", "bogus-time"),
                 ev("e1", "ann", "09:00:00")])
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "1"


# ---------------------------------------------------------------- duplicates

def test_duplicate_with_later_timestamp_is_dropped_regardless_of_order_in_file():
    r = run_job([P("e1", "ann", full("09:10:00"), "9.00"),
                 P("e1", "ann", full("09:00:00"), "7.00")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["revenue"] == "7.00"
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"


def test_duplicate_tie_keeps_first_in_file():
    r = run_job([P("e1", "ann", full("09:00:00"), "1.00"),
                 P("e1", "ann", full("09:00:00"), "2.00")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["revenue"] == "1.00"


def test_duplicate_tie_compares_instants_not_text():
    # 09:00+07:00 is the same instant as 02:00Z; the first in file wins.
    r = run_job([P("e1", "ann", "2026-03-01T09:00:00+07:00", "1.00"),
                 P("e1", "ann", "2026-03-01T02:00:00Z", "2.00")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["revenue"] == "1.00"


def test_duplicate_earlier_instant_with_offset_wins():
    # 10:00+07:00 == 03:00Z, which is earlier than 04:00Z.
    r = run_job([P("e1", "ann", "2026-03-01T04:00:00Z", "1.00"),
                 P("e1", "ann", "2026-03-01T10:00:00+07:00", "2.00")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["revenue"] == "2.00"


def test_three_copies_count_two_dropped():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:02:00"), ev("e1", "ann", "09:04:00")])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "1"


def test_duplicate_dropped_does_not_create_or_split_sessions():
    # The dropped copy at 11:00 must not start a new session or count as an event.
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "11:00:00"), ev("e2", "ann", "09:20:00")])
    assert pick(r.sessions, "session_id", "start", "end", "events") == [
        ("ann-1", "2026-03-01T09:00:00Z", "2026-03-01T09:20:00Z", "2")]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_of_earliest_keeps_session_start():
    # Keeping the 09:00 copy (not the 09:30 one) decides the session start.
    r = run_job([ev("e1", "ann", "09:30:00"), ev("e1", "ann", "09:00:00")])
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["events"] == "1"


def test_duplicates_match_across_users():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "bob", "09:05:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert pick(r.sessions, "user") == [("ann",)]


def test_event_id_match_is_exact_string():
    # " e1" and "e1" are different strings, so neither is a duplicate of the other.
    r = run_job([ev(" e1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_event_id_case_matters():
    r = run_job([ev("E1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_duplicates_not_counted_as_rejects():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00")])
    assert r.rejects == []
    assert r.daily["rejected"] == 0


# ---------------------------------------------------------------- sessions

def test_gap_exactly_30_minutes_stays_in_session():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00")])
    assert len(s) == 1 and s[0]["events"] == "2"


def test_gap_just_over_30_minutes_splits():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01")])
    assert len(s) == 2


def test_gap_of_30_minutes_with_fractional_seconds_stays():
    s = sessions_of([ev("e1", "ann", "09:00:00.000"), ev("e2", "ann", "09:29:59.999")])
    assert len(s) == 1


def test_gap_fraction_over_30_minutes_splits_full_precision():
    # Gap is 30 min + 1 ms: must split even though it is 30 min to the second.
    s = sessions_of([ev("e1", "ann", "09:00:00.000"), ev("e2", "ann", "09:30:00.001")])
    assert len(s) == 2


def test_gap_measured_from_previous_event_not_session_start():
    s = sessions_of([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:25:00"),
                     ev("e3", "ann", "09:50:00"), ev("e4", "ann", "10:15:00")])
    assert len(s) == 1
    assert s[0]["events"] == "4"
    assert s[0]["duration_s"] == str(75 * 60)


def test_events_sorted_by_time_before_sessionizing():
    s = one_session([ev("e1", "ann", "09:40:00"), ev("e2", "ann", "09:00:00"), ev("e3", "ann", "09:20:00")])
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:40:00Z"
    assert s["events"] == "3"


def test_out_of_order_file_does_not_create_false_split():
    # In time order the gaps are 20 and 20 minutes: a single session.
    s = sessions_of([ev("e1", "ann", "09:40:00"), ev("e2", "ann", "09:00:00"), ev("e3", "ann", "09:20:00")])
    assert len(s) == 1


def test_session_ids_numbered_in_time_order_not_file_order():
    r = run_job([ev("e1", "ann", "12:00:00"), ev("e2", "ann", "09:00:00")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T12:00:00Z")]


def test_sessions_are_per_user_gaps_do_not_cross_users():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"), ev("e3", "ann", "09:20:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("bob-1", "1")]


def test_user_names_trimmed_and_lowercased_in_output_and_ids():
    r = run_job([ev("e1", "  Ann ", "09:00:00"), ev("e2", "ANN", "09:05:00")])
    assert pick(r.sessions, "session_id", "user", "events") == [("ann-1", "ann", "2")]


def test_same_user_different_spelling_shares_session_numbering():
    r = run_job([ev("e1", "Ann", "09:00:00"), ev("e2", "ann ", "12:00:00")])
    assert pick(r.sessions, "session_id") == [("ann-1",), ("ann-2",)]


def test_unicode_user_lowercased():
    r = run_job([ev("e1", "ÉLISE", "09:00:00")])
    assert pick(r.sessions, "session_id", "user") == [("élise-1", "élise")]


def test_user_with_internal_space_kept():
    r = run_job([ev("e1", "Ann Lee", "09:00:00")])
    assert pick(r.sessions, "session_id", "user") == [("ann lee-1", "ann lee")]


def test_single_event_session_has_zero_duration_and_one_event():
    s = one_session([ev("e1", "ann", "09:00:00")])
    assert s["duration_s"] == "0"
    assert s["events"] == "1"
    assert s["start"] == s["end"] == "2026-03-01T09:00:00Z"


def test_duration_is_whole_seconds_rounded_down():
    s = one_session([ev("e1", "ann", "09:00:00.900"), ev("e2", "ann", "09:00:02.000")])
    assert s["duration_s"] == "1"


def test_duration_just_under_a_whole_second_is_zero():
    s = one_session([ev("e1", "ann", "09:00:00.000"), ev("e2", "ann", "09:00:00.999")])
    assert s["duration_s"] == "0"


def test_duration_with_large_fraction_rounds_down_not_up():
    s = one_session([ev("e1", "ann", "09:00:00.000"), ev("e2", "ann", "09:00:10.999")])
    assert s["duration_s"] == "10"


def test_start_end_truncated_to_whole_seconds_in_output():
    s = one_session([ev("e1", "ann", "09:00:00.999"), ev("e2", "ann", "09:00:05.500")])
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:00:05Z"
    assert s["duration_s"] == "4"  # 4.501 s, rounded down


def test_start_end_output_format():
    r = run_job([ev("e1", "ann", "09:00:00.250"), ev("e2", "ann", "09:01:00")])
    for row in r.sessions:
        assert UTC_RE.match(row["start"]), row["start"]
        assert UTC_RE.match(row["end"]), row["end"]


def test_times_converted_to_utc_for_output():
    s = one_session([E("e1", "ann", "2026-03-01T09:00:00+07:00"),
                     E("e2", "ann", "2026-03-01T09:10:00+07:00")])
    assert s["start"] == "2026-03-01T02:00:00Z"
    assert s["end"] == "2026-03-01T02:10:00Z"


def test_negative_offset_converted_to_utc():
    s = one_session([E("e1", "ann", "2026-03-01T09:00:00.250-05:30")])
    assert s["start"] == "2026-03-01T14:30:00Z"


def test_mixed_offsets_for_same_user_order_by_instant():
    # 09:00+07:00 (02:00Z) is before 02:20Z even though its text shows a later clock time.
    r = run_job([E("e1", "ann", "2026-03-01T02:20:00Z"),
                 E("e2", "ann", "2026-03-01T09:00:00+07:00")])
    assert r.sessions[0]["start"] == "2026-03-01T02:00:00Z"
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[0]["duration_s"] == "1200"


def test_gap_computed_across_offsets_in_utc():
    # 09:00+07:00 == 02:00Z, 02:45Z is a 45 minute gap: two sessions.
    r = run_job([E("e1", "ann", "2026-03-01T09:00:00+07:00"),
                 E("e2", "ann", "2026-03-01T02:45:00Z")])
    assert len(r.sessions) == 2


def test_gap_across_utc_midnight_is_measured_correctly():
    r = run_job([E("e1", "ann", "2026-03-01T23:50:00Z"), E("e2", "ann", "2026-03-02T00:15:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == str(25 * 60)


def test_row_counts_events_purchases_and_revenue():
    s = one_session([ev("e1", "ann", "09:00:00"),
                     P("e2", "ann", full("09:05:00"), "19.99"),
                     P("e3", "ann", full("09:06:00"), "0.01"),
                     E("e4", "ann", full("09:07:00"), "click")])
    assert s["events"] == "4"
    assert s["purchases"] == "2"
    assert s["revenue"] == "20.00"


def test_revenue_is_exact_decimal_arithmetic():
    s = one_session([P("e1", "ann", full("09:00:00"), "0.10"), P("e2", "ann", full("09:01:00"), "0.20")])
    assert s["revenue"] == "0.30"


def test_revenue_exact_for_many_small_amounts():
    lines = [P(f"e{i}", "ann", full(f"09:{i:02d}:00"), "0.01") for i in range(10)]
    s = one_session(lines)
    assert s["revenue"] == "0.10"


def test_revenue_exact_for_very_large_amounts():
    s = one_session([P("e1", "ann", full("09:00:00"), "99999999999999999.99"),
                     P("e2", "ann", full("09:01:00"), "99999999999999999.99")])
    assert s["revenue"] == "199999999999999999.98"


def test_revenue_always_two_decimals():
    s = one_session([P("e1", "ann", full("09:00:00"), "5")])
    assert s["revenue"] == "5.00"
    s = one_session([P("e1", "ann", full("09:00:00"), "5.5")])
    assert s["revenue"] == "5.50"


def test_zero_revenue_formatted_with_two_decimals():
    s = one_session([ev("e1", "ann", "09:00:00"), P("e2", "ann", full("09:01:00"), "0")])
    assert s["revenue"] == "0.00"
    assert s["purchases"] == "1"


def test_session_without_purchases_has_zero_revenue():
    s = one_session([ev("e1", "ann", "09:00:00"), E("e2", "ann", full("09:01:00"), "click")])
    assert s["purchases"] == "0"
    assert s["revenue"] == "0.00"


def test_sessions_csv_rows_ordered_by_start_then_user():
    r = run_job([ev("e1", "cy", "09:00:00"), ev("e2", "bob", "08:00:00"), ev("e3", "ann", "09:00:00")])
    assert pick(r.sessions, "session_id") == [("bob-1",), ("ann-1",), ("cy-1",)]


def test_rows_ordered_by_full_precision_start_not_truncated_text():
    # Both truncate to 09:00:00Z; full precision puts bob (09:00:00.200) before ann (.500).
    r = run_job([ev("e1", "ann", "09:00:00.500"), ev("e2", "bob", "09:00:00.200")])
    assert pick(r.sessions, "user") == [("bob",), ("ann",)]


def test_identical_start_ties_broken_by_user_name():
    r = run_job([ev("e1", "bob", "09:00:00.500"), ev("e2", "ann", "09:00:00.500")])
    assert pick(r.sessions, "user") == [("ann",), ("bob",)]


# ---------------------------------------------------------------- daily summary

def test_daily_keys_and_types():
    r = run_job([P("e1", "ann", full("09:00:00"), "1.00")])
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}
    assert isinstance(r.daily["days"][0]["sessions"], int)
    assert isinstance(r.daily["days"][0]["users"], int)
    assert isinstance(r.daily["days"][0]["revenue"], str)


def test_days_listed_ascending_and_only_days_with_sessions():
    r = run_job([ev("e1", "ann", "2026-03-05T09:00:00Z"),
                 ev("e2", "bob", "2026-03-01T09:00:00Z"),
                 ev("e3", "cy", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_day_counts_sessions_and_distinct_users():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "12:00:00"), ev("e3", "bob", "09:00:00")])
    d = day(r, DAY)
    assert d["sessions"] == 3
    assert d["users"] == 2


def test_day_revenue_sums_session_revenue_with_two_decimals():
    r = run_job([P("e1", "ann", full("09:00:00"), "0.10"), P("e2", "bob", full("09:00:00"), "0.20"),
                 P("e3", "ann", full("12:00:00"), "5")])
    d = day(r, DAY)
    assert d["revenue"] == "5.30"


def test_day_revenue_zero_formatted():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert day(r, DAY)["revenue"] == "0.00"


def test_top_user_is_highest_revenue_of_that_day():
    r = run_job([P("e1", "ann", full("09:00:00"), "5.00"),
                 P("e2", "bob", full("09:00:00"), "20.00"),
                 P("e3", "cy", full("09:00:00"), "7.00")])
    assert day(r, DAY)["top_user"] == "bob"


def test_top_user_tie_goes_to_name_sorting_first():
    r = run_job([P("e1", "bob", full("09:00:00"), "10.00"), P("e2", "ann", full("09:00:00"), "10.00")])
    assert day(r, DAY)["top_user"] == "ann"


def test_top_user_uses_revenue_not_session_count():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "12:00:00"), ev("e3", "ann", "15:00:00"),
                 P("e4", "bob", full("09:00:00"), "1.00")])
    assert day(r, DAY)["top_user"] == "bob"


def test_top_user_is_per_day_not_global():
    r = run_job([P("e1", "ann", "2026-03-01T09:00:00Z", "50.00"),
                 P("e2", "bob", "2026-03-02T09:00:00Z", "1.00")])
    assert day(r, "2026-03-01")["top_user"] == "ann"
    assert day(r, "2026-03-02")["top_user"] == "bob"


def test_top_user_null_when_day_revenue_is_zero():
    r = run_job([ev("e1", "ann", "09:00:00"), P("e2", "bob", full("09:00:00"), "0")])
    d = day(r, DAY)
    assert d["revenue"] == "0.00"
    assert d["top_user"] is None


def test_top_user_ignores_zero_revenue_users_when_day_has_revenue():
    r = run_job([ev("e1", "aaa", "09:00:00"), P("e2", "zed", full("09:00:00"), "0.01")])
    assert day(r, DAY)["top_user"] == "zed"


def test_session_belongs_to_utc_date_of_start_across_midnight():
    # One session from 23:50 to 00:20 the next day: a single day, the first date.
    r = run_job([P("e1", "ann", "2026-03-01T23:50:00Z", "2.00"),
                 P("e2", "ann", "2026-03-02T00:20:00Z", "3.00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]
    d = day(r, "2026-03-01")
    assert d["sessions"] == 1 and d["revenue"] == "5.00"


def test_utc_date_used_not_local_date():
    # 00:30 at +07:00 is still the previous day in UTC.
    r = run_job([E("e1", "ann", "2026-03-01T00:30:00+07:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-02-28"]
    assert pick(r.sessions, "start") == [("2026-02-28T17:30:00Z",)]


def test_daily_summary_top_level_counts():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00"),
                 "{bad", ev("e3", "ann", "09:02:00", "dance"),
                 ev("e4", "ann", "09:03:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2


def test_daily_summary_sessions_match_sessions_csv():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "10:00:00"),
                 ev("e3", "ann", "12:00:00"), P("e4", "ann", "2026-03-02T09:00:00Z", "1.00")])
    per_day = {}
    for s in r.sessions:
        per_day[s["start"][:10]] = per_day.get(s["start"][:10], 0) + 1
    assert {d["date"]: d["sessions"] for d in r.daily["days"]} == per_day


def test_daily_revenue_matches_sum_of_session_revenue():
    r = run_job([P("e1", "ann", "2026-03-01T09:00:00Z", "1.25"),
                 P("e2", "ann", "2026-03-01T12:00:00Z", "2.50"),
                 P("e3", "bob", "2026-03-01T12:00:00Z", "0.05")])
    from decimal import Decimal
    total = sum(Decimal(s["revenue"]) for s in r.sessions if s["start"].startswith(DAY))
    assert day(r, DAY)["revenue"] == f"{total:.2f}"


def test_empty_day_list_when_no_sessions():
    r = run_job(["{x", "[]"])
    assert r.daily["days"] == []
    assert r.daily["rejected"] == 2


# ---------------------------------------------------------------- combined scenario

def test_combined_scenario_end_to_end():
    lines = [
        "",                                                                      # 1 blank, skipped
        ev("e1", "Ann", "09:00:00", "purchase", amount="19.99"),                # 2
        "{broken",                                                               # 3 bad_json
        ev("e1", "Ann", "09:00:00", "purchase", amount="19.99"),                # 4 duplicate
        ev("e2", "ann", "09:20:00", "click"),                                    # 5
        ev("e3", "ann", "10:00:00", "view"),                                     # 6 gap 40 min -> new session
        E("e4", "bob", "2026-03-01T09:00:00+07:00", "purchase", amount="5.5"),  # 7 == 02:00Z
        ev("e5", "bob", "09:00:00", "dance"),                                    # 8 bad_type
        E("e6", "bob", "2026-03-01T02:10:00Z", "purchase", amount="x"),         # 9 bad_amount
        ev("e7", "ann", "23:50:00", "purchase", amount="1"),                     # 10 gap 13h40 -> new session
        E("e8", "ann", "2026-03-02T00:15:00Z", "view"),                          # 11 gap 25 min -> same session
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 3, "reason": "bad_json"},
                         {"line": 8, "reason": "bad_type"},
                         {"line": 9, "reason": "bad_amount"}]
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 3
    assert r.header == HEADER
    assert pick(r.sessions, "session_id", "user", "start", "end", "duration_s",
                "events", "purchases", "revenue") == [
        ("bob-1", "bob", "2026-03-01T02:00:00Z", "2026-03-01T02:00:00Z", "0", "1", "1", "5.50"),
        ("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T09:20:00Z", "1200", "2", "1", "19.99"),
        ("ann-2", "ann", "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z", "0", "1", "0", "0.00"),
        ("ann-3", "ann", "2026-03-01T23:50:00Z", "2026-03-02T00:15:00Z", "1500", "2", "1", "1.00"),
    ]
    d = day(r, DAY)
    assert d["sessions"] == 4
    assert d["users"] == 2
    assert d["revenue"] == "26.49"
    assert d["top_user"] == "ann"
    assert [x["date"] for x in r.daily["days"]] == [DAY]
