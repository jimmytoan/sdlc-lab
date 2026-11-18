"""Black-box regression coverage for the meeting room API."""
import json
from urllib.parse import urlencode

import pytest
from helpers import api, ts  # noqa: F401


def check(result, status, code=None):
    s, body, headers = result
    assert s == status, (s, body)
    if body is not None:
        assert headers.get('content-type', '').startswith('application/json')
    if code:
        assert set(body) == {'error'}
        assert body['error']['code'] == code
        assert isinstance(body['error']['message'], str) and body['error']['message']
    return body


def booking(room='r1', start='09:00', end='10:00', title='sync', attendees=2, **extra):
    return {'room_id': room, 'start': ts(start), 'end': ts(end), 'title': title,
            'attendees': attendees, **extra}


def post(api, body, key=None):
    return api.call('POST', '/bookings', body, headers={'Idempotency-Key': key} if key is not None else None)


def listing(api, **filters):
    return check(api.call('GET', '/bookings' + ('?' + urlencode(filters) if filters else '')), 200)


def availability(api, room, date='2026-03-02'):
    return check(api.call('GET', f'/rooms/{room}/availability?date={date}'), 200)


def gap(start, end):
    return {'start': ts(start), 'end': ts(end)}


def test_rooms_empty_creation_order_trimming_duplicates(api):
    assert check(api.call('GET', '/rooms'), 200) == {'rooms': []}
    assert listing(api) == {'bookings': [], 'next_cursor': None}
    first = check(api.call('POST', '/rooms', {'name': '  Atlas  ', 'capacity': 1, 'extra': 4}), 201)
    second = check(api.call('POST', '/rooms', {'name': ' ' + 'x' * 50 + ' ', 'capacity': 500}), 201)
    assert first == {'id': 'r1', 'name': 'Atlas', 'capacity': 1}
    assert second == {'id': 'r2', 'name': 'x' * 50, 'capacity': 500}
    assert check(api.call('GET', '/rooms'), 200) == {'rooms': [first, second]}
    assert check(api.call('GET', '/rooms/r1'), 200) == first
    check(api.call('GET', '/rooms/r3'), 404, 'not_found')
    check(api.call('POST', '/rooms', {'name': ' aTLAS ', 'capacity': 3}), 409, 'room_exists')
    assert check(api.call('POST', '/rooms', {'name': 'other', 'capacity': 2}), 201)['id'] == 'r3'


@pytest.mark.parametrize('body', [
    {}, {'name': 'ok'}, {'capacity': 1}, {'name': None, 'capacity': 1},
    {'name': 123, 'capacity': 1}, {'name': '', 'capacity': 1},
    {'name': ' \t ', 'capacity': 1}, {'name': 'x' * 51, 'capacity': 1},
    *[{'name': 'ok', 'capacity': v} for v in (None, True, False, 0, -1, 501, 1.5, '1', [], {})],
])
def test_invalid_room_fields(api, body):
    check(api.call('POST', '/rooms', body), 422, 'validation_error')
    assert check(api.call('GET', '/rooms'), 200) == {'rooms': []}


@pytest.mark.parametrize('path', ['/rooms', '/bookings'])
@pytest.mark.parametrize('raw', [b'{', b'not json', b'null', b'[]', b'"str"', b'42', b'true', b''])
def test_bad_json_or_non_object(api, path, raw):
    check(api.call('POST', path, raw=raw), 400, 'bad_json')


@pytest.mark.parametrize('method,path', [
    ('GET', '/nope'), ('POST', '/nope'), ('GET', '/rooms/r1/extra'), ('GET', '/bookings/b1/extra'),
])
def test_unknown_path(api, method, path):
    check(api.call(method, path), 404, 'not_found')


@pytest.mark.parametrize('method,path', [
    ('DELETE', '/rooms'), ('PUT', '/rooms'), ('POST', '/rooms/r1'), ('DELETE', '/rooms/r1'),
    ('POST', '/rooms/r1/availability'), ('DELETE', '/bookings'), ('PUT', '/bookings'),
    ('POST', '/bookings/b1'), ('PUT', '/bookings/b1'),
])
def test_unsupported_method(api, method, path):
    check(api.call(method, path), 405, 'method_not_allowed')


@pytest.mark.parametrize('field,value', [
    ('room_id', None), ('room_id', 1), ('room_id', True), ('start', None), ('start', 7), ('end', []),
    ('title', None), ('title', 123), ('title', ''), ('title', ' \t '), ('title', 'x' * 101),
    ('attendees', None), ('attendees', True), ('attendees', False), ('attendees', 0),
    ('attendees', -1), ('attendees', 1.0), ('attendees', '1'),
])
def test_booking_invalid_fields(api, field, value):
    api.room()
    body = booking()
    body[field] = value
    check(post(api, body), 422, 'validation_error')
    assert listing(api)['bookings'] == []


@pytest.mark.parametrize('field', ['room_id', 'start', 'end', 'title', 'attendees'])
def test_booking_missing_fields(api, field):
    api.room()
    body = booking()
    del body[field]
    check(post(api, body), 422, 'validation_error')


@pytest.mark.parametrize('field', ['start', 'end'])
@pytest.mark.parametrize('value', [
    '2026-03-02', '2026-03-02T09:00Z', '2026-03-02T09:00:00+00:00',
    '2026-03-02 09:00:00Z', '2026-3-02T09:00:00Z', '2026-03-02T9:00:00Z',
    '2026-03-02T09:00:00.000Z', '2026-03-02T09:00:00z', '2026-02-29T09:00:00Z',
    '2026-13-02T09:00:00Z', '2026-03-02T24:00:00Z', '2026-03-02T09:01:00Z',
    '2026-03-02T09:15:01Z', '2026-03-02T09:60:00Z',
])
def test_booking_timestamp_format_and_alignment(api, field, value):
    api.room()
    body = booking(start='08:00', end='12:00')
    body[field] = value
    check(post(api, body), 422, 'validation_error')


def test_duration_boundaries_and_title(api):
    r = api.room(capacity=10)
    for start, end in [('09:00', '09:00'), ('10:00', '09:00'), ('08:00', '16:15')]:
        check(post(api, booking(r, start, end)), 422, 'validation_error')
    b = check(post(api, booking(r, '08:00', '16:00', '  ' + 'x' * 100 + '  ', 10, extra=5)), 201)
    assert b == {'id': 'b1', 'room_id': r, 'start': ts('08:00'), 'end': ts('16:00'),
                 'title': 'x' * 100, 'attendees': 10}
    assert check(api.call('GET', '/bookings/b1'), 200) == b
    assert check(post(api, booking(r, '16:00', '16:15', 'a', 1)), 201)['id'] == 'b2'
    check(post(api, booking(r, '2026-12-31T23:45:00Z', '2027-01-01T00:15:00Z')), 201)
    check(post(api, booking(r, '2028-02-29T09:30:00Z', '2028-02-29T10:45:00Z')), 201)


def test_booking_error_precedence(api):
    r = api.room(capacity=2)
    check(post(api, booking(r)), 201)
    check(post(api, booking('missing', attendees=0)), 422, 'validation_error')
    check(post(api, booking('missing', attendees=100)), 404, 'not_found')
    check(post(api, booking(r, attendees=3)), 422, 'over_capacity')
    check(post(api, booking(r)), 409, 'conflict')
    assert len(listing(api)['bookings']) == 1


@pytest.mark.parametrize('start,end', [
    ('09:00', '10:00'), ('09:15', '09:45'), ('08:00', '11:00'),
    ('08:30', '09:15'), ('09:45', '10:15'), ('08:00', '09:00'), ('10:00', '11:00'),
])
def test_half_open_overlap_and_room_isolation(api, start, end):
    r = api.room()
    other = api.room('Other')
    check(api.book(r, '09:00', '10:00'), 201)
    ok = end == '09:00' or start == '10:00'
    check(api.book(r, start, end), 201 if ok else 409, None if ok else 'conflict')
    check(api.book(other, start, end), 201)


def test_delete_and_ids_never_reused(api):
    r = api.room()
    one = check(api.book(r, '09:00', '10:00'), 201)
    two = check(api.book(r, '10:00', '11:00'), 201)
    assert [one['id'], two['id']] == ['b1', 'b2']
    check(api.call('GET', '/bookings/b999'), 404, 'not_found')
    check(api.call('DELETE', '/bookings/b999'), 404, 'not_found')
    assert check(api.call('DELETE', '/bookings/b1'), 204) is None
    check(api.call('GET', '/bookings/b1'), 404, 'not_found')
    check(api.call('DELETE', '/bookings/b1'), 404, 'not_found')
    assert check(api.call('GET', '/bookings/b2'), 200) == two
    three = check(api.book(r, '09:00', '10:00'), 201)
    assert three['id'] == 'b3'
    assert listing(api)['bookings'] == [three, two]


def test_idempotency_replay_mismatch_and_keys(api):
    r = api.room()
    body = booking(r, title='  title  ', extra={'a': 1, 'b': [2, 3]})
    original = check(post(api, body, 'key-1'), 201)
    reordered = {k: body[k] for k in reversed(body)}
    reordered['extra'] = {'b': [2, 3], 'a': 1}
    raw = json.dumps(reordered, indent=2).encode()
    assert check(api.call('POST', '/bookings', raw=raw, headers={'Idempotency-Key': 'key-1'}), 201) == original
    for changed in [dict(body, title='title'), dict(body, attendees=1), dict(body, extra={'a': 1}),
                    dict(body, room_id='missing'), {'bad': 'body'}]:
        check(post(api, changed, 'key-1'), 422, 'idempotency_mismatch')
    check(post(api, body, 'other-key'), 409, 'conflict')
    assert listing(api)['bookings'] == [original]
    check(api.call('DELETE', '/bookings/b1'), 204)
    assert check(post(api, body, 'key-1'), 201) == original
    assert listing(api)['bookings'] == []
    assert check(post(api, body, 'other-key'), 201)['id'] == 'b2'


def test_failed_idempotent_requests_not_remembered(api):
    r = api.room(capacity=2)
    key = 'retry'
    check(post(api, booking(r, attendees=0), key), 422, 'validation_error')
    check(post(api, booking('missing'), key), 404, 'not_found')
    check(post(api, booking(r, attendees=3), key), 422, 'over_capacity')
    check(api.book(r, '09:00', '10:00'), 201)
    check(post(api, booking(r), key), 409, 'conflict')
    created = check(post(api, booking(r, '10:00', '11:00'), key), 201)
    assert created['id'] == 'b2'
    assert check(post(api, booking(r, '10:00', '11:00'), key), 201) == created


def test_list_filters_order_and_half_open_window(api):
    a, b = api.room('A'), api.room('B')
    one = check(api.book(a, '09:00', '10:00'), 201)
    two = check(api.book(b, '09:00', '09:30'), 201)
    three = check(api.book(a, '10:00', '11:00'), 201)
    four = check(api.book(b, '11:00', '12:00'), 201)
    assert listing(api)['bookings'] == [one, two, three, four]
    assert listing(api, room_id=a)['bookings'] == [one, three]
    assert listing(api, room_id='unknown')['bookings'] == []
    assert listing(api, **{'from': ts('10:00')})['bookings'] == [three, four]
    assert listing(api, to=ts('10:00'))['bookings'] == [one, two]
    assert listing(api, **{'from': '2026-03-02T09:29:59Z', 'to': '2026-03-02T10:00:01Z'})['bookings'] == [one, two, three]
    assert listing(api, room_id=b, **{'from': ts('09:30'), 'to': ts('11:00')})['bookings'] == []


@pytest.mark.parametrize('param,value', [
    ('from', ''), ('from', '2026-03-02'), ('to', 'nope'), ('to', '2026-02-29T10:00:00Z'),
    ('from', '2026-03-02T10:00:00+00:00'), ('limit', ''), ('limit', '0'), ('limit', '-1'),
    ('limit', '101'), ('limit', '1.5'), ('limit', 'true'), ('limit', 'abc'), ('cursor', 'not-a-valid-cursor!'),
])
def test_list_invalid_query(api, param, value):
    check(api.call('GET', '/bookings?' + urlencode({param: value})), 422, 'validation_error')


def walk(api, filters):
    got, seen = [], set()
    while True:
        page = listing(api, **filters)
        assert len(page['bookings']) <= int(filters.get('limit', 20))
        got.extend(page['bookings'])
        cursor = page['next_cursor']
        if cursor is None:
            return got, seen
        assert isinstance(cursor, str) and cursor and cursor not in seen
        seen.add(cursor)
        filters = dict(filters, cursor=cursor)


def test_pagination_order_and_filters(api):
    a, b = api.room('A'), api.room('B')
    entries = [check(api.book(a, '12:00', '12:15'), 201),
               check(api.book(b, '09:00', '09:15'), 201),
               check(api.book(a, '09:00', '09:15'), 201),
               check(api.book(a, '10:00', '10:15'), 201),
               check(api.book(b, '11:00', '11:15'), 201)]
    expected = [entries[i] for i in [1, 2, 3, 4, 0]]
    assert listing(api, limit=100) == {'bookings': expected, 'next_cursor': None}
    assert walk(api, {'limit': 1})[0] == expected
    filters = {'room_id': a, 'from': ts('08:00'), 'to': ts('13:00'), 'limit': 1}
    got, cursors = walk(api, filters)
    assert got == [entries[2], entries[3], entries[0]] and len(cursors) == 2
    assert listing(api, room_id='missing', limit=1) == {'bookings': [], 'next_cursor': None}


def test_default_page_size(api):
    r = api.room()
    created = []
    for i in range(23):
        start = f'2026-03-{i + 1:02}T09:00:00Z'
        end = f'2026-03-{i + 1:02}T10:00:00Z'
        created.append(check(api.book(r, start, end), 201))
    first = listing(api)
    assert first['bookings'] == created[:20]
    assert isinstance(first['next_cursor'], str) and first['next_cursor']
    assert listing(api, cursor=first['next_cursor']) == {'bookings': created[20:], 'next_cursor': None}


def test_availability_clipping_adjacency_and_other_rooms(api):
    r, other = api.room('A'), api.room('B')
    assert availability(api, r) == {'date': '2026-03-02', 'free': [gap('08:00', '18:00')]}
    for start, end in [('07:00', '08:30'), ('09:00', '10:00'), ('10:00', '11:00'),
                       ('17:00', '19:00'), ('05:00', '06:00'), ('20:00', '21:00')]:
        check(api.book(r, start, end), 201)
    check(api.book(other, '11:00', '17:00'), 201)
    assert availability(api, r) == {'date': '2026-03-02', 'free': [gap('08:30', '09:00'), gap('11:00', '17:00')]}
    assert availability(api, r, '2026-03-03') == {'date': '2026-03-03', 'free': [
        {'start': '2026-03-03T08:00:00Z', 'end': '2026-03-03T18:00:00Z'}]}
    check(api.call('DELETE', '/bookings/b2'), 204)
    assert availability(api, r)['free'] == [gap('08:30', '10:00'), gap('11:00', '17:00')]


def test_availability_full_day_and_cross_midnight(api):
    r = api.room()
    check(api.book(r, '2026-03-01T23:00:00Z', '2026-03-02T01:00:00Z'), 201)
    check(api.book(r, '08:00', '16:00'), 201)
    check(api.book(r, '16:00', '18:00'), 201)
    assert availability(api, r)['free'] == []
    check(api.call('DELETE', '/bookings/b2'), 204)
    assert availability(api, r)['free'] == [gap('08:00', '16:00')]


@pytest.mark.parametrize('query', ['', '?date=', '?date=2026-02-29', '?date=2026-13-01',
                                   '?date=2026-3-02', '?date=2026-03-02T00:00:00Z', '?date=abc'])
def test_availability_date_validation_and_room_precedence(api, query):
    r = api.room()
    check(api.call('GET', f'/rooms/{r}/availability{query}'), 422, 'validation_error')
    check(api.call('GET', f'/rooms/missing/availability{query}'), 404, 'not_found')


def test_availability_leap_day(api):
    r = api.room()
    assert availability(api, r, '2028-02-29') == {'date': '2028-02-29', 'free': [
        {'start': '2028-02-29T08:00:00Z', 'end': '2028-02-29T18:00:00Z'}]}
