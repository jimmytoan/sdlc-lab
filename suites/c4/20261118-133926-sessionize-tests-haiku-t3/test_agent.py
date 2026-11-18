"""Regression suite for the sessionize batch job (job.py), derived from SPEC.md.

Every test drives job.py as a black box through a subprocess. Expectations come
from SPEC.md, not from the implementation.
"""
import csv
import datetime as dt
import io
import json
import os
import random
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from helpers import APP_DIR, ev

JOB = os.path.join(APP_DIR, "job.py")
HEADER = "session_id,user,start,end,duration_s,events,purchases,revenue"
OUT_FILES = ["daily.json", "rejects.jsonl", "sessions.csv"]
MISSING = object()  # sentinel: leave the key out of the JSON object


# ---------------------------------------------------------------- helpers

def obj(event_id="e1", user="ann", ts="2026-03-01T09:00:00Z", type="view", **extra):
    """One event line as JSON text. Pass MISSING to omit a key entirely."""
    d = {"event_id": event_id, "user": user, "ts": ts, "type": type, **extra}
    return json.dumps({k: v for k, v in d.items() if v is not MISSING})


class Run:
    """Result of one job invocation with lazy access to the output files."""

    def __init__(self, proc, out):
        self.code = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.out = out
        self.files = sorted(os.listdir(out)) if out.is_dir() else []

    def text(self, name):
        return (self.out / name).read_text(encoding="utf-8")

    @property
    def csv_text(self):
        return self.text("sessions.csv")

    @property
    def header(self):
        return self.csv_text.split("\n")[0]

    @property
    def sessions(self):
        return list(csv.DictReader(io.StringIO(self.csv_text)))

    @property
    def daily(self):
        return json.loads(self.text("daily.json"))

    @property
    def rejects(self):
        return [json.loads(x) for x in self.text("rejects.jsonl").splitlines() if x.strip()]


@pytest.fixture
def run_job(tmp_path):
    """run_job(*lines, text=None, make_input=True, out="out", args=()) -> Run.

    Each line gets a trailing "\\n". Pass text= to write raw file content.
    """
    def _run(*lines, text=None, make_input=True, out="out", args=()):
        inp = tmp_path / "in"
        inp.mkdir(exist_ok=True)
        if make_input:
            body = text if text is not None else "".join(line + "\n" for line in lines)
            (inp / "events.jsonl").write_bytes(body.encode("utf-8"))
        out_dir = tmp_path / out
        proc = subprocess.run([sys.executable, JOB, "--in", str(inp), "--out", str(out_dir), *args],
                              capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
        return Run(proc, out_dir)
    return _run


def cols(run, *names):
    return [tuple(s[c] for c in names) for s in run.sessions]


def day(date, sessions, users, revenue, top_user):
    return {"date": date, "sessions": sessions, "users": users, "revenue": revenue, "top_user": top_user}


# ------------------------------------------------------- files and exit codes

def test_success_writes_three_files_and_exits_0(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"))
    assert r.code == 0
    assert r.files == OUT_FILES


def test_exit_0_when_some_lines_rejected(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), "{broken")
    assert r.code == 0
    assert len(r.rejects) == 1


def test_missing_input_exits_2_with_stderr_message(run_job):
    r = run_job(make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""


def test_empty_input_file_still_writes_all_outputs(run_job):
    r = run_job(text="")
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.header == HEADER
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    assert r.rejects == []


def test_whitespace_only_input_writes_all_outputs(run_job):
    r = run_job("   ", "\t", "")
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.sessions == []
    assert r.daily["days"] == []


def test_all_rejected_still_writes_all_outputs(run_job):
    r = run_job("{x", obj(type="dance"))
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 2}
    assert [x["reason"] for x in r.rejects] == ["bad_json", "bad_type"]


def test_output_dir_is_created_including_parents(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), out="deep/er/still")
    assert r.code == 0
    assert r.files == OUT_FILES


def test_output_dir_path_with_spaces(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), out="my out/dir name")
    assert r.code == 0
    assert r.files == OUT_FILES


def test_rerun_overwrites_stale_outputs(run_job):
    first = run_job(ev("e1", "ann", "09:00:00"), ev("e2", "bob", "10:00:00"))
    assert len(first.sessions) == 2
    second = run_job(text="", out="out")
    assert second.code == 0
    assert second.sessions == []
    assert second.header == HEADER
    assert second.daily["days"] == []


def test_output_is_deterministic(run_job):
    lines = [ev("e1", "bob", "10:00:00", "purchase", amount="2.50"), ev("e2", "ann", "09:00:00"),
             "{bad", ev("e1", "bob", "10:00:00", "purchase", amount="2.50")]
    a = run_job(*lines, out="a")
    b = run_job(*lines, out="b")
    for name in OUT_FILES:
        assert (a.out / name).read_bytes() == (b.out / name).read_bytes()


def test_sessions_csv_header_is_exact_and_uses_lf(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"))
    raw = (r.out / "sessions.csv").read_bytes()
    assert raw.split(b"\n")[0] == HEADER.encode()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")


def test_daily_json_has_exactly_the_documented_keys(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "purchase", amount="1"))
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}


def test_rejects_objects_have_exactly_line_and_reason(run_job):
    r = run_job("{x")
    assert r.text("rejects.jsonl") == '{"line": 1, "reason": "bad_json"}\n'
    assert set(r.rejects[0]) == {"line", "reason"}


# ------------------------------------------------------- line handling

def test_no_trailing_newline_on_last_line(run_job):
    r = run_job(text=obj("e1", "ann", "2026-03-01T09:00:00Z") + "\n" + obj("e2", "ann", "2026-03-01T09:05:00Z"))
    assert r.code == 0
    assert r.sessions[0]["events"] == "2"


def test_crlf_line_endings_are_accepted(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z") + "\r", obj("e2", "ann", "2026-03-01T09:05:00Z") + "\r")
    assert r.code == 0
    assert r.rejects == []
    assert r.sessions[0]["events"] == "2"


def test_blank_lines_skipped_but_counted_in_line_numbers(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), "", "   ", "{bad", ev("e2", "ann", "09:01:00"))
    assert r.rejects == [{"line": 4, "reason": "bad_json"}]
    assert r.daily["rejected"] == 1


def test_line_numbers_are_1_based_physical_lines(run_job):
    r = run_job("{a", ev("e1", "ann", "09:00:00"), "{b", "", "{c")
    assert r.rejects == [{"line": 1, "reason": "bad_json"}, {"line": 3, "reason": "bad_json"},
                         {"line": 5, "reason": "bad_json"}]


def test_rejects_listed_in_input_order(run_job):
    r = run_job(obj(type="dance"), ev("e1", "ann", "09:00:00"), obj("e2", ts="nope"), "{x")
    assert [(x["line"], x["reason"]) for x in r.rejects] == [(1, "bad_type"), (3, "bad_ts"), (4, "bad_json")]


def test_rejects_file_is_empty_when_nothing_rejected(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"))
    assert r.text("rejects.jsonl") == ""
    assert r.daily["rejected"] == 0


# ------------------------------------------------------- rejects: bad_json

@pytest.mark.parametrize("line", [
    "{not json", "[1, 2]", '"just a string"', "42", "null", "true",
    '{"event_id": "e1",', "{'event_id': 'e1'}",
])
def test_bad_json_is_rejected(run_job, line):
    r = run_job(line)
    assert r.rejects == [{"line": 1, "reason": "bad_json"}]


# ------------------------------------------------------- rejects: missing_field

@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", [MISSING, None, 5, ["x"], "", "   ", "\t"])
def test_missing_field_variants_are_rejected(run_job, field, value):
    r = run_job(obj(**{field: value}))
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]
    assert r.sessions == []


def test_missing_field_is_checked_before_bad_ts(run_job):
    r = run_job(obj(user=MISSING, ts="garbage", type="dance"))
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


# ------------------------------------------------------- rejects: bad_ts

@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",          # no offset
    "2026-03-01T09:00Z",            # no seconds
    "2026-03-01",                   # date only
    "2026-03-01T09:00:00+7:00",     # offset not HH:MM
    "2026-02-30T09:00:00Z",         # impossible date
    "2026-13-01T09:00:00Z",         # impossible month
    "2026-03-01T24:00:00Z",         # impossible hour
    "2026-03-01T09:60:00Z",         # impossible minute
    "yesterday",
    "1772355600",                   # epoch number
])
def test_bad_ts_is_rejected(run_job, ts):
    r = run_job(obj(ts=ts))
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00Z",
    "2026-03-01T09:00:00+07:00",
    "2026-03-01T09:00:00-05:30",
    "2026-03-01T09:00:00.250-05:30",
    "2026-03-01T09:00:00.5Z",
    "2026-03-01T09:00:00.123456Z",
])
def test_valid_ts_forms_are_accepted(run_job, ts):
    r = run_job(obj(ts=ts))
    assert r.rejects == []
    assert len(r.sessions) == 1


def test_fractional_seconds_do_not_change_output_format(run_job):
    r = run_job(obj(ts="2026-03-01T09:00:00.250-05:30"))
    assert r.sessions[0]["start"] == "2026-03-01T14:30:00Z"


# ------------------------------------------------------- rejects: bad_type

@pytest.mark.parametrize("typ", ["dance", "Purchase", "VIEW", "logout", "refund", "page_view", "purchase "])
def test_unknown_type_is_rejected(run_job, typ):
    r = run_job(obj(type=typ, amount="1"))
    assert r.rejects == [{"line": 1, "reason": "bad_type"}]


# ------------------------------------------------------- rejects: bad_amount

@pytest.mark.parametrize("amount", [
    MISSING, None, 5, 19.99, ["5"], "", "abc", "-1", "1.234", "1e3", "5.", ".5",
    "+5", "1,50", "$5", "NaN", "Infinity", "1.2.3", "--1", "0x10",
])
def test_invalid_purchase_amount_is_rejected(run_job, amount):
    r = run_job(obj(type="purchase", amount=amount))
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]
    assert r.sessions == []


@pytest.mark.parametrize("amount, revenue", [
    ("0", "0.00"), ("0.00", "0.00"), ("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"),
    ("100000000000000000000.01", "100000000000000000000.01"),
])
def test_valid_purchase_amounts_are_accepted(run_job, amount, revenue):
    r = run_job(obj(type="purchase", amount=amount))
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == revenue


@pytest.mark.parametrize("extra", [{"amount": "abc"}, {"amount": 5}, {"amount": MISSING}, {"amount": "-3"}])
@pytest.mark.parametrize("typ", ["view", "click"])
def test_amount_is_ignored_for_non_purchase(run_job, typ, extra):
    r = run_job(obj(type=typ, **extra))
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


# ------------------------------------------------------- rejects: precedence

@pytest.mark.parametrize("line, reason", [
    (obj(ts="2026-03-01T09:00:00", type="dance"), "bad_ts"),
    (obj(ts="bogus", type="purchase", amount=MISSING), "bad_ts"),
    (obj(type="dance", amount="abc"), "bad_type"),
    (obj(type="purchase", amount=MISSING), "bad_amount"),
    (obj(event_id=MISSING, ts="bogus"), "missing_field"),
])
def test_first_failing_check_is_reported(run_job, line, reason):
    r = run_job(line)
    assert r.rejects == [{"line": 1, "reason": reason}]


def test_rejected_line_takes_no_further_part(run_job):
    # The bad_amount purchase must not add revenue, count as an event, or start a session.
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="5.00"),
                obj("e2", "ann", "2026-03-01T09:05:00Z", "purchase", amount="x"),
                obj("e3", "ann", "2026-03-01T12:00:00Z", type="dance"))
    assert cols(r, "events", "purchases", "revenue") == [("1", "1", "5.00")]
    assert r.daily["rejected"] == 2


# ------------------------------------------------------- normalisation

def test_user_trimmed_and_lowercased(run_job):
    r = run_job(obj("e1", "  Ann ", "2026-03-01T09:00:00Z"), obj("e2", "ANN", "2026-03-01T09:01:00Z"),
                obj("e3", "ann", "2026-03-01T09:02:00Z"))
    assert cols(r, "session_id", "user", "events") == [("ann-1", "ann", "3")]


def test_user_with_inner_space_kept(run_job):
    r = run_job(obj("e1", "Ann  Lee", "2026-03-01T09:00:00Z"))
    assert cols(r, "session_id", "user") == [("ann  lee-1", "ann  lee")]


def test_event_id_matching_is_case_sensitive(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z"), obj("E1", "ann", "2026-03-01T09:01:00Z"))
    assert r.daily["duplicates_dropped"] == 0
    assert cols(r, "events") == [("2",)]


def test_event_id_matching_is_not_trimmed(run_job):
    r = run_job(obj(" e1", "ann", "2026-03-01T09:00:00Z"), obj("e1", "ann", "2026-03-01T09:01:00Z"))
    assert r.daily["duplicates_dropped"] == 0
    assert cols(r, "events") == [("2",)]


def test_same_event_id_for_different_users_is_a_duplicate(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z"), obj("e1", "bob", "2026-03-01T09:05:00Z"))
    assert r.daily["duplicates_dropped"] == 1
    assert cols(r, "user", "events") == [("ann", "1")]


# ------------------------------------------------------- duplicates

def test_duplicate_keeps_earliest_timestamp_not_first_in_file(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T10:00:00Z", "purchase", amount="7.00"),
                obj("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="3.00"))
    assert r.daily["duplicates_dropped"] == 1
    assert cols(r, "start", "revenue") == [("2026-03-01T09:00:00Z", "3.00")]


def test_equal_timestamp_duplicate_keeps_first_in_file(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "view"),
                obj("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="5"))
    assert cols(r, "events", "purchases") == [("1", "0")]


def test_equal_instant_with_different_offsets_is_a_tie(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00+07:00", "purchase", amount="5.00"),
                obj("e1", "ann", "2026-03-01T02:00:00Z", "view"))
    assert r.daily["duplicates_dropped"] == 1
    assert cols(r, "purchases", "revenue") == [("1", "5.00")]


def test_multiple_copies_all_but_one_dropped(run_job):
    r = run_job(*[obj("e1", "ann", f"2026-03-01T09:0{i}:00Z") for i in range(4)])
    assert r.daily["duplicates_dropped"] == 3
    assert cols(r, "events") == [("1",)]


def test_invalid_line_never_wins_as_duplicate(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T08:00:00Z", type="dance"),
                obj("e1", "ann", "2026-03-01T09:00:00Z"))
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 1
    assert cols(r, "start") == [("2026-03-01T09:00:00Z",)]


def test_invalid_line_does_not_cause_valid_event_to_be_dropped(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", type="purchase", amount="x"),
                obj("e1", "ann", "2026-03-01T09:30:00Z", "purchase", amount="4"))
    assert r.daily["duplicates_dropped"] == 0
    assert cols(r, "start", "revenue") == [("2026-03-01T09:30:00Z", "4.00")]


def test_duplicates_are_not_rejects(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"))
    assert r.rejects == []
    assert r.daily["rejected"] == 0
    assert r.daily["duplicates_dropped"] == 1


# ------------------------------------------------------- sessions: gaps

def test_gap_of_exactly_30_minutes_stays_in_session(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00"))
    assert cols(r, "session_id", "events") == [("ann-1", "2")]


def test_gap_of_30_minutes_plus_one_second_splits(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01"))
    assert cols(r, "session_id", "start") == [("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T09:30:01Z")]


def test_gap_uses_full_fractional_precision(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00.000Z"), obj("e2", "ann", "2026-03-01T09:30:00.001Z"))
    assert len(r.sessions) == 2


def test_gap_just_under_30_minutes_with_fractions_stays(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00.500Z"), obj("e2", "ann", "2026-03-01T09:30:00.400Z"))
    assert len(r.sessions) == 1


def test_gap_is_measured_from_previous_event_not_session_start(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:25:00"), ev("e3", "ann", "09:50:00"))
    assert cols(r, "session_id", "duration_s", "events") == [("ann-1", "3000", "3")]


def test_each_user_has_independent_sessions(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"), ev("e3", "ann", "09:40:00"))
    assert cols(r, "session_id", "user", "start") == [
        ("ann-1", "ann", "2026-03-01T09:00:00Z"),
        ("bob-1", "bob", "2026-03-01T09:10:00Z"),
        ("ann-2", "ann", "2026-03-01T09:40:00Z"),
    ]


def test_events_sorted_by_timestamp_before_sessionizing(run_job):
    r = run_job(ev("e2", "ann", "09:20:00"), ev("e1", "ann", "09:00:00"), ev("e3", "ann", "10:30:00"))
    assert cols(r, "session_id", "start", "events") == [
        ("ann-1", "2026-03-01T09:00:00Z", "2"), ("ann-2", "2026-03-01T10:30:00Z", "1")]


def test_session_ids_count_from_one_in_time_order(run_job):
    r = run_job(ev("e3", "ann", "12:00:00"), ev("e1", "ann", "08:00:00"), ev("e2", "ann", "10:00:00"))
    assert [s["session_id"] for s in r.sessions] == ["ann-1", "ann-2", "ann-3"]
    assert [s["start"] for s in r.sessions] == [
        "2026-03-01T08:00:00Z", "2026-03-01T10:00:00Z", "2026-03-01T12:00:00Z"]


def test_session_numbering_continues_across_days(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z"), obj("e2", "ann", "2026-03-02T09:00:00Z"))
    assert [s["session_id"] for s in r.sessions] == ["ann-1", "ann-2"]


def test_session_crossing_utc_midnight_is_one_session(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T23:50:00Z"), obj("e2", "ann", "2026-03-02T00:10:00Z"))
    assert cols(r, "session_id", "start", "end", "duration_s") == [
        ("ann-1", "2026-03-01T23:50:00Z", "2026-03-02T00:10:00Z", "1200")]


# ------------------------------------------------------- sessions: fields

def test_single_event_session_fields(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "view"))
    assert r.sessions == [{"session_id": "ann-1", "user": "ann", "start": "2026-03-01T09:00:00Z",
                           "end": "2026-03-01T09:00:00Z", "duration_s": "0", "events": "1",
                           "purchases": "0", "revenue": "0.00"}]


def test_spec_example_session_row(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00+07:00", "view"),
                obj("e2", "ann", "2026-03-01T09:05:00+07:00", "click"),
                obj("e3", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99"))
    assert r.sessions[0] == {"session_id": "ann-1", "user": "ann", "start": "2026-03-01T02:00:00Z",
                             "end": "2026-03-01T02:10:30Z", "duration_s": "630", "events": "3",
                             "purchases": "1", "revenue": "19.99"}


def test_start_and_end_are_converted_to_utc(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00+07:00"), obj("e2", "ann", "2026-03-01T09:10:00+07:00"))
    assert cols(r, "start", "end") == [("2026-03-01T02:00:00Z", "2026-03-01T02:10:00Z")]


def test_negative_offset_converted_to_utc(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T20:00:00-05:30"))
    assert cols(r, "start") == [("2026-03-02T01:30:00Z",)]


def test_start_and_end_fraction_is_truncated_not_rounded(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00.999Z"), obj("e2", "ann", "2026-03-01T09:00:01.999Z"))
    assert cols(r, "start", "end", "duration_s") == [("2026-03-01T09:00:00Z", "2026-03-01T09:00:01Z", "1")]


def test_duration_is_whole_seconds_rounded_down(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z"), obj("e2", "ann", "2026-03-01T09:00:10.999Z"))
    assert cols(r, "duration_s") == [("10",)]


def test_duration_uses_full_precision(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00.900Z"), obj("e2", "ann", "2026-03-01T09:00:01.100Z"))
    assert cols(r, "duration_s", "events") == [("0", "2")]


def test_event_and_purchase_counts(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "view"), ev("e2", "ann", "09:01:00", "click"),
                ev("e3", "ann", "09:02:00", "purchase", amount="1"),
                ev("e4", "ann", "09:03:00", "purchase", amount="0"))
    assert cols(r, "events", "purchases", "revenue") == [("4", "2", "1.00")]


def test_purchase_amounts_sum_exactly(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "purchase", amount="0.10"),
                ev("e2", "ann", "09:01:00", "purchase", amount="0.20"))
    assert cols(r, "revenue") == [("0.30",)]


def test_large_revenue_is_exact(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "purchase", amount="12345678901234567.89"),
                ev("e2", "ann", "09:01:00", "purchase", amount="0.11"))
    assert cols(r, "revenue") == [("12345678901234568.00",)]


def test_revenue_always_has_two_decimals(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "purchase", amount="7"),
                ev("e2", "bob", "09:00:00", "purchase", amount="2.5"))
    assert cols(r, "user", "revenue") == [("ann", "7.00"), ("bob", "2.50")]


def test_session_without_purchases_has_zero_revenue(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "click"))
    assert cols(r, "revenue") == [("0.00",)]


def test_rows_sorted_by_full_precision_start_then_user(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00.900Z"),
                obj("e2", "bob", "2026-03-01T09:00:00.100Z"))
    assert cols(r, "user") == [("bob",), ("ann",)]


def test_rows_with_same_start_sorted_by_user(run_job):
    r = run_job(ev("e1", "carl", "09:00:00"), ev("e2", "bob", "09:00:00"), ev("e3", "ann", "09:00:00"))
    assert cols(r, "user") == [("ann",), ("bob",), ("carl",)]


# ------------------------------------------------------- daily summary

def test_daily_summary_spec_example(run_job):
    r = run_job(ev("e1", "ann", "09:00:00", "purchase", amount="5.00"),
                ev("e2", "bob", "09:00:00", "purchase", amount="20"),
                ev("e3", "ann", "2026-03-02T09:00:00Z"))
    assert r.daily["days"] == [day("2026-03-01", 2, 2, "25.00", "bob"),
                               day("2026-03-02", 1, 1, "0.00", None)]


def test_session_belongs_to_utc_date_of_start(run_job):
    # 01:00 on 2026-03-01 at +07:00 is 2026-02-28 in UTC.
    r = run_job(obj("e1", "ann", "2026-03-01T01:00:00+07:00", "purchase", amount="3"))
    assert r.daily["days"] == [day("2026-02-28", 1, 1, "3.00", "ann")]


def test_session_crossing_midnight_counts_on_start_day(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T23:50:00Z", "purchase", amount="4"),
                obj("e2", "ann", "2026-03-02T00:10:00Z", "view"))
    assert r.daily["days"] == [day("2026-03-01", 1, 1, "4.00", "ann")]


def test_days_only_cover_dates_with_sessions_in_ascending_order(run_job):
    r = run_job(obj("e1", "ann", "2026-03-03T09:00:00Z"), obj("e2", "ann", "2026-03-01T09:00:00Z"))
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03"]


def test_users_counts_distinct_users_and_sessions_counts_sessions(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z"), obj("e2", "ann", "2026-03-01T12:00:00Z"),
                obj("e3", "bob", "2026-03-01T09:00:00Z"))
    assert r.daily["days"] == [day("2026-03-01", 3, 2, "0.00", None)]


def test_day_revenue_sums_sessions_and_top_user_sums_per_user(run_job):
    # ann: 5 + 5 = 10 across two sessions; bob: 8 in one session.
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="5"),
                obj("e2", "ann", "2026-03-01T12:00:00Z", "purchase", amount="5"),
                obj("e3", "bob", "2026-03-01T09:00:00Z", "purchase", amount="8"))
    assert r.daily["days"] == [day("2026-03-01", 3, 2, "18.00", "ann")]


def test_top_user_tie_goes_to_name_sorting_first(run_job):
    r = run_job(obj("e1", "zed", "2026-03-01T09:00:00Z", "purchase", amount="5"),
                obj("e2", "Adam", "2026-03-01T09:00:00Z", "purchase", amount="5"))
    assert r.daily["days"][0]["top_user"] == "adam"


def test_top_user_is_highest_revenue_not_first_alphabetically(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="1"),
                obj("e2", "bob", "2026-03-01T09:00:00Z", "purchase", amount="2"))
    assert r.daily["days"][0]["top_user"] == "bob"


def test_top_user_null_when_day_revenue_is_zero(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "view"),
                obj("e2", "bob", "2026-03-01T09:00:00Z", "purchase", amount="0"))
    assert r.daily["days"] == [day("2026-03-01", 2, 2, "0.00", None)]


def test_top_user_is_set_when_only_one_user_has_revenue(run_job):
    r = run_job(obj("e1", "ann", "2026-03-01T09:00:00Z", "view"),
                obj("e2", "bob", "2026-03-01T09:00:00Z", "purchase", amount="0.01"))
    assert r.daily["days"][0]["top_user"] == "bob"
    assert r.daily["days"][0]["revenue"] == "0.01"


def test_daily_counts_match_top_level_summary(run_job):
    r = run_job(ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), "{x", obj(type="dance"))
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2


def test_empty_days_list_when_no_sessions(run_job):
    r = run_job("{x")
    assert r.daily["days"] == []


# ------------------------------------------------------- oracle comparison

def _oracle(lines):
    """Independent implementation of SPEC.md for clean generated input.

    Returns (session_rows, days, duplicates_dropped, rejected_count).
    """
    valid, rejected = [], 0
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            rejected += 1
            continue
        t = dt.datetime.fromisoformat(o["ts"]).astimezone(dt.timezone.utc)
        valid.append({"id": o["event_id"], "user": o["user"].strip().lower(), "ts": t, "n": n,
                      "type": o["type"], "amt": Decimal(o.get("amount", "0")) if o["type"] == "purchase" else Decimal(0)})
    best = {}
    for e in valid:
        cur = best.get(e["id"])
        if cur is None or (e["ts"], e["n"]) < (cur["ts"], cur["n"]):
            best[e["id"]] = e
    kept = sorted(best.values(), key=lambda e: (e["ts"], e["n"]))
    dropped = len(valid) - len(kept)

    sessions = []
    last, count = {}, {}
    for e in kept:
        u = e["user"]
        if u not in last or e["ts"] - last[u]["last"] > dt.timedelta(minutes=30):
            count[u] = count.get(u, 0) + 1
            s = {"id": f"{u}-{count[u]}", "user": u, "start": e["ts"], "last": e["ts"], "events": 0,
                 "purchases": 0, "revenue": Decimal(0)}
            last[u] = s
            sessions.append(s)
        s = last[u]
        s["last"] = e["ts"]
        s["events"] += 1
        if e["type"] == "purchase":
            s["purchases"] += 1
            s["revenue"] += e["amt"]
    sessions.sort(key=lambda s: (s["start"], s["user"]))

    def fmt(t):
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = [(s["id"], s["user"], fmt(s["start"]), fmt(s["last"]),
             str(int((s["last"] - s["start"]).total_seconds())), str(s["events"]),
             str(s["purchases"]), f"{s['revenue']:.2f}") for s in sessions]

    per_day = {}
    for s in sessions:
        d = per_day.setdefault(s["start"].strftime("%Y-%m-%d"), {"sessions": 0, "users": {}})
        d["sessions"] += 1
        d["users"][s["user"]] = d["users"].get(s["user"], Decimal(0)) + s["revenue"]
    days = []
    for date in sorted(per_day):
        d = per_day[date]
        total = sum(d["users"].values(), Decimal(0))
        top = min(d["users"].items(), key=lambda kv: (-kv[1], kv[0]))[0] if total > 0 else None
        days.append(day(date, d["sessions"], len(d["users"]), f"{total:.2f}", top))
    return rows, days, dropped, rejected


def _generated_lines(seed, n=160):
    rnd = random.Random(seed)
    users = ["Ann", "bob ", "CY", "dee"]
    base = dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)
    offsets = [dt.timezone.utc, dt.timezone(dt.timedelta(hours=7)), dt.timezone(dt.timedelta(hours=-5, minutes=-30))]
    lines = []
    for _ in range(n):
        if rnd.random() < 0.05:
            lines.append("{broken")
            continue
        t = base + dt.timedelta(seconds=rnd.randint(0, 3 * 86400), milliseconds=rnd.choice([0, 0, 250, 500]))
        tz = rnd.choice(offsets)
        local = t.astimezone(tz)
        stamp = local.isoformat(timespec="milliseconds" if local.microsecond else "seconds")
        stamp = stamp.replace("+00:00", "Z")
        typ = rnd.choice(["view", "click", "purchase", "purchase"])
        extra = {}
        if typ == "purchase":
            cents = rnd.randint(0, 99999)
            extra["amount"] = f"{cents // 100}.{cents % 100:02d}" if rnd.random() < 0.8 else str(cents // 100)
        lines.append(obj(f"e{rnd.randint(0, 90)}", rnd.choice(users), stamp, typ, **extra))
    return lines


@pytest.mark.parametrize("seed", range(6))
def test_matches_independent_model_on_generated_input(run_job, seed):
    lines = _generated_lines(seed)
    r = run_job(*lines)
    rows, days, dropped, rejected = _oracle(lines)
    assert r.code == 0
    assert [tuple(s[c] for c in ("session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"))
            for s in r.sessions] == rows
    assert r.daily == {"days": days, "duplicates_dropped": dropped, "rejected": rejected}
    assert len(r.rejects) == rejected
    starts = [s["start"] for s in r.sessions]
    assert starts == sorted(starts)
