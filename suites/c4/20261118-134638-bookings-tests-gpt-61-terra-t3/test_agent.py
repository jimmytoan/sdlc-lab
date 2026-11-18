import json
from urllib.parse import urlencode

import pytest
from helpers import api, ts  # noqa: F401


def expect(result, status, error=None):
    s, body, headers = result
    assert s == status, (s, body, status)
    if body is not None:
        assert headers['content-type'].lower().startswith('application/json')
    if error is not None:
        assert isinstance(body, dict) and set(body) == {'error'}
        assert body['error']['code'] == error
        assert isinstance(body['error']['message'], str) and body['error']['message']
    return body


def booking(room='r1', start='09:00', end='10:00', title='sync', attendees=2, **extra):
    return {'room_id': room, 'start': ts(start), 'end': ts(end),
            'title': title, 'attendees': attendees, **extra}


def post(api, body, headers=None):
    return api.call('POST', '/bookings', body, headers=headers)


def query(path, **params):
    return path + '?' + urlencode(params)


def test_routing_methods_and_errors(api):
    for path in ['/nope', '/rooms/r1/nope', '/bookings/b1/extra', '/rooms/r1/availability/extra']:
        for method in ['GET', 'POST', 'DELETE']:
            expect(api.call(method, path), 404, 'not_found')
    for path, methods in [('/rooms', ['DELETE', 'PUT', 'PATCH']),
                          ('/rooms/r1', ['POST', 'DELETE', 'PUT']),
                          ('/rooms/r1/availability', ['POST', 'DELETE']),
                          ('/bookings', ['DELETE', 'PUT', 'PATCH']),
                          ('/bookings/b1', ['POST', 'PUT', 'PATCH'])]:
        for method in methods:
            expect(api.call(method, path), 405, 'method_not_allowed')
    for path in ['/rooms/missing', '/bookings/missing']:
        expect(api.call('GET', path), 404, 'not_found')
    expect(api.call('DELETE', '/bookings/missing'), 404, 'not_found')
    assert expect(api.call('GET', '/rooms'), 200) == {'rooms': []}
    assert expect(api.call('GET', '/bookings'), 200) == {'bookings': [], 'next_cursor': None}


def test_json_parsing_and_unknown_fields(api):
    for path in ['/rooms', '/bookings']:
        for raw in [b'', b'{', b'null', b'[]', b'1', b'"text"', b'true', b'\xff']:
            expect(api.call('POST', path, raw=raw), 400, 'bad_json')
    room = expect(api.call('POST', '/rooms', {'name': ' x ', 'capacity': 2, 'unused': {'a': 1}}), 201)
    assert room == {'id': 'r1', 'name': 'x', 'capacity': 2}
    b = expect(post(api, booking(title='  hi  ', extra=['ignored'])), 201)
    assert b == {'id': 'b1', 'room_id': 'r1', 'start': ts('09:00'), 'end': ts('10:00'),
                 'title': 'hi', 'attendees': 2}


def test_rooms_validation_boundaries_and_identity(api):
    invalid = [({},), ({'capacity': 1},), ({'name': 'ok'},)]
    for (body,) in invalid:
        expect(api.call('POST', '/rooms', body), 422, 'validation_error')
    for name in ['', '   ', 'x' * 51, None, 42, True, [], {}]:
        expect(api.call('POST', '/rooms', {'name': name, 'capacity': 1}), 422, 'validation_error')
    for cap in [0, -1, 501, 1.5, '2', True, False, None, [], {}]:
        expect(api.call('POST', '/rooms', {'name': 'ok', 'capacity': cap}), 422, 'validation_error')
    a = expect(api.call('POST', '/rooms', {'name': '  Alpha \t', 'capacity': 1}), 201)
    assert a == {'id': 'r1', 'name': 'Alpha', 'capacity': 1}
    expect(api.call('POST', '/rooms', {'name': ' aLPHA ', 'capacity': 500}), 409, 'room_exists')
    b = expect(api.call('POST', '/rooms', {'name': 'x' * 50, 'capacity': 500}), 201)
    assert b == {'id': 'r2', 'name': 'x' * 50, 'capacity': 500}
    assert expect(api.call('GET', '/rooms/r1'), 200) == a
    assert expect(api.call('GET', '/rooms/r2'), 200) == b
    assert expect(api.call('GET', '/rooms'), 200) == {'rooms': [a, b]}
    expect(api.call('GET', '/rooms/r3'), 404, 'not_found')


def test_booking_field_validation(api):
    api.room(capacity=500)
    good = booking()
    for field in good:
        body = good.copy()
        del body[field]
        expect(post(api, body), 422, 'validation_error')
    invalid_values = {
        'room_id': [None, 1, True, [], {}],
        'title': ['', ' \t ', 'x' * 101, None, 1, True, []],
        'attendees': [0, -1, 1.5, '1', True, False, None, []],
        'start': [None, 1, True, '', '2026-03-02', '2026-03-02T09:00Z',
                  '2026-03-02T09:00:00+00:00', '2026-03-02 09:00:00Z',
                  '2026-3-02T09:00:00Z', '2026-02-30T09:00:00Z',
                  '2026-03-02T24:00:00Z', '2026-03-02T09:01:00Z',
                  '2026-03-02T09:15:01Z'],
        'end': [None, '2026-03-02T10:01:00Z', '2026-03-02T10:00:01Z',
                '2026-03-02T10:00:00.000Z'],
    }
    for field, values in invalid_values.items():
        for value in values:
            expect(post(api, {**good, field: value}), 422, 'validation_error')
    for start, end in [('10:00', '10:00'), ('10:00', '09:45'), ('00:00', '08:15')]:
        expect(post(api, booking(start=start, end=end)), 422, 'validation_error')
    b = expect(post(api, booking(start='00:00', end='08:00', title=' ' + 'x' * 100 + ' ', attendees=500)), 201)
    assert b['id'] == 'b1' and b['title'] == 'x' * 100 and b['attendees'] == 500
    b = expect(post(api, booking(start='08:15', end='08:30', title='x', attendees=1)), 201)
    assert b['id'] == 'b2'
    b = expect(post(api, booking(start='2026-03-02T23:45:00Z', end='2026-03-03T00:00:00Z')), 201)
    assert b['id'] == 'b3'


def test_booking_checks_order_and_overlap(api):
    r = api.room(capacity=2)
    other = api.room('Other', 4)
    base = expect(post(api, booking(r, '10:00', '11:00')), 201)
    expect(post(api, booking('missing', attendees=0)), 422, 'validation_error')
    expect(post(api, booking('missing', attendees=999)), 404, 'not_found')
    expect(post(api, booking(r, '10:00', '11:00', attendees=3)), 422, 'over_capacity')
    for start, end in [('09:45', '10:15'), ('10:45', '11:15'), ('10:15', '10:45'),
                       ('09:00', '12:00'), ('10:00', '11:00')]:
        expect(post(api, booking(r, start, end)), 409, 'conflict')
    before = expect(post(api, booking(r, '09:00', '10:00')), 201)
    after = expect(post(api, booking(r, '11:00', '12:00')), 201)
    different = expect(post(api, booking(other, '10:00', '11:00')), 201)
    assert [base['id'], before['id'], after['id'], different['id']] == ['b1', 'b2', 'b3', 'b4']
    assert expect(api.call('GET', '/bookings/b1'), 200) == base
    expect(api.call('DELETE', '/bookings/b1'), 204)
    expect(api.call('GET', '/bookings/b1'), 404, 'not_found')
    expect(api.call('DELETE', '/bookings/b1'), 404, 'not_found')
    new = expect(post(api, booking(r, '10:00', '11:00')), 201)
    assert new['id'] == 'b5'


def test_idempotency_replay_mismatch_and_delete(api):
    api.room()
    key = {'Idempotency-Key': 'request-1'}
    body = booking(title=' test ', extra={'x': 1})
    first = expect(post(api, body, key), 201)
    raw = json.dumps(dict(reversed(list(body.items()))), indent=2).encode()
    assert expect(api.call('POST', '/bookings', raw=raw, headers=key), 201) == first
    for changed in [{**body, 'title': 'test'}, {**body, 'extra': {'x': 2}},
                    {**body, 'attendees': 0}, {}]:
        expect(post(api, changed, key), 422, 'idempotency_mismatch')
    assert expect(api.call('GET', '/bookings'), 200)['bookings'] == [first]
    expect(api.call('DELETE', '/bookings/b1'), 204)
    assert expect(post(api, body, key), 201) == first
    expect(api.call('GET', '/bookings/b1'), 404, 'not_found')
    second = expect(post(api, body, {'Idempotency-Key': 'request-2'}), 201)
    assert second['id'] == 'b2'


def test_idempotency_failures_not_cached_and_empty_key(api):
    api.room(capacity=2)
    key = {'Idempotency-Key': 'retry'}
    expect(post(api, booking(attendees=0), key), 422, 'validation_error')
    expect(post(api, booking('missing'), key), 404, 'not_found')
    expect(post(api, booking(attendees=3), key), 422, 'over_capacity')
    first = expect(post(api, booking(), {'Idempotency-Key': ''}), 201)
    expect(post(api, booking(), key), 409, 'conflict')
    b = expect(post(api, booking(start='10:00', end='11:00'), key), 201)
    assert b['id'] == 'b2' and first['id'] == 'b1'
    c = expect(post(api, booking(start='11:00', end='12:00'), {'Idempotency-Key': ''}), 201)
    assert c['id'] == 'b3'


def test_list_filters_and_order(api):
    r1, r2 = api.room('One'), api.room('Two')
    specs = [(r1, '11:00', '12:00'), (r1, '09:00', '10:00'),
             (r2, '09:00', '10:30'), (r2, '12:00', '13:00'), (r1, '10:00', '11:00')]
    made = [expect(post(api, booking(r, s, e)), 201) for r, s, e in specs]
    def listing(**params):
        return expect(api.call('GET', query('/bookings', **params)), 200)
    assert listing() == {'bookings': [made[i] for i in [1, 2, 4, 0, 3]], 'next_cursor': None}
    assert listing(room_id=r1)['bookings'] == [made[i] for i in [1, 4, 0]]
    assert listing(room_id='unknown')['bookings'] == []
    assert listing(**{'from': ts('10:00')})['bookings'] == [made[i] for i in [2, 4, 0, 3]]
    assert listing(to=ts('11:00'))['bookings'] == [made[i] for i in [1, 2, 4]]
    assert listing(**{'room_id': r1, 'from': '2026-03-02T09:59:59Z', 'to': '2026-03-02T10:00:01Z'})['bookings'] == [made[1], made[4]]
    assert listing(**{'from': ts('10:00'), 'to': ts('10:00')})['bookings'] == [made[2]]
    expect(api.call('DELETE', '/bookings/b3'), 204)
    assert listing()['bookings'] == [made[i] for i in [1, 4, 0, 3]]


def test_list_invalid_parameters(api):
    for name, values in {
        'from': ['', '2026-02-30T09:00:00Z', '2026-03-02', '2026-03-02T09:00:00+00:00'],
        'to': ['', 'nope', '2026-03-02T10:00Z'],
        'limit': ['', '0', '101', '-1', '1.0', 'true', 'abc'],
        'cursor': ['', 'bad', 'c2026-02-30T09:00:00Z_1'],
    }.items():
        for value in values:
            expect(api.call('GET', query('/bookings', **{name: value})), 422, 'validation_error')
    assert expect(api.call('GET', '/bookings?limit=100'), 200)['bookings'] == []


def test_pagination_default_limit_ties_and_filters(api):
    rooms = [api.room('room' + str(i)) for i in range(26)]
    made = [expect(post(api, booking(r, '09:00', '10:00')), 201) for r in rooms]
    result = expect(api.call('GET', '/bookings'), 200)
    assert result['bookings'] == made[:20] and isinstance(result['next_cursor'], str)
    result = expect(api.call('GET', query('/bookings', cursor=result['next_cursor'])), 200)
    assert result == {'bookings': made[20:], 'next_cursor': None}
    for limit in [1, 7, 26, 100]:
        cursor = None
        seen = []
        for _ in range(30):
            params = {'limit': limit, 'from': '2026-03-02T09:30:01Z', 'to': ts('10:00')}
            if cursor is not None:
                params['cursor'] = cursor
            page = expect(api.call('GET', query('/bookings', **params)), 200)
            assert len(page['bookings']) <= limit
            seen.extend(page['bookings'])
            cursor = page['next_cursor']
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor
        else:
            pytest.fail('pagination never ended')
        assert seen == made
    page = expect(api.call('GET', query('/bookings', room_id=rooms[0], limit=1)), 200)
    assert page == {'bookings': [made[0]], 'next_cursor': None}


def free(*pairs):
    return [{'start': ts(s), 'end': ts(e)} for s, e in pairs]


def test_availability_validation_and_room_precedence(api):
    r = api.room()
    expect(api.call('GET', '/rooms/missing/availability'), 404, 'not_found')
    expect(api.call('GET', '/rooms/missing/availability?date=bad'), 404, 'not_found')
    for suffix in ['', '?date=', '?date=nope', '?date=2026-02-29', '?date=2026-3-02',
                   '?date=2026-03-02T00:00:00Z', '?date=2026-13-01']:
        expect(api.call('GET', f'/rooms/{r}/availability{suffix}'), 422, 'validation_error')
    assert expect(api.call('GET', f'/rooms/{r}/availability?date=2026-03-02'), 200) == {
        'date': '2026-03-02', 'free': free(('08:00', '18:00'))}
    assert expect(api.call('GET', f'/rooms/{r}/availability?date=2024-02-29'), 200) == {
        'date': '2024-02-29', 'free': [{'start': '2024-02-29T08:00:00Z', 'end': '2024-02-29T18:00:00Z'}]}


def test_availability_clipping_adjacent_other_rooms_and_delete(api):
    r, other = api.room('A'), api.room('B')
    expect(post(api, booking(r, '07:00', '09:00')), 201)
    expect(post(api, booking(r, '10:00', '11:00')), 201)
    expect(post(api, booking(r, '11:00', '12:00')), 201)
    expect(post(api, booking(r, '17:00', '19:00')), 201)
    expect(post(api, booking(other, '12:00', '16:00')), 201)
    expect(post(api, booking(r, '2026-03-03T08:00:00Z', '2026-03-03T09:00:00Z')), 201)
    path = f'/rooms/{r}/availability?date=2026-03-02'
    assert expect(api.call('GET', path), 200) == {'date': '2026-03-02', 'free': free(('09:00', '10:00'), ('12:00', '17:00'))}
    expect(api.call('DELETE', '/bookings/b2'), 204)
    assert expect(api.call('GET', path), 200)['free'] == free(('09:00', '11:00'), ('12:00', '17:00'))


def test_availability_full_day_and_outside_boundaries(api):
    r = api.room()
    for start, end in [('07:00', '08:00'), ('08:00', '16:00'), ('16:00', '18:00'), ('18:00', '19:00')]:
        expect(post(api, booking(r, start, end)), 201)
    path = f'/rooms/{r}/availability?date=2026-03-02'
    assert expect(api.call('GET', path), 200)['free'] == []
    expect(api.call('DELETE', '/bookings/b2'), 204)
    expect(api.call('DELETE', '/bookings/b3'), 204)
    assert expect(api.call('GET', path), 200)['free'] == free(('08:00', '18:00'))


def test_pagination_filtered_across_start_times(api):
    r1, r2 = api.room('A'), api.room('B')
    data = [(r1, '12:00', '13:00'), (r2, '10:00', '11:00'),
            (r1, '09:00', '10:00'), (r1, '10:00', '11:00'),
            (r1, '11:00', '12:00'), (r2, '09:00', '10:00')]
    made = [expect(post(api, booking(r, s, e)), 201) for r, s, e in data]
    params = {'room_id': r1, 'from': '2026-03-02T09:30:00Z',
              'to': '2026-03-02T12:00:00Z', 'limit': 2}
    first = expect(api.call('GET', query('/bookings', **params)), 200)
    assert first['bookings'] == [made[2], made[3]]
    assert isinstance(first['next_cursor'], str) and first['next_cursor']
    second = expect(api.call('GET', query('/bookings', **params, cursor=first['next_cursor'])), 200)
    assert second == {'bookings': [made[4]], 'next_cursor': None}


def test_availability_date_and_quarter_hour_gaps(api):
    r = api.room()
    for start, end in [('08:15', '09:00'), ('09:15', '10:00'), ('17:45', '18:15')]:
        expect(post(api, booking(r, start, end)), 201)
    expect(post(api, booking(r, '2026-03-03T08:00:00Z', '2026-03-03T16:00:00Z')), 201)
    assert expect(api.call('GET', f'/rooms/{r}/availability?date=2026-03-02'), 200)['free'] == free(
        ('08:00', '08:15'), ('09:00', '09:15'), ('10:00', '17:45'))
    assert expect(api.call('GET', f'/rooms/{r}/availability?date=2026-03-03'), 200)['free'] == [
        {'start': '2026-03-03T16:00:00Z', 'end': '2026-03-03T18:00:00Z'}]
