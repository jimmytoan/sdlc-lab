import json
import os
import subprocess
import sys

from helpers import APP_DIR, ev, pick, run_job

HEADER = ['session_id', 'user', 'start', 'end', 'duration_s', 'events', 'purchases', 'revenue']
FILES = ['daily.json', 'rejects.jsonl', 'sessions.csv']


def row(*values):
    return dict(zip(HEADER, map(str, values)))


def test_empty_and_whitespace():
    for lines in ([], ['', ' ', '\t']):
        r = run_job(lines)
        assert r.code == 0
        assert r.files == FILES and r.header == HEADER
        assert r.sessions == [] and r.rejects == []
        assert r.daily == {'days': [], 'duplicates_dropped': 0, 'rejected': 0}


def test_validation_and_error_precedence():
    valid = {'event_id': 'id', 'user': 'Ann', 'ts': '2026-03-01T09:00:00Z', 'type': 'purchase', 'amount': '1.00'}
    lines, expected = ['  ', '\t'], []

    def add(raw, reason):
        lines.append(raw)
        expected.append({'line': len(lines), 'reason': reason})

    def obj(changes, reason, remove=()):
        item = valid | changes
        for field in remove:
            item.pop(field, None)
        add(json.dumps(item), reason)

    for raw in ('{', 'null', '[]', '"string"', '12', 'true', '{"a": 1} trailing'):
        add(raw, 'bad_json')
    for field in ('event_id', 'user', 'ts', 'type'):
        obj({}, 'missing_field', remove=(field,))
        for value in (None, 0, True, [], {}, '', ' \t '):
            obj({field: value}, 'missing_field')
    for ts in ('not a date', '2026-03-01', '2026-03-01T09:00Z',
               '2026-03-01T09:00:00', '2026-02-30T09:00:00Z',
               '2026-03-01T25:00:00Z', '2026-03-01T09:00:00+25:00'):
        obj({'ts': ts}, 'bad_ts')
    for typ in ('VIEW', ' view ', 'dance'):
        obj({'type': typ}, 'bad_type')
    obj({}, 'bad_amount', remove=('amount',))
    for amount in (None, 1, 1.25, True, [], '', '-0.01', '1.234', '1.', '.5', '1e2', 'NaN', 'Infinity'):
        obj({'amount': amount}, 'bad_amount')
    obj({'ts': 'bad', 'type': 'bad', 'amount': 'bad'}, 'missing_field', remove=('event_id',))
    obj({'ts': 'bad', 'type': 'bad', 'amount': 'bad'}, 'bad_ts')
    obj({'type': 'bad', 'amount': 'bad'}, 'bad_type')
    # All rejected records reuse the id; none should enter deduplication.
    lines.append(json.dumps(valid))
    r = run_job(lines)
    assert r.code == 0 and r.rejects == expected
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 1, 'users': 1,
                                  'revenue': '1.00', 'top_user': 'ann'}],
                       'duplicates_dropped': 0, 'rejected': len(expected)}
    assert r.sessions == [row('ann-1', 'ann', '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 1, 1, '1.00')]


def test_all_rejected():
    r = run_job(['{', '', '[]', ev('x', 'u', 'bad')])
    assert r.code == 0 and r.files == FILES and r.header == HEADER and r.sessions == []
    assert r.rejects == [{'line': 1, 'reason': 'bad_json'}, {'line': 3, 'reason': 'bad_json'}, {'line': 4, 'reason': 'bad_ts'}]
    assert r.daily == {'days': [], 'duplicates_dropped': 0, 'rejected': 3}


def test_amounts_and_user_normalization():
    lines = [ev('a', '  ANN  ', '09:00:00', 'purchase', amount='0'),
             ev('b', 'ann', '09:01:00', 'purchase', amount='5'),
             ev('c', 'Ann', '09:02:00', 'purchase', amount='5.5'),
             ev('d', 'ANN', '09:03:00', 'purchase', amount='0.01'),
             ev('e', 'ann', '09:04:00', 'purchase', amount='0.02'),
             ev('f', 'ann', '09:05:00', 'view', amount={'invalid': True}),
             ev('g', 'ann', '09:06:00', 'click', amount=-10)]
    r = run_job(lines)
    assert r.code == 0 and r.rejects == []
    assert r.sessions == [row('ann-1', 'ann', '2026-03-01T09:00:00Z', '2026-03-01T09:06:00Z', 360, 7, 5, '10.53')]
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 1, 'users': 1,
                                  'revenue': '10.53', 'top_user': 'ann'}],
                       'duplicates_dropped': 0, 'rejected': 0}


def test_dedup_global_earliest_utc_ties_exact_ids():
    lines = [
        ev('dup', 'late', '2026-03-01T12:00:00Z', 'purchase', amount='100'),
        ev('tie', 'first', '2026-03-01T10:00:00+01:00', 'purchase', amount='2'),
        ev('tie', 'second', '2026-03-01T09:00:00Z', 'purchase', amount='50'),
        ev('dup', 'winner', '2026-03-01T11:30:00+03:00', 'purchase', amount='3'),
        ev('dup', 'middle', '2026-03-01T10:00:00Z'),
        ev(' dup', 'winner', '2026-03-01T08:31:00Z'),
        ev('dup ', 'winner', '2026-03-01T08:32:00Z'),
        ev('DUP', 'winner', '2026-03-01T08:33:00Z'),
        ev('dup', 'invalid', '2026-03-01T01:00:00Z', 'purchase', amount='-1'),
    ]
    r = run_job(lines)
    assert r.code == 0 and r.rejects == [{'line': 9, 'reason': 'bad_amount'}]
    assert r.sessions == [
        row('winner-1', 'winner', '2026-03-01T08:30:00Z', '2026-03-01T08:33:00Z', 180, 4, 1, '3.00'),
        row('first-1', 'first', '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 1, 1, '2.00')]
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 2, 'users': 2,
                                  'revenue': '5.00', 'top_user': 'winner'}],
                       'duplicates_dropped': 3, 'rejected': 1}


def test_fractional_dedup_keeps_earlier_even_when_later_in_file():
    r = run_job([ev('same', 'wrong', '2026-03-01T09:00:00.9Z'),
                 ev('same', 'right', '2026-03-01T09:00:00.1Z')])
    assert r.code == 0 and r.rejects == []
    assert r.sessions == [row('right-1', 'right', '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 1, 0, '0.00')]
    assert r.daily['duplicates_dropped'] == 1


def test_full_precision_gap_duration_and_row_order():
    lines = [ev('z3', 'z', '2026-03-01T01:00:00.100001Z'),
             ev('a2', 'a', '2026-03-01T00:00:02Z'),
             ev('z2', 'z', '2026-03-01T00:30:00.100Z'),
             ev('a1', 'a', '2026-03-01T00:00:00.900Z'),
             ev('z1', 'z', '2026-03-01T00:00:00.100Z')]
    r = run_job(lines)
    assert r.code == 0 and r.rejects == []
    assert r.sessions == [
        row('z-1', 'z', '2026-03-01T00:00:00Z', '2026-03-01T00:30:00Z', 1800, 2, 0, '0.00'),
        row('a-1', 'a', '2026-03-01T00:00:00Z', '2026-03-01T00:00:02Z', 1, 2, 0, '0.00'),
        row('z-2', 'z', '2026-03-01T01:00:00Z', '2026-03-01T01:00:00Z', 0, 1, 0, '0.00')]
    assert r.daily['days'] == [{'date': '2026-03-01', 'sessions': 3, 'users': 2, 'revenue': '0.00', 'top_user': None}]


def test_ties_and_sessions_use_gap_to_previous_not_first():
    lines = [ev('a2', 'ALICE', '11:00:00'), ev('z', 'zoe', '10:00:00'),
             ev('a1', ' alice ', '10:00:00'), ev('b', 'bob', '10:00:00'),
             ev('a3', 'alice', '11:30:00'), ev('a4', 'alice', '12:00:00'),
             ev('a5', 'alice', '12:30:01')]
    r = run_job(lines)
    assert r.code == 0
    assert pick(r.sessions, 'session_id', 'start', 'end', 'events') == [
        ('alice-1', '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', '1'),
        ('bob-1', '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', '1'),
        ('zoe-1', '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', '1'),
        ('alice-2', '2026-03-01T11:00:00Z', '2026-03-01T12:00:00Z', '3'),
        ('alice-3', '2026-03-01T12:30:01Z', '2026-03-01T12:30:01Z', '1')]
    assert r.daily['days'] == [{'date': '2026-03-01', 'sessions': 5, 'users': 3, 'revenue': '0.00', 'top_user': None}]


def test_daily_start_date_cross_midnight_utc_and_revenue_ties():
    lines = [ev('a1', 'Ann', '2026-03-02T00:50:00+01:00', 'purchase', amount='2.00'),
             ev('a2', 'ann', '2026-03-02T00:10:00Z', 'purchase', amount='3.00'),
             ev('b1', 'bob', '2026-03-01T23:50:00Z', 'purchase', amount='5.00'),
             ev('b2', 'bob', '2026-03-01T18:00:00-05:30'),
             ev('a3', 'ann', '2026-03-01T20:00:00Z', 'purchase', amount='1.25'),
             ev('b3', 'bob', '2026-03-01T21:00:00Z', 'purchase', amount='1.25'),
             ev('c', 'cat', '2026-03-03T00:01:00+02:00'),
             ev('d', 'dog', '2026-03-04T00:00:00Z', 'purchase', amount='0.00')]
    r = run_job(lines)
    assert r.code == 0 and r.rejects == []
    assert r.sessions == [
        row('ann-1', 'ann', '2026-03-01T20:00:00Z', '2026-03-01T20:00:00Z', 0, 1, 1, '1.25'),
        row('bob-1', 'bob', '2026-03-01T21:00:00Z', '2026-03-01T21:00:00Z', 0, 1, 1, '1.25'),
        row('bob-2', 'bob', '2026-03-01T23:30:00Z', '2026-03-01T23:50:00Z', 1200, 2, 1, '5.00'),
        row('ann-2', 'ann', '2026-03-01T23:50:00Z', '2026-03-02T00:10:00Z', 1200, 2, 2, '5.00'),
        row('cat-1', 'cat', '2026-03-02T22:01:00Z', '2026-03-02T22:01:00Z', 0, 1, 0, '0.00'),
        row('dog-1', 'dog', '2026-03-04T00:00:00Z', '2026-03-04T00:00:00Z', 0, 1, 1, '0.00')]
    assert r.daily == {'days': [
        {'date': '2026-03-01', 'sessions': 4, 'users': 2, 'revenue': '12.50', 'top_user': 'ann'},
        {'date': '2026-03-02', 'sessions': 1, 'users': 1, 'revenue': '0.00', 'top_user': None},
        {'date': '2026-03-04', 'sessions': 1, 'users': 1, 'revenue': '0.00', 'top_user': None}],
        'duplicates_dropped': 0, 'rejected': 0}


def test_top_user_sums_multiple_sessions():
    r = run_job([ev('a', 'ann', '09:00:00', 'purchase', amount='4'),
                 ev('b', 'bob', '09:00:00', 'purchase', amount='7'),
                 ev('c', 'ann', '10:00:00', 'purchase', amount='4')])
    assert r.code == 0
    assert r.daily['days'] == [{'date': '2026-03-01', 'sessions': 3, 'users': 2, 'revenue': '15.00', 'top_user': 'ann'}]


def test_unicode_and_csv_quoting():
    r = run_job([ev('1', '  ÉVA, "X"  ', '09:00:00'), ev('2', 'éva, "x"', '09:01:00')])
    assert r.code == 0 and r.rejects == [] and r.header == HEADER
    assert r.sessions == [row('éva, "x"-1', 'éva, "x"', '2026-03-01T09:00:00Z', '2026-03-01T09:01:00Z', 60, 2, 0, '0.00')]
    assert r.daily['days'][0]['users'] == 1


def test_cli_creates_output_and_overwrites_results(tmp_path):
    inp = tmp_path / 'input'
    inp.mkdir()
    events = inp / 'events.jsonl'
    out = tmp_path / 'nested' / 'output'
    command = [sys.executable, os.path.join(APP_DIR, 'job.py'), '--in', str(inp), '--out', str(out)]
    events.write_text(ev('1', 'ann', '09:00:00') + '\ninvalid\n', encoding='utf-8')
    first = subprocess.run(command, cwd=APP_DIR, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0
    assert sorted(p.name for p in out.iterdir()) == FILES
    events.write_text('', encoding='utf-8')
    second = subprocess.run(command, cwd=APP_DIR, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0
    assert (out / 'sessions.csv').read_text(encoding='utf-8').splitlines() == [','.join(HEADER)]
    assert (out / 'rejects.jsonl').read_text(encoding='utf-8') == ''
    assert json.loads((out / 'daily.json').read_text(encoding='utf-8')) == {'days': [], 'duplicates_dropped': 0, 'rejected': 0}


def test_missing_file_fails_with_stderr():
    r = run_job([], make_input=False)
    assert r.code == 2 and r.stderr.strip()
