"""Black-box regression tests for the bookings HTTP API."""
import json
from urllib.parse import urlencode

import pytest

from helpers import api, ts  # noqa: F401


def ok(response, status=200):
    s, body, headers = response
    assert s == status, (s, body)
    if status == 204:
        assert body is None
    else:
        assert headers['content-type'].split(';')[0].strip().lower() == 'application/json'
    return body


def error(response, status, code):
    body = ok(response, status)
    assert isinstance(body, dict) and set(body) == {'error'}
    assert body['error']['code'] == code
    assert isinstance(body['error']['message'], str) and body['error']['message']


def booking(r='r1', start='09:00', end='10:00', title='Meeting', attendees=2, **extra):
    return {'room_id': r, 'start': ts(start), 'end': ts(end), 'title': title,
            'attendees': attendees, **extra}


def post(api, body, key=None):
    return api.call('POST', '/bookings', body, headers={'Idempotency-Key': key} if key is not None else None)


def listing(api, **query):
    path = '/bookings' + ('?' + urlencode(query) if query else '')
    return ok(api.call('GET', path))


def test_routing_and_error_envelopes(api):
    for path in ['/missing', '/rooms/r1/nope', '/bookings/b1/extra', '/rooms/', '/']:
        for method in ['GET', 'POST', 'DELETE']:
            error(api.call(method, path), 404, 'not_found')
    for path, methods in [('/rooms', ['DELETE', 'PUT', 'PATCH']),
                          ('/rooms/r999', ['POST', 'DELETE', 'PUT']),
                          ('/rooms/r999/availability', ['POST', 'DELETE']),
                          ('/bookings', ['DELETE', 'PATCH', 'PUT']),
                          ('/bookings/b999', ['POST', 'PATCH', 'PUT'])]:
        for method in methods:
            error(api.call(method, path), 405, 'method_not_allowed')
    for path in ['/rooms/no-such', '/bookings/no-such']:
        error(api.call('GET', path), 404, 'not_found')
    error(api.call('DELETE', '/bookings/no-such'), 404, 'not_found')


def test_bad_json_and_non_objects(api):
    for path in ['/rooms', '/bookings']:
        for raw in [b'', b'{', b'{"a":}', b'null', b'[]', b'123', b'"hello"', b'true', b'\xff']:
            error(api.call('POST', path, raw=raw), 400, 'bad_json')
        error(api.call('POST', path, {}), 422, 'validation_error')


def test_rooms_validation_uniqueness_and_order(api):
    assert ok(api.call('GET', '/rooms')) == {'rooms': []}
    invalid = [({},), ({'name': 'x'},), ({'capacity': 1},),
               ({'name': '', 'capacity': 1},), ({'name': '   ', 'capacity': 1},),
               ({'name': 'x'*51, 'capacity': 1},), ({'name': 1, 'capacity': 1},),
               ({'name': None, 'capacity': 1},)]
    for (body,) in invalid:
        error(api.call('POST', '/rooms', body), 422, 'validation_error')
    for capacity in [None, True, False, 0, -1, 501, 1.0, '2', [], {}]:
        error(api.call('POST', '/rooms', {'name': 'x', 'capacity': capacity}), 422, 'validation_error')
    first = ok(api.call('POST', '/rooms', {'name': '  Alpha  ', 'capacity': 1, 'unknown': 9}), 201)
    assert first == {'id': 'r1', 'name': 'Alpha', 'capacity': 1}
    for name in ['alpha', ' ALPHA ', 'AlPhA']:
        error(api.call('POST', '/rooms', {'name': name, 'capacity': 3}), 409, 'room_exists')
    # Validation occurs even when the name would otherwise collide.
    error(api.call('POST', '/rooms', {'name': 'alpha', 'capacity': 0}), 422, 'validation_error')
    second = ok(api.call('POST', '/rooms', {'name': ' '+('z'*50)+' ', 'capacity': 500}), 201)
    assert second == {'id': 'r2', 'name': 'z'*50, 'capacity': 500}
    assert ok(api.call('GET', '/rooms')) == {'rooms': [first, second]}
    assert ok(api.call('GET', '/rooms/r1')) == first
    assert ok(api.call('GET', '/rooms/r2')) == second


def test_booking_field_validation(api):
    api.room(capacity=500)
    base = booking()
    for field in base:
        body = dict(base)
        del body[field]
        error(post(api, body), 422, 'validation_error')
    invalid = {
        'room_id': [None, 1, True, [], {}],
        'title': [None, 1, True, '', '   ', 'x'*101],
        'attendees': [None, True, False, 0, -1, 1.0, '1', []],
        'start': [None, 1, '', '2026-03-02', '2026-03-02T09:00Z',
                  '2026-03-02T09:00:00+00:00', '2026-03-02t09:00:00z',
                  '2026-3-02T09:00:00Z', '2026-02-30T09:00:00Z',
                  '2026-03-02T24:00:00Z', '2026-03-02T09:01:00Z', '2026-03-02T09:15:01Z'],
        'end': [None, 'bad', '2026-03-02T10:10:00Z', '2026-03-02T10:00:01Z'],
    }
    for field, values in invalid.items():
        for value in values:
            error(post(api, {**base, field: value}), 422, 'validation_error')
    for start, end in [('09:00', '09:00'), ('10:00', '09:00'), ('09:00', '17:15')]:
        error(post(api, booking(start=start, end=end)), 422, 'validation_error')
    b = ok(post(api, booking(start='09:00', end='17:00', title=' '+('x'*100)+' ', attendees=500, ignored=True)), 201)
    assert b == {'id': 'b1', 'room_id': 'r1', 'start': ts('09:00'), 'end': ts('17:00'), 'title': 'x'*100, 'attendees': 500}
    # Quarter-hour alignment, across a day boundary, is valid.
    b2 = ok(post(api, booking(start='2026-03-03T23:45:00Z', end='2026-03-04T00:15:00Z', attendees=1)), 201)
    assert b2['id'] == 'b2'


def test_booking_rule_order_and_overlap_shapes(api):
    r1 = api.room(capacity=2)
    r2 = api.room('Other', capacity=2)
    original = ok(post(api, booking(r1, '10:00', '11:00')), 201)
    error(post(api, booking('missing', attendees=0)), 422, 'validation_error')
    error(post(api, booking('missing', attendees=500)), 404, 'not_found')
    error(post(api, booking(r1, '10:00', '11:00', attendees=3)), 422, 'over_capacity')
    for start, end in [('10:00', '11:00'), ('09:45', '10:15'), ('10:45', '11:15'),
                       ('10:15', '10:45'), ('09:45', '11:15')]:
        error(post(api, booking(r1, start, end)), 409, 'conflict')
    assert ok(post(api, booking(r1, '09:00', '10:00')), 201)['id'] == 'b2'
    assert ok(post(api, booking(r1, '11:00', '12:00')), 201)['id'] == 'b3'
    assert ok(post(api, booking(r2, '10:00', '11:00')), 201)['id'] == 'b4'
    assert ok(api.call('GET', '/bookings/b1')) == original
    ok(api.call('DELETE', '/bookings/b1'), 204)
    error(api.call('GET', '/bookings/b1'), 404, 'not_found')
    error(api.call('DELETE', '/bookings/b1'), 404, 'not_found')
    assert ok(post(api, booking(r1, '10:00', '11:00')), 201)['id'] == 'b5'


def test_idempotency_parsed_body_and_replay(api):
    r = api.room()
    body = booking(r, title='  Review  ', extra={'x': [1, 2]})
    first = ok(post(api, body, 'request-1'), 201)
    assert first['title'] == 'Review'
    reordered = dict(reversed(list(body.items())))
    raw = json.dumps(reordered, indent=2).encode()
    assert ok(api.call('POST', '/bookings', raw=raw, headers={'Idempotency-Key': 'request-1'}), 201) == first
    for changed in [{**body, 'title': 'Review'}, {**body, 'extra': {'x': [2, 1]}},
                    {**body, 'attendees': 0}, {}]:
        error(post(api, changed, 'request-1'), 422, 'idempotency_mismatch')
    assert [b['id'] for b in listing(api)['bookings']] == ['b1']
    ok(api.call('DELETE', '/bookings/b1'), 204)
    assert ok(post(api, body, 'request-1'), 201) == first
    assert listing(api)['bookings'] == []
    assert ok(post(api, body, 'request-2'), 201)['id'] == 'b2'


def test_idempotency_failures_not_remembered_and_empty_key(api):
    r = api.room(capacity=2)
    for bad, status, code in [(booking(r, attendees=0), 422, 'validation_error'),
                              (booking('missing'), 404, 'not_found'),
                              (booking(r, attendees=3), 422, 'over_capacity')]:
        error(post(api, bad, 'retry'), status, code)
    first = ok(post(api, booking(r), 'retry'), 201)
    error(post(api, booking(r, '09:30', '10:30'), 'other'), 409, 'conflict')
    assert ok(post(api, booking(r, '10:00', '11:00'), 'other'), 201)['id'] == 'b2'
    assert ok(post(api, booking(r), 'retry'), 201) == first
    assert ok(post(api, booking(r, '11:00', '12:00'), ''), 201)['id'] == 'b3'
    assert ok(post(api, booking(r, '12:00', '13:00'), ''), 201)['id'] == 'b4'


def test_listing_filters_half_open_and_order(api):
    r1, r2 = api.room(), api.room('Second')
    b1 = ok(post(api, booking(r1, '10:00', '11:00')), 201)
    b2 = ok(post(api, booking(r1, '08:00', '09:00')), 201)
    b3 = ok(post(api, booking(r2, '10:00', '10:30')), 201)
    b4 = ok(post(api, booking(r1, '09:00', '10:00')), 201)
    b5 = ok(post(api, booking(r2, '11:00', '12:00')), 201)
    def check(expected, **q):
        assert listing(api, **q) == {'bookings': expected, 'next_cursor': None}
    check([b2, b4, b1, b3, b5])
    check([b2, b4, b1], room_id=r1)
    check([b3, b5], room_id=r2)
    check([], room_id='unknown')
    check([b1, b3, b5], **{'from': ts('10:00')})
    check([b2, b4], to=ts('10:00'))
    check([b1, b3], **{'from': ts('10:00'), 'to': ts('11:00')})
    check([b1, b3], **{'from': '2026-03-02T10:29:59Z', 'to': '2026-03-02T10:30:01Z'})
    check([b3], **{'room_id': r2, 'from': ts('10:00'), 'to': ts('11:00')})
    ok(api.call('DELETE', '/bookings/b3'), 204)
    check([b2, b4, b1, b5])


def test_invalid_list_parameters(api):
    api.room()
    for name, values in {
        'from': ['', '2026-02-30T09:00:00Z', '2026-03-02T09:00Z', 'not-a-date'],
        'to': ['', '2026-03-02', '2026-03-02T09:00:00+00:00'],
        'limit': ['', '0', '101', '-1', '1.5', 'true', 'abc'],
        'cursor': ['', 'nonsense', 'c2026-02-30T10:00:00Z_1'],
    }.items():
        for value in values:
            error(api.call('GET', '/bookings?' + urlencode({name: value})), 422, 'validation_error')
    assert listing(api, limit=1) == {'bookings': [], 'next_cursor': None}
    assert listing(api, limit=100) == {'bookings': [], 'next_cursor': None}


def test_pagination_default_limit_and_filtered_walk(api):
    # Same start times in different rooms exercise the creation-order tie breaker.
    rooms = [api.room(f'Room {i}') for i in range(26)]
    created = [ok(post(api, booking(r, '09:00', '10:00')), 201) for r in rooms]
    default = listing(api)
    assert default['bookings'] == created[:20]
    assert isinstance(default['next_cursor'], str) and default['next_cursor']
    assert listing(api, cursor=default['next_cursor']) == {'bookings': created[20:], 'next_cursor': None}
    for size in [1, 7, 26, 100]:
        seen, cursor = [], None
        for _ in range(27):
            query = {'limit': size, 'from': ts('09:30'), 'to': ts('10:30')}
            if cursor is not None:
                query['cursor'] = cursor
            page = listing(api, **query)
            assert len(page['bookings']) <= size
            seen.extend(page['bookings'])
            cursor = page['next_cursor']
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor
        else:
            pytest.fail('pagination did not terminate')
        assert seen == created
    assert listing(api, room_id=rooms[3], limit=1) == {'bookings': [created[3]], 'next_cursor': None}


def free(api, room, date='2026-03-02'):
    return ok(api.call('GET', f'/rooms/{room}/availability?' + urlencode({'date': date})))


def gaps(*times):
    return [{'start': ts(start), 'end': ts(end)} for start, end in times]


def test_availability_validation_and_empty_day(api):
    r = api.room()
    for query in ['', '?date=', '?date=2026-02-29', '?date=2026-3-02', '?date=2026-03-02T00:00:00Z', '?date=garbage']:
        error(api.call('GET', f'/rooms/{r}/availability{query}'), 422, 'validation_error')
        error(api.call('GET', f'/rooms/missing/availability{query}'), 404, 'not_found')
    assert free(api, r) == {'date': '2026-03-02', 'free': gaps(('08:00', '18:00'))}
    assert free(api, r, '2024-02-29') == {'date': '2024-02-29', 'free': [{'start': '2024-02-29T08:00:00Z', 'end': '2024-02-29T18:00:00Z'}]}


def test_availability_clipping_adjacent_and_other_days(api):
    r, other = api.room(), api.room('Other')
    for start, end in [('06:00', '08:30'), ('09:00', '10:00'), ('10:00', '11:00'),
                       ('12:00', '12:15'), ('17:00', '19:00'), ('20:00', '21:00')]:
        ok(post(api, booking(r, start, end)), 201)
    ok(post(api, booking(other, '08:30', '09:00')), 201)
    ok(post(api, booking(r, '2026-03-03T09:00:00Z', '2026-03-03T10:00:00Z')), 201)
    assert free(api, r) == {'date': '2026-03-02', 'free': gaps(('08:30', '09:00'), ('11:00', '12:00'), ('12:15', '17:00'))}
    ok(api.call('DELETE', '/bookings/b2'), 204)
    assert free(api, r)['free'] == gaps(('08:30', '10:00'), ('11:00', '12:00'), ('12:15', '17:00'))


def test_availability_fully_booked_and_boundary_touch(api):
    r = api.room()
    ok(post(api, booking(r, '07:00', '08:00')), 201)
    ok(post(api, booking(r, '18:00', '19:00')), 201)
    assert free(api, r)['free'] == gaps(('08:00', '18:00'))
    ok(post(api, booking(r, '08:00', '16:00')), 201)
    ok(post(api, booking(r, '16:00', '18:00')), 201)
    assert free(api, r)['free'] == []


def test_filtered_pagination_multiple_pages(api):
    r1, r2 = api.room(), api.room('Second')
    for start, end in [('08:00', '08:15'), ('08:15', '08:30'), ('08:30', '08:45'),
                       ('08:45', '09:00'), ('09:00', '09:15'), ('09:15', '09:30')]:
        ok(post(api, booking(r1, start, end)), 201)
        ok(post(api, booking(r2, start, end)), 201)
    filters = {'room_id': r1, 'from': ts('08:15'), 'to': ts('09:15'), 'limit': 2}
    first = listing(api, **filters)
    assert [b['id'] for b in first['bookings']] == ['b3', 'b5']
    assert isinstance(first['next_cursor'], str) and first['next_cursor']
    second = listing(api, **filters, cursor=first['next_cursor'])
    assert [b['id'] for b in second['bookings']] == ['b7', 'b9']
    assert second['next_cursor'] is None


def test_availability_cross_midnight_and_short_gap(api):
    r = api.room()
    # A booking spanning midnight, followed by one ending during the working day.
    ok(post(api, booking(r, '2026-03-01T23:45:00Z', '2026-03-02T07:45:00Z')), 201)
    ok(post(api, booking(r, '07:45', '09:00')), 201)
    ok(post(api, booking(r, '09:15', '17:15')), 201)
    ok(post(api, booking(r, '17:15', '2026-03-03T01:15:00Z')), 201)
    assert free(api, r) == {'date': '2026-03-02', 'free': gaps(('09:00', '09:15'))}
    assert free(api, r, '2026-03-03') == {'date': '2026-03-03', 'free': [{'start': '2026-03-03T08:00:00Z', 'end': '2026-03-03T18:00:00Z'}]}
