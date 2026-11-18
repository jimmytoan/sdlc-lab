import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helpers import ev, run_job


HEADER = ['session_id', 'user', 'start', 'end', 'duration_s', 'events', 'purchases', 'revenue']
FILES = ['daily.json', 'rejects.jsonl', 'sessions.csv']


def check_success(r):
    assert r.code == 0, r.stderr
    assert r.files == FILES
    assert r.header == HEADER


def row(sid, user, start, end, duration, events, purchases, revenue):
    return dict(zip(HEADER, map(str, (sid, user, start, end, duration, events, purchases, revenue))))


def test_empty_and_whitespace_files():
    for lines in ([], ['', '  ', '\t']):
        r = run_job(lines)
        check_success(r)
        assert r.sessions == []
        assert r.rejects == []
        assert r.daily == {'days': [], 'duplicates_dropped': 0, 'rejected': 0}


def test_missing_input():
    r = run_job([], make_input=False)
    assert r.code == 2
    assert r.stderr.strip()


def test_reject_order_line_numbers_and_check_precedence():
    good = {'event_id': 'id', 'user': 'u', 'ts': '2026-03-01T00:00:00Z', 'type': 'view'}
    lines = ['', '  ', '{', 'null', '[]', '42', '"text"']
    expected = [(3, 'bad_json'), (4, 'bad_json'), (5, 'bad_json'), (6, 'bad_json'), (7, 'bad_json')]
    for field in ('event_id', 'user', 'ts', 'type'):
        for value in (None, 12, [], {}, '', '   '):
            d = dict(good, ts='not a timestamp', type='bad')
            d[field] = value
            lines.append(json.dumps(d))
            expected.append((len(lines), 'missing_field'))
        d = dict(good)
        del d[field]
        lines.append(json.dumps(d))
        expected.append((len(lines), 'missing_field'))
    # A bad timestamp wins over both a bad type and a missing amount.
    for timestamp in ('not a timestamp', '2026-03-01T12:00:00', '2026-02-30T12:00:00Z', '2026-03-01',
                      '2026-03-01T10:20Z', '2026-03-01T25:00:00Z',
                      '2026-03-01T10:20:60Z', '2026-03-01T10:20:00+24:00'):
        lines.append(json.dumps(dict(good, ts=timestamp, type='invalid')))
        expected.append((len(lines), 'bad_ts'))
    lines.append(json.dumps(dict(good, type='Purchase')))
    expected.append((len(lines), 'bad_type'))
    lines.append(json.dumps(dict(good, type='purchase')))
    expected.append((len(lines), 'bad_amount'))
    lines.append(ev('ok', 'u', '00:00:00'))
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{'line': n, 'reason': reason} for n, reason in expected]
    assert r.daily['rejected'] == len(expected)
    assert r.daily['duplicates_dropped'] == 0
    assert len(r.sessions) == 1 and r.sessions[0]['events'] == '1'


def test_amount_validation_and_ignored_for_non_purchase():
    bad = [None, 1, -1, True, [], {}, '', ' ', '-1', '1.234', 'abc', '1e2', 'NaN', 'Infinity', '1,00']
    lines = [ev('bad' + str(i), 'u', '00:00:00', 'purchase', amount=a) for i, a in enumerate(bad)]
    lines += [ev('missing', 'u', '00:00:00', 'purchase')]
    lines += [ev('v', 'u', '00:00:00', 'view', amount={'nonsense': 1}),
              ev('c', 'u', '00:00:01', 'click', amount='-999'),
              ev('p1', 'u', '00:00:02', 'purchase', amount='0'),
              ev('p2', 'u', '00:00:03', 'purchase', amount='5.5'),
              ev('p3', 'u', '00:00:04', 'purchase', amount='19.99')]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{'line': i, 'reason': 'bad_amount'} for i in range(1, len(bad) + 2)]
    assert r.sessions == [row('u-1', 'u', '2026-03-01T00:00:00Z', '2026-03-01T00:00:04Z', 4, 5, 3, '25.49')]
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 1, 'users': 1, 'revenue': '25.49', 'top_user': 'u'}],
                       'duplicates_dropped': 0, 'rejected': len(bad) + 1}


def test_dedup_global_earliest_exact_id_and_file_tie():
    lines = [
        ev('same', 'late', '12:00:00', 'purchase', amount='90'),
        ev('same', ' Winner ', '10:00:00', 'purchase', amount='2.50'),
        ev('same', 'tie-loses', '2026-03-01T11:00:00+01:00', 'purchase', amount='40'),
        ev('same', 'invalid', '09:00:00', 'purchase', amount='bad'),
        ev(' same ', 'winner', '10:01:00', 'view'),
        ev('SAME', 'WINNER', '10:02:00', 'purchase', amount='0.25'),
        ev('same', 'even-later', '13:00:00'),
        ev('bad-first', 'u', 'bad'),
        ev('bad-first', 'WINNER', '10:03:00'),
    ]
    r = run_job(lines)
    check_success(r)
    assert r.rejects == [{'line': 4, 'reason': 'bad_amount'}, {'line': 8, 'reason': 'bad_ts'}]
    assert r.sessions == [row('winner-1', 'winner', '2026-03-01T10:00:00Z', '2026-03-01T10:03:00Z', 180, 4, 2, '2.75')]
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 1, 'users': 1, 'revenue': '2.75', 'top_user': 'winner'}],
                       'duplicates_dropped': 3, 'rejected': 2}


def test_fractional_gap_order_duration_and_tie_sorting():
    lines = [
        ev('a3', 'Ann', '2026-03-01T10:30:00.900001Z'),
        ev('a1', ' ANN ', '2026-03-01T10:00:00.900000Z'),
        ev('a2', 'ann', '2026-03-01T10:30:00.900000Z'),
        ev('b', 'bob', '2026-03-01T10:00:00.100000Z'),
        ev('z', 'zoo', '2026-03-01T10:00:00.900000Z'),
        ev('a4', 'ann', '2026-03-01T11:00:00.900002Z'),
        ev('a5', 'ann', '2026-03-01T11:00:02.100000Z'),
    ]
    r = run_job(lines)
    check_success(r)
    assert r.sessions == [
        row('bob-1', 'bob', '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', 0, 1, 0, '0.00'),
        row('ann-1', 'ann', '2026-03-01T10:00:00Z', '2026-03-01T10:30:00Z', 1800, 3, 0, '0.00'),
        row('zoo-1', 'zoo', '2026-03-01T10:00:00Z', '2026-03-01T10:00:00Z', 0, 1, 0, '0.00'),
        row('ann-2', 'ann', '2026-03-01T11:00:00Z', '2026-03-01T11:00:02Z', 1, 2, 0, '0.00'),
    ]
    assert r.daily == {'days': [{'date': '2026-03-01', 'sessions': 4, 'users': 3, 'revenue': '0.00', 'top_user': None}],
                       'duplicates_dropped': 0, 'rejected': 0}


def test_utc_dates_cross_midnight_daily_aggregate_and_ties():
    lines = [
        ev('b1', 'bob', '2026-03-02T00:55:00+01:00', 'purchase', amount='0.10'),
        ev('b2', 'bob', '2026-03-02T00:05:00Z', 'purchase', amount='0.20'),
        ev('a1', 'Ann', '2026-03-01T23:55:00Z', 'purchase', amount='0.30'),
        ev('a2', 'ann', '2026-03-01T23:00:00Z'),
        ev('a3', 'ann', '2026-03-02T01:00:00Z', 'purchase', amount='1.01'),
        ev('b3', 'bob', '2026-03-01T20:30:00-05:30', 'purchase', amount='1.00'),
        ev('c', 'charlie', '2026-03-03T00:01:00+02:00', 'view'),
    ]
    r = run_job(lines)
    check_success(r)
    assert r.sessions == [
        row('ann-1', 'ann', '2026-03-01T23:00:00Z', '2026-03-01T23:00:00Z', 0, 1, 0, '0.00'),
        row('ann-2', 'ann', '2026-03-01T23:55:00Z', '2026-03-01T23:55:00Z', 0, 1, 1, '0.30'),
        row('bob-1', 'bob', '2026-03-01T23:55:00Z', '2026-03-02T00:05:00Z', 600, 2, 2, '0.30'),
        row('ann-3', 'ann', '2026-03-02T01:00:00Z', '2026-03-02T01:00:00Z', 0, 1, 1, '1.01'),
        row('bob-2', 'bob', '2026-03-02T02:00:00Z', '2026-03-02T02:00:00Z', 0, 1, 1, '1.00'),
        row('charlie-1', 'charlie', '2026-03-02T22:01:00Z', '2026-03-02T22:01:00Z', 0, 1, 0, '0.00'),
    ]
    assert r.daily == {'days': [
        {'date': '2026-03-01', 'sessions': 3, 'users': 2, 'revenue': '0.60', 'top_user': 'ann'},
        {'date': '2026-03-02', 'sessions': 3, 'users': 3, 'revenue': '2.01', 'top_user': 'ann'},
    ], 'duplicates_dropped': 0, 'rejected': 0}


def test_top_user_sums_multiple_sessions_and_zero_purchase():
    r = run_job([
        ev('1', 'bob', '00:00:00', 'purchase', amount='5.00'),
        ev('2', 'ann', '00:00:00', 'purchase', amount='3.00'),
        ev('3', 'ann', '01:00:00', 'purchase', amount='3.00'),
        ev('4', 'bob', '2026-03-04T00:00:00Z', 'purchase', amount='0.00'),
    ])
    check_success(r)
    assert r.daily['days'] == [
        {'date': '2026-03-01', 'sessions': 3, 'users': 2, 'revenue': '11.00', 'top_user': 'ann'},
        {'date': '2026-03-04', 'sessions': 1, 'users': 1, 'revenue': '0.00', 'top_user': None},
    ]
    assert r.sessions[-1]['purchases'] == '1'


def test_csv_escaping_unicode_and_extra_fields():
    r = run_job([
        ev('1', '  A, "B"  ', '09:00:00', extra={'ignored': True}),
        ev('2', 'a, "b"', '09:01:00'),
        ev('3', ' ÜSER ', '09:00:00'),
    ])
    check_success(r)
    assert r.sessions == [
        row('a, "b"-1', 'a, "b"', '2026-03-01T09:00:00Z', '2026-03-01T09:01:00Z', 60, 2, 0, '0.00'),
        row('üser-1', 'üser', '2026-03-01T09:00:00Z', '2026-03-01T09:00:00Z', 0, 1, 0, '0.00'),
    ]
    assert r.rejects == []


def test_all_rejected_no_sessions():
    r = run_job(['[]', '', ev('x', 'u', 'wrong'), ev('y', 'u', '00:00:00', 'purchase')])
    check_success(r)
    assert r.sessions == []
    assert r.rejects == [{'line': 1, 'reason': 'bad_json'}, {'line': 3, 'reason': 'bad_ts'},
                         {'line': 4, 'reason': 'bad_amount'}]
    assert r.daily == {'days': [], 'duplicates_dropped': 0, 'rejected': 3}


def test_repeated_run_replaces_outputs_and_creates_nested_directory(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from helpers import APP_DIR

    inp = tmp_path / 'in'
    inp.mkdir()
    out = tmp_path / 'nested' / 'out'
    events = inp / 'events.jsonl'
    cmd = [sys.executable, str(Path(APP_DIR) / 'job.py'), '--in', str(inp), '--out', str(out)]
    events.write_text(ev('one', 'u', '00:00:00') + '\nnot json\n', encoding='utf-8')
    first = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stderr
    assert sorted(p.name for p in out.iterdir()) == FILES
    assert (out / 'rejects.jsonl').read_text(encoding='utf-8').strip()
    events.write_text('', encoding='utf-8')
    second = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    assert (out / 'rejects.jsonl').read_text(encoding='utf-8').strip() == ''
    assert (out / 'sessions.csv').read_text(encoding='utf-8').splitlines() == [','.join(HEADER)]
    assert json.loads((out / 'daily.json').read_text(encoding='utf-8')) == {
        'days': [], 'duplicates_dropped': 0, 'rejected': 0}


def reference_valid_events(events):
    """Independent in-memory model for generated, already-valid events."""
    kept = {}
    for index, event in enumerate(events):
        timestamp = datetime.fromisoformat(event['ts']).astimezone(timezone.utc)
        candidate = (timestamp, index, event)
        key = event['event_id']
        if key not in kept or candidate[:2] < kept[key][:2]:
            kept[key] = candidate

    by_user = {}
    for timestamp, index, event in kept.values():
        by_user.setdefault(event['user'].strip().lower(), []).append((timestamp, index, event))

    records = []
    for user, items in by_user.items():
        items.sort(key=lambda x: (x[0], x[1]))
        groups = []
        for item in items:
            if not groups or item[0] - groups[-1][-1][0] > timedelta(minutes=30):
                groups.append([])
            groups[-1].append(item)
        for number, group in enumerate(groups, 1):
            start, end = group[0][0], group[-1][0]
            purchases = [e for _, _, e in group if e['type'] == 'purchase']
            revenue = sum((Decimal(e['amount']) for e in purchases), Decimal('0'))
            fmt = lambda t: t.strftime('%Y-%m-%dT%H:%M:%SZ')
            result = row(f'{user}-{number}', user, fmt(start), fmt(end),
                         (end - start) // timedelta(seconds=1), len(group), len(purchases), f'{revenue:.2f}')
            records.append((start, user, revenue, result))
    records.sort(key=lambda x: (x[0], x[1]))
    per_day = {}
    for start, user, revenue, _ in records:
        date = start.date().isoformat()
        per_day.setdefault(date, []).append((user, revenue))
    days = []
    for date, items in sorted(per_day.items()):
        totals = {}
        for user, amount in items:
            totals[user] = totals.get(user, Decimal('0')) + amount
        total = sum(totals.values(), Decimal('0'))
        top = min(totals, key=lambda user: (-totals[user], user)) if total else None
        days.append({'date': date, 'sessions': len(items), 'users': len(totals),
                     'revenue': f'{total:.2f}', 'top_user': top})
    return [record[-1] for record in records], {
        'days': days, 'duplicates_dropped': len(events) - len(kept), 'rejected': 0}


@pytest.mark.parametrize('seed', [3, 42, 819])
def test_generated_valid_streams_against_reference(seed):
    rng = random.Random(seed)
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    offsets = [timezone.utc, timezone(timedelta(hours=7)), timezone(-timedelta(hours=5, minutes=30))]
    events = []
    for i in range(240):
        # Many duplicate IDs, users, close gaps, and repeated timestamps; stream is not sorted.
        t = base + timedelta(days=rng.randrange(3), minutes=rng.randrange(0, 240),
                             seconds=rng.choice([0, 0, 1, 30]), microseconds=rng.choice([0, 100000, 900000]))
        e = {'event_id': f'e{rng.randrange(160)}',
             'user': rng.choice([' Ann ', 'ann', 'BOB', 'bob', 'Charlie', 'dana']),
             'ts': t.astimezone(rng.choice(offsets)).isoformat(),
             'type': rng.choice(['view', 'click', 'purchase'])}
        if e['type'] == 'purchase':
            e['amount'] = f'{rng.randrange(10000) / 100:.2f}'
        events.append(e)
    expected_sessions, expected_daily = reference_valid_events(events)
    r = run_job([json.dumps(e) for e in events])
    check_success(r)
    assert r.rejects == []
    assert r.sessions == expected_sessions
    assert r.daily == expected_daily
