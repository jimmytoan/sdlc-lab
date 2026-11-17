"""Behavioural test suite for the sessionize batch job, written against SPEC.md.

The product is run as a black box (subprocess), exactly like tests/test_public.py.
"""
import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import APP_DIR, ev, run_job, pick

JOB = os.path.join(APP_DIR, "job.py")
HEADER = "session_id,user,start,end,duration_s,events,purchases,revenue"
DAY = "2026-03-01"


# ---------------------------------------------------------------- helpers

def make(event_id="e1", user="ann", ts="2026-03-01T09:00:00Z", type="view", **extra):
    """Build an event as a dict (so tests can drop or corrupt single fields)."""
    d = {"event_id": event_id, "user": user, "ts": ts, "type": type}
    d.update(extra)
    return d


def as_line(item):
    return item if isinstance(item, str) else json.dumps(item)


class Run:
    """Result of one CLI invocation."""

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
    def csv_rows(self):
        return list(csv.reader(io.StringIO(self.csv_text)))

    @property
    def header(self):
        return self.csv_rows[0]

    @property
    def sessions(self):
        return [dict(zip(self.header, r)) for r in self.csv_rows[1:]]

    @property
    def daily(self):
        return json.loads(self.text("daily.json"))

    @property
    def rejects(self):
        return [json.loads(x) for x in self.text("rejects.jsonl").split("\n") if x.strip()]


def run_cli(tmp_path, lines=None, *, text=None, make_input=True, out=None, argv_order="in-first"):
    """Run job.py in tmp_path. `lines` are written joined by newlines (each item may be a dict or raw str)."""
    inp = tmp_path / "in"
    inp.mkdir(parents=True, exist_ok=True)
    out = Path(out) if out is not None else tmp_path / "out"
    if make_input:
        if text is None:
            body = "\n".join(as_line(x) for x in (lines or []))
            text = body + "\n" if lines else ""
        (inp / "events.jsonl").write_bytes(text.encode("utf-8"))
    args = ["--in", str(inp), "--out", str(out)]
    if argv_order == "out-first":
        args = ["--out", str(out), "--in", str(inp)]
    proc = subprocess.run([sys.executable, JOB, *args], capture_output=True, text=True,
                          timeout=60, cwd=str(tmp_path))
    return Run(proc, out)


def ok(tmp_path, lines):
    """Run and assert success; return the Run."""
    r = run_cli(tmp_path, lines)
    assert r.code == 0, r.stderr
    return r


def only_reject(tmp_path, line):
    """Run a single input line and assert it is rejected with some reason; return reason."""
    r = ok(tmp_path, [line])
    assert len(r.rejects) == 1, r.rejects
    assert r.rejects[0]["line"] == 1
    return r.rejects[0]["reason"]


def accepted(tmp_path, line):
    r = ok(tmp_path, [line])
    assert r.rejects == []
    assert len(r.sessions) == 1
    return r


# ------------------------------------------------- outputs, exit codes, files

def test_success_writes_exactly_three_files(tmp_path):
    r = ok(tmp_path, [make()])
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_csv_header_is_exact(tmp_path):
    r = ok(tmp_path, [make()])
    assert r.csv_text.splitlines()[0] == HEADER


def test_output_dir_created_when_missing_including_parents(tmp_path):
    out = tmp_path / "a" / "b" / "c"
    r = run_cli(tmp_path, [make()], out=out)
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_existing_output_dir_is_overwritten(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "sessions.csv").write_text("stale junk\n")
    (out / "daily.json").write_text("{}")
    (out / "rejects.jsonl").write_text('{"line": 99, "reason": "stale"}\n')
    r = run_cli(tmp_path, [make()], out=out)
    assert r.code == 0
    assert "stale" not in r.csv_text
    assert r.daily["rejected"] == 0
    assert r.rejects == []


def test_all_three_files_written_when_input_is_empty(tmp_path):
    r = run_cli(tmp_path, [])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.csv_rows == [HEADER.split(",")]
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    assert r.rejects == []


def test_input_with_only_blank_lines_writes_empty_results(tmp_path):
    r = run_cli(tmp_path, text="\n   \n\t\n")
    assert r.code == 0
    assert r.csv_rows == [HEADER.split(",")]
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    assert r.rejects == []


def test_exit_zero_when_lines_are_rejected(tmp_path):
    r = run_cli(tmp_path, ["{oops", make()])
    assert r.code == 0
    assert len(r.rejects) == 1


def test_missing_input_file_exits_2_with_stderr_message(tmp_path):
    r = run_cli(tmp_path, [], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""


def test_argument_order_does_not_matter(tmp_path):
    r = run_cli(tmp_path, [make()], argv_order="out-first")
    assert r.code == 0
    assert len(r.sessions) == 1


def test_output_is_deterministic(tmp_path):
    lines = [make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="3.50"),
             make("e2", "bob", "2026-03-01T10:00:00Z"), "{bad"]
    a = run_cli(tmp_path / "x", lines)
    b = run_cli(tmp_path / "y", lines)
    assert a.csv_text == b.csv_text
    assert a.text("daily.json") == b.text("daily.json")
    assert a.text("rejects.jsonl") == b.text("rejects.jsonl")


# ---------------------------------------------------------- line handling

def test_blank_and_whitespace_lines_are_skipped_but_counted(tmp_path):
    r = run_cli(tmp_path, ["", make("e1"), "   ", "{bad", ""])
    assert r.code == 0
    assert r.rejects == [{"line": 4, "reason": "bad_json"}]
    assert r.daily["rejected"] == 1
    assert len(r.sessions) == 1


def test_blank_lines_are_not_rejects(tmp_path):
    r = run_cli(tmp_path, ["", "", make()])
    assert r.rejects == []
    assert r.daily["rejected"] == 0


def test_line_numbers_are_physical_and_one_based(tmp_path):
    r = run_cli(tmp_path, [make("e1"), "", "{x", make("e2"), "[]", make("e3", type="dance")])
    assert r.rejects == [
        {"line": 3, "reason": "bad_json"},
        {"line": 5, "reason": "bad_json"},
        {"line": 6, "reason": "bad_type"},
    ]


def test_rejects_are_in_input_order(tmp_path):
    r = run_cli(tmp_path, ["{a", make("e1", type="nope"), "{b"])
    assert [x["line"] for x in r.rejects] == [1, 2, 3]


def test_rejects_file_has_one_object_per_line_with_exact_keys(tmp_path):
    r = run_cli(tmp_path, ["{a", make(type="nope")])
    raw = r.text("rejects.jsonl").splitlines()
    assert raw == ['{"line": 1, "reason": "bad_json"}', '{"line": 2, "reason": "bad_type"}']


def test_rejects_file_empty_when_nothing_rejected(tmp_path):
    r = ok(tmp_path, [make()])
    assert r.text("rejects.jsonl") == ""


def test_rejected_lines_take_no_part_in_sessions_or_duplicates(tmp_path):
    # the invalid copy of e1 must not cause the valid e1 to be treated as a duplicate
    r = ok(tmp_path, [make("e1", type="bogus"), make("e1", "ann", "2026-03-01T09:00:00Z")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "1"
    assert r.daily["rejected"] == 1


# ------------------------------------------------------ bad_json

@pytest.mark.parametrize("line", [
    "{not json",
    '{"event_id": "e1",',
    "[]",
    "[1, 2]",
    '"just a string"',
    "42",
    "null",
    "true",
])
def test_bad_json(tmp_path, line):
    assert only_reject(tmp_path, line) == "bad_json"


# ------------------------------------------------------ missing_field

@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_missing_field_when_absent(tmp_path, field):
    d = make()
    del d[field]
    assert only_reject(tmp_path, d) == "missing_field"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", [None, 123, 1.5, ["ann"], {"x": 1}, True])
def test_missing_field_when_not_a_string(tmp_path, field, value):
    d = make()
    d[field] = value
    assert only_reject(tmp_path, d) == "missing_field"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", ["", "   ", "\t \t"])
def test_missing_field_when_empty_after_trim(tmp_path, field, value):
    d = make()
    d[field] = value
    assert only_reject(tmp_path, d) == "missing_field"


def test_event_id_is_trimmed_for_emptiness_but_padded_value_is_valid(tmp_path):
    r = accepted(tmp_path, make(event_id="  e1  "))
    assert r.daily["duplicates_dropped"] == 0


# ------------------------------------------------------ bad_ts

@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",              # no offset
    "2026-03-01T09:00Z",                # no seconds
    "2026-03-01",                       # date only
    "2026-03-01 09:00:00Z",             # space separator
    "2026-02-29T09:00:00Z",             # not a leap year
    "2026-13-01T09:00:00Z",             # month 13
    "2026-03-01T24:00:00Z",             # hour 24
    "2026-03-01T09:61:00Z",             # minute 61
    "2026-03-01T09:00:00.Z",            # dot without digits
    "2026-03-01T09:00:00ZZ",            # doubled designator
    "2026-03-01T09:00:00+07:00 extra",  # trailing junk
    "yesterday",
])
def test_bad_ts(tmp_path, ts):
    assert only_reject(tmp_path, make(ts=ts)) == "bad_ts"


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00Z",
    "2026-03-01T09:00:00+07:00",
    "2026-03-01T09:00:00-05:30",
    "2026-03-01T09:00:00.250-05:30",
    "2026-03-01T09:00:00.123Z",
    "2028-02-29T09:00:00Z",
])
def test_valid_ts_forms_accepted(tmp_path, ts):
    accepted(tmp_path, make(ts=ts))


# ------------------------------------------------------ bad_type

@pytest.mark.parametrize("typ", ["view", "click", "purchase"])
def test_allowed_types_accepted(tmp_path, typ):
    extra = {"amount": "1"} if typ == "purchase" else {}
    accepted(tmp_path, make(type=typ, **extra))


@pytest.mark.parametrize("typ", ["logout", "refund", "Purchase", "VIEW", "page_view", "buy", "view "])
def test_bad_type(tmp_path, typ):
    assert only_reject(tmp_path, make(type=typ)) == "bad_type"


# ------------------------------------------------------ bad_amount

@pytest.mark.parametrize("amount", [
    None,          # present but null
    5,             # JSON number, not string
    19.99,
    True,
    "-1",
    "+5",
    "1e3",
    "abc",
    "",
    "19.999",
    "1,50",
    "1.2.3",
])
def test_bad_amount(tmp_path, amount):
    assert only_reject(tmp_path, make(type="purchase", amount=amount)) == "bad_amount"


def test_bad_amount_when_purchase_has_no_amount(tmp_path):
    assert only_reject(tmp_path, make(type="purchase")) == "bad_amount"


@pytest.mark.parametrize("amount", ["0", "5", "5.5", "19.99", "0.00", "1000000"])
def test_valid_amounts_accepted(tmp_path, amount):
    r = accepted(tmp_path, make(type="purchase", amount=amount))
    assert r.sessions[0]["purchases"] == "1"


@pytest.mark.parametrize("typ", ["view", "click"])
@pytest.mark.parametrize("amount", ["abc", "-5", 3, None, "1e3"])
def test_amount_ignored_for_non_purchase(tmp_path, typ, amount):
    r = accepted(tmp_path, make(type=typ, amount=amount))
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


# ------------------------------------------------------ check precedence

def test_bad_json_is_reported_even_if_nothing_else_is_checkable(tmp_path):
    assert only_reject(tmp_path, "{") == "bad_json"


def test_missing_field_beats_bad_ts(tmp_path):
    d = make(ts="nonsense")
    del d["user"]
    assert only_reject(tmp_path, d) == "missing_field"


def test_empty_event_id_beats_bad_ts(tmp_path):
    assert only_reject(tmp_path, make(event_id="  ", ts="nonsense")) == "missing_field"


def test_bad_ts_beats_bad_type(tmp_path):
    assert only_reject(tmp_path, make(ts="2026-03-01T09:00:00", type="dance")) == "bad_ts"


def test_bad_type_beats_bad_amount(tmp_path):
    assert only_reject(tmp_path, make(type="dance", amount="x")) == "bad_type"


def test_bad_ts_beats_bad_amount(tmp_path):
    assert only_reject(tmp_path, make(type="purchase", amount="x", ts="2026-03-01")) == "bad_ts"


def test_missing_field_beats_bad_amount(tmp_path):
    d = make(type="purchase", amount="x")
    del d["event_id"]
    assert only_reject(tmp_path, d) == "missing_field"


def test_missing_type_beats_bad_ts(tmp_path):
    d = make(ts="bad")
    del d["type"]
    assert only_reject(tmp_path, d) == "missing_field"


# ------------------------------------------------------ user / id normalisation

def test_user_trimmed_and_lowercased_into_one_user(tmp_path):
    r = ok(tmp_path, [make("e1", "  Ann "), make("e2", "ANN", "2026-03-01T09:01:00Z")])
    assert len(r.sessions) == 1
    s = r.sessions[0]
    assert s["user"] == "ann"
    assert s["session_id"] == "ann-1"
    assert s["events"] == "2"


def test_user_is_lowercase_in_output(tmp_path):
    r = ok(tmp_path, [make("e1", "BoB")])
    assert r.sessions[0]["user"] == "bob"
    assert r.sessions[0]["session_id"] == "bob-1"


def test_event_id_comparison_is_exact_and_case_sensitive(tmp_path):
    r = ok(tmp_path, [make("E1", "ann"), make("e1", "ann", "2026-03-01T09:01:00Z")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


def test_event_id_comparison_does_not_trim(tmp_path):
    r = ok(tmp_path, [make("e1", "ann"), make(" e1", "ann", "2026-03-01T09:01:00Z")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "2"


# ------------------------------------------------------ duplicates

def test_duplicate_dropped_and_counted(tmp_path):
    r = ok(tmp_path, [make("e1"), make("e1"), make("e2", ts="2026-03-01T09:01:00Z")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["events"] == "2"
    assert r.rejects == []


def test_several_copies_count_all_but_one(tmp_path):
    r = ok(tmp_path, [make("e1"), make("e1"), make("e1"), make("e1")])
    assert r.daily["duplicates_dropped"] == 3
    assert r.sessions[0]["events"] == "1"


def test_duplicate_keeps_earliest_timestamp_not_first_in_file(tmp_path):
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:30:00Z", "purchase", amount="7.00"),
        make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="7.00"),
    ])
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:00:00Z"


def test_duplicate_tie_keeps_first_in_file(tmp_path):
    r = ok(tmp_path, [make("e1", type="view"), make("e1", type="purchase", amount="4")])
    assert r.sessions[0]["purchases"] == "0"
    assert r.daily["duplicates_dropped"] == 1

    r2 = ok(tmp_path / "second", [make("e1", type="purchase", amount="4"), make("e1", type="view")])
    assert r2.sessions[0]["purchases"] == "1"
    assert r2.sessions[0]["revenue"] == "4.00"


def test_duplicate_comparison_uses_full_fractional_precision(tmp_path):
    # the kept copy is 09:00:00.100 (the later-in-file one). Gap from it to 09:30:00.150 is > 30 min.
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00.200Z"),
        make("e1", "ann", "2026-03-01T09:00:00.100Z"),
        make("e2", "ann", "2026-03-01T09:30:00.150Z"),
    ])
    assert len(r.sessions) == 2
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_equal_instants_in_different_offsets(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00+07:00"), make("e1", ts="2026-03-01T02:00:00Z")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["events"] == "1"


def test_duplicate_across_different_users_keeps_one(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", "2026-03-01T09:05:00Z"), make("e1", "bob", "2026-03-01T09:00:00Z")])
    assert r.daily["duplicates_dropped"] == 1
    assert pick(r.sessions, "user") == [("bob",)]


def test_invalid_copies_are_not_duplicates(tmp_path):
    r = ok(tmp_path, [make("e1", type="dance"), make("e1", "ann", "2026-03-01T09:00:00Z")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 1
    assert r.sessions[0]["events"] == "1"


def test_duplicates_are_not_rejects(tmp_path):
    r = ok(tmp_path, [make("e1"), make("e1")])
    assert r.rejects == []


def test_duplicate_purchase_revenue_counted_once(tmp_path):
    r = ok(tmp_path, [make("p", type="purchase", amount="10.00"), make("p", type="purchase", amount="10.00")])
    assert r.sessions[0]["purchases"] == "1"
    assert r.sessions[0]["revenue"] == "10.00"


# ------------------------------------------------------ sessions: gaps

def test_gap_of_exactly_30_minutes_stays_in_session(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00Z"), make("e2", ts="2026-03-01T09:30:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == "1800"


def test_gap_just_over_30_minutes_splits(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00.000Z"), make("e2", ts="2026-03-01T09:30:00.001Z")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T09:30:00Z")]


def test_gap_just_under_30_minutes_stays(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00.000Z"), make("e2", ts="2026-03-01T09:29:59.999Z")])
    assert len(r.sessions) == 1


def test_gap_of_one_hour_splits(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00Z"), make("e2", ts="2026-03-01T10:00:00Z")])
    assert len(r.sessions) == 2


def test_gap_is_between_consecutive_events_not_session_start(tmp_path):
    # 09:00 -> 09:25 -> 09:50: each gap is 25 min, so one session of 50 minutes
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00Z"), make("e2", ts="2026-03-01T09:25:00Z"),
                      make("e3", ts="2026-03-01T09:50:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["duration_s"] == "3000"
    assert r.sessions[0]["events"] == "3"


def test_gap_split_is_per_user(tmp_path):
    # bob's event at 09:10 must not split ann's 09:00 -> 09:25 session
    r = ok(tmp_path, [make("e1", "ann", "2026-03-01T09:00:00Z"), make("e2", "bob", "2026-03-01T09:10:00Z"),
                      make("e3", "ann", "2026-03-01T09:25:00Z")])
    assert pick(r.sessions, "session_id", "events") == [("ann-1", "2"), ("bob-1", "1")]


def test_events_are_ordered_by_time_not_file_order(tmp_path):
    r = ok(tmp_path, [make("e2", ts="2026-03-01T09:20:00Z"), make("e1", ts="2026-03-01T09:00:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["end"] == "2026-03-01T09:20:00Z"


def test_out_of_order_file_still_splits_on_gap(tmp_path):
    r = ok(tmp_path, [make("e2", ts="2026-03-01T10:00:00Z"), make("e1", ts="2026-03-01T09:00:00Z")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T10:00:00Z")]


def test_same_timestamp_same_user_is_one_session(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00Z"), make("e2", ts="2026-03-01T09:00:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[0]["duration_s"] == "0"


def test_gap_measured_on_utc_instants_across_offsets(tmp_path):
    # 10:00+07:00 is 03:00Z; 02:00Z is one hour earlier -> two sessions, ordered by UTC
    r = ok(tmp_path, [make("e1", ts="2026-03-01T10:00:00+07:00"), make("e2", ts="2026-03-01T02:00:00Z")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T02:00:00Z"), ("ann-2", "2026-03-01T03:00:00Z")]


def test_gap_across_offsets_within_30_minutes_stays(tmp_path):
    # 09:15+07:00 = 02:15Z ; 02:40Z is 25 min later
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:15:00+07:00"), make("e2", ts="2026-03-01T02:40:00Z")])
    assert len(r.sessions) == 1


def test_session_ids_count_from_one_in_time_order_per_user(tmp_path):
    r = ok(tmp_path, [
        make("e3", "ann", "2026-03-01T12:00:00Z"),
        make("e1", "ann", "2026-03-01T09:00:00Z"),
        make("e2", "ann", "2026-03-01T10:00:00Z"),
    ])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T10:00:00Z"), ("ann-3", "2026-03-01T12:00:00Z")]


def test_session_ids_restart_per_user(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", "2026-03-01T09:00:00Z"), make("e2", "bob", "2026-03-01T09:00:00Z"),
                      make("e3", "bob", "2026-03-01T11:00:00Z")])
    ids = sorted(s["session_id"] for s in r.sessions)
    assert ids == ["ann-1", "bob-1", "bob-2"]


def test_session_numbering_continues_across_days(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T23:00:00Z"), make("e2", ts="2026-03-02T09:00:00Z")])
    assert pick(r.sessions, "session_id", "start") == [
        ("ann-1", "2026-03-01T23:00:00Z"), ("ann-2", "2026-03-02T09:00:00Z")]


# ------------------------------------------------------ sessions: columns

def test_spec_example_row(tmp_path):
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00+07:00"),
        make("e2", "ann", "2026-03-01T09:05:00+07:00", "click"),
        make("e3", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99"),
    ])
    assert r.csv_rows[1] == ["ann-1", "ann", "2026-03-01T02:00:00Z", "2026-03-01T02:10:30Z",
                             "630", "3", "1", "19.99"]


def test_start_and_end_are_utc_with_z_suffix(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00-05:30")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T14:30:00Z"
    assert s["end"] == "2026-03-01T14:30:00Z"


def test_fractional_seconds_cut_off_in_start_and_end(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00.999Z"), make("e2", ts="2026-03-01T09:00:10.900Z")])
    s = r.sessions[0]
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:00:10Z"


def test_duration_rounds_down_using_full_precision(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00.000Z"), make("e2", ts="2026-03-01T09:00:10.900Z")])
    assert r.sessions[0]["duration_s"] == "10"


def test_duration_zero_for_sub_second_span(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00.900Z"), make("e2", ts="2026-03-01T09:00:01.000Z")])
    assert r.sessions[0]["duration_s"] == "0"


def test_duration_is_whole_seconds_from_first_to_last(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T09:00:00Z"), make("e2", ts="2026-03-01T09:29:00Z")])
    assert r.sessions[0]["duration_s"] == "1740"


def test_events_and_purchases_counts(tmp_path):
    r = ok(tmp_path, [make("e1", type="view"), make("e2", type="click", ts="2026-03-01T09:01:00Z"),
                      make("e3", type="purchase", amount="1", ts="2026-03-01T09:02:00Z"),
                      make("e4", type="purchase", amount="2", ts="2026-03-01T09:03:00Z")])
    s = r.sessions[0]
    assert s["events"] == "4"
    assert s["purchases"] == "2"
    assert s["revenue"] == "3.00"


@pytest.mark.parametrize("amount, expected", [
    ("0", "0.00"), ("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"), ("0.1", "0.10"), ("0.00", "0.00"),
])
def test_revenue_always_two_decimals(tmp_path, amount, expected):
    r = ok(tmp_path, [make("e1", type="purchase", amount=amount)])
    assert r.sessions[0]["revenue"] == expected


def test_session_without_purchases_has_zero_revenue(tmp_path):
    r = ok(tmp_path, [make("e1"), make("e2", type="click", ts="2026-03-01T09:01:00Z")])
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


def test_revenue_is_exact_decimal(tmp_path):
    r = ok(tmp_path, [make("e1", type="purchase", amount="0.10"),
                      make("e2", type="purchase", amount="0.20", ts="2026-03-01T09:01:00Z")])
    assert r.sessions[0]["revenue"] == "0.30"


def test_revenue_exact_for_large_values(tmp_path):
    # a float round-trip would lose the last cents here
    r = ok(tmp_path, [make("e1", type="purchase", amount="9007199254740993.01"),
                      make("e2", type="purchase", amount="0.01", ts="2026-03-01T09:01:00Z")])
    assert r.sessions[0]["revenue"] == "9007199254740993.02"


def test_csv_quotes_user_containing_comma(tmp_path):
    r = ok(tmp_path, [make("e1", user="Doe, Jane")])
    assert r.sessions[0]["user"] == "doe, jane"
    assert len(r.csv_rows[1]) == 8


# ------------------------------------------------------ sessions: ordering

def test_rows_ordered_by_start_then_user(tmp_path):
    r = ok(tmp_path, [make("e1", "zed", "2026-03-01T09:00:00Z"), make("e2", "bob", "2026-03-01T09:00:00Z"),
                      make("e3", "amy", "2026-03-01T08:00:00Z")])
    assert [s["user"] for s in r.sessions] == ["amy", "bob", "zed"]


def test_same_start_is_ordered_by_user_regardless_of_file_order(tmp_path):
    r = ok(tmp_path, [make("e1", "bob", "2026-03-01T09:00:00Z"), make("e2", "ann", "2026-03-01T09:00:00Z")])
    assert [s["user"] for s in r.sessions] == ["ann", "bob"]


def test_row_order_uses_full_precision_start(tmp_path):
    # both start at 09:00:00 when cut to seconds; ann starts later (.500) so bob comes first
    r = ok(tmp_path, [make("e1", "ann", "2026-03-01T09:00:00.500Z"), make("e2", "bob", "2026-03-01T09:00:00.100Z")])
    assert [s["user"] for s in r.sessions] == ["bob", "ann"]


def test_rows_sorted_by_start_across_users_with_interleaving(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", "2026-03-01T10:00:00Z"), make("e2", "bob", "2026-03-01T09:00:00Z"),
                      make("e3", "ann", "2026-03-01T08:00:00Z")])
    assert pick(r.sessions, "session_id") == [("ann-1",), ("bob-1",), ("ann-2",)]


# ------------------------------------------------------ daily: basics

def test_daily_keys_and_types(tmp_path):
    r = ok(tmp_path, [make("e1", type="purchase", amount="5")])
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    day = r.daily["days"][0]
    assert set(day) == {"date", "sessions", "users", "revenue", "top_user"}
    assert isinstance(day["revenue"], str)
    assert isinstance(day["sessions"], int)
    assert isinstance(day["users"], int)


def test_daily_summary_example(tmp_path):
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="5.00"),
        make("e2", "bob", "2026-03-01T09:00:00Z", "purchase", amount="20"),
        make("e3", "ann", "2026-03-02T09:00:00Z"),
    ])
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "25.00", "top_user": "bob"},
        {"date": "2026-03-02", "sessions": 1, "users": 1, "revenue": "0.00", "top_user": None},
    ]


def test_days_ascending_and_only_dates_with_sessions(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", "2026-03-03T09:00:00Z"), make("e2", "bob", "2026-03-01T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03"]


def test_daily_counts_duplicates_and_rejects(tmp_path):
    r = ok(tmp_path, [make("e1"), make("e1"), "{x", make("e2", type="nope"), make("e3", "bob")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2


def test_daily_sessions_and_users_counts(tmp_path):
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00Z"),
        make("e2", "ann", "2026-03-01T12:00:00Z"),   # second session of ann the same day
        make("e3", "bob", "2026-03-01T09:00:00Z"),
    ])
    day = r.daily["days"][0]
    assert day["sessions"] == 3
    assert day["users"] == 2


def test_daily_revenue_is_sum_of_session_revenues(tmp_path):
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="0.10"),
        make("e2", "bob", "2026-03-01T09:00:00Z", "purchase", amount="0.20"),
    ])
    assert r.daily["days"][0]["revenue"] == "0.30"


def test_daily_zero_revenue_formatted_with_two_decimals(tmp_path):
    r = ok(tmp_path, [make("e1")])
    assert r.daily["days"][0]["revenue"] == "0.00"


def test_day_of_two_sessions_is_one_user_entry_for_users(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="1"),
                      make("e2", "ann", "2026-03-01T15:00:00Z", "purchase", amount="2")])
    day = r.daily["days"][0]
    assert day["sessions"] == 2
    assert day["users"] == 1
    assert day["revenue"] == "3.00"
    assert day["top_user"] == "ann"


# ------------------------------------------------------ daily: dates and UTC

def test_session_belongs_to_utc_date_of_its_start_past_midnight(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T23:50:00Z"),
                      make("e2", ts="2026-03-02T00:10:00Z", type="purchase", amount="4")])
    assert r.sessions[0]["end"] == "2026-03-02T00:10:00Z"
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "4.00", "top_user": "ann"}]


def test_date_is_utc_not_local_offset_date(tmp_path):
    # 2026-03-02T00:30+01:00 is 2026-03-01T23:30Z
    r = ok(tmp_path, [make("e1", ts="2026-03-02T00:30:00+01:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]


def test_date_uses_start_not_end(tmp_path):
    # session runs 2026-03-01 23:50 -> 2026-03-02 00:20 (gap stays within 30 min)
    r = ok(tmp_path, [make("e1", ts="2026-03-01T23:50:00Z"), make("e2", ts="2026-03-02T00:15:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]
    assert r.daily["days"][0]["sessions"] == 1


def test_next_day_session_counted_on_its_own_date(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T23:50:00Z"), make("e2", ts="2026-03-02T00:30:01Z")])
    assert [(d["date"], d["sessions"]) for d in r.daily["days"]] == [("2026-03-01", 1), ("2026-03-02", 1)]


# ------------------------------------------------------ daily: top_user

def test_top_user_is_highest_revenue_that_day(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", type="purchase", amount="5"),
                      make("e2", "bob", type="purchase", amount="7")])
    assert r.daily["days"][0]["top_user"] == "bob"


def test_top_user_sums_user_sessions_that_day(tmp_path):
    # ann: 6 + 6 = 12 across two sessions; bob: 10 in one session
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="6"),
        make("e2", "ann", "2026-03-01T15:00:00Z", "purchase", amount="6"),
        make("e3", "bob", "2026-03-01T10:00:00Z", "purchase", amount="10"),
    ])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_top_user_tie_goes_to_name_that_sorts_first(tmp_path):
    r = ok(tmp_path, [make("e1", "zoe", type="purchase", amount="10"),
                      make("e2", "bob", type="purchase", amount="10")])
    assert r.daily["days"][0]["top_user"] == "bob"


def test_top_user_null_when_day_revenue_zero(tmp_path):
    r = ok(tmp_path, [make("e1", "ann", type="purchase", amount="0"), make("e2", "bob")])
    assert r.daily["days"][0]["top_user"] is None
    assert r.daily["days"][0]["revenue"] == "0.00"


def test_top_user_is_per_day(tmp_path):
    r = ok(tmp_path, [
        make("e1", "ann", "2026-03-01T09:00:00Z", "purchase", amount="50"),
        make("e2", "bob", "2026-03-02T09:00:00Z", "purchase", amount="1"),
    ])
    assert [d["top_user"] for d in r.daily["days"]] == ["ann", "bob"]


def test_top_user_ignores_zero_revenue_users_when_day_has_revenue(tmp_path):
    r = ok(tmp_path, [make("e1", "aaa"), make("e2", "bbb", type="purchase", amount="0.01")])
    assert r.daily["days"][0]["top_user"] == "bbb"


def test_top_user_is_lowercase(tmp_path):
    r = ok(tmp_path, [make("e1", "  BIG Spender ", type="purchase", amount="3")])
    assert r.daily["days"][0]["top_user"] == "big spender"


def test_daily_revenue_counts_purchase_after_midnight_for_start_date(tmp_path):
    r = ok(tmp_path, [make("e1", ts="2026-03-01T23:55:00Z"),
                      make("e2", ts="2026-03-02T00:05:00Z", type="purchase", amount="9.99")])
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "9.99", "top_user": "ann"}]


def test_empty_days_when_no_valid_events(tmp_path):
    r = ok(tmp_path, [make("e1", type="nope"), "{x"])
    assert r.daily["days"] == []
    assert r.daily["rejected"] == 2


# ------------------------------------------------------ spec-wide consistency

def test_end_to_end_mixed_input(tmp_path):
    lines = [
        make("p1", "Ann", "2026-03-01T09:00:00+07:00", "view"),          # 02:00Z
        "",
        make("p1", "Ann", "2026-03-01T09:00:00+07:00", "view"),          # duplicate, dropped
        make("c1", " ann ", "2026-03-01T02:10:00Z", "click"),            # same session as p1
        make("x1", "bob", "2026-03-01T02:05:00Z", "purchase", amount="12.5"),
        "{broken",                                                       # line 6
        make("x2", "bob", "2026-03-01T02:05:00Z", "purchase", amount="x"),  # line 7
        make("x3", "bob", "2026-03-01T03:30:00Z", "purchase", amount="0.50"),
        make("x4", "cy", "2026-03-01T23:59:00Z", "view"),
        make("x5", "cy", "2026-03-02T00:20:00Z", "purchase", amount="1"),  # 21 min gap, same session
    ]
    r = run_cli(tmp_path, lines)
    assert r.code == 0
    assert r.rejects == [{"line": 6, "reason": "bad_json"}, {"line": 7, "reason": "bad_amount"}]
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2
    assert pick(r.sessions, "session_id", "start", "end", "duration_s", "events", "purchases", "revenue") == [
        ("ann-1", "2026-03-01T02:00:00Z", "2026-03-01T02:10:00Z", "600", "2", "0", "0.00"),
        ("bob-1", "2026-03-01T02:05:00Z", "2026-03-01T02:05:00Z", "0", "1", "1", "12.50"),
        ("bob-2", "2026-03-01T03:30:00Z", "2026-03-01T03:30:00Z", "0", "1", "1", "0.50"),
        ("cy-1", "2026-03-01T23:59:00Z", "2026-03-02T00:20:00Z", "1260", "2", "1", "1.00"),
    ]
    # all four sessions start on 2026-03-01 UTC, including cy's that runs past midnight
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 4, "users": 3, "revenue": "14.00", "top_user": "bob"}]
