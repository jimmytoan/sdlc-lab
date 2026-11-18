import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import APP_DIR, ev, pick, run_job


HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def row(session_id, user, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, [session_id, user, start, end, str(duration), str(events), str(purchases), revenue]))


def day(date, sessions, users, revenue, top_user):
    return dict(date=date, sessions=sessions, users=users, revenue=revenue, top_user=top_user)


def test_empty_and_blank_input_produces_all_files():
    r = run_job(["", "  \t ", "", "   "])
    assert r.code == 0
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER
    assert r.sessions == []
    assert r.rejects == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_missing_file_and_output_directory_creation(tmp_path):
    inp = tmp_path / "input"
    inp.mkdir()
    out = tmp_path / "nested" / "output"
    command = [sys.executable, str(Path(APP_DIR) / "job.py"), "--in", str(inp), "--out", str(out)]
    missing = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert missing.returncode == 2
    assert missing.stderr.strip()
    (inp / "events.jsonl").write_text('', encoding="utf-8")
    success = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert success.returncode == 0
    assert sorted(p.name for p in out.iterdir()) == ["daily.json", "rejects.jsonl", "sessions.csv"]
    # Existing output files must be replaced, even for an empty batch.
    (out / "rejects.jsonl").write_text('stale\n', encoding="utf-8")
    (out / "sessions.csv").write_text('stale\n', encoding="utf-8")
    (out / "daily.json").write_text('stale', encoding="utf-8")
    again = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert again.returncode == 0
    assert (out / "rejects.jsonl").read_text(encoding="utf-8") == ""
    assert (out / "sessions.csv").read_text(encoding="utf-8").splitlines() == [','.join(HEADER)]
    assert json.loads((out / "daily.json").read_text(encoding="utf-8")) == {
        "days": [], "duplicates_dropped": 0, "rejected": 0}


def test_reject_order_line_numbers_and_precedence():
    lines = [
        " ", "not json", "[]", "null", '"object"', '42',
        '{}',
        json.dumps({"event_id": "e", "user": "a", "ts": "invalid", "type": 2}),
        json.dumps({"event_id": "e", "user": "a", "ts": "invalid", "type": "bad"}),
        json.dumps({"event_id": "e", "user": "a", "ts": "2026-03-01T00:00:00Z", "type": "bad", "amount": -1}),
        ev("e", "a", "00:00:00", "purchase"),
        "\t", ev("good", "a", "00:00:00"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [
        *[{"line": n, "reason": "bad_json"} for n in range(2, 7)],
        {"line": 7, "reason": "missing_field"},
        {"line": 8, "reason": "missing_field"},
        {"line": 9, "reason": "bad_ts"},
        {"line": 10, "reason": "bad_type"},
        {"line": 11, "reason": "bad_amount"},
    ]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "0.00", None)],
                       "duplicates_dropped": 0, "rejected": 10}
    assert r.sessions == [row("a-1", "a", "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z", 0, 1, 0, "0.00")]


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_required_fields_absent_wrong_type_or_blank(field):
    base = dict(event_id="id", user="user", ts="2026-03-01T00:00:00Z", type="view")
    variants = []
    absent = base.copy()
    del absent[field]
    variants.append(absent)
    for value in [None, 0, False, [], {}, "", " \t "]:
        item = base.copy()
        item[field] = value
        variants.append(item)
    r = run_job([json.dumps(item) for item in variants])
    assert r.code == 0
    assert r.rejects == [{"line": i, "reason": "missing_field"} for i in range(1, len(variants) + 1)]
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(variants)}


def test_invalid_timestamps_and_types():
    bad_timestamps = [
        "2026-03-01T09:00:00", "2026-03-01", "2026-03-01T09:00Z",
        "2026-03-01 09:00:00Z", "2026-03-01T09:00:00z", "2026-03-01T09:00:00+0700",
        "2026-03-01T09:00:00+24:00", "2026-03-01T09:00:00-05:60",
        "2026-02-30T09:00:00Z", "2026-13-01T09:00:00Z", "2026-03-01T24:00:00Z",
        "2026-03-01T09:60:00Z", "2026-03-01T09:00:60Z", "2026-03-01T09:00:00.Z",
        "2026-03-01T09:00:00Zjunk",
    ]
    bad_types = ["View", "PURCHASE", "purchase ", " click", "logout", "refund", "other"]
    lines = [json.dumps(dict(event_id=str(i), user="a", ts=t, type="view")) for i, t in enumerate(bad_timestamps)]
    lines += [ev("t" + str(i), "a", "00:00:00", t, amount="1") for i, t in enumerate(bad_types)]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == ([{"line": i + 1, "reason": "bad_ts"} for i in range(len(bad_timestamps))] +
                         [{"line": len(bad_timestamps) + i + 1, "reason": "bad_type"} for i in range(len(bad_types))])
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(lines)}


def test_amount_validation_and_ignored_nonpurchase_amounts():
    invalid = [None, 0, 1.5, False, [], {}, "", " ", "-1", "+1", ".5", "1.", "1.234", "1e2", "NaN", "Infinity", "1,00", "1 2"]
    lines = [ev("absent", "a", "09:00:00", "purchase")]
    lines += [ev("bad" + str(i), "a", "09:00:00", "purchase", amount=v) for i, v in enumerate(invalid)]
    lines += [ev("ok" + str(i), "a", "09:00:00", "purchase", amount=v)
              for i, v in enumerate(["0", "5", "5.5", "19.99", "0.01", "0002.10"])]
    lines += [ev("v", "a", "09:00:00", "view", amount={"bad": True}),
              ev("c", "a", "09:00:00", "click", amount="-100")]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": i, "reason": "bad_amount"} for i in range(1, len(invalid) + 2)]
    assert r.sessions == [row("a-1", "a", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 8, 6, "32.60")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "32.60", "a")],
                       "duplicates_dropped": 0, "rejected": len(invalid) + 1}


def test_deduplicate_valid_events_globally_by_exact_id_and_utc_time():
    lines = [
        ev("same", "Wrong", "2026-03-01T12:00:00Z", "purchase", amount="99"),
        ev("tie", "First", "2026-03-01T10:00:00+01:00", "purchase", amount="3"),
        ev("same", " Winner ", "2026-03-01T10:00:00+02:00", "purchase", amount="2"),
        ev("tie", "Second", "2026-03-01T09:00:00Z", "purchase", amount="50"),
        ev("same", "Bad", "2026-03-01T07:00:00Z", "purchase", amount="-1"),
        ev("same ", "Winner", "2026-03-01T08:01:00Z"),
        ev("SAME", "Winner", "2026-03-01T08:02:00Z"),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{"line": 5, "reason": "bad_amount"}]
    assert r.sessions == [
        row("winner-1", "winner", "2026-03-01T08:00:00Z", "2026-03-01T08:02:00Z", 120, 3, 1, "2.00"),
        row("first-1", "first", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 1, "3.00"),
    ]
    assert r.daily == {"days": [day("2026-03-01", 2, 2, "5.00", "first")],
                       "duplicates_dropped": 2, "rejected": 1}


def test_fractional_gaps_session_numbering_order_and_duration():
    lines = [
        ev("a4", " ANN ", "2026-03-01T10:30:00.900001Z"),
        ev("b", "BOB", "2026-03-01T09:00:00.100Z"),
        ev("a2", "ann", "2026-03-01T09:30:00.900Z", "click"),
        ev("c", "zoe", "2026-03-01T09:00:00.050Z"),
        ev("a1", "Ann", "2026-03-01T09:00:00.900Z"),
        ev("d", "amy", "2026-03-01T09:00:00.100Z"),
        ev("a3", "ann", "2026-03-01T10:00:00.900Z", "purchase", amount="0.10"),
        ev("a5", "ann", "2026-03-01T10:30:02.100001Z", "purchase", amount="0.20"),
    ]
    r = run_job(lines)
    assert r.code == 0 and r.rejects == []
    assert r.sessions == [
        row("zoe-1", "zoe", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("amy-1", "amy", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("bob-1", "bob", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z", 3600, 3, 1, "0.10"),
        row("ann-2", "ann", "2026-03-01T10:30:00Z", "2026-03-01T10:30:02Z", 1, 2, 1, "0.20"),
    ]
    assert r.daily == {"days": [day("2026-03-01", 5, 4, "0.30", "ann")],
                       "duplicates_dropped": 0, "rejected": 0}


def test_daily_utc_start_date_aggregation_distinct_users_and_ties():
    lines = [
        ev("a1", "Ann", "2026-03-01T23:50:00Z", "purchase", amount="0.10"),
        ev("a2", "ann", "2026-03-02T00:10:00Z", "purchase", amount="0.20"),
        ev("a3", "ann", "2026-03-02T01:00:00+01:00", "view"),
        ev("a4", "ann", "2026-03-01T22:00:00Z", "purchase", amount="0.20"),
        ev("b1", "Bob", "2026-03-02T05:30:00+05:30", "purchase", amount="1"),
        ev("b2", "Bob", "2026-03-01T23:00:00Z", "purchase", amount="0.50"),
        ev("a5", "ann", "2026-03-01T20:00:00Z", "purchase", amount="0.30"),
        ev("a6", "ann", "2026-03-02T02:00:00Z", "purchase", amount="2"),
        ev("b3", "bob", "2026-03-02T02:00:00Z", "purchase", amount="2.00"),
        ev("z", "zoe", "2026-03-02T20:00:00-05:30"),
        ev("y", "yan", "2026-03-03T10:00:00Z", "purchase", amount="0.00"),
    ]
    r = run_job(lines)
    assert r.code == 0 and r.rejects == []
    assert r.daily == {"days": [
        day("2026-03-01", 4, 2, "1.30", "ann"),
        day("2026-03-02", 3, 2, "5.00", "bob"),
        day("2026-03-03", 2, 2, "0.00", None),
    ], "duplicates_dropped": 0, "rejected": 0}
    assert pick(r.sessions, "session_id", "start", "end", "events", "revenue") == [
        ("ann-1", "2026-03-01T20:00:00Z", "2026-03-01T20:00:00Z", "1", "0.30"),
        ("ann-2", "2026-03-01T22:00:00Z", "2026-03-01T22:00:00Z", "1", "0.20"),
        ("bob-1", "2026-03-01T23:00:00Z", "2026-03-01T23:00:00Z", "1", "0.50"),
        ("ann-3", "2026-03-01T23:50:00Z", "2026-03-02T00:10:00Z", "3", "0.30"),
        ("bob-2", "2026-03-02T00:00:00Z", "2026-03-02T00:00:00Z", "1", "1.00"),
        ("ann-4", "2026-03-02T02:00:00Z", "2026-03-02T02:00:00Z", "1", "2.00"),
        ("bob-3", "2026-03-02T02:00:00Z", "2026-03-02T02:00:00Z", "1", "2.00"),
        ("zoe-1", "2026-03-03T01:30:00Z", "2026-03-03T01:30:00Z", "1", "0.00"),
        ("yan-1", "2026-03-03T10:00:00Z", "2026-03-03T10:00:00Z", "1", "0.00"),
    ]


def test_top_user_tie_is_alphabetical_even_with_multiple_sessions():
    r = run_job([
        ev("b1", "bob", "08:00:00", "purchase", amount="3"),
        ev("a1", "ann", "09:00:00", "purchase", amount="1"),
        ev("a2", "ann", "10:00:00", "purchase", amount="2"),
        ev("c", "cara", "11:00:00"),
    ])
    assert r.daily == {"days": [day("2026-03-01", 4, 3, "6.00", "ann")],
                       "duplicates_dropped": 0, "rejected": 0}


def test_duplicate_timestamp_precision_and_csv_quoting():
    r = run_job([
        ev("id", "discard", "2024-02-29T12:00:00.9Z", "purchase", amount="7"),
        ev("id", "  ÉVE, X  ", "2024-02-29T12:00:00.1Z", "purchase", amount="0.01"),
        ev("id", "discard too", "2024-02-29T12:00:00.100000+00:00", "purchase", amount="9"),
        ev("x", "éve, x", "2024-02-29T12:30:00.099999Z", "purchase", amount="0.02"),
        ev("y", "éve, x", "2024-02-29T13:00:00.100000Z"),
    ])
    assert r.code == 0 and r.header == HEADER and r.rejects == []
    assert r.sessions == [
        row("éve, x-1", "éve, x", "2024-02-29T12:00:00Z", "2024-02-29T12:30:00Z", 1799, 2, 2, "0.03"),
        row("éve, x-2", "éve, x", "2024-02-29T13:00:00Z", "2024-02-29T13:00:00Z", 0, 1, 0, "0.00"),
    ]
    assert r.daily == {"days": [day("2024-02-29", 2, 1, "0.03", "éve, x")],
                       "duplicates_dropped": 2, "rejected": 0}
