"""Regression suite for sessionize (job.py), written against SPEC.md.

Every test drives job.py as a black box via a subprocess and inspects the files it writes.
"""
import csv
import json
import os
import subprocess
import sys

import pytest

from helpers import APP_DIR, ev

JOB = os.path.join(APP_DIR, "job.py")
HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
DAY = "2026-03-01"


# ---------------------------------------------------------------- helpers

class Run:
    """Result of one job invocation plus accessors for the produced files."""

    def __init__(self, proc, out):
        self.code = proc.returncode
        self.stdout = proc.stdout.decode("utf-8", "replace")
        self.stderr = proc.stderr.decode("utf-8", "replace")
        self.out = out

    def path(self, name):
        return os.path.join(self.out, name)

    def files(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def read_bytes(self, name):
        with open(self.path(name), "rb") as f:
            return f.read()

    @property
    def header(self):
        with open(self.path("sessions.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        return rows[0] if rows else []

    @property
    def sessions(self):
        with open(self.path("sessions.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        return [dict(zip(rows[0], r)) for r in rows[1:] if r]

    @property
    def daily(self):
        with open(self.path("daily.json"), encoding="utf-8") as f:
            return json.load(f)

    @property
    def rejects(self):
        with open(self.path("rejects.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().split("\n") if x.strip()]


def run_bytes(tmp_path, data, out_name="out", cwd=None, make_input=True, extra=()):
    inp = tmp_path / "in"
    inp.mkdir(exist_ok=True)
    if make_input:
        (inp / "events.jsonl").write_bytes(data)
    out = tmp_path / out_name
    proc = subprocess.run(
        [sys.executable, JOB, "--in", str(inp), "--out", str(out), *extra],
        capture_output=True, timeout=60, cwd=str(cwd or tmp_path))
    return Run(proc, str(out))


def run_lines(tmp_path, lines, **kw):
    text = "\n".join(lines) + ("\n" if lines else "")
    return run_bytes(tmp_path, text.encode("utf-8"), **kw)


def E(event_id, user, ts, type="view", **extra):
    """Event line with a full timestamp (or HH:MM:SS on 2026-03-01 when no 'T' present)."""
    return ev(event_id, user, ts, type, **extra)


def RAW(event_id, user, ts, type="view", **extra):
    """Event line with the timestamp used exactly as given (no HH:MM:SS expansion)."""
    return json.dumps({"event_id": event_id, "user": user, "ts": ts, "type": type, **extra})


def sess_tuples(run, *cols):
    return [tuple(s[c] for c in cols) for s in run.sessions]


# ---------------------------------------------------------------- CLI and files

def test_writes_three_files_with_header_and_exit_zero(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "ann", "09:05:00", "click"),
                             E("e3", "ann", "09:10:30", "purchase", amount="19.99")])
    assert r.code == 0, r.stderr
    assert r.files() == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER
    assert r.sessions[0]["duration_s"] == "630"


def test_entry_point_works_from_any_working_directory(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00")], cwd=other)
    assert r.code == 0, r.stderr
    assert len(r.sessions) == 1


def test_output_dir_created_when_missing_including_parents(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00")], out_name="a/b/c")
    assert r.code == 0, r.stderr
    assert r.files() == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_existing_output_dir_is_overwritten(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "sessions.csv").write_text("stale\n")
    (out / "daily.json").write_text("{}")
    (out / "rejects.jsonl").write_text('{"line": 99, "reason": "stale"}\n')
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), "{bad"])
    assert r.code == 0
    assert "stale" not in r.read_bytes("sessions.csv").decode()
    assert r.daily["rejected"] == 1
    assert r.rejects == [{"line": 2, "reason": "bad_json"}]


def test_output_is_deterministic(tmp_path):
    lines = [E("e2", "bob", "10:00:00", "purchase", amount="3"), E("e1", "ann", "09:00:00"),
             E("e1", "ann", "09:00:00"), "{bad", E("e3", "Ann", "09:50:00", "purchase", amount="1.5")]
    a = run_lines(tmp_path, lines, out_name="o1")
    b = run_lines(tmp_path, lines, out_name="o2")
    for name in ("sessions.csv", "daily.json", "rejects.jsonl"):
        assert a.read_bytes(name) == b.read_bytes(name)


def test_missing_events_file_exits_2_with_stderr(tmp_path):
    r = run_bytes(tmp_path, b"", make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""
    assert "events.jsonl" in r.stderr


def test_missing_input_directory_exits_2(tmp_path):
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, JOB, "--in", str(tmp_path / "nope"), "--out", str(out)],
                          capture_output=True, timeout=60, cwd=str(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.strip() != ""


def test_empty_input_file_still_writes_all_three_files(tmp_path):
    r = run_bytes(tmp_path, b"")
    assert r.code == 0, r.stderr
    assert r.files() == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_whitespace_only_input_still_writes_all_three_files(tmp_path):
    r = run_lines(tmp_path, ["", "   ", "\t"])
    assert r.code == 0
    assert r.files() == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.sessions == [] and r.rejects == []
    assert r.daily["days"] == []


def test_blank_lines_skipped_but_counted_in_line_numbers(tmp_path):
    r = run_lines(tmp_path, ["", "   ", "\t", "{bad", "", E("e1", "ann", "09:00:00"), "not json"])
    assert r.code == 0
    assert r.rejects == [{"line": 4, "reason": "bad_json"}, {"line": 7, "reason": "bad_json"}]
    assert r.daily["rejected"] == 2


def test_last_line_without_trailing_newline_is_processed(tmp_path):
    r = run_bytes(tmp_path, (E("e1", "ann", "09:00:00") + "\n" + E("e2", "ann", "09:01:00")).encode())
    assert r.code == 0
    assert r.sessions[0]["events"] == "2"


def test_crlf_line_endings_are_accepted_with_same_line_numbers(tmp_path):
    data = (E("e1", "ann", "09:00:00") + "\r\n" + "{bad\r\n" + E("e2", "ann", "09:01:00") + "\r\n").encode()
    r = run_bytes(tmp_path, data)
    assert r.code == 0
    assert r.rejects == [{"line": 2, "reason": "bad_json"}]
    assert r.sessions[0]["events"] == "2"


def test_utf8_user_names_are_lowercased_and_written_as_utf8(tmp_path):
    r = run_lines(tmp_path, [E("e1", "Zoë", "09:00:00"), E("e2", "ÜNAL", "09:00:00")])
    assert r.code == 0
    assert sorted(s["user"] for s in r.sessions) == ["zoë", "ünal"]
    assert sorted(s["session_id"] for s in r.sessions) == ["zoë-1", "ünal-1"]


# ---------------------------------------------------------------- input rules: rejects

@pytest.mark.parametrize("bad", [
    "{not json",
    '{"event_id": "e1"',
    "[1, 2]",
    '"just a string"',
    "123",
    "null",
    "true",
    "[]",
])
def test_bad_json_lines_rejected(tmp_path, bad):
    r = run_lines(tmp_path, [bad])
    assert r.code == 0
    assert r.rejects == [{"line": 1, "reason": "bad_json"}]
    assert r.sessions == []


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_missing_required_field_rejected(tmp_path, field):
    obj = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    del obj[field]
    r = run_lines(tmp_path, [json.dumps(obj)])
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


@pytest.mark.parametrize("obj", [
    {"event_id": 5, "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": None, "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": 1772355600, "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": ["view"]},
    {"event_id": "  ", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": " \t ", "ts": "2026-03-01T09:00:00Z", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "   ", "type": "view"},
    {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": ""},
])
def test_wrong_type_or_empty_after_trim_is_missing_field(tmp_path, obj):
    r = run_lines(tmp_path, [json.dumps(obj)])
    assert r.code == 0
    assert r.rejects == [{"line": 1, "reason": "missing_field"}]


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",            # no offset
    "2026-03-01T09:00:00.250",        # fraction but no offset
    "2026-03-01T09:00Z",              # no seconds
    "2026-03-01T09Z",
    "2026-03-01",                     # date only
    "09:00:00Z",                      # time only
    "2026-03-01 09:00:00Z",           # space instead of T
    "2026-02-30T09:00:00Z",           # impossible day
    "2025-02-29T00:00:00Z",           # not a leap year
    "2026-13-01T09:00:00Z",           # bad month
    "2026-03-01T25:00:00Z",           # bad hour
    "2026-03-01T09:60:00Z",           # bad minute
    "2026-03-01T09:00:00.Z",          # empty fraction
    "2026-03-01T09:00:00+7:00",       # offset not two digits
    "2026-03-01T09:00:00+07",         # offset without minutes
    "not a timestamp",
])
def test_unparseable_ts_is_bad_ts(tmp_path, ts):
    r = run_lines(tmp_path, [RAW("e1", "ann", ts)])
    assert r.code == 0
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]
    assert r.daily["rejected"] == 1


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00Z",
    "2026-03-01T09:00:00+07:00",
    "2026-03-01T09:00:00-05:30",
    "2026-03-01T09:00:00.250-05:30",
    "2026-03-01T09:00:00.5Z",
    "2024-02-29T09:00:00Z",           # leap day is valid
])
def test_valid_ts_forms_are_accepted(tmp_path, ts):
    r = run_lines(tmp_path, [RAW("e1", "ann", ts)])
    assert r.code == 0
    assert r.rejects == []
    assert len(r.sessions) == 1


@pytest.mark.parametrize("typ", ["dance", "Purchase", "VIEW", "refund", "logout", "impression", "1"])
def test_unknown_type_is_bad_type(tmp_path, typ):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", typ)])
    assert r.rejects == [{"line": 1, "reason": "bad_type"}]
    assert r.sessions == []


@pytest.mark.parametrize("amount", [
    "-1", "1.234", "abc", "", "1e3", "1,00", "1.", " ", "--5", "5.5.5",
    19.99, 5, None, True, ["5"],
])
def test_invalid_purchase_amount_is_bad_amount(tmp_path, amount):
    r = run_lines(tmp_path, [json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z",
                                         "type": "purchase", "amount": amount})])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]


def test_purchase_without_amount_is_bad_amount(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase")])
    assert r.rejects == [{"line": 1, "reason": "bad_amount"}]


@pytest.mark.parametrize("amount,expected_revenue", [
    ("5", "5.00"), ("5.5", "5.50"), ("19.99", "19.99"), ("0", "0.00"), ("0.00", "0.00"), ("0.1", "0.10"),
])
def test_valid_purchase_amounts_accepted(tmp_path, amount, expected_revenue):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount=amount)])
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == expected_revenue


def test_amount_is_ignored_for_non_purchase_types(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "view", amount="abc"),
                             E("e2", "ann", "09:01:00", "click", amount=-7),
                             E("e3", "ann", "09:02:00", "view", amount="50")])
    assert r.code == 0
    assert r.rejects == []
    assert r.sessions[0]["revenue"] == "0.00"
    assert r.sessions[0]["purchases"] == "0"


@pytest.mark.parametrize("line,reason", [
    # missing_field beats every later check
    ({"event_id": "e1", "user": "", "ts": "bogus", "type": "dance"}, "missing_field"),
    ({"event_id": "e1", "ts": "bogus", "type": "view"}, "missing_field"),
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z"}, "missing_field"),
    # bad_ts beats bad_type and bad_amount
    ({"event_id": "e1", "user": "ann", "ts": "bogus", "type": "dance"}, "bad_ts"),
    ({"event_id": "e1", "user": "ann", "ts": "bogus", "type": "purchase", "amount": "x"}, "bad_ts"),
    ({"event_id": "e1", "user": "ann", "ts": "bogus", "type": "purchase"}, "bad_ts"),
    # bad_type beats bad_amount
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "dance", "amount": "x"}, "bad_type"),
    ({"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "refund", "amount": "-1"}, "bad_type"),
])
def test_first_failing_check_is_reported(tmp_path, line, reason):
    r = run_lines(tmp_path, [json.dumps(line)])
    assert r.rejects == [{"line": 1, "reason": reason}]


def test_rejects_are_in_input_order_with_physical_line_numbers(tmp_path):
    r = run_lines(tmp_path, [
        E("e1", "ann", "09:00:00"),          # 1 ok
        "{oops",                              # 2 bad_json
        E("e2", "ann", "09:01:00", "dance"),  # 3 bad_type
        "",                                   # 4 blank
        E("e3", "ann", "nope", "view"),       # 5 bad_ts
        E("e4", "", "09:02:00"),              # 6 missing_field
        E("e5", "ann", "09:03:00", "purchase", amount="1.001"),  # 7 bad_amount
        E("e6", "ann", "09:04:00"),           # 8 ok
    ])
    assert r.code == 0
    assert r.rejects == [
        {"line": 2, "reason": "bad_json"},
        {"line": 3, "reason": "bad_type"},
        {"line": 5, "reason": "bad_ts"},
        {"line": 6, "reason": "missing_field"},
        {"line": 7, "reason": "bad_amount"},
    ]
    assert r.daily["rejected"] == 5
    assert r.sessions[0]["events"] == "2"


def test_reject_records_have_exactly_line_and_reason(tmp_path):
    r = run_lines(tmp_path, ["{x"])
    with open(r.path("rejects.jsonl"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"line": 1, "reason": "bad_json"}


def test_rejects_file_present_and_empty_when_nothing_rejected(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00")])
    assert r.code == 0
    assert r.read_bytes("rejects.jsonl") == b""


def test_rejected_line_does_not_take_part_in_dedupe(tmp_path):
    # The earlier copy is invalid (no offset), so the valid later copy is the only one kept.
    r = run_lines(tmp_path, [
        json.dumps({"event_id": "e1", "user": "ann", "ts": "2026-03-01T08:00:00", "type": "view"}),
        E("e1", "ann", "09:00:00", "purchase", amount="4"),
    ])
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["start"] == "2026-03-01T09:00:00Z"
    assert r.sessions[0]["purchases"] == "1"


def test_bad_lines_do_not_stop_processing_of_good_lines(tmp_path):
    r = run_lines(tmp_path, ["{", E("e1", "ann", "09:00:00"), "]"])
    assert r.code == 0
    assert len(r.sessions) == 1 and r.sessions[0]["events"] == "1"


# ---------------------------------------------------------------- duplicates

def test_duplicate_with_later_timestamp_is_dropped_earliest_kept(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:05:00", "click"),
                             E("e1", "ann", "09:00:00", "purchase", amount="5")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.rejects == []
    s = r.sessions[0]
    assert (s["start"], s["events"], s["purchases"], s["revenue"]) == ("2026-03-01T09:00:00Z", "1", "1", "5.00")


def test_equal_timestamp_duplicates_keep_first_in_file(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="5"),
                             E("e1", "ann", "09:00:00", "view")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["purchases"] == "1"

    r2 = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "view"),
                              E("e1", "ann", "09:00:00", "purchase", amount="5")], out_name="o2")
    assert r2.sessions[0]["purchases"] == "0"
    assert r2.sessions[0]["revenue"] == "0.00"


def test_equal_instant_with_different_offsets_is_a_tie(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00+07:00", "view"),
                             E("e1", "ann", "2026-03-01T02:00:00Z", "purchase", amount="5")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["purchases"] == "0"


def test_earlier_instant_with_other_offset_wins(tmp_path):
    # 09:00 at -05:00 is 14:00Z, later than 02:00Z; the 02:00Z copy must win.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00-05:00", "purchase", amount="5"),
                             E("e1", "ann", "2026-03-01T02:00:00Z", "view")])
    assert r.sessions[0]["purchases"] == "0"
    assert r.sessions[0]["start"] == "2026-03-01T02:00:00Z"


def test_multiple_copies_all_counted_as_dropped(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00")] * 3 + [E("e2", "ann", "09:01:00")])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "2"


def test_duplicate_event_id_across_users_is_a_duplicate(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e1", "bob", "09:30:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert sess_tuples(r, "user") == [("ann",)]


def test_event_id_match_is_exact(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("E1", "ann", "09:01:00"),
                             E("e1 ", "ann", "09:02:00")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "3"


def test_dedupe_happens_before_sessionising(tmp_path):
    # Without dedupe the gaps would be 09:00->09:20->09:55 (35 min) and split the session.
    r = run_lines(tmp_path, [E("e2", "ann", "09:00:00"), E("e1", "ann", "09:55:00"),
                             E("e1", "ann", "09:20:00")])
    assert r.daily["duplicates_dropped"] == 1
    assert sess_tuples(r, "session_id", "events") == [("ann-1", "2")]


def test_duplicates_dropped_not_counted_as_rejects(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e1", "ann", "09:00:00"), "{x"])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 1
    assert r.rejects == [{"line": 3, "reason": "bad_json"}]


# ---------------------------------------------------------------- sessions

def test_gap_of_exactly_30_minutes_stays_in_session(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "ann", "09:30:00")])
    assert sess_tuples(r, "session_id", "events", "duration_s") == [("ann-1", "2", "1800")]


def test_gap_just_over_30_minutes_splits(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00.000Z"),
                             E("e2", "ann", "2026-03-01T09:30:00.001Z")])
    assert sess_tuples(r, "session_id", "start") == [("ann-1", "2026-03-01T09:00:00Z"),
                                                     ("ann-2", "2026-03-01T09:30:00Z")]


def test_gap_of_exactly_30_minutes_with_fractions_stays_in_session(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00.500Z"),
                             E("e2", "ann", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 1


def test_sub_second_excess_over_30_minutes_splits(tmp_path):
    # The gap is 30 min 0.5 s; cut-off at whole seconds must not hide it.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00.000Z"),
                             E("e2", "ann", "2026-03-01T09:30:00.500Z")])
    assert len(r.sessions) == 2


def test_gap_is_measured_from_previous_event_not_session_start(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "ann", "09:25:00"),
                             E("e3", "ann", "09:50:00"), E("e4", "ann", "10:15:00")])
    assert sess_tuples(r, "session_id", "duration_s", "events") == [("ann-1", "4500", "4")]


def test_gap_measured_in_utc_across_offsets(tmp_path):
    # 09:00+07:00 is 02:00Z; 02:20Z is 20 minutes later -> same session.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00+07:00"),
                             E("e2", "ann", "2026-03-01T02:20:00Z")])
    assert len(r.sessions) == 1
    r2 = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00+07:00"),
                              E("e2", "ann", "2026-03-01T02:31:00Z")], out_name="o2")
    assert len(r2.sessions) == 2


def test_gaps_are_per_user(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "bob", "09:10:00"),
                             E("e3", "ann", "09:20:00"), E("e4", "bob", "10:00:00")])
    assert sess_tuples(r, "session_id", "user", "events") == [
        ("ann-1", "ann", "2"), ("bob-1", "bob", "1"), ("bob-2", "bob", "1")]


def test_session_ids_count_per_user_in_time_order(tmp_path):
    r = run_lines(tmp_path, [E("e1", "bob", "08:00:00"), E("e2", "ann", "12:00:00"),
                             E("e3", "ann", "09:00:00"), E("e4", "ann", "10:30:00"),
                             E("e5", "bob", "13:00:00")])
    ids = sorted(s["session_id"] for s in r.sessions if s["user"] == "ann")
    assert ids == ["ann-1", "ann-2", "ann-3"]
    assert sorted(s["session_id"] for s in r.sessions if s["user"] == "bob") == ["bob-1", "bob-2"]
    assert [s["start"] for s in r.sessions if s["user"] == "ann"] == [
        "2026-03-01T09:00:00Z", "2026-03-01T10:30:00Z", "2026-03-01T12:00:00Z"]


def test_events_processed_in_timestamp_order_not_file_order(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "10:00:00"), E("e2", "ann", "09:00:00"),
                             E("e3", "ann", "09:40:00")])
    # Sorted: 09:00 (e2), 09:40 (e3), 10:00 (e1). The 40-min gap splits; 20-min gap does not.
    assert sess_tuples(r, "session_id", "start", "end", "events") == [
        ("ann-1", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", "1"),
        ("ann-2", "2026-03-01T09:40:00Z", "2026-03-01T10:00:00Z", "2")]


def test_user_names_trimmed_and_lowercased(tmp_path):
    r = run_lines(tmp_path, [E("e1", "  Ann ", "09:00:00"), E("e2", "ANN", "09:05:00"),
                             E("e3", "ann\t", "09:10:00")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "ann"
    assert r.sessions[0]["session_id"] == "ann-1"


def test_user_with_inner_space_keeps_space_in_id(tmp_path):
    r = run_lines(tmp_path, [E("e1", "Ann Lee", "09:00:00")])
    assert r.sessions[0]["user"] == "ann lee"
    assert r.sessions[0]["session_id"] == "ann lee-1"


def test_user_comparison_is_exact_after_trim_and_lowercase(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "ann.", "09:01:00")])
    assert sorted(s["user"] for s in r.sessions) == ["ann", "ann."]


def test_single_event_session_has_zero_duration(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00")])
    assert r.header == HEADER
    s = r.sessions[0]
    assert s == {"session_id": "ann-1", "user": "ann", "start": "2026-03-01T09:00:00Z",
                 "end": "2026-03-01T09:00:00Z", "duration_s": "0", "events": "1",
                 "purchases": "0", "revenue": "0.00"}


def test_duration_rounded_down_to_whole_seconds(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00Z"),
                             E("e2", "ann", "2026-03-01T09:10:30.900Z")])
    assert r.sessions[0]["duration_s"] == "630"
    assert r.sessions[0]["end"] == "2026-03-01T09:10:30Z"


def test_sub_second_duration_is_zero_and_fraction_cut_from_output(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00.900Z"),
                             E("e2", "ann", "2026-03-01T09:00:01.100Z")])
    s = r.sessions[0]
    assert s["duration_s"] == "0"
    assert s["start"] == "2026-03-01T09:00:00Z"
    assert s["end"] == "2026-03-01T09:00:01Z"


def test_duration_uses_full_precision_then_floors(tmp_path):
    # 09:00:00.999 -> 09:00:05.250 is 4.251 s, so 4 s; the output start is cut to 09:00:00.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00.999Z"),
                             E("e2", "ann", "2026-03-01T09:00:05.250Z")])
    s = r.sessions[0]
    assert s["duration_s"] == "4"
    assert (s["start"], s["end"]) == ("2026-03-01T09:00:00Z", "2026-03-01T09:00:05Z")


def test_timestamps_output_in_utc(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00+07:00"),
                             E("e2", "ann", "2026-03-01T09:10:00+07:00")])
    s = r.sessions[0]
    assert (s["start"], s["end"]) == ("2026-03-01T02:00:00Z", "2026-03-01T02:10:00Z")


def test_session_crossing_midnight_is_one_session(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T23:50:00Z"),
                             E("e2", "ann", "2026-03-02T00:20:00Z")])
    s = r.sessions[0]
    assert len(r.sessions) == 1
    assert (s["start"], s["end"], s["duration_s"]) == ("2026-03-01T23:50:00Z", "2026-03-02T00:20:00Z", "1800")


def test_counts_events_and_purchases_and_revenue(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "ann", "09:01:00", "click"),
                             E("e3", "ann", "09:02:00", "purchase", amount="19.99"),
                             E("e4", "ann", "09:03:00", "purchase", amount="0.01"),
                             E("e5", "ann", "09:04:00", "purchase", amount="0")])
    s = r.sessions[0]
    assert (s["events"], s["purchases"], s["revenue"]) == ("5", "3", "20.00")


def test_revenue_is_exact_decimal_arithmetic(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="0.10"),
                             E("e2", "ann", "09:01:00", "purchase", amount="0.20")])
    assert r.sessions[0]["revenue"] == "0.30"


def test_revenue_exact_for_large_amounts(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="99999999999999.99"),
                             E("e2", "ann", "09:01:00", "purchase", amount="0.01")])
    assert r.sessions[0]["revenue"] == "100000000000000.00"


def test_revenue_always_has_two_decimals(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="7"),
                             E("e2", "bob", "09:00:00", "purchase", amount="2.5")])
    assert sorted(s["revenue"] for s in r.sessions) == ["2.50", "7.00"]


def test_session_without_purchases_has_zero_revenue(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "click")])
    assert r.sessions[0]["revenue"] == "0.00"


def test_session_rows_ordered_by_full_precision_start(tmp_path):
    # Both print as 09:00:00Z, but bob's start is earlier at full precision.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T09:00:00.900Z"),
                             E("e2", "bob", "2026-03-01T09:00:00.100Z")])
    assert [s["user"] for s in r.sessions] == ["bob", "ann"]


def test_session_rows_tied_start_ordered_by_user(tmp_path):
    r = run_lines(tmp_path, [E("e1", "zed", "09:00:00"), E("e2", "bob", "09:00:00"),
                             E("e3", "amy", "09:00:00")])
    assert [s["user"] for s in r.sessions] == ["amy", "bob", "zed"]


def test_session_rows_ordered_by_start_across_users(tmp_path):
    r = run_lines(tmp_path, [E("e1", "amy", "11:00:00"), E("e2", "zed", "08:00:00"),
                             E("e3", "bob", "09:30:00")])
    assert [s["user"] for s in r.sessions] == ["zed", "bob", "amy"]


def test_header_is_exact(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00")])
    assert r.header == ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def test_user_containing_comma_is_csv_quoted(tmp_path):
    r = run_lines(tmp_path, [E("e1", "a,b", "09:00:00")])
    assert r.code == 0
    assert r.sessions[0]["user"] == "a,b"
    assert r.sessions[0]["session_id"] == "a,b-1"


# ---------------------------------------------------------------- daily summary

def test_daily_summary_top_level_keys(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="1")])
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}


def test_daily_summary_matches_spec_shape_and_values(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="5.00"),
                             E("e2", "bob", "09:00:00", "purchase", amount="20"),
                             E("e3", "ann", "2026-03-02T09:00:00Z"),
                             E("e3", "ann", "2026-03-02T09:00:00Z"),
                             "{x"])
    assert r.daily == {
        "days": [
            {"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "25.00", "top_user": "bob"},
            {"date": "2026-03-02", "sessions": 1, "users": 1, "revenue": "0.00", "top_user": None},
        ],
        "duplicates_dropped": 1,
        "rejected": 1,
    }


def test_day_is_utc_date_of_session_start(tmp_path):
    # 23:50Z starts on 03-01 and runs past midnight: it belongs to 03-01 only.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T23:50:00Z", "purchase", amount="3"),
                             E("e2", "ann", "2026-03-02T00:20:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01"]
    assert r.daily["days"][0]["sessions"] == 1


def test_day_uses_utc_not_local_date(tmp_path):
    # 23:30 at -05:00 is 04:30Z on 03-02.
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T23:30:00-05:00")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-02"]


def test_days_listed_ascending_and_only_days_with_sessions(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-05T09:00:00Z"),
                             E("e2", "ann", "2026-03-01T09:00:00Z"),
                             E("e3", "bob", "2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_day_counts_sessions_and_distinct_users(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "ann", "10:00:00"),
                             E("e3", "ann", "10:05:00"), E("e4", "bob", "09:30:00")])
    d = r.daily["days"][0]
    assert d["sessions"] == 3
    assert d["users"] == 2


def test_day_revenue_is_sum_of_session_revenue(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00", "purchase", amount="0.10"),
                             E("e2", "ann", "10:00:00", "purchase", amount="0.20"),
                             E("e3", "bob", "09:00:00", "purchase", amount="0.70")])
    assert r.daily["days"][0]["revenue"] == "1.00"


def test_top_user_is_highest_total_revenue_across_sessions(tmp_path):
    # ann: two sessions of 3 each = 6; bob: one session of 5. ann wins on the day total.
    r = run_lines(tmp_path, [E("a1", "ann", "09:00:00", "purchase", amount="3"),
                             E("a2", "ann", "11:00:00", "purchase", amount="3"),
                             E("b1", "bob", "09:00:00", "purchase", amount="5")])
    assert r.daily["days"][0]["top_user"] == "ann"


def test_top_user_tie_goes_to_name_that_sorts_first(tmp_path):
    r = run_lines(tmp_path, [E("e1", "zed", "09:00:00", "purchase", amount="5"),
                             E("e2", "amy", "09:00:00", "purchase", amount="5")])
    assert r.daily["days"][0]["top_user"] == "amy"


def test_top_user_ignores_users_with_zero_revenue(tmp_path):
    r = run_lines(tmp_path, [E("e1", "amy", "09:00:00"),
                             E("e2", "zed", "09:00:00", "purchase", amount="0.01")])
    assert r.daily["days"][0]["top_user"] == "zed"


def test_top_user_null_when_day_revenue_is_zero(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e2", "bob", "09:00:00", "click"),
                             E("e3", "cy", "09:00:00", "purchase", amount="0")])
    d = r.daily["days"][0]
    assert d["revenue"] == "0.00"
    assert d["top_user"] is None
    assert r.read_bytes("daily.json").decode().count("null") == 1


def test_day_revenue_attributed_to_session_start_day(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "2026-03-01T23:50:00Z"),
                             E("e2", "ann", "2026-03-02T00:05:00Z", "purchase", amount="9.00")])
    days = {d["date"]: d for d in r.daily["days"]}
    assert days["2026-03-01"]["revenue"] == "9.00"
    assert days["2026-03-01"]["top_user"] == "ann"
    assert "2026-03-02" not in days


def test_daily_counts_rejected_lines_and_dropped_duplicates(tmp_path):
    r = run_lines(tmp_path, [E("e1", "ann", "09:00:00"), E("e1", "ann", "09:00:00"),
                             E("e2", "ann", "09:00:00", "dance"), "{", ""])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 2


def test_empty_days_list_when_only_rejects(tmp_path):
    r = run_lines(tmp_path, ["{", E("e1", "ann", "nope")])
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 2}
    assert r.sessions == []


# ---------------------------------------------------------------- end-to-end

def test_full_example_from_spec(tmp_path):
    r = run_lines(tmp_path, [
        E("e1", "Ann", "2026-03-01T09:00:00+07:00", "view"),
        E("e2", "ann", "2026-03-01T09:05:00+07:00", "click"),
        E("e3", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99"),
        E("e3", "ann", "2026-03-01T09:10:30+07:00", "purchase", amount="19.99"),
        E("e4", "Bob", "2026-03-01T02:00:00Z", "purchase", amount="5"),
        "{bad json",
    ])
    assert r.code == 0
    assert r.header == HEADER
    by_id = {s["session_id"]: s for s in r.sessions}
    assert by_id["ann-1"] == {"session_id": "ann-1", "user": "ann", "start": "2026-03-01T02:00:00Z",
                              "end": "2026-03-01T02:10:30Z", "duration_s": "630", "events": "3",
                              "purchases": "1", "revenue": "19.99"}
    assert by_id["bob-1"]["revenue"] == "5.00"
    assert [s["session_id"] for s in r.sessions] == ["ann-1", "bob-1"]
    assert r.daily == {
        "days": [{"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "24.99", "top_user": "ann"}],
        "duplicates_dropped": 1,
        "rejected": 1,
    }
    assert r.rejects == [{"line": 6, "reason": "bad_json"}]
