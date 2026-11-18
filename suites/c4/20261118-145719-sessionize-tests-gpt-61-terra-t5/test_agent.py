import json
import subprocess
import sys

import pytest

from helpers import APP_DIR, ev, pick, run_job


HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def obj(**changes):
    data = {"event_id": "id", "user": "Ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    data.update(changes)
    return json.dumps(data)


def row(id, user, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, [id, user, start, end, str(duration), str(events), str(purchases), revenue]))


def test_empty_and_blank_inputs_always_create_all_outputs():
    for lines in ([], ["", "  \t ", "\r"]):
        r = run_job(lines)
        assert r.code == 0
        assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
        assert r.header == HEADER
        assert r.sessions == [] and r.rejects == []
        assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_missing_input_is_error():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert "events.jsonl" in r.stderr and r.stderr.strip()


def test_reject_order_line_numbers_and_first_error():
    lines = [
        "  ", "not json", "null", "[]", '"object"',
        "{}", obj(event_id=" ", ts="invalid", type="no"),
        obj(user=3, ts="invalid"), obj(ts="invalid", type="no"),
        obj(type="no", amount=-5), obj(type="purchase"),
        obj(event_id="ok", user="Valid", ts="2026-03-01T00:00:00Z"),
        "\t", obj(type="purchase", amount="-1"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        {"line": n, "reason": reason} for n, reason in [
            (2, "bad_json"), (3, "bad_json"), (4, "bad_json"), (5, "bad_json"),
            (6, "missing_field"), (7, "missing_field"), (8, "missing_field"),
            (9, "bad_ts"), (10, "bad_type"), (11, "bad_amount"), (14, "bad_amount")
        ]
    ]
    assert r.daily == {"days": [{"date": "2026-03-01", "sessions": 1, "users": 1,
                                  "revenue": "0.00", "top_user": None}],
                       "duplicates_dropped": 0, "rejected": 11}
    assert len(r.sessions) == 1 and r.sessions[0]["user"] == "valid"


def test_each_required_field_missing_or_wrong_type():
    lines = []
    for field in ("event_id", "user", "ts", "type"):
        base = json.loads(obj())
        del base[field]
        lines.append(json.dumps(base))
        for value in (None, 123, False, [], {}, "", " \t "):
            lines.append(obj(**{field: value}))
    r = run_job(lines)
    assert r.code == 0 and r.sessions == []
    assert r.rejects == [{"line": i, "reason": "missing_field"} for i in range(1, len(lines) + 1)]
    assert r.daily["rejected"] == len(lines)


def test_timestamp_validation_and_offsets():
    invalid = [
        "2026-03-01T09:00:00", "2026-03-01", "2026-03-01T09:00Z",
        "2026-03-01 09:00:00Z", "2026-03-01t09:00:00z", "2026-03-01T09:00:00+0700",
        "2026-03-01T09:00:00+24:00", "2026-03-01T09:00:00-05:60",
        "2026-02-29T09:00:00Z", "2026-13-01T09:00:00Z", "2026-03-01T24:00:00Z",
        "2026-03-01T09:60:00Z", "2026-03-01T09:00:60Z", "2026-03-01T09:00:00.Z",
        "2026-03-01T09:00:00Zextra",
    ]
    lines = [obj(event_id=f"bad{i}", ts=ts) for i, ts in enumerate(invalid)]
    lines += [
        obj(event_id="a", user="A", ts="2026-03-01T00:15:00+01:00"),
        obj(event_id="b", user="B", ts="2026-03-01T23:30:00-05:30"),
        obj(event_id="c", user="C", ts=" 2026-03-01T09:00:00.250Z "),
        obj(event_id="d", user="D", ts="2024-02-29T12:30:15+00:00"),
    ]
    r = run_job(lines)
    assert r.rejects == [{"line": i, "reason": "bad_ts"} for i in range(1, len(invalid) + 1)]
    assert pick(r.sessions, "user", "start") == [
        ("d", "2024-02-29T12:30:15Z"), ("a", "2026-02-28T23:15:00Z"),
        ("c", "2026-03-01T09:00:00Z"), ("b", "2026-03-02T05:00:00Z")]
    assert [d["date"] for d in r.daily["days"]] == ["2024-02-29", "2026-02-28", "2026-03-01", "2026-03-02"]


def test_amount_validation_and_ignored_amounts():
    bad = [None, 1, 1.5, True, [], {}, "", " ", "-1", "+1", ".5", "5.",
           "1.000", "1e2", "1,00", "NaN", "Infinity", "1 2"]
    lines = [obj(event_id=f"bad{i}", type="purchase", amount=x) for i, x in enumerate(bad)]
    lines += [obj(event_id="missing", type="purchase")]
    lines += [obj(event_id=f"good{i}", type="purchase", amount=x)
              for i, x in enumerate(["0", "00.01", "5", "5.5", "19.99", " 0.10 "])]
    lines += [obj(event_id="view", type="view", amount={"invalid": True}),
              obj(event_id="click", type="click", amount=-100)]
    r = run_job(lines)
    assert r.rejects == [{"line": i, "reason": "bad_amount"} for i in range(1, len(bad) + 2)]
    assert r.sessions == [row("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 8, 6, "30.60")]
    assert r.daily["days"][0]["revenue"] == "30.60"


def test_type_is_exact_and_rejects_do_not_reserve_ids():
    lines = [obj(event_id=f"bad{i}", type=t) for i, t in enumerate(["VIEW", " click ", "refund", "logout", "other"])]
    lines += [obj(event_id="same", type="purchase", amount="bad"),
              obj(event_id="same", type="click"), obj(event_id="same", type="view")]
    r = run_job(lines)
    assert r.rejects == ([{"line": i, "reason": "bad_type"} for i in range(1, 6)] +
                         [{"line": 6, "reason": "bad_amount"}])
    assert r.daily["duplicates_dropped"] == 1
    assert r.sessions[0]["events"] == "1"


def test_duplicate_selection_global_earliest_and_first_file_tie_exact_ids():
    lines = [
        ev("shared", "Late", "11:00:00", "purchase", amount="90"),
        ev("tie", "First", "2026-03-01T10:00:00+01:00", "purchase", amount="2.25"),
        ev("shared", "Earliest", "08:00:00", "purchase", amount="3.50"),
        ev("tie", "Second", "09:00:00", "purchase", amount="99"),
        ev("shared", "Middle", "09:30:00"),
        ev(" shared ", "Distinct", "07:00:00"),
        ev("SHARED", "Distinct", "07:01:00"),
        obj(event_id="shared", user="Invalid", ts="bad"),
    ]
    r = run_job(lines)
    assert r.rejects == [{"line": 8, "reason": "bad_ts"}]
    assert r.daily["duplicates_dropped"] == 3 and r.daily["rejected"] == 1
    assert r.sessions == [
        row("distinct-1", "distinct", "2026-03-01T07:00:00Z", "2026-03-01T07:01:00Z", 60, 2, 0, "0.00"),
        row("earliest-1", "earliest", "2026-03-01T08:00:00Z", "2026-03-01T08:00:00Z", 0, 1, 1, "3.50"),
        row("first-1", "first", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 1, "2.25"),
    ]
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 3, "users": 3, "revenue": "5.75", "top_user": "earliest"}]


def test_gap_is_between_consecutive_events_with_full_precision():
    lines = [
        ev("d", " ANN ", "2026-03-01T10:30:01.100001Z"),
        ev("b", "ann", "2026-03-01T09:30:00.100000Z", "purchase", amount="0.10"),
        ev("a", "Ann", "2026-03-01T09:00:00.100000Z"),
        ev("c", "Ann", "2026-03-01T10:00:00.100000Z", "purchase", amount="0.20"),
        ev("e", "Ann", "2026-03-01T10:30:01.999999Z", "purchase", amount="0.30"),
    ]
    r = run_job(lines)
    assert r.sessions == [
        row("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z", 3600, 3, 2, "0.30"),
        row("ann-2", "ann", "2026-03-01T10:30:01Z", "2026-03-01T10:30:01Z", 0, 2, 1, "0.30"),
    ]
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 2, "users": 1, "revenue": "0.60", "top_user": "ann"}]


def test_duration_floored_from_full_precision_not_formatted_times():
    r = run_job([ev("a", "a", "2026-03-01T09:00:00.900Z"),
                 ev("b", "a", "2026-03-01T09:00:02.100Z")])
    assert r.sessions == [row("a-1", "a", "2026-03-01T09:00:00Z", "2026-03-01T09:00:02Z", 1, 2, 0, "0.00")]


def test_row_order_uses_full_start_precision_then_normalized_user():
    r = run_job([
        ev("z", "Zoe", "2026-03-01T09:00:00.100Z"),
        ev("a", "Ann", "2026-03-01T09:00:00.900Z"),
        ev("c", "CARL", "2026-03-01T09:00:00.100Z"),
        ev("b", " bob ", "2026-03-01T09:00:00.100Z"),
    ])
    assert pick(r.sessions, "session_id", "start") == [
        ("bob-1", "2026-03-01T09:00:00Z"), ("carl-1", "2026-03-01T09:00:00Z"),
        ("zoe-1", "2026-03-01T09:00:00Z"), ("ann-1", "2026-03-01T09:00:00Z")]


def test_daily_uses_session_start_date_and_aggregates_per_user():
    r = run_job([
        ev("a1", "Zoe", "2026-03-01T23:50:00Z", "purchase", amount="1.20"),
        ev("a2", "zoe", "2026-03-02T00:10:00Z", "purchase", amount="2.30"),
        ev("b1", "Ann", "2026-03-01T09:00:00Z", "purchase", amount="1.50"),
        ev("b2", "ANN", "2026-03-01T10:00:00Z", "purchase", amount="2.00"),
        ev("c1", "Bob", "2026-03-01T08:00:00Z"),
        ev("a3", "ZOE", "2026-03-02T01:00:00Z"),
        ev("d", "Dave", "2026-03-02T02:00:00Z", "purchase", amount="0.00"),
        ev("e", "Eve", "2026-03-03T00:00:00+02:00", "purchase", amount="4.00"),
    ])
    assert r.daily == {"days": [
        {"date": "2026-03-01", "sessions": 4, "users": 3, "revenue": "7.00", "top_user": "ann"},
        {"date": "2026-03-02", "sessions": 3, "users": 3, "revenue": "4.00", "top_user": "eve"},
    ], "duplicates_dropped": 0, "rejected": 0}
    assert pick(r.sessions, "session_id", "start", "end", "revenue") == [
        ("bob-1", "2026-03-01T08:00:00Z", "2026-03-01T08:00:00Z", "0.00"),
        ("ann-1", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", "1.50"),
        ("ann-2", "2026-03-01T10:00:00Z", "2026-03-01T10:00:00Z", "2.00"),
        ("zoe-1", "2026-03-01T23:50:00Z", "2026-03-02T00:10:00Z", "3.50"),
        ("zoe-2", "2026-03-02T01:00:00Z", "2026-03-02T01:00:00Z", "0.00"),
        ("dave-1", "2026-03-02T02:00:00Z", "2026-03-02T02:00:00Z", "0.00"),
        ("eve-1", "2026-03-02T22:00:00Z", "2026-03-02T22:00:00Z", "4.00"),
    ]


def test_daily_zero_revenue_top_user_null_even_with_purchases():
    r = run_job([ev("a", "z", "09:00:00", "purchase", amount="0"),
                 ev("b", "a", "09:00:00")])
    assert r.daily["days"] == [{"date": "2026-03-01", "sessions": 2, "users": 2,
                                "revenue": "0.00", "top_user": None}]


def test_csv_quotes_user_and_unicode_lowercase():
    r = run_job([ev("a", '  ÉVA, "X"  ', "09:00:00"), ev("b", 'éva, "x"', "09:01:00")])
    assert r.header == HEADER
    assert r.sessions == [row('éva, "x"-1', 'éva, "x"', "2026-03-01T09:00:00Z", "2026-03-01T09:01:00Z", 60, 2, 0, "0.00")]
    assert r.daily["days"][0]["users"] == 1


def test_existing_outputs_are_replaced_and_nested_output_created(tmp_path):
    inp = tmp_path / "input"
    out = tmp_path / "nested" / "output"
    inp.mkdir()
    (inp / "events.jsonl").write_text(ev("one", "Ann", "09:00:00") + "\n", encoding="utf-8")
    cmd = [sys.executable, str(APP_DIR) + "/job.py", "--in", str(inp), "--out", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and out.is_dir()
    assert "ann-1" in (out / "sessions.csv").read_text(encoding="utf-8")
    (inp / "events.jsonl").write_text("bad\n", encoding="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    assert (out / "sessions.csv").read_text(encoding="utf-8").splitlines() == [",".join(HEADER)]
    assert json.loads((out / "daily.json").read_text(encoding="utf-8")) == {"days": [], "duplicates_dropped": 0, "rejected": 1}
    assert [json.loads(x) for x in (out / "rejects.jsonl").read_text(encoding="utf-8").splitlines()] == [{"line": 1, "reason": "bad_json"}]


@pytest.mark.parametrize("second,expected_count", [
    ("2026-03-01T09:30:00.099999Z", 1),
    ("2026-03-01T09:30:00.100000Z", 1),
    ("2026-03-01T09:30:00.100001Z", 2),
])
def test_thirty_minute_boundary_uses_subsecond_precision(second, expected_count):
    r = run_job([ev("a", "u", "2026-03-01T09:00:00.100000Z"),
                 ev("b", "u", second)])
    assert len(r.sessions) == expected_count
    assert sum(int(s["events"]) for s in r.sessions) == 2


def test_daily_top_is_per_user_total_across_sessions_and_resets_each_day():
    r = run_job([
        ev("a1", "ann", "09:00:00", "purchase", amount="0.60"),
        ev("b1", "bob", "09:00:00", "purchase", amount="1.00"),
        ev("a2", "ann", "10:00:00", "purchase", amount="0.60"),
        ev("b2", "bob", "2026-03-02T09:00:00Z", "purchase", amount="0.01"),
        ev("a3", "ann", "2026-03-02T09:00:00Z"),
    ])
    assert r.daily["days"] == [
        {"date": "2026-03-01", "sessions": 3, "users": 2, "revenue": "2.20", "top_user": "ann"},
        {"date": "2026-03-02", "sessions": 2, "users": 2, "revenue": "0.01", "top_user": "bob"},
    ]
