"""Black-box regression tests for the booking service."""
import json
from urllib.parse import urlencode

from helpers import api, ts  # noqa: F401


def response(api, method, path, status, **kwargs):
    actual, body, headers = api.call(method, path, **kwargs)
    assert actual == status, (method, path, actual, body)
    if body is not None:
        assert headers.get("content-type", "").lower().startswith("application/json")
    return body


def error(api, method, path, status, code, **kwargs):
    body = response(api, method, path, status, **kwargs)
    assert isinstance(body, dict) and isinstance(body.get("error"), dict)
    assert body["error"].get("code") == code
    assert isinstance(body["error"].get("message"), str) and body["error"]["message"]


def payload(room="r1", start="09:00", end="10:00", title="sync", attendees=2):
    return {"room_id": room, "start": ts(start), "end": ts(end),
            "title": title, "attendees": attendees}


def query(path, **params):
    return path + "?" + urlencode(params)


def create(api, body, headers=None):
    return response(api, "POST", "/bookings", 201, body=body, headers=headers)


def listed(api, **params):
    return response(api, "GET", query("/bookings", **params) if params else "/bookings", 200)


def test_routes_methods_and_errors(api):
    for path in ("/missing", "/rooms/r1/unknown", "/rooms/r1/availability/extra", "/bookings/b1/extra"):
        for method in ("GET", "POST", "DELETE"):
            error(api, method, path, 404, "not_found")
    for path, methods in [('/rooms', ['PUT', 'DELETE', 'PATCH']),
                          ('/rooms/r1', ['POST', 'DELETE', 'PUT']),
                          ('/rooms/r1/availability', ['POST', 'DELETE']),
                          ('/bookings', ['PUT', 'DELETE', 'PATCH']),
                          ('/bookings/b1', ['POST', 'PUT', 'PATCH'])]:
        for method in methods:
            error(api, method, path, 405, "method_not_allowed")
    for path in ("/rooms/r9", "/bookings/b9"):
        error(api, "GET", path, 404, "not_found")
    error(api, "DELETE", "/bookings/b9", 404, "not_found")


def test_bad_json_and_non_object(api):
    for path in ("/rooms", "/bookings"):
        for raw in (b'', b'{', b'null', b'[]', b'"x"', b'1', b'true', b'\xff'):
            error(api, "POST", path, 400, "bad_json", raw=raw)
        error(api, "POST", path, 422, "validation_error", body={})


def test_rooms_validation_and_identity(api):
    assert response(api, "GET", "/rooms", 200) == {"rooms": []}
    invalid = [{}, {"name": "x"}, {"capacity": 1},
               {"name": None, "capacity": 1}, {"name": 3, "capacity": 1},
               {"name": "  \t ", "capacity": 1}, {"name": "x" * 51, "capacity": 1}]
    invalid += [{"name": "A", "capacity": x} for x in (None, True, False, 0, -1, 501, 1.0, "2", [])]
    for body in invalid:
        error(api, "POST", "/rooms", 422, "validation_error", body=body)
    a = response(api, "POST", "/rooms", 201, body={"name": "  Atlas  ", "capacity": 1, "extra": 7})
    assert a == {"id": "r1", "name": "Atlas", "capacity": 1}
    for name in ("atlas", " ATLAS ", "AtLaS"):
        error(api, "POST", "/rooms", 409, "room_exists", body={"name": name, "capacity": 2})
    # Invalid fields are checked before duplicate names.
    error(api, "POST", "/rooms", 422, "validation_error", body={"name": "atlas", "capacity": 0})
    b = response(api, "POST", "/rooms", 201, body={"name": " " + "x" * 50 + " ", "capacity": 500})
    assert b == {"id": "r2", "name": "x" * 50, "capacity": 500}
    assert response(api, "GET", "/rooms", 200) == {"rooms": [a, b]}
    assert response(api, "GET", "/rooms/r1", 200) == a
    assert response(api, "GET", "/rooms/r2", 200) == b


def test_booking_field_validation(api):
    api.room()
    valid = payload()
    for key in valid:
        bad = dict(valid)
        del bad[key]
        error(api, "POST", "/bookings", 422, "validation_error", body=bad)
    values = {
        "room_id": [None, 5, True, []],
        "title": [None, 1, [], "", "  \t", "x" * 101],
        "attendees": [None, True, False, 0, -1, 1.0, "1"],
        "start": [None, 123, "", "2026-03-02T09:00Z", "2026-03-02T09:00:00+00:00",
                  "2026-03-02 09:00:00Z", "2026-03-02T09:00:00.000Z", "2026-3-02T09:00:00Z",
                  "2026-02-29T09:00:00Z", "2026-03-02T24:00:00Z", "2026-03-02T09:01:00Z",
                  "2026-03-02T09:15:01Z"],
        "end": [None, "2026-03-02T10:00:00", "2026-03-02T10:14:00Z", "2026-03-02T10:00:01Z",
                "2026-03-02T09:00:00Z", "2026-03-02T08:45:00Z", "2026-03-02T17:15:00Z"],
    }
    for field, bad_values in values.items():
        for value in bad_values:
            error(api, "POST", "/bookings", 422, "validation_error", body={**valid, field: value})
    result = create(api, {**valid, "start": ts("09:15"), "end": ts("17:15"),
                          "title": " " + "t" * 100 + " ", "attendees": 8, "extra": "ignored"})
    assert result == {"id": "b1", "room_id": "r1", "start": ts("09:15"), "end": ts("17:15"),
                      "title": "t" * 100, "attendees": 8}
    assert response(api, "GET", "/bookings/b1", 200) == result


def test_booking_checks_order_overlap_and_delete(api):
    r1 = api.room(capacity=2)
    r2 = api.room("Other", 2)
    first = create(api, payload(r1, "10:00", "11:00"))
    for start, end in [("09:45", "10:15"), ("10:15", "10:45"), ("10:45", "11:15"),
                       ("09:00", "12:00"), ("10:00", "11:00")]:
        error(api, "POST", "/bookings", 409, "conflict", body=payload(r1, start, end))
    error(api, "POST", "/bookings", 422, "validation_error", body=payload("missing", title=""))
    error(api, "POST", "/bookings", 404, "not_found", body=payload("missing", attendees=999))
    error(api, "POST", "/bookings", 422, "over_capacity", body=payload(r1, "10:00", "11:00", attendees=3))
    before = create(api, payload(r1, "09:00", "10:00", attendees=1))
    after = create(api, payload(r1, "11:00", "12:00"))
    other = create(api, payload(r2, "10:00", "11:00"))
    assert [first['id'], before['id'], after['id'], other['id']] == ['b1', 'b2', 'b3', 'b4']
    assert response(api, "DELETE", "/bookings/b1", 204) is None
    error(api, "GET", "/bookings/b1", 404, "not_found")
    error(api, "DELETE", "/bookings/b1", 404, "not_found")
    assert create(api, payload(r1, "10:00", "11:00"))["id"] == "b5"
    assert [b['id'] for b in listed(api)['bookings']] == ['b2', 'b4', 'b5', 'b3']


def test_idempotency_replay_mismatch_and_failure(api):
    r = api.room(capacity=2)
    key = {"Idempotency-Key": "request-1"}
    body = payload(r, title="  meeting  ")
    original = create(api, body, key)
    # Different JSON key order and whitespace compare equal after parsing.
    raw = json.dumps(dict(reversed(list(body.items()))), indent=2).encode()
    assert response(api, "POST", "/bookings", 201, raw=raw, headers=key) == original
    for changed in ({**body, "title": "meeting"}, {**body, "extra": 1}, {**body, "attendees": 3}, {}):
        error(api, "POST", "/bookings", 422, "idempotency_mismatch", body=changed, headers=key)
    assert len(listed(api)['bookings']) == 1
    # All kinds of failed attempts leave the key available for a different body.
    failed_key = {"Idempotency-Key": "retry"}
    for bad, status, code in [(payload(r, title=""), 422, 'validation_error'),
                              (payload('missing'), 404, 'not_found'),
                              (payload(r, attendees=3), 422, 'over_capacity'),
                              (payload(r), 409, 'conflict')]:
        error(api, 'POST', '/bookings', status, code, body=bad, headers=failed_key)
    second = create(api, payload(r, '11:00', '12:00'), failed_key)
    assert second['id'] == 'b2'
    response(api, 'DELETE', '/bookings/b1', 204)
    assert response(api, 'POST', '/bookings', 201, body=body, headers=key) == original
    error(api, 'GET', '/bookings/b1', 404, 'not_found')
    assert create(api, payload(r, '09:00', '10:00'))['id'] == 'b3'


def test_listing_filters_and_half_open_windows(api):
    r1, r2 = api.room(), api.room('Other')
    a = create(api, payload(r1, '10:00', '11:00'))
    b = create(api, payload(r1, '08:00', '09:00'))
    c = create(api, payload(r2, '10:00', '10:30'))
    d = create(api, payload(r1, '09:00', '10:00'))
    assert listed(api) == {'bookings': [b, d, a, c], 'next_cursor': None}
    assert listed(api, room_id=r1)['bookings'] == [b, d, a]
    assert listed(api, room_id='unknown')['bookings'] == []
    assert listed(api, **{'from': ts('09:00')})['bookings'] == [d, a, c]
    assert listed(api, to=ts('10:00'))['bookings'] == [b, d]
    assert listed(api, **{'from': ts('09:00'), 'to': ts('10:00')})['bookings'] == [d]
    assert listed(api, **{'from': '2026-03-02T10:29:59Z', 'to': '2026-03-02T10:30:01Z'})['bookings'] == [a, c]
    assert listed(api, room_id=r2, **{'from': ts('10:30')})['bookings'] == []


def test_listing_invalid_parameters(api):
    for name, values in {
        'from': ['', '2026-02-30T09:00:00Z', '2026-03-02', '2026-03-02T09:00:00+00:00'],
        'to': ['', 'garbage', '2026-03-02T09:00Z'],
        'limit': ['', '0', '-1', '101', '1.0', 'abc'],
        'cursor': ['', 'invalid', 'c2026-02-30T09:00:00Z_1'],
    }.items():
        for value in values:
            error(api, 'GET', query('/bookings', **{name: value}), 422, 'validation_error')
    assert listed(api, limit=1) == {'bookings': [], 'next_cursor': None}
    assert listed(api, limit=100) == {'bookings': [], 'next_cursor': None}


def test_pagination_default_limit_ties_and_filters(api):
    # Different rooms permit identical timestamps; interleaved creation order must break ties.
    r1, r2 = api.room(), api.room('Other')
    created = []
    for day in range(1, 13):
        for room in (r2, r1):
            start = f'2026-03-{day:02d}T09:00:00Z'
            end = f'2026-03-{day:02d}T10:00:00Z'
            created.append(create(api, payload(room, start, end)))
    first = listed(api)
    assert first['bookings'] == created[:20]
    assert isinstance(first['next_cursor'], str) and first['next_cursor']
    second = listed(api, cursor=first['next_cursor'])
    assert second == {'bookings': created[20:], 'next_cursor': None}
    filters = {'room_id': r1, 'from': '2026-03-03T09:30:00Z', 'to': '2026-03-11T09:30:00Z', 'limit': 3}
    expected = [x for x in created if x['room_id'] == r1 and x['end'] > filters['from'] and x['start'] < filters['to']]
    found, cursor, seen = [], None, set()
    while True:
        page = listed(api, **filters, **({'cursor': cursor} if cursor else {}))
        assert len(page['bookings']) <= 3
        found.extend(page['bookings'])
        cursor = page['next_cursor']
        if cursor is None:
            break
        assert isinstance(cursor, str) and cursor not in seen
        seen.add(cursor)
    assert found == expected
    # Walk through ties with page boundaries between the two rooms.
    found, cursor = [], None
    for _ in range(30):
        page = listed(api, limit=1, **({'cursor': cursor} if cursor else {}))
        found.extend(page['bookings'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert cursor is None and found == created


def test_availability_validation_and_empty_day(api):
    r = api.room()
    error(api, 'GET', '/rooms/missing/availability', 404, 'not_found')
    error(api, 'GET', '/rooms/missing/availability?date=bad', 404, 'not_found')
    for date in (None, '', 'bad', '2026-02-29', '2026-13-01', '2026-3-02', '20260302', '2026-03-02T00:00:00Z'):
        path = f'/rooms/{r}/availability' + ('' if date is None else '?' + urlencode({'date': date}))
        error(api, 'GET', path, 422, 'validation_error')
    assert response(api, 'GET', f'/rooms/{r}/availability?date=2024-02-29', 200) == {
        'date': '2024-02-29', 'free': [{'start': '2024-02-29T08:00:00Z', 'end': '2024-02-29T18:00:00Z'}]}


def test_availability_clipping_adjacent_other_rooms_and_deletion(api):
    r, other = api.room(), api.room('Other')
    create(api, payload(r, '06:00', '07:30'))  # outside working hours
    create(api, payload(r, '07:30', '08:30'))  # clipped at opening
    create(api, payload(r, '09:00', '10:00'))
    adjacent = create(api, payload(r, '10:00', '11:00'))
    create(api, payload(r, '17:00', '19:00'))
    create(api, payload(other, '11:00', '17:00'))
    path = f'/rooms/{r}/availability?date=2026-03-02'
    def gap(a, b):
        return {'start': ts(a), 'end': ts(b)}
    assert response(api, 'GET', path, 200) == {'date': '2026-03-02', 'free': [gap('08:30', '09:00'), gap('11:00', '17:00')]}
    response(api, 'DELETE', '/bookings/' + adjacent['id'], 204)
    assert response(api, 'GET', path, 200)['free'] == [gap('08:30', '09:00'), gap('10:00', '17:00')]
    assert response(api, 'GET', f'/rooms/{r}/availability?date=2026-03-03', 200)['free'] == [
        {'start': '2026-03-03T08:00:00Z', 'end': '2026-03-03T18:00:00Z'}]


def test_availability_full_day_and_cross_midnight(api):
    r = api.room()
    create(api, payload(r, '07:00', '15:00'))
    create(api, payload(r, '15:00', '20:00'))
    assert response(api, 'GET', f'/rooms/{r}/availability?date=2026-03-02', 200) == {'date': '2026-03-02', 'free': []}
    other = api.room('Night')
    create(api, payload(other, '2026-03-02T01:00:00Z', '2026-03-02T08:15:00Z'))
    create(api, payload(other, '2026-03-02T17:45:00Z', '2026-03-03T01:00:00Z'))
    assert response(api, 'GET', f'/rooms/{other}/availability?date=2026-03-02', 200)['free'] == [
        {'start': ts('08:15'), 'end': ts('17:45')}]


def test_quarter_hour_boundaries_and_short_gaps(api):
    r = api.room(capacity=1)
    # Shortest aligned interval, single-character trimmed title, and capacity boundary.
    a = create(api, payload(r, '08:00', '08:15', title=' x ', attendees=1))
    b = create(api, payload(r, '08:30', '08:45', attendees=1))
    c = create(api, payload(r, '08:45', '09:00', attendees=1))
    assert a['title'] == 'x' and [a['id'], b['id'], c['id']] == ['b1', 'b2', 'b3']
    assert response(api, 'GET', f'/rooms/{r}/availability?date=2026-03-02', 200)['free'] == [
        {'start': ts('08:15'), 'end': ts('08:30')},
        {'start': ts('09:00'), 'end': ts('18:00')},
    ]
    # Capacity of the largest valid room can be fully used.
    large = api.room('Large', 500)
    assert create(api, payload(large, attendees=500))['attendees'] == 500
    error(api, 'POST', '/bookings', 422, 'over_capacity', body=payload(large, attendees=501))


def test_calendar_transition_listing_and_unknown_fields(api):
    r = api.room()
    body = payload(r, '2024-02-29T23:30:00Z', '2024-03-01T00:30:00Z', attendees=1)
    booking = create(api, {**body, 'metadata': {'nested': [1, True]}})
    assert booking == {'id': 'b1', **body}
    assert listed(api, **{'from': '2024-03-01T00:00:01Z', 'to': '2024-03-01T00:00:02Z'})['bookings'] == [booking]
    assert listed(api, **{'from': '2024-03-01T00:30:00Z'})['bookings'] == []
    assert listed(api, to='2024-02-29T23:30:00Z')['bookings'] == []
