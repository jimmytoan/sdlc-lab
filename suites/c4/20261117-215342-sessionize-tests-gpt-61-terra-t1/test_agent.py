import json
import os
import subprocess
import sys

from helpers import APP_DIR, ev, pick, run_job


HEADER = ['session_id', 'user', 'start', 'end', 'duration_s', 'events', 'purchases', 'revenue']


def obj(**changes):
    event = {'event_id': 'id', 'user': 'Ann', 'ts': '2026-03-01T09:00:00Z', 'type': 'view'}
    event.update(changes)
    return json.dumps(event)


def row(user, number, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, [f'{user}-{number}', user, start, end, str(duration), str(events), str(purchases), revenue]))


def test_empty_blank_and_all_rejected_outputs():
    r = run_job(['', '  \t ', 'null', '[]', 'broken', '   '])
    assert r.code == 0
    assert r.files == ['daily.json', 'rejects.jsonl', 'sessions.csv']
    assert r.header == HEADER and r.sessions == []
    assert r.rejects == [{'line': 3, 'reason': 'bad_json'}, {'line': 4, 'reason': 'bad_json'}, {'line': 5, 'reason': 'bad_json'}]
    assert r.daily == {'days': [], 'duplicates_dropped': 0, 'rejected': 3}
    r = run_job([])
    assert r.code == 0 and r.header == HEADER and r.sessions == [] and r.rejects == []
    assert r.daily == {'days': [], 'duplicates_dropped': 0, 'rejected': 0}


def test_missing_input():
    r = run_job([], make_input=False)
    assert r.code == 2 and 'events.jsonl' in r.stderr and r.stderr.strip()


def test_field_validation_and_first_error_precedence():
    lines = ['  ', '{', '42', '"object"', 'true', 'null', '[]']
    reasons = ['bad_json'] * 6
    for field in ('event_id', 'user', 'ts', 'type'):
        for val in (None, 123, False, [], {}, '', ' \t '):
            lines.append(obj(**{field: val}))
            reasons.append('missing_field')
        d = json.loads(obj())
        del d[field]
        lines.append(json.dumps(d))
        reasons.append('missing_field')
    # Multiple simultaneous failures must report the first stage, not a later one.
    lines.extend([
        obj(user='', ts='no', type='wrong'),
        obj(ts='no', type='wrong'),
        obj(type='wrong', amount=None),
        obj(type='purchase', amount=None),
        ev('good', 'Ann', '09:00:00'),
    ])
    reasons.extend(['missing_field', 'bad_ts', 'bad_type', 'bad_amount'])
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{'line': i + 2, 'reason': reason} for i, reason in enumerate(reasons)]
    assert r.daily['rejected'] == len(reasons)
    assert r.daily['duplicates_dropped'] == 0
    assert r.sessions == [row('ann', 1, '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 1, 0, '0.00')]


def test_timestamp_validation_and_offsets():
    invalid = [
        '2026-03-01T09:00:00', '2026-03-01', '2026-03-01T09:00Z',
        '2026-03-01 09:00:00Z', '2026-03-01T09:00:00z',
        '2026-03-01T09:00:00+0700', '2026-03-01T09:00:00+07',
        '2026-03-01T09:00:00+24:00', '2026-03-01T09:00:00-05:60',
        '2026-02-30T09:00:00Z', '2026-03-01T24:00:00Z',
        '2026-03-01T09:60:00Z', '2026-03-01T09:00:60Z',
        '2026-03-01T09:00:00.Z', '2026-03-01T09:00:00Zjunk',
    ]
    lines = [obj(event_id=f'bad{i}', ts=ts) for i, ts in enumerate(invalid)]
    lines += [
        ev('a', 'A', '2026-03-01T00:15:00+05:30'),
        ev('b', 'B', '2026-02-28T19:45:00-05:30'),
        ev('c', 'C', '2026-03-01T09:00:00.250+07:00'),
    ]
    r = run_job(lines)
    assert r.code == 0
    assert r.rejects == [{'line': i + 1, 'reason': 'bad_ts'} for i in range(len(invalid))]
    assert pick(r.sessions, 'user', 'start') == [
        ('a', '2026-02-28T18:45:00Z'), ('b', '2026-03-01T01:15:00Z'), ('c', '2026-03-01T02:00:00Z')]
    assert [d['date'] for d in r.daily['days']] == ['2026-02-28', '2026-03-01']


def test_type_and_purchase_amount_validation():
    invalid_amounts = [None, 0, 5.5, True, [], {}, '', ' ', '-1', '+1', '.5', '1.', '1.234', '1e2', '1,00', 'NaN', 'Infinity']
    lines = [obj(event_id=f'a{i}', type='purchase', amount=v) for i, v in enumerate(invalid_amounts)]
    no_amount = json.loads(obj(event_id='absent', type='purchase'))
    lines.append(json.dumps(no_amount))
    bad_types = ['Purchase', 'VIEW', 'click ', ' refund', 'refund', 'logout', 'other']
    lines.extend(obj(event_id=f't{i}', type=t, amount='1') for i, t in enumerate(bad_types))
    lines += [
        obj(event_id='v', type='view', amount={'ignored': True}),
        obj(event_id='c', type='click', amount='-bad'),
        obj(event_id='p1', type='purchase', amount='5'),
        obj(event_id='p2', type='purchase', amount='5.5'),
        obj(event_id='p3', type='purchase', amount='19.99'),
        obj(event_id='p4', type='purchase', amount='0.00'),
        obj(event_id='p5', type='purchase', amount='0.01'),
    ]
    r = run_job(lines)
    bad_amount_count = len(invalid_amounts) + 1
    assert r.rejects == ([{'line': i + 1, 'reason': 'bad_amount'} for i in range(bad_amount_count)] +
                         [{'line': bad_amount_count + i + 1, 'reason': 'bad_type'} for i in range(len(bad_types))])
    assert r.daily['rejected'] == bad_amount_count + len(bad_types)
    assert r.daily['duplicates_dropped'] == 0
    assert r.sessions == [row('ann', 1, '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 7, 5, '30.50')]
    assert r.daily['days'][0]['revenue'] == '30.50'


def test_duplicates_are_global_exact_id_valid_only_earliest_and_first_tie():
    lines = [
        ev('x', 'Late', '10:00:00', 'purchase', amount='99'),
        ev('same', 'First', '2026-03-01T09:00:00+01:00', 'purchase', amount='3'),
        ev('x', ' Early ', '08:30:00', 'purchase', amount='2'),
        ev('same', 'Second', '08:00:00', 'purchase', amount='7'),
        ev('x', 'Wrong', '07:00:00', 'purchase', amount='-1'),
        ev(' x', 'Early', '08:31:00'),
        ev('X', 'Early', '08:32:00'),
        ev('unique', 'Third', '08:00:00'),
        ev('unique', 'Third', '08:00:00'),
    ]
    r = run_job(lines)
    assert r.rejects == [{'line': 5, 'reason': 'bad_amount'}]
    assert r.daily['duplicates_dropped'] == 3 and r.daily['rejected'] == 1
    assert r.sessions == [
        row('first', 1, '2026-03-01T08:00:00Z', '2026-03-01T08:00:00Z', 0, 1, 1, '3.00'),
        row('third', 1, '2026-03-01T08:00:00Z', '2026-03-01T08:00:00Z', 0, 1, 0, '0.00'),
        row('early', 1, '2026-03-01T08:30:00Z', '2026-03-01T08:32:00Z', 120, 3, 1, '2.00'),
    ]
    assert r.daily['days'] == [{'date': '2026-03-01', 'sessions': 3, 'users': 3, 'revenue': '5.00', 'top_user': 'first'}]


def test_sessions_gap_full_precision_duration_sort_and_user_normalization():
    lines = [
        ev('1', '  ANN ', '2026-03-01T10:00:00.900Z'),
        ev('2', 'ann', '2026-03-01T10:30:00.900Z', 'purchase', amount='0.10'),
        ev('3', 'Ann', '2026-03-01T11:00:00.901Z', 'purchase', amount='0.20'),
        ev('4', 'aNN', '2026-03-01T11:00:01.100Z'),
        ev('5', 'Ann', '2026-03-01T11:30:01.100Z'),
        ev('6', 'ann', '2026-03-01T12:01:00Z'),
        ev('7', 'zoe', '2026-03-01T10:00:00.100Z'),
        ev('8', 'bob', '2026-03-01T10:00:00.100Z'),
        ev('9', 'amy', '2026-03-01T10:00:00.800Z'),
    ]
    # Input intentionally out of order; session gaps are between adjacent events, not from the start.
    r = run_job(list(reversed(lines)))
    assert r.code == 0 and r.rejects == []
    assert r.sessions == [
        row('bob', 1, '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', 0, 1, 0, '0.00'),
        row('zoe', 1, '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', 0, 1, 0, '0.00'),
        row('amy', 1, '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', 0, 1, 0, '0.00'),
        row('ann', 1, '2026-03-01T10:00:00Z', '2026-03-01T10:30:00Z', 1800, 2, 1, '0.10'),
        row('ann', 2, '2026-03-01T11:00:00Z', '2026-03-01T11:30:01Z', 1800, 3, 1, '0.20'),
        row('ann', 3, '2026-03-01T12:01:00Z', '2026-03-01T12:01:00Z', 0, 1, 0, '0.00'),
    ]
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 6, 'users': 4, 'revenue': '0.30', 'top_user': 'ann'}], 'duplicates_dropped': 0, 'rejected': 0}


def test_daily_start_day_distinct_users_and_top_user_aggregated_ties():
    lines = [
        ev('1', 'Zoe', '2026-03-01T23:50:00Z', 'purchase', amount='1.25'),
        ev('2', 'zoe', '2026-03-02T00:10:00Z', 'purchase', amount='1.25'),
        ev('3', 'Amy', '2026-03-01T12:00:00Z', 'purchase', amount='1.00'),
        ev('4', 'amy', '2026-03-01T13:00:00Z', 'purchase', amount='1.50'),
        ev('5', 'bob', '2026-03-01T14:00:00Z', 'purchase', amount='2.00'),
        ev('6', 'Zoe', '2026-03-02T02:00:00Z'),
        ev('7', 'Bob', '2026-03-02T03:00:00Z', 'purchase', amount='0'),
        ev('8', 'Bob', '2026-02-28T23:59:00-02:00', 'purchase', amount='0.10'),
    ]
    r = run_job(lines)
    assert r.daily == {'days': [
        {'date': '2026-03-01', 'sessions': 5, 'users': 3, 'revenue': '7.10', 'top_user': 'amy'},
        {'date': '2026-03-02', 'sessions': 2, 'users': 2, 'revenue': '0.00', 'top_user': None},
    ], 'duplicates_dropped': 0, 'rejected': 0}
    cross_midnight = next(s for s in r.sessions if s['session_id'] == 'zoe-1')
    assert cross_midnight == row('zoe', 1, '2026-03-01T23:50:00Z', '2026-03-02T00:10:00Z', 1200, 2, 2, '2.50')


def test_csv_quoting_unicode_and_existing_output_replaced(tmp_path):
    inp = tmp_path / 'input'
    out = tmp_path / 'nested' / 'output'
    inp.mkdir()
    out.mkdir(parents=True)
    for name in ('sessions.csv', 'daily.json', 'rejects.jsonl'):
        (out / name).write_text('stale output\n', encoding='utf-8')
    (inp / 'events.jsonl').write_text(ev('1', '  ÉVA, "A"  ', '09:00:00', 'purchase', amount='12.34') + '\n', encoding='utf-8')
    command = [sys.executable, os.path.join(APP_DIR, 'job.py'), '--in', str(inp), '--out', str(out)]
    p = subprocess.run(command, capture_output=True, text=True, cwd=APP_DIR, timeout=30)
    assert p.returncode == 0
    import csv
    with (out / 'sessions.csv').open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    assert rows == [row('éva, "a"', 1, '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 1, 1, '12.34')]
    assert json.loads((out / 'daily.json').read_text(encoding='utf-8')) == {
        'days': [{'date': '2026-03-01', 'sessions': 1, 'users': 1, 'revenue': '12.34', 'top_user': 'éva, "a"'}],
        'duplicates_dropped': 0, 'rejected': 0}
    assert (out / 'rejects.jsonl').read_text(encoding='utf-8') == ''
    # Re-running with no input events must remove the old rows and summary.
    (inp / 'events.jsonl').write_text('', encoding='utf-8')
    p = subprocess.run(command, capture_output=True, text=True, cwd=APP_DIR, timeout=30)
    assert p.returncode == 0
    with (out / 'sessions.csv').open(encoding='utf-8', newline='') as f:
        assert list(csv.reader(f)) == [HEADER]
    assert json.loads((out / 'daily.json').read_text(encoding='utf-8')) == {'days': [], 'duplicates_dropped': 0, 'rejected': 0}
    assert (out / 'rejects.jsonl').read_text(encoding='utf-8') == ''


def test_fractional_gaps_and_duration_are_not_rounded():
    r = run_job([
        ev('a', 'a', '2026-02-28T23:59:59.900Z'),
        ev('b', 'a', '2026-03-01T00:00:00.100Z'),
        ev('c', 'a', '2026-03-01T00:30:00.099999Z'),
        ev('d', 'a', '2026-03-01T01:00:00.100000Z'),
        ev('e', 'b', '2026-03-01T09:00:00.123456Z'),
        ev('f', 'b', '2026-03-01T09:00:01.123455Z'),
    ])
    assert r.rejects == []
    assert r.sessions == [
        row('a', 1, '2026-02-28T23:59:59Z', '2026-03-01T00:30:00Z', 1800, 3, 0, '0.00'),
        row('a', 2, '2026-03-01T01:00:00Z', '2026-03-01T01:00:00Z', 0, 1, 0, '0.00'),
        row('b', 1, '2026-03-01T09:00:00Z', '2026-03-01T09:00:01Z', 0, 2, 0, '0.00'),
    ]
    assert r.daily == {'days': [
        {'date': '2026-02-28', 'sessions': 1, 'users': 1, 'revenue': '0.00', 'top_user': None},
        {'date': '2026-03-01', 'sessions': 2, 'users': 2, 'revenue': '0.00', 'top_user': None},
    ], 'duplicates_dropped': 0, 'rejected': 0}
