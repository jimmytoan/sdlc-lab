"""Black-box regression tests for the sessionize batch job."""
import json
import os
import subprocess
import sys

import pytest

from helpers import APP_DIR, ev, pick, run_job


HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]
FILES = ["daily.json", "rejects.jsonl", "sessions.csv"]


def raw(**changes):
    obj = {"event_id": "e", "user": "Ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    obj.update(changes)
    return json.dumps(obj)


def session(user, n, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, [f"{user}-{n}", user, start, end, str(duration),
                             str(events), str(purchases), revenue]))


def day(date, sessions, users, revenue, top_user):
    return {"date": date, "sessions": sessions, "users": users,
            "revenue": revenue, "top_user": top_user}


def test_empty_and_whitespace_input_writes_all_outputs():
    for lines in ([], ["", "  ", "\t", ""]):
        r = run_job(lines)
        assert r.code == 0
        assert r.files == FILES
        assert r.header == HEADER
        assert r.sessions == []
        assert r.rejects == []
        assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_missing_input_is_error():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip()


def test_bad_json_and_physical_line_numbers():
    lines = [" ", "{", "null", "[]", '"string"', "123", "true", "",
             raw(event_id="ok"), "{\"a\":1} garbage", "\t", "{}"]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == ([{"line": n, "reason": "bad_json"} for n in (2, 3, 4, 5, 6, 7, 10)]
                         + [{"line": 12, "reason": "missing_field"}])
    assert r.daily["rejected"] == 8
    assert len(r.sessions) == 1


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_required_fields_missing_wrong_type_and_blank(field):
    base = {"event_id": "id", "user": "ann", "ts": "2026-03-01T00:00:00Z", "type": "view"}
    objects = [{k: v for k, v in base.items() if k != field}]
    objects += [dict(base, **{field: v}) for v in (None, 0, False, [], {}, "", " \t ")]
    r = run_job([json.dumps(o) for o in objects])
    assert r.code == 0
    assert r.rejects == [{"line": i, "reason": "missing_field"} for i in range(1, len(objects) + 1)]
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(objects)}


def test_validation_order_and_rejected_duplicates_do_not_count():
    lines = [
        raw(event_id="same", ts="bad", type="dance"),
        raw(event_id="same", ts="bad", type="purchase", amount="wrong"),
        raw(event_id="same", type="dance", amount="wrong"),
        raw(event_id="same", type="purchase", amount="wrong"),
        raw(event_id="same", user="", ts="bad", type="dance"),
        raw(event_id="same"),
        raw(event_id="same", type="purchase", amount="-1"),
        raw(event_id="same", ts="2026-03-01T09:01:00Z"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": n, "reason": reason} for n, reason in [
        (1, "bad_ts"), (2, "bad_ts"), (3, "bad_type"), (4, "bad_amount"),
        (5, "missing_field"), (7, "bad_amount")]]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "0.00", None)],
                       "duplicates_dropped": 1, "rejected": 6}
    assert r.sessions == [session("ann", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00")]


def test_invalid_timestamps_and_types():
    bad_ts = ["not-a-date", "2026-03-01", "2026-03-01T09:00:00", "2026-03-01T09:00Z",
              "2026-02-30T09:00:00Z", "2026-03-01T25:00:00Z", "2026-03-01T09:00:00+25:00"]
    bad_type = ["View", "VIEW", "purchasee", " view ", "dance"]
    r = run_job([raw(event_id=f"t{i}", ts=t) for i, t in enumerate(bad_ts)] +
                [raw(event_id=f"y{i}", type=t) for i, t in enumerate(bad_type)])
    assert r.code == 0
    assert r.rejects == ([{"line": i + 1, "reason": "bad_ts"} for i in range(len(bad_ts))] +
                         [{"line": len(bad_ts) + i + 1, "reason": "bad_type"} for i in range(len(bad_type))])
    assert r.daily["rejected"] == len(bad_ts) + len(bad_type)
    assert r.sessions == []


def test_amount_validation_and_ignored_for_non_purchase():
    invalid = [None, 1, 1.25, True, [], {}, "", " ", "-1", "-0.01", "1.234", "abc", "1e2"]
    lines = [raw(event_id="missing", type="purchase")]
    lines += [raw(event_id=f"bad{i}", type="purchase", amount=a) for i, a in enumerate(invalid)]
    lines += [raw(event_id=f"good{i}", type="purchase", amount=a) for i, a in enumerate(["0", "5", "5.5", "19.99", "0.01"])]
    lines += [raw(event_id="view", type="view", amount={"bad": "ignored"}),
              raw(event_id="click", type="click", amount=-5)]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": i, "reason": "bad_amount"} for i in range(1, len(invalid) + 2)]
    assert r.sessions == [session("ann", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 7, 5, "30.50")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "30.50", "ann")],
                       "duplicates_dropped": 0, "rejected": len(invalid) + 1}


def test_duplicate_selection_global_earliest_and_file_order_on_equal_instants():
    lines = [
        ev("dup", "Wrong", "2026-03-01T12:00:00Z", "purchase", amount="99"),
        ev("tie", "First", "2026-03-01T10:00:00+01:00", "purchase", amount="3.25"),
        ev("dup", " Ann ", "2026-03-01T10:00:00+02:00", "purchase", amount="2"),
        ev("tie", "Second", "2026-03-01T09:00:00Z", "purchase", amount="50"),
        ev("dup", "Other", "2026-03-01T08:00:00.500Z"),
        ev("DUP", "Ann", "2026-03-01T08:10:00Z"),
        ev(" dup ", "Ann", "2026-03-01T08:20:00Z"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == []
    assert r.sessions == [
        session("ann", 1, "2026-03-01T08:00:00Z", "2026-03-01T08:20:00Z", 1200, 3, 1, "2.00"),
        session("first", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 1, "3.25"),
    ]
    assert r.daily == {"days": [day("2026-03-01", 2, 2, "5.25", "first")],
                       "duplicates_dropped": 3, "rejected": 0}


def test_gaps_use_previous_event_and_fractional_precision():
    lines = [ev("d", " Ann ", "2026-03-01T10:30:00.801Z", "purchase", amount="0.10"),
             ev("b", "ANN", "2026-03-01T09:30:00.500Z"),
             ev("a", "ann", "2026-03-01T09:00:00.500Z"),
             ev("c", "ann", "2026-03-01T10:00:00.500Z", "purchase", amount="0.20"),
             ev("e", "ann", "2026-03-01T10:30:01.100Z", "purchase", amount="0.03")]
    r = run_job(lines)
    assert r.code == 0
    assert r.sessions == [
        session("ann", 1, "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z", 3600, 3, 1, "0.20"),
        session("ann", 2, "2026-03-01T10:30:00Z", "2026-03-01T10:30:01Z", 0, 2, 2, "0.13"),
    ]
    assert r.daily == {"days": [day("2026-03-01", 2, 1, "0.33", "ann")],
                       "duplicates_dropped": 0, "rejected": 0}


def test_row_order_by_full_precision_then_normalized_user():
    lines = [ev("a", "zOE", "2026-03-01T09:00:00.900Z"),
             ev("b", "Zed", "2026-03-01T09:00:00.100Z"),
             ev("c", "Bob", "2026-03-01T09:00:00.900Z"),
             ev("d", "ann", "2026-03-01T09:00:00.900Z"),
             ev("e", "ZED", "2026-03-01T10:00:00Z")]
    r = run_job(lines)
    assert r.code == 0
    assert pick(r.sessions, "session_id") == [("zed-1",), ("ann-1",), ("bob-1",), ("zoe-1",), ("zed-2",)]
    assert r.daily == {"days": [day("2026-03-01", 5, 4, "0.00", None)],
                       "duplicates_dropped": 0, "rejected": 0}


def test_utc_date_cross_midnight_and_daily_top_user_ties():
    lines = [
        ev("1", " Zoe ", "2026-03-02T00:55:00+01:00", "purchase", amount="1.00"),
        ev("2", "zoe", "2026-03-02T00:15:00Z", "purchase", amount="2.00"),
        ev("3", "BOB", "2026-03-01T20:00:00-04:00", "purchase", amount="3"),
        ev("4", "Ann", "2026-03-01T23:00:00Z", "purchase", amount="1.50"),
        ev("5", "ann", "2026-03-01T23:45:00Z", "purchase", amount="1.50"),
        ev("6", "bob", "2026-03-02T00:10:00Z", "purchase", amount="0"),
        ev("7", "zoe", "2026-03-03T00:00:00+01:00"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.sessions == [
        session("ann", 1, "2026-03-01T23:00:00Z", "2026-03-01T23:00:00Z", 0, 1, 1, "1.50"),
        session("ann", 2, "2026-03-01T23:45:00Z", "2026-03-01T23:45:00Z", 0, 1, 1, "1.50"),
        session("zoe", 1, "2026-03-01T23:55:00Z", "2026-03-02T00:15:00Z", 1200, 2, 2, "3.00"),
        session("bob", 1, "2026-03-02T00:00:00Z", "2026-03-02T00:10:00Z", 600, 2, 2, "3.00"),
        session("zoe", 2, "2026-03-02T23:00:00Z", "2026-03-02T23:00:00Z", 0, 1, 0, "0.00"),
    ]
    assert r.daily == {"days": [day("2026-03-01", 3, 2, "6.00", "ann"),
                                day("2026-03-02", 2, 2, "3.00", "bob")],
                       "duplicates_dropped": 0, "rejected": 0}


def test_negative_half_hour_offset_and_days_sorted_with_zero_purchase():
    r = run_job([
        ev("late", "Ann", "2026-03-05T00:00:00Z", "purchase", amount="0.00"),
        ev("first", "Ann", "2026-03-01T23:45:00.750-05:30", "click"),
        ev("second", "ann", "2026-03-02T05:15:02.250Z", "purchase", amount="0.01"),
        ev("earlier", "Bob", "2026-02-28T23:59:59Z"),
    ])
    assert r.code == 0
    assert r.sessions == [
        session("bob", 1, "2026-02-28T23:59:59Z", "2026-02-28T23:59:59Z", 0, 1, 0, "0.00"),
        session("ann", 1, "2026-03-02T05:15:00Z", "2026-03-02T05:15:02Z", 1, 2, 1, "0.01"),
        session("ann", 2, "2026-03-05T00:00:00Z", "2026-03-05T00:00:00Z", 0, 1, 1, "0.00"),
    ]
    assert r.daily == {"days": [day("2026-02-28", 1, 1, "0.00", None),
                                day("2026-03-02", 1, 1, "0.01", "ann"),
                                day("2026-03-05", 1, 1, "0.00", None)],
                       "duplicates_dropped": 0, "rejected": 0}


def test_existing_output_files_are_replaced_on_next_run(tmp_path):
    inp = tmp_path / "input with spaces"
    out = tmp_path / "nested" / "output with spaces"
    inp.mkdir()
    event_file = inp / "events.jsonl"
    cmd = [sys.executable, os.path.join(APP_DIR, "job.py"),
           "--in", str(inp), "--out", str(out)]
    event_file.write_text(ev("a", "ann", "09:00:00", "purchase", amount="12") + "\n{\n", encoding="utf-8")
    p = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    assert sorted(p.name for p in out.iterdir()) == FILES
    assert json.loads((out / "daily.json").read_text(encoding="utf-8"))["rejected"] == 1
    assert (out / "rejects.jsonl").read_text(encoding="utf-8").strip()
    event_file.write_text("", encoding="utf-8")
    p = subprocess.run(cmd, cwd=APP_DIR, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    assert (out / "sessions.csv").read_text(encoding="utf-8").splitlines() == [",".join(HEADER)]
    assert json.loads((out / "daily.json").read_text(encoding="utf-8")) == {
        "days": [], "duplicates_dropped": 0, "rejected": 0}
    assert not (out / "rejects.jsonl").read_text(encoding="utf-8").strip()
