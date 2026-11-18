"""Black-box regression suite for the sessionize batch job (see SPEC.md).

Everything here drives job.py through its command line only. Helpers that are
specific to this suite live in this file; tests/helpers.py is imported, not changed.
"""
import csv
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from helpers import APP_DIR, run_job

JOB = os.path.join(APP_DIR, "job.py")
CSV_HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
BASE = {"event_id": "e1", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}


# ---------------------------------------------------------------- helpers

def rec(drop=(), **over):
    """One raw events.jsonl line: a valid view event by default.

    Keyword arguments override fields; names listed in `drop` are removed."""
    d = dict(BASE)
    d.update(over)
    if isinstance(d.get("ts"), str) and re.match(r"^\d\d:\d\d", d["ts"]):  # short form '09:00:00Z' -> 2026-03-01T09:00:00Z
        d["ts"] = "2026-03-01T" + d["ts"]
    for k in drop:
        d.pop(k, None)
    return json.dumps(d)


def purchase(event_id="p1", user="ann", ts="2026-03-01T09:00:00Z", amount="5.00", **extra):
    return rec(event_id=event_id, user=user, ts=ts, type="purchase", amount=amount, **extra)


def run_raw(content, out_rel="out", name="events.jsonl", make_input=True):
    """Run the job with arbitrary bytes as input. Returns code, stderr and the bytes of every output file."""
    with tempfile.TemporaryDirectory() as d:
        inp = os.path.join(d, "in")
        os.makedirs(inp)
        if make_input:
            with open(os.path.join(inp, name), "wb") as f:
                f.write(content)
        out = os.path.join(d, *out_rel.split("/"))
        p = subprocess.run([sys.executable, JOB, "--in", inp, "--out", out],
                           capture_output=True, timeout=30, cwd=APP_DIR)
        files = {}
        if os.path.isdir(out):
            for n in os.listdir(out):
                with open(os.path.join(out, n), "rb") as f:
                    files[n] = f.read()
        return SimpleNamespace(code=p.returncode, stderr=p.stderr.decode("utf-8", "replace"), files=files)


def sessions_of(lines):
    r = run_job(lines)
    assert r.code == 0, r.stderr
    return r.sessions


def cols(sessions, *names):
    return [tuple(s[n] for n in names) for s in sessions]


def reject_reason(line):
    """Feed one line on its own and return its reject reason (None if it was accepted)."""
    r = run_job([line])
    assert r.code == 0, r.stderr
    assert len(r.rejects) <= 1
    if not r.rejects:
        return None
    assert r.rejects[0]["line"] == 1
    return r.rejects[0]["reason"]


def model_run(records, rejects):
    """Reference implementation of the spec, used for randomized cross-checks.

    records: list of dicts {line, event_id, user, utc, type, cents}
    rejects: list of (line, reason) already determined by the generator
    """
    kept = {}
    dups = 0
    for r in sorted(records, key=lambda r: (r["utc"], r["line"])):
        if r["event_id"] in kept:
            dups += 1
        else:
            kept[r["event_id"]] = r
    per_user = {}
    for r in kept.values():
        per_user.setdefault(r["user"], []).append(r)
    sessions = []
    for user, evs in per_user.items():
        evs.sort(key=lambda r: (r["utc"], r["line"]))
        cur = None
        for r in evs:
            if cur is None or r["utc"] - cur["end"] > timedelta(minutes=30):
                cur = {"user": user, "start": r["utc"], "end": r["utc"], "events": 0, "purchases": 0, "cents": 0}
                sessions.append(cur)
            cur["end"] = r["utc"]
            cur["events"] += 1
            if r["type"] == "purchase":
                cur["purchases"] += 1
                cur["cents"] += r["cents"]
    sessions.sort(key=lambda s: (s["start"], s["user"]))
    counter = {}
    rows = []
    for s in sessions:
        counter[s["user"]] = counter.get(s["user"], 0) + 1
        rows.append([
            f"{s['user']}-{counter[s['user']]}", s["user"],
            s["start"].strftime("%Y-%m-%dT%H:%M:%SZ"), s["end"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            str((s["end"] - s["start"]) // timedelta(seconds=1)), str(s["events"]), str(s["purchases"]),
            money(s["cents"]),
        ])
    days = {}
    for s in sessions:
        d = days.setdefault(s["start"].date().isoformat(), {"sessions": 0, "users": set(), "cents": 0, "by_user": {}})
        d["sessions"] += 1
        d["users"].add(s["user"])
        d["cents"] += s["cents"]
        d["by_user"][s["user"]] = d["by_user"].get(s["user"], 0) + s["cents"]
    day_list = []
    for date in sorted(days):
        d = days[date]
        top = None
        if d["cents"] > 0:
            top = min(d["by_user"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        day_list.append({"date": date, "sessions": d["sessions"], "users": len(d["users"]),
                         "revenue": money(d["cents"]), "top_user": top})
    daily = {"days": day_list, "duplicates_dropped": dups, "rejected": len(rejects)}
    return rows, daily


def money(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def parse_csv_bytes(data):
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


# ---------------------------------------------------------------- command line and files

def test_writes_all_three_files_on_success():
    r = run_job([rec()])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]


def test_sessions_csv_header_is_exact():
    r = run_job([rec()])
    assert r.header == CSV_HEADER


def test_empty_input_still_writes_all_files():
    r = run_job([])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == CSV_HEADER
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_whitespace_only_input_is_empty_input():
    res = run_raw(b"\n   \n\t\n")
    assert res.code == 0
    assert set(res.files) == {"daily.json", "rejects.jsonl", "sessions.csv"}
    assert res.files["rejects.jsonl"] == b""
    assert parse_csv_bytes(res.files["sessions.csv"]) == [CSV_HEADER]
    assert json.loads(res.files["daily.json"]) == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_zero_byte_input_is_accepted():
    res = run_raw(b"")
    assert res.code == 0
    assert parse_csv_bytes(res.files["sessions.csv"]) == [CSV_HEADER]


def test_output_dir_created_when_missing_including_parents():
    res = run_raw(rec().encode(), out_rel="deep/nested/out")
    assert res.code == 0
    assert set(res.files) == {"daily.json", "rejects.jsonl", "sessions.csv"}


def test_missing_events_file_exits_2_with_stderr_message():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() != ""


def test_missing_input_directory_exits_2():
    res = run_raw(b"", make_input=False)
    assert res.code == 2
    assert res.stderr.strip() != ""


def test_wrong_input_file_name_exits_2():
    res = run_raw(rec().encode(), name="events.json")
    assert res.code == 2
    assert res.stderr.strip() != ""


def test_rejects_do_not_change_exit_code():
    r = run_job([rec(), "{nope", rec(drop=("user",))])
    assert r.code == 0
    assert len(r.rejects) == 2


def test_output_is_deterministic_across_runs():
    lines = [rec(event_id="a", user="bob", ts="10:00:00"), rec(event_id="b", user="ann", ts="09:00:00"),
             "{bad", purchase(event_id="c", user="ann", ts="09:10:00", amount="1.5")]
    first = run_raw("\n".join(lines).encode())
    second = run_raw("\n".join(lines).encode())
    assert first.files == second.files


# ---------------------------------------------------------------- input format

def test_blank_and_whitespace_lines_are_skipped_but_counted():
    lines = ["", rec(event_id="a"), "   ", "\t", "{bad", "", rec(event_id="b", user="bob")]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 5, "reason": "bad_json"}]
    assert len(r.sessions) == 2


def test_last_line_without_trailing_newline_is_read():
    res = run_raw(rec().encode() + b"\n" + rec(event_id="e2", ts="09:05:00Z").encode())
    assert res.code == 0
    rows = parse_csv_bytes(res.files["sessions.csv"])
    assert len(rows) == 2 and rows[1][5] == "2"


def test_extra_fields_are_ignored():
    assert reject_reason(rec(extra_field=[1, 2], another={"x": 1})) is None


def test_amount_is_ignored_for_non_purchase_types():
    assert reject_reason(rec(type="view", amount="not money")) is None
    assert reject_reason(rec(type="click", amount=5)) is None
    assert reject_reason(rec(type="click", amount=None)) is None


def test_user_is_trimmed_and_lowercased_in_output():
    s = sessions_of([rec(event_id="a", user="  ANN  "), rec(event_id="b", user="ann", ts="09:01:00Z")])
    assert len(s) == 1
    assert s[0]["user"] == "ann" and s[0]["session_id"] == "ann-1"


def test_user_internal_spaces_are_kept():
    s = sessions_of([rec(user="Ann Lee")])
    assert s[0]["user"] == "ann lee"


def test_unicode_user_is_lowercased_and_utf8_written():
    res = run_raw(rec(user="Zoë").encode())
    rows = parse_csv_bytes(res.files["sessions.csv"])
    assert rows[1][:2] == ["zoë-1", "zoë"]


def test_user_with_comma_is_quoted_in_csv():
    r = run_job([rec(user="a,b")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["user"] == "a,b"
    assert r.sessions[0]["session_id"] == "a,b-1"


def test_event_id_trimmed_is_still_valid_and_nonempty():
    assert reject_reason(rec(event_id="  x  ")) is None


# ---------------------------------------------------------------- rejects

@pytest.mark.parametrize("line", [
    "{not json",
    "[1, 2, 3]",
    "null",
    "42",
    '"just a string"',
    "true",
    '{"event_id": "e1", "user": "ann",',
])
def test_bad_json_or_non_object(line):
    assert reject_reason(line) == "bad_json"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_missing_required_field(field):
    assert reject_reason(rec(drop=(field,))) == "missing_field"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", [None, 5, True, ["x"], {"a": 1}])
def test_required_field_not_a_string(field, value):
    assert reject_reason(rec(**{field: value})) == "missing_field"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_required_field_empty_after_trimming(field, value):
    assert reject_reason(rec(**{field: value})) == "missing_field"


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00",              # no offset
    "2026-03-01T09:00:00.250",          # no offset with fraction
    "2026-03-01T09:00Z",                # no seconds
    "2026-03-01",                       # date only
    "2026-03-01T09:00:00+0700",         # offset without colon
    "2026-02-30T09:00:00Z",             # impossible date
    "2026-03-01T25:00:00Z",             # impossible hour
    "2026-03-01T09:61:00Z",             # impossible minute
    "yesterday",
    "1772355600",                       # epoch-like digits
])
def test_bad_timestamp(ts):
    assert reject_reason(rec(ts=ts)) == "bad_ts"


@pytest.mark.parametrize("ts", [
    "2026-03-01T09:00:00Z",
    "2026-03-01T09:00:00+07:00",
    "2026-03-01T09:00:00-05:30",
    "2026-03-01T09:00:00.250Z",
    "2026-03-01T09:00:00.250-05:30",
    "2026-03-01T09:00:00+00:00",
])
def test_good_timestamp_formats_accepted(ts):
    assert reject_reason(rec(ts=ts)) is None


@pytest.mark.parametrize("type_", ["dance", "View", "PURCHASE", "purchases", "buy"])
def test_bad_type(type_):
    assert reject_reason(rec(type=type_)) == "bad_type"


@pytest.mark.parametrize("type_", ["view", "click", "purchase"])
def test_good_types_accepted(type_):
    extra = {"amount": "1"} if type_ == "purchase" else {}
    assert reject_reason(rec(type=type_, **extra)) is None


@pytest.mark.parametrize("amount", ["", "abc", "-1", "-0.01", "1.234", "1e3", "1,50", "5.5.5", "0x10", "$5"])
def test_bad_amount_strings(amount):
    assert reject_reason(purchase(amount=amount)) == "bad_amount"


@pytest.mark.parametrize("amount", [5, 5.5, 0, None, True, ["5"]])
def test_amount_must_be_a_string(amount):
    assert reject_reason(rec(type="purchase", amount=amount)) == "bad_amount"


def test_purchase_without_amount_is_bad_amount():
    assert reject_reason(rec(type="purchase", drop=("amount",))) == "bad_amount"


@pytest.mark.parametrize("amount", ["5", "5.5", "19.99", "0", "0.00", "0.1", "1234567.89"])
def test_valid_amount_strings_accepted(amount):
    assert reject_reason(purchase(amount=amount)) is None


# precedence: the first failing check is reported
def test_precedence_non_object_is_bad_json():
    assert reject_reason("[]") == "bad_json"


def test_precedence_missing_field_before_bad_ts():
    assert reject_reason(rec(drop=("user",), ts="nope")) == "missing_field"


def test_precedence_missing_field_before_bad_type():
    assert reject_reason(rec(drop=("event_id",), type="dance")) == "missing_field"


def test_precedence_empty_type_is_missing_not_bad_type():
    assert reject_reason(rec(type="")) == "missing_field"


def test_precedence_bad_ts_before_bad_type():
    assert reject_reason(rec(ts="nope", type="dance")) == "bad_ts"


def test_precedence_bad_ts_before_bad_amount():
    assert reject_reason(rec(ts="nope", type="purchase", drop=("amount",))) == "bad_ts"


def test_precedence_bad_type_before_amount_checks():
    # type is checked first, so an invalid type never reaches amount validation
    assert reject_reason(rec(type="dance", amount=None)) == "bad_type"


def test_reject_file_lists_rejects_in_input_order_with_physical_line_numbers():
    lines = [rec(event_id="a"), "", "{bad", rec(event_id="b", ts="2026-03-01T09:00:00"),
             rec(event_id="c", type="dance"), "   ", rec(drop=("amount",), event_id="d", type="purchase")]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        {"line": 3, "reason": "bad_json"},
        {"line": 4, "reason": "bad_ts"},
        {"line": 5, "reason": "bad_type"},
        {"line": 7, "reason": "bad_amount"},
    ]
    assert r.daily["rejected"] == 4


def test_reject_file_has_one_json_object_per_line():
    res = run_raw(b"{bad\n" + rec(type="dance").encode() + b"\n")
    text = res.files["rejects.jsonl"].decode("utf-8")
    rows = [json.loads(x) for x in text.splitlines()]
    assert rows == [{"line": 1, "reason": "bad_json"}, {"line": 2, "reason": "bad_type"}]
    assert all(set(x) == {"line", "reason"} for x in rows)


def test_rejected_purchase_contributes_no_revenue_or_session():
    r = run_job([rec(event_id="a", ts="09:00:00Z"), purchase(event_id="b", ts="09:01:00Z", amount="x")])
    assert r.rejects == [{"line": 2, "reason": "bad_amount"}]
    assert r.sessions[0]["events"] == "1" and r.sessions[0]["purchases"] == "0"
    assert r.sessions[0]["revenue"] == "0.00"


def test_rejected_line_does_not_drop_a_valid_duplicate():
    r = run_job([rec(event_id="a", type="dance"), rec(event_id="a", ts="09:00:00Z")])
    assert r.rejects == [{"line": 1, "reason": "bad_type"}]
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "1"


def test_rejected_line_is_not_counted_as_duplicate_of_valid_one():
    r = run_job([rec(event_id="a", ts="09:00:00Z"), rec(event_id="a", ts="09:20:00Z", type="dance")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.daily["rejected"] == 1
    assert r.sessions[0]["events"] == "1"


# ---------------------------------------------------------------- duplicates

def test_duplicate_dropped_and_counted():
    r = run_job([rec(event_id="a"), rec(event_id="a"), rec(event_id="a")])
    assert r.daily["duplicates_dropped"] == 2
    assert r.sessions[0]["events"] == "1"


def test_duplicates_are_not_rejects():
    r = run_job([rec(event_id="a"), rec(event_id="a")])
    assert r.rejects == []
    assert r.daily["rejected"] == 0


def test_duplicate_keeps_earliest_timestamp_even_if_later_in_file():
    r = run_job([rec(event_id="a", ts="09:10:00Z", user="bob"), rec(event_id="a", ts="09:00:00Z", user="ann")])
    assert cols(r.sessions, "user", "start") == [("ann", "2026-03-01T09:00:00Z")]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_with_equal_timestamp_keeps_first_in_file():
    r = run_job([rec(event_id="a", ts="09:00:00Z", user="ann"), rec(event_id="a", ts="09:00:00Z", user="bob")])
    assert cols(r.sessions, "user") == [("ann",)]


def test_duplicate_timestamps_compared_in_utc():
    # 09:00+07:00 is 02:00Z; 01:59:59Z is earlier, so the second line must be kept
    r = run_job([rec(event_id="a", ts="2026-03-01T09:00:00+07:00", user="bob"),
                 rec(event_id="a", ts="2026-03-01T01:59:59Z", user="ann")])
    assert cols(r.sessions, "user") == [("ann",)]


def test_equal_instants_in_different_offsets_are_equal_timestamps():
    r = run_job([rec(event_id="a", ts="2026-03-01T09:00:00+07:00", user="ann"),
                 rec(event_id="a", ts="2026-03-01T02:00:00Z", user="bob")])
    assert cols(r.sessions, "user") == [("ann",)]


def test_duplicate_identity_is_event_id_only_across_users():
    r = run_job([rec(event_id="a", user="ann"), rec(event_id="a", user="bob", ts="09:05:00Z")])
    assert [s["user"] for s in r.sessions] == ["ann"]
    assert r.daily["duplicates_dropped"] == 1


def test_duplicate_match_is_exact_string():
    r = run_job([rec(event_id="a"), rec(event_id="a ", ts="09:05:00Z"), rec(event_id="A", ts="09:06:00Z")])
    assert r.daily["duplicates_dropped"] == 0
    assert r.sessions[0]["events"] == "3"


def test_dropped_duplicate_does_not_split_a_session():
    # the duplicate at 10:00 must not count as a gap: kept events are 09:00 and 09:20
    r = run_job([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", ts="09:20:00Z"),
                 rec(event_id="a", ts="10:00:00Z")])
    assert len(r.sessions) == 1
    assert r.sessions[0]["events"] == "2"
    assert r.sessions[0]["duration_s"] == "1200"


def test_dropped_duplicate_that_would_bridge_a_gap_does_not_join_sessions():
    r = run_job([rec(event_id="a", ts="09:00:00Z"), rec(event_id="a", ts="09:50:00Z"),
                 rec(event_id="b", ts="10:30:00Z")])
    assert [s["session_id"] for s in r.sessions] == ["ann-1", "ann-2"]


# ---------------------------------------------------------------- sessions

def test_gap_of_exactly_30_minutes_stays_in_session():
    s = sessions_of([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", ts="09:30:00Z")])
    assert len(s) == 1 and s[0]["events"] == "2"


def test_gap_of_30_minutes_and_one_second_splits():
    s = sessions_of([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", ts="09:30:01Z")])
    assert cols(s, "session_id", "start") == [("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T09:30:01Z")]


def test_gap_of_30_minutes_and_one_millisecond_splits():
    s = sessions_of([rec(event_id="a", ts="09:00:00.000Z"), rec(event_id="b", ts="09:30:00.001Z")])
    assert len(s) == 2


def test_fractional_gap_of_exactly_30_minutes_stays_in_session():
    s = sessions_of([rec(event_id="a", ts="09:00:00.500Z"), rec(event_id="b", ts="09:30:00.500Z")])
    assert len(s) == 1


def test_fractional_gap_just_over_30_minutes_splits():
    # full-precision gap is 30m00.5s -> new session
    s = sessions_of([rec(event_id="a", ts="09:00:00.000Z"), rec(event_id="b", ts="09:30:00.500Z")])
    assert len(s) == 2


def test_gap_measured_from_previous_event_not_session_start():
    evs = [rec(event_id=f"e{i}", ts=f"{9 + (i * 20) // 60:02d}:{(i * 20) % 60:02d}:00Z") for i in range(7)]
    s = sessions_of(evs)  # 09:00 .. 11:00 in 20-minute steps: one session
    assert len(s) == 1 and s[0]["duration_s"] == "7200"


def test_gap_is_per_user_other_users_do_not_split():
    s = sessions_of([rec(event_id="a", user="ann", ts="09:00:00Z"),
                     rec(event_id="b", user="bob", ts="09:10:00Z"),
                     rec(event_id="c", user="ann", ts="09:25:00Z")])
    assert cols(s, "session_id") == [("ann-1",), ("bob-1",)]
    assert s[0]["events"] == "2"


def test_session_numbering_follows_time_order_not_file_order():
    s = sessions_of([rec(event_id="a", ts="12:00:00Z"), rec(event_id="b", ts="09:00:00Z"),
                     rec(event_id="c", ts="10:30:00Z")])
    assert cols(s, "session_id", "start") == [
        ("ann-1", "2026-03-01T09:00:00Z"), ("ann-2", "2026-03-01T10:30:00Z"), ("ann-3", "2026-03-01T12:00:00Z")]


def test_events_with_equal_timestamps_are_one_session():
    s = sessions_of([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", ts="09:00:00Z", type="click"),
                     rec(event_id="c", ts="09:00:00Z", type="click")])
    assert len(s) == 1 and s[0]["events"] == "3" and s[0]["duration_s"] == "0"


def test_session_fields_for_a_full_example():
    s = sessions_of([rec(event_id="a", ts="09:00:00Z"),
                     rec(event_id="b", ts="09:05:00Z", type="click"),
                     purchase(event_id="c", ts="09:10:30Z", amount="19.99")])
    assert s == [{"session_id": "ann-1", "user": "ann", "start": "2026-03-01T09:00:00Z",
                  "end": "2026-03-01T09:10:30Z", "duration_s": "630", "events": "3",
                  "purchases": "1", "revenue": "19.99"}]


def test_session_row_timestamps_are_utc_with_z():
    s = sessions_of([rec(ts="2026-03-01T16:00:00-05:30")])
    assert s[0]["start"] == "2026-03-01T21:30:00Z" and s[0]["end"] == "2026-03-01T21:30:00Z"


def test_start_and_end_are_truncated_to_whole_seconds():
    s = sessions_of([rec(event_id="a", ts="09:00:00.900Z"), rec(event_id="b", ts="09:00:01.800Z")])
    assert s[0]["start"] == "2026-03-01T09:00:00Z"
    assert s[0]["end"] == "2026-03-01T09:00:01Z"


def test_duration_is_whole_seconds_rounded_down():
    s = sessions_of([rec(event_id="a", ts="09:00:00.100Z"), rec(event_id="b", ts="09:00:01.900Z")])
    assert s[0]["duration_s"] == "1"


def test_duration_rounds_down_for_large_fraction():
    s = sessions_of([rec(event_id="a", ts="09:00:00.000Z"), rec(event_id="b", ts="09:10:30.999Z")])
    assert s[0]["duration_s"] == "630"


def test_ordering_uses_full_precision_start_before_user_name():
    # both truncate to 09:00:00Z; bob starts 0.1s earlier, so bob sorts first despite 'ann' < 'bob'
    s = sessions_of([rec(event_id="a", user="ann", ts="09:00:00.900Z"),
                     rec(event_id="b", user="bob", ts="09:00:00.100Z")])
    assert cols(s, "user") == [("bob",), ("ann",)]


def test_rows_with_same_start_are_ordered_by_user_ascending():
    s = sessions_of([rec(event_id="a", user="zed", ts="09:00:00Z"),
                     rec(event_id="b", user="amy", ts="09:00:00Z"),
                     rec(event_id="c", user="kim", ts="09:00:00Z")])
    assert cols(s, "user") == [("amy",), ("kim",), ("zed",)]


def test_rows_ordered_by_start_across_users():
    s = sessions_of([rec(event_id="a", user="ann", ts="11:00:00Z"),
                     rec(event_id="b", user="bob", ts="09:00:00Z"),
                     rec(event_id="c", user="cat", ts="10:00:00Z")])
    assert cols(s, "user") == [("bob",), ("cat",), ("ann",)]


def test_timezone_offsets_with_fractions_convert_to_utc():
    s = sessions_of([rec(event_id="a", ts="2026-03-01T04:30:00.250-05:30")])
    assert s[0]["start"] == "2026-03-01T10:00:00Z"


def test_session_crossing_midnight_keeps_its_start_date_in_session_row():
    s = sessions_of([rec(event_id="a", ts="2026-03-01T23:50:00Z"), rec(event_id="b", ts="2026-03-02T00:10:00Z")])
    assert len(s) == 1
    assert s[0]["start"] == "2026-03-01T23:50:00Z" and s[0]["end"] == "2026-03-02T00:10:00Z"
    assert s[0]["duration_s"] == "1200"


def test_revenue_is_two_decimals_always():
    s = sessions_of([purchase(event_id="a", amount="5"), ])
    assert s[0]["revenue"] == "5.00"
    s = sessions_of([purchase(event_id="a", amount="5.5")])
    assert s[0]["revenue"] == "5.50"


def test_revenue_zero_without_purchases():
    s = sessions_of([rec(event_id="a")])
    assert s[0]["revenue"] == "0.00" and s[0]["purchases"] == "0"


def test_revenue_is_exact_decimal_sum():
    s = sessions_of([purchase(event_id="a", amount="0.10", ts="09:00:00Z"),
                     purchase(event_id="b", amount="0.20", ts="09:01:00Z"),
                     purchase(event_id="c", amount="0.30", ts="09:02:00Z")])
    assert s[0]["revenue"] == "0.60"
    s = sessions_of([purchase(event_id=f"p{i}", amount="0.01", ts=f"09:0{i}:00Z") for i in range(3)])
    assert s[0]["revenue"] == "0.03"


def test_revenue_large_amounts_exact():
    s = sessions_of([purchase(event_id="a", amount="9999999999999.99"),
                     purchase(event_id="b", amount="0.01", ts="09:01:00Z")])
    assert s[0]["revenue"] == "10000000000000.00"


def test_purchases_and_revenue_are_per_session():
    s = sessions_of([purchase(event_id="a", amount="3", ts="09:00:00Z"),
                     purchase(event_id="b", amount="4", ts="12:00:00Z"),
                     purchase(event_id="c", amount="0.5", ts="12:01:00Z")])
    assert cols(s, "purchases", "revenue") == [("1", "3.00"), ("2", "4.50")]


def test_view_and_click_events_count_as_events_not_purchases():
    s = sessions_of([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", ts="09:01:00Z", type="click")])
    assert s[0]["events"] == "2" and s[0]["purchases"] == "0"


def test_csv_has_header_then_one_line_per_session_with_eight_fields():
    res = run_raw("\n".join([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", user="bob", ts="10:00:00Z")]).encode())
    rows = parse_csv_bytes(res.files["sessions.csv"])
    assert rows[0] == CSV_HEADER
    assert len(rows) == 3
    assert all(len(r) == 8 for r in rows)


# ---------------------------------------------------------------- daily summary

def test_daily_top_level_keys_are_exact():
    r = run_job([rec()])
    assert set(r.daily) == {"days", "duplicates_dropped", "rejected"}
    assert set(r.daily["days"][0]) == {"date", "sessions", "users", "revenue", "top_user"}


def test_day_uses_utc_date_of_session_start():
    r = run_job([rec(event_id="a", ts="2026-03-01T23:30:00-05:00")])  # 2026-03-02T04:30Z
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-02"]


def test_session_crossing_midnight_counts_on_start_day():
    r = run_job([purchase(event_id="a", ts="2026-03-01T23:50:00Z", amount="5"),
                 rec(event_id="b", ts="2026-03-02T00:10:00Z")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 1, "users": 1, "revenue": "5.00", "top_user": "ann"}]


def test_days_ascending_and_days_without_sessions_omitted():
    r = run_job([rec(event_id="a", ts="2026-03-05T09:00:00Z"), rec(event_id="b", ts="2026-03-01T09:00:00Z"),
                 rec(event_id="c", ts="2026-03-03T09:00:00Z")])
    assert [d["date"] for d in r.daily["days"]] == ["2026-03-01", "2026-03-03", "2026-03-05"]


def test_same_user_two_sessions_same_day_counts_two_sessions_one_user():
    r = run_job([rec(event_id="a", ts="09:00:00Z"), rec(event_id="b", ts="11:00:00Z")])
    d = r.daily["days"][0]
    assert d["sessions"] == 2 and d["users"] == 1


def test_day_revenue_and_counts_sum_sessions():
    r = run_job([purchase(event_id="a", user="ann", amount="5.00"),
                 purchase(event_id="b", user="bob", amount="20"),
                 purchase(event_id="c", user="ann", ts="2026-03-02T09:00:00Z", amount="1")])
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "25.00", "top_user": "bob"},
        {"date": "2026-03-02", "sessions": 1, "users": 1, "revenue": "1.00", "top_user": "ann"},
    ]


def test_top_user_is_highest_total_revenue_across_that_days_sessions():
    # ann: two sessions of 10 each = 20; bob: one session of 15 -> ann wins
    r = run_job([purchase(event_id="a", user="ann", ts="09:00:00Z", amount="10"),
                 purchase(event_id="b", user="ann", ts="12:00:00Z", amount="10"),
                 purchase(event_id="c", user="bob", ts="10:00:00Z", amount="15")])
    d = r.daily["days"][0]
    assert d["top_user"] == "ann" and d["revenue"] == "35.00" and d["sessions"] == 3


def test_top_user_tie_goes_to_name_sorting_first_regardless_of_file_order():
    r = run_job([purchase(event_id="a", user="zed", amount="10"), purchase(event_id="b", user="amy", amount="10")])
    assert r.daily["days"][0]["top_user"] == "amy"


def test_top_user_considers_only_that_days_revenue():
    r = run_job([purchase(event_id="a", user="ann", ts="2026-03-01T09:00:00Z", amount="50"),
                 purchase(event_id="b", user="bob", ts="2026-03-02T09:00:00Z", amount="1")])
    assert [d["top_user"] for d in r.daily["days"]] == ["ann", "bob"]


def test_top_user_null_when_day_revenue_is_zero():
    r = run_job([rec(event_id="a", user="ann"), rec(event_id="b", user="bob", ts="09:00:00Z")])
    d = r.daily["days"][0]
    assert d["revenue"] == "0.00" and d["top_user"] is None and d["sessions"] == 2


def test_revenue_string_has_two_decimals_in_daily():
    r = run_job([purchase(event_id="a", amount="5")])
    assert r.daily["days"][0]["revenue"] == "5.00"


def test_duplicates_dropped_and_rejected_counts_in_daily():
    r = run_job([rec(event_id="a"), rec(event_id="a", ts="09:01:00Z"), "{bad", rec(drop=("ts",)),
                 rec(event_id="b", type="dance")])
    assert r.daily["duplicates_dropped"] == 1
    assert r.daily["rejected"] == 3


def test_daily_summary_with_only_rejects_has_no_days():
    r = run_job(["{bad", "[]"])
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 2}


# ---------------------------------------------------------------- randomized cross-check against a reference model

def generate_case(seed):
    rnd = random.Random(seed)
    users = ["Ann", "bob", "  CARA ", "dan", "Eve Q"]
    ids = [f"e{i}" for i in range(rnd.randint(4, 30))]
    base = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
    offsets = {"Z": timedelta(0), "+00:00": timedelta(0), "+07:00": timedelta(hours=7),
               "-05:30": timedelta(hours=-5, minutes=-30)}
    lines, records, rejects = [], [], []
    for _ in range(rnd.randint(5, 80)):
        if rnd.random() < 0.08:
            lines.append("")
            continue
        lineno = len(lines) + 1
        roll = rnd.random()
        if roll < 0.05:
            lines.append("{broken")
            rejects.append((lineno, "bad_json"))
            continue
        if roll < 0.08:
            lines.append(json.dumps({"event_id": "x", "user": "ann", "ts": "2026-03-01T09:00:00Z"}))
            rejects.append((lineno, "missing_field"))
            continue
        if roll < 0.10:
            lines.append(json.dumps({"event_id": "x", "user": "ann", "ts": "2026-03-01T09:00:00", "type": "view"}))
            rejects.append((lineno, "bad_ts"))
            continue
        utc = base + timedelta(milliseconds=rnd.randint(0, 8 * 3600 * 1000))
        frac = rnd.random() < 0.5
        if not frac:
            utc = utc.replace(microsecond=0)
        off_name = rnd.choice(list(offsets))
        local = utc.astimezone(timezone(offsets[off_name]))
        ts = local.strftime("%Y-%m-%dT%H:%M:%S")
        if frac:
            ts += f".{utc.microsecond // 1000:03d}"
        ts += off_name
        etype = rnd.choice(["view", "click", "purchase"])
        raw_user = rnd.choice(users)
        event_id = rnd.choice(ids)
        obj = {"event_id": event_id, "user": raw_user, "ts": ts, "type": etype}
        cents = 0
        if etype == "purchase":
            cents = rnd.randint(0, 50000)
            if cents % 100 == 0:
                amount = str(cents // 100)
            elif cents % 10 == 0:
                amount = f"{cents // 100}.{(cents % 100) // 10}"
            else:
                amount = f"{cents // 100}.{cents % 100:02d}"
            obj["amount"] = amount
        else:
            if rnd.random() < 0.3:
                obj["amount"] = "not a number"  # ignored for non-purchases
        lines.append(json.dumps(obj))
        records.append({"line": lineno, "event_id": event_id, "user": raw_user.strip().lower(),
                        "utc": utc, "type": etype, "cents": cents})
    return lines, records, rejects


@pytest.mark.parametrize("seed", range(40))
def test_random_inputs_match_reference_model(seed):
    lines, records, rejects = generate_case(seed)
    r = run_job(lines)
    assert r.code == 0, r.stderr
    expected_rows, expected_daily = model_run(records, rejects)
    assert r.rejects == [{"line": ln, "reason": reason} for ln, reason in rejects]
    got_rows = [[s[c] for c in CSV_HEADER] for s in r.sessions]
    assert got_rows == expected_rows
    assert r.daily == expected_daily
