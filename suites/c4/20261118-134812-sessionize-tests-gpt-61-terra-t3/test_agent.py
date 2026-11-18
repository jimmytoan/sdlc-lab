import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import APP_DIR, ev, pick, run_job


HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def event(**changes):
    obj = {"event_id": "id", "user": "ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    obj.update(changes)
    return json.dumps(obj)


def row(user, n, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, [f"{user}-{n}", user, start, end, str(duration), str(events), str(purchases), revenue]))


def day(date, sessions, users, revenue, top):
    return {"date": date, "sessions": sessions, "users": users, "revenue": revenue, "top_user": top}


def test_empty_and_missing_input():
    for lines in ([], ["", "  ", "\t"]):
        r = run_job(lines)
        assert r.code == 0
        assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
        assert r.header == HEADER
        assert r.sessions == [] and r.rejects == []
        assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}
    r = run_job([], make_input=False)
    assert r.code == 2 and r.stderr.strip()


def test_creates_nested_output_and_overwrites_old_files(tmp_path):
    inp = tmp_path / "input"
    inp.mkdir()
    (inp / "events.jsonl").write_text(ev("a", "Ann", "09:00:00"), encoding="utf-8")
    out = tmp_path / "nested" / "output"
    cmd = [sys.executable, str(Path(APP_DIR) / "job.py"), "--in", str(inp), "--out", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    assert sorted(p.name for p in out.iterdir()) == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert json.loads((out / "daily.json").read_text(encoding="utf-8"))["days"] == [day("2026-03-01", 1, 1, "0.00", None)]
    (inp / "events.jsonl").write_text("\n", encoding="utf-8")
    for name in ("sessions.csv", "daily.json", "rejects.jsonl"):
        (out / name).write_text("old contents\n", encoding="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    assert (out / "sessions.csv").read_text(encoding="utf-8").strip() == ",".join(HEADER)
    assert json.loads((out / "daily.json").read_text(encoding="utf-8")) == {
        "days": [], "duplicates_dropped": 0, "rejected": 0}
    assert (out / "rejects.jsonl").read_text(encoding="utf-8") == ""


def test_rejection_order_physical_lines_and_first_failure():
    base = {"event_id": "e", "user": "u", "ts": "bad", "type": "bad"}
    lines = [
        "", "  ", "{", "null", "[]", '"text"', "42",
        json.dumps({}),
        json.dumps({**base, "user": " "}),  # missing beats bad timestamp and type
        json.dumps(base),  # bad timestamp beats bad type
        event(type="purchase", ts="not a timestamp"),
        event(type="PURCHASE", amount="bad"),
        event(type="purchase", amount="bad"),
        "\t", ev("ok", "U", "09:00:00"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        {"line": n, "reason": reason} for n, reason in [
            (3, "bad_json"), (4, "bad_json"), (5, "bad_json"), (6, "bad_json"), (7, "bad_json"),
            (8, "missing_field"), (9, "missing_field"), (10, "bad_ts"), (11, "bad_ts"),
            (12, "bad_type"), (13, "bad_amount")]]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "0.00", None)], "duplicates_dropped": 0, "rejected": 11}
    assert r.sessions == [row("u", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00")]


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_required_fields_missing_wrong_type_and_whitespace(field):
    base = {"event_id": "id", "user": "u", "ts": "2026-03-01T00:00:00Z", "type": "view"}
    missing = dict(base)
    del missing[field]
    variants = [missing] + [{**base, field: value} for value in (None, 0, True, [], {}, "", " \t ")]
    r = run_job([json.dumps(v) for v in variants])
    assert r.rejects == [{"line": i, "reason": "missing_field"} for i in range(1, len(variants) + 1)]
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(variants)}
    assert r.sessions == []


def test_bad_timestamps_and_types():
    bad_ts = ["2026-03-01", "2026-03-01T09:00:00", "2026-03-01T09:00Z",
              "2026-03-01 09:00:00Z", "2026-03-01T09:00:00z", "2026-03-01T09:00:00+0700",
              "2026-03-01T09:00:00+24:00", "2026-03-01T09:00:00-05:60",
              "2026-02-29T09:00:00Z", "2026-13-01T09:00:00Z", "2026-03-01T24:00:00Z",
              "2026-03-01T09:60:00Z", "2026-03-01T09:00:60Z", "2026-03-01T09:00:00.Z",
              "2026-03-01T09:00:00Zjunk"]
    bad_types = ["View", "VIEW", "purchase ", " view", "logout", "refund", "other"]
    lines = [event(event_id=f"t{i}", ts=t) for i, t in enumerate(bad_ts)]
    lines += [event(event_id=f"y{i}", type=t) for i, t in enumerate(bad_types)]
    r = run_job(lines)
    assert r.rejects == ([{"line": i + 1, "reason": "bad_ts"} for i in range(len(bad_ts))] +
                         [{"line": len(bad_ts) + i + 1, "reason": "bad_type"} for i in range(len(bad_types))])
    assert r.daily["rejected"] == len(lines) and r.sessions == []


def test_amount_validation_and_ignored_amounts():
    invalid = [None, 0, 5.5, True, [], {}, "", " ", "-1", "+1", ".5", "5.", "1.001", "1e2", "NaN", "Infinity", "1,00", "1 2"]
    lines = [event(event_id=f"bad{i}", type="purchase", amount=a) for i, a in enumerate(invalid)]
    lines += [event(event_id="missing", type="purchase")]
    good = ["0", "5", "5.5", "19.99", "0.01", " 1.20 "]
    lines += [event(event_id=f"good{i}", type="purchase", amount=a) for i, a in enumerate(good)]
    lines += [event(event_id="view", type="view", amount={"bad": True}),
              event(event_id="click", type="click", amount="-99")]
    r = run_job(lines)
    nbad = len(invalid) + 1
    assert r.rejects == [{"line": i, "reason": "bad_amount"} for i in range(1, nbad + 1)]
    assert r.sessions == [row("ann", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 8, 6, "31.70")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "31.70", "ann")], "duplicates_dropped": 0, "rejected": nbad}


def test_duplicates_earliest_global_exact_ids_and_file_tiebreak():
    lines = [
        ev("shared", "Later", "11:00:00", "purchase", amount="100"),
        ev("same", "First", "10:00:00", "purchase", amount="2"),
        ev("same", "Second", "2026-03-01T11:00:00+01:00", "purchase", amount="99"),
        ev("shared", "Winner", "09:00:00", "purchase", amount="3"),
        ev("shared", "Invalid", "08:00:00", "purchase", amount="bad"),
        ev(" shared", "Winner", "09:01:00"),
        ev("shared ", "Winner", "09:02:00"),
        ev("SHARED", "Winner", "09:03:00"),
        ev("shared", "Other", "09:30:00"),
        ev("only-invalid", "X", "08:00:00", "wrong"),
        ev("only-invalid", "X", "08:01:00"),
    ]
    r = run_job(lines)
    assert r.rejects == [{"line": 5, "reason": "bad_amount"}, {"line": 10, "reason": "bad_type"}]
    assert r.sessions == [
        row("x", 1, "2026-03-01T08:01:00Z", "2026-03-01T08:01:00Z", 0, 1, 0, "0.00"),
        row("winner", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:03:00Z", 180, 4, 1, "3.00"),
        row("first", 1, "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z", 0, 1, 1, "2.00")]
    assert r.daily == {"days": [day("2026-03-01", 3, 3, "5.00", "winner")], "duplicates_dropped": 3, "rejected": 2}


def test_sessions_gap_precision_order_and_normalized_users():
    lines = [
        ev("a3", " ANN ", "2026-03-01T10:00:00.800Z", "purchase", amount="0.10"),
        ev("a1", "Ann", "2026-03-01T09:00:00.800Z"),
        ev("a2", "ann", "2026-03-01T09:30:00.800Z", "click"),
        ev("a4", "aNn", "2026-03-01T10:30:00.800001Z", "purchase", amount="0.20"),
        ev("a5", "ann", "2026-03-01T10:30:02.100Z"),
        ev("z", "Zoe", "2026-03-01T09:00:00.100Z"),
        ev("b", "Bob", "2026-03-01T09:00:00.800Z"),
        ev("c", "Carl", "2026-03-01T09:00:00.800Z"),
    ]
    r = run_job(lines)
    assert r.header == HEADER
    assert r.sessions == [
        row("zoe", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("ann", 1, "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z", 3600, 3, 1, "0.10"),
        row("bob", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("carl", 1, "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("ann", 2, "2026-03-01T10:30:00Z", "2026-03-01T10:30:02Z", 1, 2, 1, "0.20")]
    assert r.daily == {"days": [day("2026-03-01", 5, 4, "0.30", "ann")], "duplicates_dropped": 0, "rejected": 0}


def test_timezone_cross_midnight_daily_revenue_and_top_user():
    lines = [
        ev("a1", "Ann", "2026-03-02T00:50:00+01:00", "purchase", amount="0.10"),
        ev("a2", "ann", "2026-03-02T00:05:00Z", "purchase", amount="0.20"),
        ev("a3", "ann", "2026-03-01T20:00:00-05:30", "purchase", amount="1.00"),
        ev("b1", "Bob", "2026-03-01T23:45:00Z", "purchase", amount="0.30"),
        ev("b2", "BOB", "2026-03-02T02:00:00+00:00", "purchase", amount="1.00"),
        ev("c1", "Carl", "2026-03-03T01:00:00+02:00"),
        ev("c2", "carl", "2026-03-03T00:05:00Z", "purchase", amount="0"),
        ev("d", "D", "2026-03-03T08:00:00Z"),
    ]
    r = run_job(lines)
    assert r.rejects == []
    assert r.sessions == [
        row("bob", 1, "2026-03-01T23:45:00Z", "2026-03-01T23:45:00Z", 0, 1, 1, "0.30"),
        row("ann", 1, "2026-03-01T23:50:00Z", "2026-03-02T00:05:00Z", 900, 2, 2, "0.30"),
        row("ann", 2, "2026-03-02T01:30:00Z", "2026-03-02T01:30:00Z", 0, 1, 1, "1.00"),
        row("bob", 2, "2026-03-02T02:00:00Z", "2026-03-02T02:00:00Z", 0, 1, 1, "1.00"),
        row("carl", 1, "2026-03-02T23:00:00Z", "2026-03-02T23:00:00Z", 0, 1, 0, "0.00"),
        row("carl", 2, "2026-03-03T00:05:00Z", "2026-03-03T00:05:00Z", 0, 1, 1, "0.00"),
        row("d", 1, "2026-03-03T08:00:00Z", "2026-03-03T08:00:00Z", 0, 1, 0, "0.00")]
    assert r.daily == {"days": [day("2026-03-01", 2, 2, "0.60", "ann"),
                                day("2026-03-02", 3, 3, "2.00", "ann"),
                                day("2026-03-03", 2, 2, "0.00", None)],
                       "duplicates_dropped": 0, "rejected": 0}


def test_daily_top_user_uses_sum_across_sessions_not_largest_session():
    r = run_job([ev("b", "bob", "08:00:00", "purchase", amount="1.50"),
                 ev("a1", "ann", "09:00:00", "purchase", amount="0.80"),
                 ev("a2", "Ann", "10:00:00", "purchase", amount="0.80"),
                 ev("c", "cat", "11:00:00")])
    assert r.daily == {"days": [day("2026-03-01", 4, 3, "3.10", "ann")], "duplicates_dropped": 0, "rejected": 0}
    assert pick(r.sessions, "session_id", "revenue") == [("bob-1", "1.50"), ("ann-1", "0.80"), ("ann-2", "0.80"), ("cat-1", "0.00")]


def test_unicode_and_csv_escaping_and_leap_day():
    r = run_job([
        ev("two", ' é, "Z" ', "2024-03-01T05:30:00.400+05:30", "purchase", amount="0.01"),
        ev("one", 'É, "z"', "2024-02-29T23:59:59.900Z"),
        ev("three", "另一个", "2024-02-29T23:59:59.900Z"),
    ])
    assert r.rejects == []
    assert r.sessions == [
        row('é, "z"', 1, "2024-02-29T23:59:59Z", "2024-03-01T00:00:00Z", 0, 2, 1, "0.01"),
        row("另一个", 1, "2024-02-29T23:59:59Z", "2024-02-29T23:59:59Z", 0, 1, 0, "0.00")]
    assert r.daily == {"days": [day("2024-02-29", 2, 2, "0.01", 'é, "z"')], "duplicates_dropped": 0, "rejected": 0}
