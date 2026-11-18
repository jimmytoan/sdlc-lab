"""Black-box regression tests for the sessionize batch contract."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import APP_DIR, ev, run_job


HEADER = ["session_id", "user", "start", "end", "duration_s", "events", "purchases", "revenue"]


def row(id, user, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, [id, user, start, end, str(duration), str(events), str(purchases), revenue]))


def day(date, sessions, users, revenue, top):
    return {"date": date, "sessions": sessions, "users": users, "revenue": revenue, "top_user": top}


def obj(**changes):
    base = {"event_id": "e", "user": "Ann", "ts": "2026-03-01T09:00:00Z", "type": "view"}
    base.update(changes)
    return base


def check_success(r):
    assert r.code == 0, r.stderr
    assert r.files == ["daily.json", "rejects.jsonl", "sessions.csv"]
    assert r.header == HEADER


def test_empty_and_whitespace_only_inputs():
    for lines in ([], ["", "   ", "\t", "  \t  "]):
        r = run_job(lines)
        check_success(r)
        assert r.sessions == []
        assert r.rejects == []
        assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": 0}


def test_missing_input():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip() and "events.jsonl" in r.stderr


def test_recreates_outputs_in_existing_directory(tmp_path):
    inp = tmp_path / "input"
    out = tmp_path / "nested" / "output"
    inp.mkdir()
    (inp / "events.jsonl").write_text(ev("1", "Ann", "09:00:00") + "\n", encoding="utf-8")
    cmd = [sys.executable, str(Path(APP_DIR) / "job.py"), "--in", str(inp), "--out", str(out)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert sorted(p.name for p in out.iterdir()) == ["daily.json", "rejects.jsonl", "sessions.csv"]
    (inp / "events.jsonl").write_text("bad json\n", encoding="utf-8")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert (out / "sessions.csv").read_text(encoding="utf-8").splitlines() == [",".join(HEADER)]
    assert json.loads((out / "daily.json").read_text(encoding="utf-8")) == {"days": [], "duplicates_dropped": 0, "rejected": 1}
    assert [json.loads(x) for x in (out / "rejects.jsonl").read_text(encoding="utf-8").splitlines()] == [{"line": 1, "reason": "bad_json"}]


def test_reject_json_and_physical_line_numbers():
    lines = ["", "   ", "{broken", "null", "[]", '"text"', "42", "true", "{}", "\t", ev("ok", "u", "09:00:00"), "{"]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == ([{"line": i, "reason": "bad_json"} for i in range(3, 9)] +
                         [{"line": 9, "reason": "missing_field"}, {"line": 12, "reason": "bad_json"}])
    assert r.daily["rejected"] == 8
    assert len(r.sessions) == 1 and r.sessions[0]["events"] == "1"


@pytest.mark.parametrize("field", ["event_id", "user", "ts", "type"])
def test_required_fields_are_nonblank_strings(field):
    cases = []
    missing = obj()
    del missing[field]
    cases.append(missing)
    for value in (None, 12, False, [], {}, "", "  \t "):
        cases.append(obj(**{field: value}))
    r = run_job([json.dumps(x) for x in cases])
    check_success(r)
    assert r.rejects == [{"line": i, "reason": "missing_field"} for i in range(1, len(cases) + 1)]
    assert r.sessions == []
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(cases)}


def test_first_failure_order_and_invalid_duplicates():
    cases = [
        obj(event_id="", ts="bad", type="bad"),
        obj(ts="bad", type="bad", amount="bad"),
        obj(type="bad", amount="bad"),
        obj(type="purchase", amount="bad"),
        obj(event_id="same", ts="bad"),
        obj(event_id="same", type="purchase", amount="-1"),
        obj(event_id="same", user="Valid", type="purchase", amount="2"),
    ]
    r = run_job([json.dumps(x) for x in cases])
    check_success(r)
    assert r.rejects == [{"line": i, "reason": reason} for i, reason in enumerate(
        ["missing_field", "bad_ts", "bad_type", "bad_amount", "bad_ts", "bad_amount"], 1)]
    assert r.sessions == [row("valid-1", "valid", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 1, "2.00")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "2.00", "valid")], "duplicates_dropped": 0, "rejected": 6}


def test_invalid_timestamps():
    invalid = ["not a date", "2026-03-01", "2026-03-01T09:00:00", "2026-03-01T09:00Z",
               "2026-03-01 09:00:00Z", "2026-03-01T09:00:00z", "2026-03-01T09:00:00+0700",
               "2026-03-01T09:00:00+07", "2026-03-01T09:00:00+24:00", "2026-03-01T09:00:00-05:60",
               "2026-02-29T09:00:00Z", "2026-13-01T09:00:00Z", "2026-03-01T24:00:00Z",
               "2026-03-01T09:60:00Z", "2026-03-01T09:00:60Z", "2026-03-01T09:00:00.Z"]
    r = run_job([json.dumps(obj(event_id=str(i), ts=t)) for i, t in enumerate(invalid)])
    check_success(r)
    assert r.rejects == [{"line": i, "reason": "bad_ts"} for i in range(1, len(invalid) + 1)]
    assert r.daily == {"days": [], "duplicates_dropped": 0, "rejected": len(invalid)}


def test_types_and_amount_validation():
    bad_types = ["VIEW", "Purchase", " view ", "logout", "refund", "dance"]
    bad_amounts = [None, 5, 0.5, False, [], {}, "", " ", "-1", "+1", ".5", "5.", "1.234", "1e2", "NaN", "Infinity", "1,00"]
    cases = [obj(event_id=f"t{i}", type=t) for i, t in enumerate(bad_types)]
    cases += [obj(event_id="missing", type="purchase")]
    cases += [obj(event_id=f"a{i}", type="purchase", amount=a) for i, a in enumerate(bad_amounts)]
    cases += [obj(event_id="v", type="view", amount={"bad": True}),
              obj(event_id="c", type="click", amount="-99")]
    r = run_job([json.dumps(x) for x in cases])
    check_success(r)
    expected = ([{"line": i, "reason": "bad_type"} for i in range(1, len(bad_types) + 1)] +
                [{"line": i, "reason": "bad_amount"} for i in range(len(bad_types) + 1, len(bad_types) + len(bad_amounts) + 2)])
    assert r.rejects == expected
    assert r.sessions == [row("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 2, 0, "0.00")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "0.00", None)], "duplicates_dropped": 0, "rejected": len(expected)}


def test_decimal_amounts_are_exact_and_zero_purchases_count():
    amounts = ["0", "0.00", "5", "5.5", "19.99", "0.01", "0.10", "0.20", " 1.20 "]
    r = run_job([ev(str(i), "  ANN  ", "09:00:00", "purchase", amount=a) for i, a in enumerate(amounts)])
    check_success(r)
    assert r.rejects == []
    assert r.sessions == [row("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 9, 9, "32.00")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "32.00", "ann")], "duplicates_dropped": 0, "rejected": 0}


def test_duplicate_earliest_timestamp_global_exact_id_and_equal_tie():
    lines = [ev("x", "late", "10:00:00", "purchase", amount="99"),
             ev("same", "First", "2026-03-01T09:00:00Z", "purchase", amount="3"),
             ev("x", "earliest", "2026-03-01T10:30:00+02:00", "purchase", amount="7"),
             ev("same", "Second", "2026-03-01T10:00:00+01:00", "purchase", amount="100"),
             ev(" x ", "earliest", "08:31:00"),
             ev("X", "earliest", "08:32:00"),
             ev("x", "middle", "09:15:00"),
             ev("invalid", "none", "bad", "purchase", amount="2")]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{"line": 8, "reason": "bad_ts"}]
    assert r.sessions == [
        row("earliest-1", "earliest", "2026-03-01T08:30:00Z", "2026-03-01T08:32:00Z", 120, 3, 1, "7.00"),
        row("first-1", "first", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 1, "3.00")]
    assert r.daily == {"days": [day("2026-03-01", 2, 2, "10.00", "earliest")], "duplicates_dropped": 3, "rejected": 1}


def test_gap_precision_chained_events_duration_and_order():
    lines = [ev("a4", "  Ann ", "2026-03-01T10:30:01.000001Z"),
             ev("b", "Bob", "2026-03-01T09:00:00.100000Z"),
             ev("a3", "ANN", "2026-03-01T10:00:01.000000Z"),
             ev("z", "Zed", "2026-03-01T09:00:00.050000Z"),
             ev("a1", "ann", "2026-03-01T09:00:00.100000Z"),
             ev("a2", "ann", "2026-03-01T09:30:00.100000Z"),
             ev("a5", "ann", "2026-03-01T10:30:01.900000Z")]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == []
    assert r.sessions == [
        row("zed-1", "zed", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("ann-1", "ann", "2026-03-01T09:00:00Z", "2026-03-01T09:30:00Z", 1800, 2, 0, "0.00"),
        row("bob-1", "bob", "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 1, 0, "0.00"),
        row("ann-2", "ann", "2026-03-01T10:00:01Z", "2026-03-01T10:00:01Z", 0, 1, 0, "0.00"),
        row("ann-3", "ann", "2026-03-01T10:30:01Z", "2026-03-01T10:30:01Z", 0, 2, 0, "0.00")]
    assert r.daily == {"days": [day("2026-03-01", 5, 3, "0.00", None)], "duplicates_dropped": 0, "rejected": 0}


def test_daily_utc_dates_cross_midnight_ties_and_per_user_totals():
    lines = [ev("1", "Bob", "2026-03-02T00:05:00Z", "purchase", amount="2.00"),
             ev("2", "Bob", "2026-03-01T23:55:00Z", "purchase", amount="3.00"),
             ev("3", "Ann", "2026-03-01T16:00:00-05:30", "purchase", amount="2.00"),
             ev("4", "Ann", "2026-03-01T23:30:00Z", "purchase", amount="3.00"),
             ev("5", "Charlie", "2026-03-02T01:00:00+02:00"),
             ev("6", "Bob", "2026-03-02T09:00:00Z"),
             ev("7", "Ann", "2026-03-03T00:10:00+01:00", "purchase", amount="4.00")]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == []
    assert r.sessions == [
        row("ann-1", "ann", "2026-03-01T21:30:00Z", "2026-03-01T21:30:00Z", 0, 1, 1, "2.00"),
        row("charlie-1", "charlie", "2026-03-01T23:00:00Z", "2026-03-01T23:00:00Z", 0, 1, 0, "0.00"),
        row("ann-2", "ann", "2026-03-01T23:30:00Z", "2026-03-01T23:30:00Z", 0, 1, 1, "3.00"),
        row("bob-1", "bob", "2026-03-01T23:55:00Z", "2026-03-02T00:05:00Z", 600, 2, 2, "5.00"),
        row("bob-2", "bob", "2026-03-02T09:00:00Z", "2026-03-02T09:00:00Z", 0, 1, 0, "0.00"),
        row("ann-3", "ann", "2026-03-02T23:10:00Z", "2026-03-02T23:10:00Z", 0, 1, 1, "4.00")]
    assert r.daily == {"days": [day("2026-03-01", 4, 3, "10.00", "ann"), day("2026-03-02", 2, 2, "4.00", "ann")], "duplicates_dropped": 0, "rejected": 0}


def test_csv_quoting_unicode_user_and_extra_fields():
    r = run_job([json.dumps(obj(event_id="e1", user='  ÉVE, \"Boss\"  ', extra="ignored"), ensure_ascii=False),
                 json.dumps(obj(event_id="e2", user='éve, \"boss\"', type="click"), ensure_ascii=False)])
    check_success(r)
    assert r.rejects == []
    assert r.sessions == [row('éve, "boss"-1', 'éve, "boss"', "2026-03-01T09:00:00Z", "2026-03-01T09:00:00Z", 0, 2, 0, "0.00")]
    assert r.daily == {"days": [day("2026-03-01", 1, 1, "0.00", None)], "duplicates_dropped": 0, "rejected": 0}


def test_chained_exact_gaps_fractional_duration_and_timezone_equivalence():
    # The session extends beyond 30 minutes from its start: each gap is measured
    # from the previous event, with fractional precision, after UTC conversion.
    lines = [ev("4", "u", "2026-02-29T01:30:00Z"),  # 2026 is not a leap year
             ev("3", "u", "2024-03-01T00:00:00.900000+00:00", "purchase", amount="0.01"),
             ev("1", "u", "2024-02-29T18:30:00.900000-04:30"),
             ev("2", "U", "2024-02-29T23:30:00.900000Z", "click"),
             ev("5", "u", "2024-03-01T00:00:02.100000Z", "purchase", amount="0.02"),
             ev("6", "u", "2024-03-01T00:30:02.100001Z")]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{"line": 1, "reason": "bad_ts"}]
    assert r.sessions == [
        row("u-1", "u", "2024-02-29T23:00:00Z", "2024-03-01T00:00:02Z", 3601, 4, 2, "0.03"),
        row("u-2", "u", "2024-03-01T00:30:02Z", "2024-03-01T00:30:02Z", 0, 1, 0, "0.00")]
    assert r.daily == {"days": [day("2024-02-29", 1, 1, "0.03", "u"), day("2024-03-01", 1, 1, "0.00", None)], "duplicates_dropped": 0, "rejected": 1}
