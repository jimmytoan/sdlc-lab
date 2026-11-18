"""Black-box regression suite for the sessionize batch job, written from SPEC.md.

Every test drives job.py as a subprocess, feeds it an events.jsonl and inspects
sessions.csv, daily.json and rejects.jsonl (and the exit code / stderr).
"""
import csv
import json
import os
import random
import subprocess
import sys
import tempfile
from decimal import Decimal
from types import SimpleNamespace

import pytest

from helpers import APP_DIR, ev, run_job, pick

JOB = os.path.join(APP_DIR, "job.py")
HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
DAY = "2026-03-01"
OUT_FILES = ["daily.json", "rejects.jsonl", "sessions.csv"]


# ---------------------------------------------------------------- helpers ---

def invoke(in_dir, out_dir):
    """Run the job with the given input dir and (possibly nested, missing) output dir."""
    return subprocess.run([sys.executable, JOB, "--in", in_dir, "--out", out_dir],
                          capture_output=True, text=True, timeout=30, cwd=APP_DIR)


def read_outputs(out_dir):
    files = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    res = SimpleNamespace(files=files, header=None, sessions=None, daily=None, rejects=None,
                          csv_text=None, daily_text=None, rejects_text=None)
    if "sessions.csv" in files:
        with open(os.path.join(out_dir, "sessions.csv"), encoding="utf-8", newline="") as f:
            res.csv_text = f.read()
        rows = list(csv.reader(res.csv_text.splitlines()))
        res.header = rows[0] if rows else []
        res.sessions = [dict(zip(res.header, r)) for r in rows[1:] if r]
    if "daily.json" in files:
        with open(os.path.join(out_dir, "daily.json"), encoding="utf-8") as f:
            res.daily_text = f.read()
        res.daily = json.loads(res.daily_text)
    if "rejects.jsonl" in files:
        with open(os.path.join(out_dir, "rejects.jsonl"), encoding="utf-8") as f:
            res.rejects_text = f.read()
        res.rejects = [json.loads(x) for x in res.rejects_text.split("\n") if x.strip()]
    return res


def run_nested(lines):
    """Run with the output directory nested under a not-yet-existing path."""
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        out = os.path.join(d, "a", "b", "out")
        p = invoke(inp, out)
        res = read_outputs(out)
        res.code, res.stderr = p.returncode, p.stderr
        return res


def ev_raw(**fields):
    """Event built from explicit fields; a field set to None is removed from the object."""
    obj = {"event_id": "e1", "user": "ann", "ts": f"{DAY}T09:00:00Z", "type": "view"}
    for k, v in fields.items():
        if v is None:
            obj.pop(k, None)
        else:
            obj[k] = v
    return json.dumps(obj)


def reason_of(line):
    """Reject reason for a single-line input; that line must be rejected."""
    r = run_job([line])
    assert r.code == 0
    assert len(r.rejects) == 1, r.rejects
    assert r.rejects[0]["line"] == 1
    return r.rejects[0]["reason"]


def sess_rows(r):
    return [(s["session_id"], s["user"], s["start"], s["end"], s["duration_s"],
             s["events"], s["purchases"], s["revenue"]) for s in r.sessions]


# ------------------------------------------------------- invocation / files --

def test_outputs_and_header_exact():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.header == HEADER


def test_all_files_written_for_empty_input():
    r = run_nested([])
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.csv_text.splitlines() == [",".join(HEADER)]
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_all_files_written_when_only_blank_lines():
    r = run_job(["", "   ", "\t"])
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.sessions == [] and r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_all_files_written_when_only_rejects():
    r = run_job(["{nope"])
    assert r.code == 0
    assert r.files == OUT_FILES
    assert r.sessions == []
    assert r.rejects == [{"line": 1, "reason": "bad_json"}]
    assert r.daily["rejected"] == 1 and r.daily["days"] == []


def test_output_dir_created_including_parents():
    r = run_nested([ev("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.files == OUT_FILES
    assert len(r.sessions) == 1


def test_rerun_overwrites_previous_outputs():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "out")
        inp1, inp2 = os.path.join(d, "in1"), os.path.join(d, "in2")
        for inp, lines in ((inp1, [ev("e1", "ann", "09:00:00"), ev("e2", "bob", "10:00:00"), "{x"]),
                           (inp2, [ev("e9", "cy", "12:00:00")])):
            os.makedirs(inp)
            with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        assert invoke(inp1, out).returncode == 0
        assert invoke(inp2, out).returncode == 0
        r = read_outputs(out)
        assert pick(r.sessions, "user") == [("cy",)]
        assert r.rejects == []
        assert r.daily["rejected"] == 0


def test_run_is_deterministic():
    lines = [ev("e1", "bob", "10:00:00", "purchase", amount="1.5"), ev("e2", "ann", "09:00:00"),
             ev("e3", "ann", "09:50:00", "purchase", amount="2"), "{bad", ev("e1", "bob", "10:00:00")]
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        with open(os.path.join(inp, "events.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        outs = []
        for name in ("o1", "o2"):
            out = os.path.join(d, name)
            assert invoke(inp, out).returncode == 0
            outs.append(read_outputs(out))
        assert outs[0].csv_text == outs[1].csv_text
        assert outs[0].daily_text == outs[1].daily_text
        assert outs[0].rejects_text == outs[1].rejects_text


def test_missing_events_file_exits_2_with_stderr():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""


def test_missing_events_file_when_input_dir_has_other_file():
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        with open(os.path.join(inp, "other.jsonl"), "w") as f:
            f.write(ev("e1", "ann", "09:00:00") + "\n")
        p = invoke(inp, os.path.join(d, "out"))
        assert p.returncode == 2
        assert p.stderr.strip() != ""


def test_exit_0_even_when_lines_rejected():
    r = run_job([ev("e1", "ann", "09:00:00"), "garbage", ev("", "ann", "09:00:00")])
    assert r.code == 0


# ----------------------------------------------------------------- input ----

def test_blank_lines_skipped_but_counted():
    r = run_job(["", "   ", ev("e1", "ann", "09:00:00"), "\t", "{bad"])
    assert r.code == 0
    assert r.rejects == [{"line": 5, "reason": "bad_json"}]
    assert len(r.sessions) == 1


def test_reject_line_numbers_are_physical_lines():
    r = run_job(["", ev("e1", "ann", "09:00:00"), "", "{bad", ev("e2", "ann", "09:01:00"), "  ", "[]"])
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 7, "reason": "bad_json"}]


def test_rejects_in_input_order():
    r = run_job([ev("e1", "ann", "09:00:00", "dance"), ev("e2", "ann", "09:00:00", "purchase"),
                 "nope", ev_raw(event_id="e3", ts=None)])
    assert r.rejects == [{"line": 1, "reason": "bad_type"}, {"line": 2, "reason": "bad_amount"},
                         {"line": 3, "reason": "bad_json"}, {"line": 4, "reason": "missing_field"}]


def test_reject_record_shape():
    r = run_nested(["{bad"])
    assert json.loads(r.rejects_text.splitlines()[0]) == {"line": 1, "reason": "bad_json"}


def test_rejected_lines_do_not_form_sessions():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:05:00", "dance")])
    assert len(r.sessions) == 1 and r.sessions[0]["events"] == "1"


def test_whitespace_only_lines_are_not_rejects():
    r = run_job(["   ", ev("e1", "ann", "09:00:00")])
    assert r.rejects == []
    assert len(r.sessions) == 1


def test_fields_are_trimmed_for_checks():
    r = run_job([ev("  e1  ", "  Ann  ", "09:00:00")])
    assert r.rejects == []
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["session_id"] == "ann-1"


def test_unicode_utf8_input_is_read():
    r = run_job([ev("e-é", "zoë", "09:00:00", "purchase", amount="3.50")])
    assert r.code == 0
    assert r.sessions[0]["user"] == "zoë"
    assert r.sessions[0]["revenue"] == "3.50"


# -------------------------------------------------------- reject reasons -----

@pytest.mark.parametrize("line", [
    "{not json",
    '{"event_id": "e1", "user": "ann"',
    "[1, 2, 3]",
    '"just a string"',
    "42",
    "null",
    "true",
    "{'event_id': 'e1'}",
])
def test_bad_json(line):
    assert reason_of(line) == "bad_json"


@pytest.mark.parametrize("fields", [
    {"event_id": None},
    {"user": None},
    {"ts": None},
    {"type": None},
    {"event_id": ""},
    {"event_id": "   "},
    {"user": ""},
    {"user": "  \t "},
    {"ts": ""},
    {"event_id": 7},
    {"user": 7},
    {"ts": 1772355600},
    {"type": 1},
    {"type": True},
    {"event_id": ["e1"]},
    {"user": {"name": "ann"}},
    {"type": None, "event_id": None},
])
def test_missing_field(fields):
    assert reason_of(ev_raw(**fields)) == "missing_field"


def test_missing_field_checked_before_bad_ts():
    assert reason_of(ev_raw(user=None, ts="not a time")) == "missing_field"
    assert reason_of(ev_raw(ts=5, type="dance")) == "missing_field"


def test_missing_field_checked_before_bad_type():
    assert reason_of(ev_raw(event_id=None, type="dance")) == "missing_field"


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",          # no offset
    "2026-03-01T09:00Z",            # no seconds
    "2026-03-01",                   # date only
    "09:00:00",                     # time only
    "not a time",
    "2026-13-01T09:00:00Z",         # month 13
    "2026-02-30T09:00:00Z",         # impossible date
    "2026-03-01T25:00:00Z",         # hour 25
    "2026-03-01T09:00:00.Z",        # empty fraction
    "2026/03/01T09:00:00Z",
    "1772355600",                   # epoch seconds, not ISO 8601
])
def test_bad_ts(ts):
    assert reason_of(ev_raw(ts=ts)) == "bad_ts"


def test_bad_ts_checked_before_bad_type():
    assert reason_of(ev_raw(ts="2026-03-01T09:00:00", type="dance")) == "bad_ts"


def test_bad_ts_checked_before_bad_amount():
    assert reason_of(ev_raw(ts="2026-03-01T09:00:00", type="purchase", amount="x")) == "bad_ts"


@pytest.mark.parametrize("etype", ["dance", "VIEW", "Click", "buy", "purchases"])
def test_bad_type(etype):
    assert reason_of(ev_raw(type=etype)) == "bad_type"


def test_bad_type_checked_before_bad_amount():
    assert reason_of(ev_raw(type="dance", amount="x")) == "bad_type"


@pytest.mark.parametrize("amount_field", [
    {},                               # absent amount
    {"amount": None},
    {"amount": ""},
    {"amount": 5},                    # number, not string
    {"amount": 19.99},
    {"amount": True},
    {"amount": ["5"]},
    {"amount": "-1"},
    {"amount": "-0.50"},
    {"amount": "1.234"},
    {"amount": "1,5"},
    {"amount": "$5"},
    {"amount": "abc"},
    {"amount": "1e3"},
    {"amount": "NaN"},
    {"amount": "Infinity"},
    {"amount": "1.2.3"},
    {"amount": "5 USD"},
])
def test_bad_amount(amount_field):
    assert reason_of(ev_raw(type="purchase", **amount_field)) == "bad_amount"


@pytest.mark.parametrize("amount", ["0", "5", "5.5", "19.99", "0.00", "1234567.89", "0.5", "10.10"])
def test_valid_amounts_accepted(amount):
    r = run_job([ev_raw(type="purchase", amount=amount)])
    assert r.rejects == []
    assert r.sessions[0]["purchases"] == "1"


@pytest.mark.parametrize("etype", ["view", "click"])
@pytest.mark.parametrize("amount", [None, "", "garbage", -5, "-1", "1.234", 7])
def test_amount_ignored_for_non_purchase(etype, amount):
    r = run_job([ev_raw(type=etype, amount=amount)])
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00"


def test_amount_on_non_purchase_does_not_add_revenue():
    r = run_job([ev("e1", "ann", "09:00:00", "view", amount="50"),
                 ev("e2", "ann", "09:01:00", "click", amount="7.25")])
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


def test_valid_timestamp_forms_are_accepted():
    lines = [
        ev_raw(event_id="a", user="u1", ts="2026-03-01T09:00:00Z"),
        ev_raw(event_id="b", user="u2", ts="2026-03-01T09:00:00+07:00"),
        ev_raw(event_id="c", user="u3", ts="2026-03-01T09:00:00.250-05:30"),
        ev_raw(event_id="d", user="u4", ts="2026-03-01T09:00:00.5Z"),
        ev_raw(event_id="e", user="u5", ts="2026-03-01T09:00:00.123Z"),
        ev_raw(event_id="f", user="u6", ts="2026-03-01T09:00:00+00:00"),
        ev_raw(event_id="g", user="u7", ts="2026-03-01T09:00:00-00:00"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == []
    assert len(r.sessions) == 7


# ---------------------------------------------------------------- users -----

def test_users_compared_after_trim_and_lowercase():
    r = run_job([ev("e1", "Ann", "09:00:00"), ev("e2", "  ANN ", "09:05:00"), ev("e3", "ann", "09:10:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["session_id"] == "ann-1"
    assert r.sessions[0]["events"] == "3"


def test_user_output_is_lowercase_and_session_id_uses_it():
    r = run_job([ev("e1", "BOB", "09:00:00")])
    assert r.sessions[0]["user"] == "bob"
    assert r.sessions[0]["session_id"] == "bob-1"


def test_user_with_inner_space_kept():
    r = run_job([ev("e1", "Mary  Ann", "09:00:00")])
    assert r.sessions[0]["user"] == "mary  ann"
    assert r.sessions[0]["session_id"] == "mary  ann-1"


# ----------------------------------------------------------- duplicates -----

def test_duplicate_dropped_and_counted():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:01:00")])
    assert r.sessions[0]["events"] == "2"
    assert r.daily["duplicates_dropped"] == 1
    assert r.rejects == []


def test_duplicate_is_not_a_reject():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00")])
    assert r.rejects == []
    assert r.daily["rejected"] == 0
    assert r.daily["duplicates_dropped"] == 1


def test_triple_duplicate_counts_two():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:01:00"), ev("e1", "ann", "09:02:00")])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "1"


def test_earliest_timestamp_kept_even_if_later_in_file():
    r = run_job([ev("e1", "ann", "09:30:00"), ev("e1", "bob", "09:00:00")])
    assert pick(r.sessions, "user", "start") == [("bob", "2026-03-01T09:00:00Z")]


def test_equal_timestamp_keeps_first_in_file():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "bob", "09:00:00")])
    assert pick(r.sessions, "user") == [("ann",)]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_removed_before_sessionizing_changes_split():
    # e2 is kept at 09:35 (its earlier copy), so the gap from 09:00 is 35 min -> two sessions
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:40:00"), ev("e2", "ann", "09:35:00")])
    assert pick(r.sessions, "session_id", "start") == [("ann-1", "2026-03-01T09:00:00Z"),
                                                       ("ann-2", "2026-03-01T09:35:00Z")]


def test_duplicate_earliest_with_fractions():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.700Z"), ev("e1", "bob", "2026-03-01T09:00:00.200Z")])
    assert pick(r.sessions, "user") == [("bob",)]


def test_event_id_comparison_is_exact_and_case_sensitive():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("E1", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_invalid_line_with_same_event_id_is_not_a_duplicate():
    r = run_job([ev("e1", "ann", "09:00:00", "dance"), ev("e1", "bob", "09:30:00")])
    assert r.rejects == [{"line": 1, "reason": "bad_type"}]
    assert r.daily["duplicates_dropped"] == 0
    assert pick(r.sessions, "user", "start") == [("bob", "2026-03-01T09:30:00Z")]


def test_invalid_earlier_line_does_not_displace_valid_copy():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase"), ev("e1", "bob", "10:00:00")])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]
    assert pick(r.sessions, "user") == [("bob",)]


def test_duplicates_across_users_collapse():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "bob", "09:00:01")])
    assert pick(r.sessions, "user") == [("ann",)]
    assert r.daily["duplicates_dropped"] == 1


# ------------------------------------------------------------- sessions -----

def test_single_event_session_fields():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.sessions[0] == {"session_id": "ann-1", "user": "ann", "start": "2026-03-01T09:00:00Z",
                             "end": "2026-03-01T09:00:00Z", "duration_s": "0", "events": "1",
                             "purchases": "0", "revenue": "0.00"}


def test_gap_of_exactly_30_minutes_stays_in_session():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == "1800"


def test_gap_of_30_minutes_plus_one_second_splits():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:30:01")])
    assert pick(r.sessions, "session_id") == [("ann-1",), ("ann-2",)]


def test_gap_of_30_minutes_plus_one_millisecond_splits():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:30:00.001Z")])
    assert len(r.sessions) == 2


def test_gap_of_exactly_30_minutes_with_fraction_stays():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.500Z"), ev("e2", "ann", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 1


def test_gap_measured_from_previous_event_not_session_start():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:25:00"),
                 ev("e3", "ann", "09:50:00"), ev("e4", "ann", "10:20:00"), ev("e5", "ann", "10:51:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "4"), ("ann-2", "1")]


def test_gap_is_per_user_not_global():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"), ev("e3", "ann", "09:20:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("bob-1", "1")]


def test_interleaved_users_sessionized_independently():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e2", "bob", "09:10:00"),
                 ev("e3", "ann", "09:20:00"), ev("e4", "bob", "09:55:00"), ev("e5", "ann", "09:50:00")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "3"), ("bob-1", "1"), ("bob-2", "1")]


def test_session_numbering_is_per_user_in_time_order():
    r = run_job([ev("e3", "ann", "12:00:00"), ev("e1", "ann", "09:00:00"), ev("e2", "ann", "10:30:00")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T10:30:00Z"), ("ann-3", "2026-03-01T12:00:00Z")]


def test_file_order_does_not_matter_for_sessions():
    base = [ev("e1", "ann", "09:00:00"), ev("e2", "ann", "09:20:00"), ev("e3", "ann", "10:30:00"),
            ev("e4", "bob", "09:05:00"), ev("e5", "bob", "09:40:00"), ev("e6", "ann", "09:10:00")]
    expected = sess_rows(run_job(base))
    rnd = random.Random(7)
    for _ in range(3):
        shuffled = base[:]
        rnd.shuffle(shuffled)
        assert sess_rows(run_job(shuffled)) == expected


def test_out_of_order_events_sorted_by_timestamp():
    r = run_job([ev("e2", "ann", "09:20:00", "purchase", amount="1"), ev("e1", "ann", "09:00:00")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T09:00:00Z" and s["end"] == "2026-03-01T09:20:00Z"
    assert s["duration_s"] == "1200"


def test_timestamps_compared_as_instants_across_offsets():
    # 09:00+07:00 is 02:00Z; 02:20Z is 20 minutes later -> same session
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T02:20:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["start"] == "2026-03-01T02:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T02:20:00Z"


def test_same_instant_in_different_offsets_is_zero_gap():
    r = run_job([ev("e1", "ann", "2026-03-01T02:00:00Z"), ev("e2", "ann", "2026-03-01T03:00:00+01:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == "0"
    assert r.sessions[0]["events"] == "2"


def test_start_and_end_converted_to_utc_z():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00+07:00"), ev("e2", "ann", "2026-03-01T09:10:30+07:00")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T02:00:00Z"
    assert s["end"] == "2026-03-01T02:10:30Z"
    assert s["duration_s"] == "630"


def test_negative_offset_converted_to_utc():
    r = run_job([ev("e1", "ann", "2026-03-01T23:30:00-05:00")])
    assert r.sessions[0]["start"] == "2026-03-02T04:30:00Z"


def test_fractional_offset_converted_to_utc():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.250-05:30")])
    assert r.sessions[0]["start"] == "2026-03-01T14:30:00Z"


def test_fractional_seconds_truncated_in_output():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.999Z"), ev("e2", "ann", "2026-03-01T09:00:01.999Z")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:00:01Z"


def test_duration_rounded_down_from_fractions():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.900Z"), ev("e2", "ann", "2026-03-01T09:00:01.100Z")])
    assert r.sessions[0]["duration_s"] == "0"
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:00:01.999Z")])
    assert r.sessions[0]["duration_s"] == "1"


def test_gap_check_uses_full_precision():
    # 30m00.5s apart: must split even though truncated start/end differ by exactly 30 min
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00.000Z"), ev("e2", "ann", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 2


def test_ordering_uses_full_precision_for_start():
    # both truncate to 09:00:00Z; .100 (zed) sorts before .900 (amy)
    r = run_job([ev("e1", "amy", "2026-03-01T09:00:00.900Z"), ev("e2", "zed", "2026-03-01T09:00:00.100Z")])
    assert pick(r.sessions, "user") == [("zed",), ("amy",)]


def test_sessions_ordered_by_start_then_user():
    r = run_job([ev("e1", "zed", "09:00:00"), ev("e2", "amy", "09:00:00"),
                 ev("e3", "mid", "08:00:00"), ev("e4", "amy", "11:00:00")])
    assert pick(r.sessions, "session_id", "start") == [
        ("mid-1", "2026-03-01T08:00:00Z"), ("amy-1", "2026-03-01T09:00:00Z"),
        ("zed-1", "2026-03-01T09:00:00Z"), ("amy-2", "2026-03-01T11:00:00Z")]


def test_events_and_purchases_counts():
    r = run_job([ev("e1", "ann", "09:00:00", "view"), ev("e2", "ann", "09:01:00", "click"),
                 ev("e3", "ann", "09:02:00", "purchase", amount="1"),
                 ev("e4", "ann", "09:03:00", "purchase", amount="0")])
    s = r.sessions[0]
    assert s["events"] == "4" and s["purchases"] == "2"
    assert s["revenue"] == "1.00"


def test_zero_amount_purchase_counts_but_revenue_zero():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0")])
    assert r.sessions[0]["purchases"] == "1"
    assert r.sessions[0]["revenue"] == "0.00"


@pytest.mark.parametrize("amount,expected", [
    ("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"), ("0.1", "0.10"), ("0", "0.00"),
    ("1234567.89", "1234567.89"), ("0.00", "0.00"),
])
def test_revenue_always_two_decimals(amount, expected):
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount=amount)])
    assert r.sessions[0]["revenue"] == expected


def test_revenue_is_exact_decimal_sum():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0.10"),
                 ev("e2", "ann", "09:01:00", "purchase", amount="0.20")])
    assert r.sessions[0]["revenue"] == "0.30"


def test_revenue_exact_for_large_values():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="9007199254740993.01"),
                 ev("e2", "ann", "09:01:00", "purchase", amount="0.01")])
    assert r.sessions[0]["revenue"] == "9007199254740993.02"


def test_revenue_sums_many_small_amounts_exactly():
    lines = []
    for i in range(100):  # 10 seconds apart: one session of 990 s
        m, s = divmod(i * 10, 60)
        lines.append(ev(f"e{i}", "ann", f"09:{m:02d}:{s:02d}", "purchase", amount="0.01"))
    r = run_job(lines)
    assert r.sessions[0]["revenue"] == "1.00"
    assert r.sessions[0]["purchases"] == "100"


def test_csv_row_format_fields():
    r = run_nested([ev("e1", "ann", "2026-03-01T09:00:00.5+07:00", "purchase", amount="19.9")])
    assert r.csv_text.splitlines() == [",".join(HEADER),
                                       "ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:00:00Z,0,1,1,19.90"]


def test_spec_example_row():
    r = run_nested([ev("e1", "ann", "2026-03-01T02:00:00Z"), ev("e2", "ann", "2026-03-01T02:05:00Z", "click"),
                 ev("e3", "ann", "2026-03-01T02:10:30Z", "purchase", amount="19.99")])
    assert r.csv_text.splitlines()[1] == "ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:10:30Z,630,3,1,19.99"


def test_session_without_purchases_has_zero_revenue():
    r = run_job([ev("e1", "ann", "09:00:00", "view")])
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


def test_revenue_belongs_to_its_own_session_only():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5"),
                 ev("e2", "ann", "10:00:00", "purchase", amount="7")])
    assert pick(r.sessions, "session_id", "revenue") == [("ann-1", "5.00"), ("ann-2", "7.00")]


def test_csv_has_header_even_when_no_sessions():
    r = run_nested(["{bad"])
    assert r.csv_text.splitlines() == [",".join(HEADER)]


# ---------------------------------------------------------------- daily -----

def test_daily_top_level_keys():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert set(r.daily.keys()) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0].keys()) == {"date", "sessions", "users", "revenue", "top_user"}


def test_daily_summary_row():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5.00"),
                 ev("e2", "bob", "09:00:00", "purchase", amount="20")])
    assert r.daily["days"] == [{"date": DAY, "sessions": 2, "users": 2, "revenue": "25.00", "top_user": "bob"}]


def test_daily_users_is_distinct_count():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="1"),
                 ev("e2", "ann", "11:00:00", "purchase", amount="2")])
    assert r.daily["days"] == [{"date": DAY, "sessions": 2, "users": 1, "revenue": "3.00", "top_user": "ann"}]


def test_daily_top_user_sums_revenue_across_sessions():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="10"),
                 ev("e2", "ann", "11:00:00", "purchase", amount="10"),
                 ev("e3", "bob", "09:00:00", "purchase", amount="15")])
    assert r.daily["days"][0]["top_user"] == "ann"
    assert r.daily["days"][0]["revenue"] == "35.00"


def test_daily_top_user_tie_goes_to_name_sorting_first():
    r = run_job([ev("e1", "zoe", "09:00:00", "purchase", amount="4"),
                 ev("e2", "amy", "09:00:00", "purchase", amount="4")])
    assert r.daily["days"][0]["top_user"] == "amy"


def test_daily_top_user_null_when_revenue_zero():
    r = run_job([ev("e1", "ann", "09:00:00", "view"), ev("e2", "bob", "09:00:00", "purchase", amount="0")])
    assert r.daily["days"] == [{"date": DAY, "sessions": 2, "users": 2, "revenue": "0.00", "top_user": None}]


def test_daily_revenue_is_two_decimal_string():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0.1")])
    assert r.daily["days"][0]["revenue"] == "0.10"
    assert isinstance(r.daily["days"][0]["revenue"], str)


def test_daily_revenue_exact_decimal():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="0.10"),
                 ev("e2", "bob", "09:00:00", "purchase", amount="0.20")])
    assert r.daily["days"][0]["revenue"] == "0.30"


def test_daily_days_ascending_and_only_days_with_sessions():
    r = run_job([ev("e1", "ann", "2026-03-05T09:00:00Z"), ev("e2", "ann", "2026-03-01T09:00:00Z"),
                 ev("e3", "ann", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_daily_session_counts_per_day():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e2", "ann", "2026-03-01T12:00:00Z"),
                 ev("e3", "ann", "2026-03-02T09:00:00Z")])
    assert [(d["date"], d["sessions"]) for d in r.daily["days"]] == [("2026-03-01", 2), ("2026-03-02", 1)]


def test_session_belongs_to_utc_date_of_start_across_midnight():
    # 23:50Z -> 00:10Z next day: one session starting on 03-01, so it lives on 03-01
    r = run_job([ev("e1", "ann", "2026-03-01T23:50:00Z", "purchase", amount="8"),
                 ev("e2", "ann", "2026-03-02T00:10:00Z")])
    assert len(r.sessions) == 1
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "8.00", "top_user": "ann"}]


def test_daily_date_is_utc_not_local_offset():
    # 2026-03-01T23:30-05:00 is 2026-03-02T04:30Z
    r = run_job([ev("e1", "ann", "2026-03-01T23:30:00-05:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-02"]


def test_daily_users_counted_per_day():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z"), ev("e2", "ann", "2026-03-02T09:00:00Z"),
                 ev("e3", "bob", "2026-03-02T10:00:00Z")])
    assert [(d["date"], d["users"]) for d in r.daily["days"]] == [("2026-03-01", 1), ("2026-03-02", 2)]


def test_daily_top_user_is_per_day():
    r = run_job([ev("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="50"),
                 ev("e2", "bob", "2026-03-02T09:00:00Z", "purchase", amount="1")])
    assert [(d["date"], d["top_user"]) for d in r.daily["days"]] == [("2026-03-01", "ann"), ("2026-03-02", "bob")]


def test_daily_top_user_is_lowercased():
    r = run_job([ev("e1", "  BoB ", "09:00:00", "purchase", amount="3")])
    assert r.daily["days"][0]["top_user"] == "bob"


def test_daily_duplicates_and_rejected_counts():
    r = run_job([ev("e1", "ann", "09:00:00"), ev("e1", "ann", "09:00:00"), "{x", ev("e2", "ann", "09:01:00", "x")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2


def test_daily_counts_zero_when_clean():
    r = run_job([ev("e1", "ann", "09:00:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 0


def test_daily_revenue_only_from_kept_events():
    r = run_job([ev("e1", "ann", "09:00:00", "purchase", amount="5"),
                 ev("e1", "ann", "09:00:00", "purchase", amount="5"),   # duplicate, dropped
                 ev("e2", "ann", "09:05:00", "purchase", amount="x")])  # rejected
    assert r.daily["days"][0]["revenue"] == "5.00"
    assert r.daily["days"][0]["sessions"] == 1


def test_daily_agrees_with_sessions_csv():
    lines = [ev("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="2"),
             ev("e2", "ann", "2026-03-01T10:00:00Z", "purchase", amount="3"),
             ev("e3", "bob", "2026-03-01T09:30:00Z", "purchase", amount="4"),
             ev("e4", "bob", "2026-03-02T09:30:00Z", "view")]
    r = run_job(lines)
    by_day = {}
    for s in r.sessions:
        by_day.setdefault(s["start"][:10], []).append(s)
    assert [d["date"] for d in r.daily["days"]] == sorted(by_day)
    for d in r.daily["days"]:
        ss = by_day[d["date"]]
        assert d["sessions"] == len(ss)
        assert d["users"] == len({s["user"] for s in ss})
        assert d["revenue"] == f"{sum(Decimal(s['revenue']) for s in ss):.2f}"


# ------------------------------------------------- whole-pipeline scenario ---

def test_combined_scenario():
    lines = [
        ev("e1", "Ann", "2026-03-01T09:00:00+07:00", "view"),                  # line 1: 02:00Z
        "",                                                                    # line 2
        ev("e2", "ann", "2026-03-01T02:20:00Z", "purchase", amount="9.99"),    # line 3
        "{broken",                                                             # line 4: bad_json
        ev("e3", "ann", "2026-03-01T03:00:01Z", "view"),                       # line 5: 40 min gap -> new session
        ev("e2", "ann", "2026-03-01T02:20:00Z", "purchase", amount="9.99"),    # line 6: duplicate
        ev("e4", "BOB", "2026-03-01T02:10:00Z", "purchase", amount="100"),     # line 7
        ev("e5", "bob", "2026-03-01T02:10:00Z", "purchase", amount="1.5"),     # line 8
        ev("e6", "bob", "2026-03-01T02:10:00Z", "purchase", amount="1.234"),   # line 9: bad_amount
        ev("e7", "cy", "2026-02-28T23:59:59Z", "click"),                       # line 10
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 9, "reason": "bad_amount"}]
    assert r.daily["rejected"] == 2
    assert r.daily["duplicates_dropped"] == 1
    assert sess_rows(r) == [
        ("cy-1", "cy", "2026-02-28T23:59:59Z", "2026-02-28T23:59:59Z", "0", "1", "0", "0.00"),
        ("ann-1", "ann", "2026-03-01T02:00:00Z", "2026-03-01T02:20:00Z", "1200", "2", "1", "9.99"),
        ("bob-1", "bob", "2026-03-01T02:10:00Z", "2026-03-01T02:10:00Z", "0", "2", "2", "101.50"),
        ("ann-2", "ann", "2026-03-01T03:00:01Z", "2026-03-01T03:00:01Z", "0", "1", "0", "0.00"),
    ]
    assert r.daily["days"] == [
        {"date": "2026-02-28", "sessions": 1, "users": 1, "revenue": "0.00", "top_user": None},
        {"date": "2026-03-01", "sessions": 3, "users": 2, "revenue": "111.49", "top_user": "bob"},
    ]
