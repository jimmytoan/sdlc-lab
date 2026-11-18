import json
from urllib.parse import urlencode

import pytest

from helpers import api, ts  # noqa: F401


def check_error(result, status, code):
    s, body, headers = result
    assert s == status, body
    assert headers.get('content-type', '').startswith('application/json')
    assert isinstance(body, dict) and set(body) == {'error'}
    assert body['error']['code'] == code
    assert isinstance(body['error']['message'], str) and body['error']['message']


def ok(result, status=200):
    s, body, headers = result
    assert s == status, body
    assert headers.get('content-type', '').startswith('application/json')
    return body


def booking(room='r1', start='09:00', end='10:00', title='Meeting', attendees=1, **extra):
    return dict(room_id=room, start=ts(start), end=ts(end), title=title, attendees=attendees, **extra)


def query(path, **params):
    return path + '?' + urlencode(params)


def test_routing_and_error_envelope(api):
    for method, path in [('GET', '/missing'), ('POST', '/rooms/r1/extra'),
                         ('DELETE', '/bookings/b1/extra'), ('GET', '/rooms/')]:
        check_error(api.call(method, path), 404, 'not_found')
    for method, path in [('DELETE', '/rooms'), ('PUT', '/rooms'),
                         ('POST', '/rooms/r1'), ('DELETE', '/rooms/r1'),
                         ('POST', '/rooms/r1/availability'), ('DELETE', '/bookings'),
                         ('PUT', '/bookings/b1'), ('PATCH', '/bookings/b1')]:
        check_error(api.call(method, path, raw=b'not json'), 405, 'method_not_allowed')
    for path in ['/rooms/no-such-room', '/bookings/no-such-booking']:
        check_error(api.call('GET', path), 404, 'not_found')
    check_error(api.call('DELETE', '/bookings/missing'), 404, 'not_found')


def test_body_must_be_json_object(api):
    for path in ['/rooms', '/bookings']:
        for raw in [b'', b'{', b'not json', b'null', b'true', b'42', b'"text"', b'[]', b'\xff']:
            check_error(api.call('POST', path, raw=raw), 400, 'bad_json')
        check_error(api.call('POST', path, body={}), 422, 'validation_error')


def test_rooms_boundaries_order_duplicates_and_unknown_fields(api):
    assert ok(api.call('GET', '/rooms')) == {'rooms': []}
    r1 = ok(api.call('POST', '/rooms', {'name': '  Atlas \t', 'capacity': 1, 'unknown': 123}), 201)
    r2 = ok(api.call('POST', '/rooms', {'name': 'x' * 50, 'capacity': 500}), 201)
    assert r1 == {'id': 'r1', 'name': 'Atlas', 'capacity': 1}
    assert r2 == {'id': 'r2', 'name': 'x' * 50, 'capacity': 500}
    check_error(api.call('POST', '/rooms', {'name': ' aTLAS ', 'capacity': 4}), 409, 'room_exists')
    # Validation takes precedence over duplicate-name detection.
    check_error(api.call('POST', '/rooms', {'name': 'Atlas', 'capacity': 0}), 422, 'validation_error')
    r3 = ok(api.call('POST', '/rooms', {'name': 'third', 'capacity': 2}), 201)
    assert r3['id'] == 'r3'
    assert ok(api.call('GET', '/rooms')) == {'rooms': [r1, r2, r3]}
    for room in [r1, r2, r3]:
        assert ok(api.call('GET', '/rooms/' + room['id'])) == room


def test_room_validation(api):
    invalid = [
        {}, {'capacity': 1}, {'name': 'A'},
        *({'name': x, 'capacity': 1} for x in ['', '  \t', 'a' * 51, None, 3, True, [], {}]),
        *({'name': 'A', 'capacity': x} for x in [0, -1, 501, True, False, 1.0, '1', None, [], {}]),
    ]
    for body in invalid:
        check_error(api.call('POST', '/rooms', body=body), 422, 'validation_error')
    assert ok(api.call('POST', '/rooms', {'name': ' A ', 'capacity': 1}), 201)['id'] == 'r1'


def test_booking_validation_types_and_missing_fields(api):
    api.room()
    base = booking()
    for field in base:
        body = dict(base)
        del body[field]
        check_error(api.call('POST', '/bookings', body), 422, 'validation_error')
    bad_values = {
        'room_id': [None, 1, True, [], {}],
        'start': [None, 1, True, [], {}],
        'end': [None, 1, True, [], {}],
        'title': [None, 1, True, [], {}, '', ' \t', 'x' * 101],
        'attendees': [None, 0, -1, True, False, 1.0, '1', [], {}],
    }
    for field, values in bad_values.items():
        for value in values:
            check_error(api.call('POST', '/bookings', {**base, field: value}), 422, 'validation_error')
    made = ok(api.call('POST', '/bookings', {**base, 'title': ' ' + 'x' * 100 + ' ', 'ignored': [1]}), 201)
    assert made == {**base, 'id': 'b1', 'title': 'x' * 100}


def test_booking_timestamp_format_alignment_and_duration(api):
    api.room()
    invalid = [
        '2026-03-02T09:00Z', '2026-03-02 09:00:00Z', '2026-03-02T09:00:00+00:00',
        '2026-03-02T09:00:00.000Z', '2026-3-02T09:00:00Z', '2026-03-02T9:00:00Z',
        '2026-03-02T09:00:00z', '2026-02-29T09:00:00Z', '2026-13-01T09:00:00Z',
        '2026-03-02T24:00:00Z', '2026-03-02T09:60:00Z', '2026-03-02T09:00:60Z',
        '2026-03-02T09:01:00Z', '2026-03-02T09:15:01Z',
    ]
    for value in invalid:
        for field in ('start', 'end'):
            check_error(api.call('POST', '/bookings', {**booking(), field: value}), 422, 'validation_error')
    for start, end in [('09:00', '09:00'), ('10:00', '09:00'), ('09:00', '17:15')]:
        check_error(api.call('POST', '/bookings', booking(start=start, end=end)), 422, 'validation_error')
    body = booking(start='2028-02-29T23:45:00Z', end='2028-03-01T07:45:00Z')
    assert ok(api.call('POST', '/bookings', body), 201) == {'id': 'b1', **body}


def test_booking_check_order_conflicts_and_room_isolation(api):
    r1 = api.room(capacity=2)
    r2 = api.room('Other', 2)
    first = ok(api.call('POST', '/bookings', booking(r1, '09:00', '10:00', attendees=2)), 201)
    for start, end in [('09:00', '10:00'), ('08:45', '09:15'), ('09:45', '10:15'),
                       ('08:00', '11:00'), ('09:15', '09:45')]:
        check_error(api.call('POST', '/bookings', booking(r1, start, end)), 409, 'conflict')
    check_error(api.call('POST', '/bookings', booking(r1, attendees=3)), 422, 'over_capacity')
    check_error(api.call('POST', '/bookings', booking('unknown', attendees=999)), 404, 'not_found')
    check_error(api.call('POST', '/bookings', booking('unknown', attendees=0)), 422, 'validation_error')
    check_error(api.call('POST', '/bookings', booking(r1, attendees=0)), 422, 'validation_error')
    assert ok(api.call('POST', '/bookings', booking(r1, '08:00', '09:00')), 201)['id'] == 'b2'
    assert ok(api.call('POST', '/bookings', booking(r1, '10:00', '11:00')), 201)['id'] == 'b3'
    assert ok(api.call('POST', '/bookings', booking(r2)), 201)['id'] == 'b4'
    assert ok(api.call('GET', '/bookings/b1')) == first


def test_delete_and_non_reused_ids(api):
    r = api.room()
    b1 = ok(api.call('POST', '/bookings', booking(r)), 201)
    result = api.call('DELETE', '/bookings/' + b1['id'])
    assert result[0] == 204 and result[1] is None
    check_error(api.call('GET', '/bookings/b1'), 404, 'not_found')
    check_error(api.call('DELETE', '/bookings/b1'), 404, 'not_found')
    assert ok(api.call('GET', '/bookings')) == {'bookings': [], 'next_cursor': None}
    assert ok(api.call('POST', '/bookings', booking(r)), 201)['id'] == 'b2'


def test_idempotency_json_equality_mismatch_and_deletion(api):
    r = api.room()
    body = booking(r, title='  Planning  ', extra='ignored')
    headers = {'Idempotency-Key': 'unique-key'}
    original = ok(api.call('POST', '/bookings', body, headers=headers), 201)
    # Different serialization and key order still denotes the same JSON object.
    raw = json.dumps(dict(reversed(list(body.items()))), indent=2).encode()
    assert ok(api.call('POST', '/bookings', headers=headers, raw=raw), 201) == original
    for changed in [{**body, 'title': 'Planning'}, {**body, 'extra': 'different'},
                    {**body, 'attendees': 0}, {}]:
        check_error(api.call('POST', '/bookings', changed, headers=headers), 422, 'idempotency_mismatch')
    assert len(ok(api.call('GET', '/bookings'))['bookings']) == 1
    assert api.call('DELETE', '/bookings/b1')[0] == 204
    assert ok(api.call('POST', '/bookings', body, headers=headers), 201) == original
    assert ok(api.call('GET', '/bookings'))['bookings'] == []
    assert ok(api.call('POST', '/bookings', body), 201)['id'] == 'b2'


def test_idempotency_failures_not_remembered_and_key_scoping(api):
    r = api.room(capacity=1)
    h = {'Idempotency-Key': 'retry'}
    check_error(api.call('POST', '/bookings', booking(r, attendees=2), headers=h), 422, 'over_capacity')
    check_error(api.call('POST', '/bookings', booking('missing'), headers=h), 404, 'not_found')
    check_error(api.call('POST', '/bookings', booking(r, title=''), headers=h), 422, 'validation_error')
    ok(api.call('POST', '/bookings', booking(r)), 201)
    check_error(api.call('POST', '/bookings', booking(r), headers=h), 409, 'conflict')
    good = booking(r, '10:00', '11:00')
    made = ok(api.call('POST', '/bookings', good, headers=h), 201)
    assert made['id'] == 'b2'
    assert ok(api.call('POST', '/bookings', good, headers=h), 201) == made
    # A different key is independent and is subject to normal conflict checks.
    check_error(api.call('POST', '/bookings', good, headers={'Idempotency-Key': 'other'}), 409, 'conflict')
    # Empty key does not activate idempotency.
    check_error(api.call('POST', '/bookings', good, headers={'Idempotency-Key': ''}), 409, 'conflict')


def test_list_filters_overlap_order_and_boundaries(api):
    a = api.room('A')
    b = api.room('B')
    made = []
    for r, start, end in [(a, '11:00', '12:00'), (a, '09:00', '10:00'),
                          (b, '09:00', '09:30'), (a, '10:00', '11:00'),
                          (b, '08:00', '09:00')]:
        made.append(ok(api.call('POST', '/bookings', booking(r, start, end)), 201))
    def ids(**params):
        data = ok(api.call('GET', query('/bookings', **params)))
        assert data['next_cursor'] is None
        return [x['id'] for x in data['bookings']]
    assert ids() == ['b5', 'b2', 'b3', 'b4', 'b1']
    assert ids(room_id=a) == ['b2', 'b4', 'b1']
    assert ids(room_id='missing') == []
    assert ids(**{'from': ts('09:00')}) == ['b2', 'b3', 'b4', 'b1']
    assert ids(to=ts('10:00')) == ['b5', 'b2', 'b3']
    assert ids(**{'from': ts('09:00'), 'to': ts('10:00')}) == ['b2', 'b3']
    assert ids(**{'from': '2026-03-02T09:29:59Z', 'to': '2026-03-02T09:30:01Z'}) == ['b2', 'b3']
    assert ids(**{'from': ts('09:30'), 'to': ts('10:00'), 'room_id': b}) == []
    assert ok(api.call('GET', '/bookings'))['bookings'] == [made[i] for i in [4, 1, 2, 3, 0]]


def test_list_invalid_parameters(api):
    for name in ['from', 'to']:
        for value in ['', '2026-03-02', '2026-02-30T09:00:00Z', '2026-03-02T09:00:00+00:00', 'garbage']:
            check_error(api.call('GET', query('/bookings', **{name: value})), 422, 'validation_error')
    for value in ['', '0', '101', '-1', '1.0', 'abc']:
        check_error(api.call('GET', query('/bookings', limit=value)), 422, 'validation_error')
    for value in ['', 'garbage', 'c2026-02-30T09:00:00Z_1']:
        check_error(api.call('GET', query('/bookings', cursor=value)), 422, 'validation_error')
    assert ok(api.call('GET', '/bookings?limit=100')) == {'bookings': [], 'next_cursor': None}


def test_pagination_with_ties_filters_and_default_limit(api):
    a, b = api.room('A'), api.room('B')
    expected = []
    # Distinct rooms permit equal start times, testing the creation-order tie break.
    for i in range(25):
        room = a if i % 2 == 0 else b
        day = 1 + i // 2
        body = booking(room, f'2026-03-{day:02d}T09:00:00Z', f'2026-03-{day:02d}T10:00:00Z')
        expected.append(ok(api.call('POST', '/bookings', body), 201))
    first = ok(api.call('GET', '/bookings'))
    assert first['bookings'] == expected[:20]
    assert isinstance(first['next_cursor'], str) and first['next_cursor']
    last = ok(api.call('GET', query('/bookings', cursor=first['next_cursor'])))
    assert last == {'bookings': expected[20:], 'next_cursor': None}
    for filters, selected in [({}, expected), ({'room_id': a}, expected[::2]),
                              ({'from': '2026-03-05T09:30:00Z', 'to': '2026-03-10T09:30:00Z'}, expected[8:20])]:
        collected, cursor = [], None
        while True:
            params = {**filters, 'limit': 3}
            if cursor is not None:
                params['cursor'] = cursor
            page = ok(api.call('GET', query('/bookings', **params)))
            assert len(page['bookings']) <= 3
            collected.extend(page['bookings'])
            cursor = page['next_cursor']
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor
            assert len(collected) < 30
        assert collected == selected
    assert ok(api.call('GET', '/bookings?limit=100')) == {'bookings': expected, 'next_cursor': None}


def test_availability_validation_and_empty_day(api):
    r = api.room()
    for suffix in ['', '?date=', '?date=2026-02-29', '?date=2026-2-01', '?date=2026-01-32',
                   '?date=2026-03-02T00:00:00Z', '?date=abc']:
        check_error(api.call('GET', f'/rooms/{r}/availability{suffix}'), 422, 'validation_error')
        check_error(api.call('GET', f'/rooms/missing/availability{suffix}'), 404, 'not_found')
    assert ok(api.call('GET', f'/rooms/{r}/availability?date=2028-02-29')) == {
        'date': '2028-02-29', 'free': [{'start': '2028-02-29T08:00:00Z', 'end': '2028-02-29T18:00:00Z'}]}


def test_availability_clips_adjacent_and_other_rooms(api):
    r, other = api.room('A'), api.room('B')
    for start, end in [('06:00', '08:30'), ('09:00', '10:00'), ('10:00', '11:00'),
                       ('12:00', '12:15'), ('17:00', '19:00'), ('20:00', '21:00')]:
        ok(api.call('POST', '/bookings', booking(r, start, end)), 201)
    ok(api.call('POST', '/bookings', booking(other, '11:00', '12:00')), 201)
    def free(date='2026-03-02'):
        return ok(api.call('GET', f'/rooms/{r}/availability?date={date}'))
    assert free() == {'date': '2026-03-02', 'free': [
        {'start': ts('08:30'), 'end': ts('09:00')},
        {'start': ts('11:00'), 'end': ts('12:00')},
        {'start': ts('12:15'), 'end': ts('17:00')}]}
    assert api.call('DELETE', '/bookings/b2')[0] == 204
    assert free()['free'][0] == {'start': ts('08:30'), 'end': ts('10:00')}
    assert free('2026-03-03')['free'] == [{'start': '2026-03-03T08:00:00Z', 'end': '2026-03-03T18:00:00Z'}]


def test_availability_full_day_and_cross_midnight(api):
    r = api.room()
    # Two adjacent bookings cover the entire working day; duration limit remains eight hours.
    ok(api.call('POST', '/bookings', booking(r, '08:00', '15:00')), 201)
    ok(api.call('POST', '/bookings', booking(r, '15:00', '18:00')), 201)
    assert ok(api.call('GET', f'/rooms/{r}/availability?date=2026-03-02'))['free'] == []
    ok(api.call('POST', '/bookings', booking(r, '2026-03-02T23:45:00Z', '2026-03-03T07:45:00Z')), 201)
    ok(api.call('POST', '/bookings', booking(r, '2026-03-03T07:45:00Z', '2026-03-03T09:00:00Z')), 201)
    assert ok(api.call('GET', f'/rooms/{r}/availability?date=2026-03-03'))['free'] == [
        {'start': '2026-03-03T09:00:00Z', 'end': '2026-03-03T18:00:00Z'}]
