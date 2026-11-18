"""Black-box regression tests for the bookings HTTP API."""
import json
from urllib.parse import urlencode

import pytest

from helpers import api, ts  # noqa: F401


def error(result, status, code):
    s, body, headers = result
    assert s == status, (s, body)
    assert headers["content-type"].startswith("application/json")
    assert isinstance(body, dict) and isinstance(body.get("error"), dict)
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def ok(result, status=200):
    s, body, headers = result
    assert s == status, (s, body)
    assert headers["content-type"].startswith("application/json")
    return body


def payload(room="r1", start="09:00", end="10:00", title="sync", attendees=2):
    return {"room_id": room, "start": ts(start), "end": ts(end),
            "title": title, "attendees": attendees}


def create(api, room, start, end, **kwargs):
    return ok(api.book(room, start, end, **kwargs), 201)


def query(api, **params):
    return api.call("GET", "/bookings?" + urlencode(params))


def test_routes_methods_and_errors(api):
    for path in ["/nope", "/rooms/r1/unknown", "/bookings/b1/extra"]:
        error(api.call("GET", path), 404, "not_found")
    for method, path in [("PUT", "/rooms"), ("DELETE", "/rooms"),
                         ("POST", "/rooms/r1"), ("DELETE", "/rooms/r1"),
                         ("POST", "/rooms/r1/availability"),
                         ("PUT", "/bookings"), ("DELETE", "/bookings"),
                         ("POST", "/bookings/b1"), ("PUT", "/bookings/b1")]:
        error(api.call(method, path), 405, "method_not_allowed")
    for path in ["/rooms/no-such-room", "/bookings/no-such-booking"]:
        error(api.call("GET", path), 404, "not_found")
    error(api.call("DELETE", "/bookings/no-such-booking"), 404, "not_found")


@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
def test_bad_json(api, path):
    for raw in [b"", b"{", b"not json", b'null', b'[]', b'"text"', b'42', b'true']:
        error(api.call("POST", path, raw=raw), 400, "bad_json")
    error(api.call("POST", path, body={}), 422, "validation_error")


def test_room_validation_boundaries_and_types(api):
    invalid = [
        {}, {"name": "x"}, {"capacity": 1},
        *[{"name": x, "capacity": 1} for x in [None, 3, True, [], {}, "", " \t ", "x" * 51]],
        *[{"name": "x", "capacity": x} for x in [None, True, False, 0, -1, 501, 1.0, "1", [], {}]],
    ]
    for body in invalid:
        error(api.call("POST", "/rooms", body), 422, "validation_error")
    first = ok(api.call("POST", "/rooms", {"name": "  A  ", "capacity": 1, "ignored": 7}), 201)
    second = ok(api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 500}), 201)
    assert first == {"id": "r1", "name": "A", "capacity": 1}
    assert second == {"id": "r2", "name": "x" * 50, "capacity": 500}
    error(api.call("POST", "/rooms", {"name": " a ", "capacity": 3}), 409, "room_exists")
    error(api.call("POST", "/rooms", {"name": "A", "capacity": 0}), 422, "validation_error")
    assert ok(api.call("GET", "/rooms")) == {"rooms": [first, second]}
    assert ok(api.call("GET", "/rooms/r2")) == second


def test_empty_state(api):
    assert ok(api.call("GET", "/rooms")) == {"rooms": []}
    assert ok(api.call("GET", "/bookings")) == {"bookings": [], "next_cursor": None}


def test_booking_validation_fields_and_types(api):
    r = api.room()
    base = payload(r)
    for field in base:
        missing = dict(base)
        del missing[field]
        error(api.call("POST", "/bookings", missing), 422, "validation_error")
    bad_values = {
        "room_id": [None, 1, True, [], {}],
        "start": [None, 1, True, [], {}], "end": [None, 1, True, [], {}],
        "title": [None, 1, True, [], {}, "", "   ", "x" * 101],
        "attendees": [None, 0, -1, True, False, 1.0, "1", [], {}],
    }
    for field, values in bad_values.items():
        for value in values:
            error(api.call("POST", "/bookings", {**base, field: value}), 422, "validation_error")
    b = ok(api.call("POST", "/bookings", {**base, "title": "  " + "x" * 100 + "  ",
                                           "attendees": 1, "ignored": {"a": 1}}), 201)
    assert b == {**base, "id": "b1", "title": "x" * 100, "attendees": 1}


def test_booking_timestamp_validation_and_duration(api):
    r = api.room()
    base = payload(r)
    invalid_times = ["2026-03-02T09:00Z", "2026-03-02 09:00:00Z", "2026-03-02T09:00:00+00:00",
                     "2026-3-02T09:00:00Z", "2026-03-02t09:00:00z", "2026-03-02T09:00:00.000Z",
                     "2026-02-29T09:00:00Z", "2026-13-02T09:00:00Z", "2026-03-02T24:00:00Z",
                     "2026-03-02T09:60:00Z", "2026-03-02T09:01:00Z", "2026-03-02T09:15:01Z"]
    for field in ("start", "end"):
        for value in invalid_times:
            error(api.call("POST", "/bookings", {**base, field: value}), 422, "validation_error")
    for start, end in [("09:00", "09:00"), ("10:00", "09:00"), ("09:00", "17:15")]:
        error(api.call("POST", "/bookings", payload(r, start, end)), 422, "validation_error")
    for start, end in [("00:00", "08:00"), ("08:15", "08:30"), ("08:30", "08:45"), ("08:45", "09:00")]:
        create(api, r, start, end)
    create(api, r, "2028-02-29T23:45:00Z", "2028-03-01T00:00:00Z")


def test_booking_check_precedence(api):
    r = api.room(capacity=2)
    create(api, r, "09:00", "10:00")
    error(api.call("POST", "/bookings", payload("missing", attendees=0)), 422, "validation_error")
    error(api.call("POST", "/bookings", payload("missing", attendees=100)), 404, "not_found")
    error(api.call("POST", "/bookings", payload(r, attendees=3)), 422, "over_capacity")
    error(api.call("POST", "/bookings", payload(r)), 409, "conflict")


def test_overlap_all_directions_and_room_isolation(api):
    a, b = api.room("A"), api.room("B")
    create(api, a, "09:00", "10:00")
    for start, end in [("09:00", "10:00"), ("08:45", "09:15"), ("09:45", "10:15"),
                       ("09:15", "09:45"), ("08:45", "10:15")]:
        error(api.book(a, start, end), 409, "conflict")
    create(api, a, "08:00", "09:00")
    create(api, a, "10:00", "11:00")
    create(api, b, "09:00", "10:00")


def test_delete_ids_and_get(api):
    r = api.room()
    one = create(api, r, "09:00", "10:00")
    two = create(api, r, "10:00", "11:00")
    assert ok(api.call("GET", "/bookings/b1")) == one
    s, body, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and body is None
    error(api.call("GET", "/bookings/b1"), 404, "not_found")
    error(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    three = create(api, r, "09:00", "10:00")
    assert three["id"] == "b3"
    assert ok(api.call("GET", "/bookings"))["bookings"] == [three, two]


def test_idempotency_equal_json_mismatch_and_failures(api):
    r = api.room(capacity=2)
    body = payload(r, title="  planning  ")
    headers = {"Idempotency-Key": "key-1"}
    first = ok(api.call("POST", "/bookings", body, headers=headers), 201)
    assert first["title"] == "planning"
    # Same parsed object, different property order and JSON formatting.
    raw = json.dumps(dict(reversed(list(body.items()))), indent=2).encode()
    assert ok(api.call("POST", "/bookings", headers=headers, raw=raw), 201) == first
    for changed in [{**body, "title": "planning"}, {**body, "extra": 1}, {**body, "attendees": 3}]:
        error(api.call("POST", "/bookings", changed, headers=headers), 422, "idempotency_mismatch")
    assert ok(api.call("GET", "/bookings"))["bookings"] == [first]
    error(api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "failed"}), 409, "conflict")
    second = ok(api.book(r, "10:00", "11:00", headers={"Idempotency-Key": "failed"}), 201)
    assert second["id"] == "b2"
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert ok(api.call("POST", "/bookings", body, headers=headers), 201) == first
    error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_list_filter_window_order_and_boundaries(api):
    a, b = api.room("A"), api.room("B")
    late = create(api, a, "11:00", "12:00")
    first = create(api, a, "09:00", "10:00")
    same = create(api, b, "09:00", "09:30")
    middle = create(api, a, "10:00", "11:00")
    assert ok(api.call("GET", "/bookings")) == {"bookings": [first, same, middle, late], "next_cursor": None}
    assert ok(query(api, room_id=a))["bookings"] == [first, middle, late]
    assert ok(query(api, room_id="missing"))["bookings"] == []
    assert ok(query(api, **{"from": ts("10:00"), "to": ts("11:00")}))["bookings"] == [middle]
    assert ok(query(api, **{"from": "2026-03-02T09:29:59Z"}))["bookings"] == [first, same, middle, late]
    assert ok(query(api, **{"from": ts("09:30")}))["bookings"] == [first, middle, late]
    assert ok(query(api, to=ts("10:00")))["bookings"] == [first, same]
    assert ok(query(api, room_id=b, **{"from": ts("09:30")}))["bookings"] == []


def test_list_invalid_parameters(api):
    api.room()
    for key in ["from", "to"]:
        for value in ["", "2026-02-30T09:00:00Z", "2026-03-02", "2026-03-02T09:00:00+00:00", "bad"]:
            error(query(api, **{key: value}), 422, "validation_error")
    for value in ["", "0", "-1", "101", "1.5", "abc", "true"]:
        error(query(api, limit=value), 422, "validation_error")
    error(query(api, cursor="not-a-valid-cursor"), 422, "validation_error")
    assert ok(query(api, limit=1))["bookings"] == []
    assert ok(query(api, limit=100))["bookings"] == []


def test_pagination_all_pages_and_filters(api):
    a, b = api.room("A"), api.room("B")
    # Same start times in different rooms exercise the creation-order tiebreaker.
    made = []
    for i in range(12):
        hour, minute = divmod(i * 15, 60)
        start = f"{8 + hour:02d}:{minute:02d}"
        hour2, minute2 = divmod((i + 1) * 15, 60)
        end = f"{8 + hour2:02d}:{minute2:02d}"
        made.append(create(api, a, start, end))
        made.append(create(api, b, start, end))
    default = ok(api.call("GET", "/bookings"))
    assert default["bookings"] == made[:20]
    assert isinstance(default["next_cursor"], str) and default["next_cursor"]
    assert ok(query(api, cursor=default["next_cursor"])) == {"bookings": made[20:], "next_cursor": None}
    filters = {"room_id": b, "from": ts("08:30"), "to": ts("10:30"), "limit": 3}
    expected = [x for x in made if x["room_id"] == b and x["end"] > filters["from"] and x["start"] < filters["to"]]
    results, cursor = [], None
    for _ in range(10):
        page = ok(query(api, **filters, **({"cursor": cursor} if cursor else {})))
        assert len(page["bookings"]) <= 3
        results.extend(page["bookings"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert isinstance(cursor, str) and cursor
    else:
        pytest.fail("pagination never terminated")
    assert results == expected


def test_availability_gaps_clipping_and_adjacent(api):
    a, b = api.room("A"), api.room("B")
    date = "2026-03-02"
    path = f"/rooms/{a}/availability?date={date}"
    assert ok(api.call("GET", path)) == {"date": date, "free": [{"start": ts("08:00"), "end": ts("18:00")}]}
    create(api, a, "06:00", "09:00")
    create(api, a, "10:00", "11:00")
    create(api, a, "11:00", "12:00")
    create(api, a, "17:00", "20:00")
    create(api, b, "12:00", "17:00")
    create(api, a, "2026-03-01T23:00:00Z", "2026-03-02T01:00:00Z")
    assert ok(api.call("GET", path)) == {"date": date, "free": [
        {"start": ts("09:00"), "end": ts("10:00")},
        {"start": ts("12:00"), "end": ts("17:00")} ]}
    create(api, a, "09:00", "10:00")
    create(api, a, "12:00", "17:00")
    assert ok(api.call("GET", path)) == {"date": date, "free": []}
    assert ok(api.call("GET", f"/rooms/{a}/availability?date=2026-03-03")) == {
        "date": "2026-03-03", "free": [{"start": "2026-03-03T08:00:00Z", "end": "2026-03-03T18:00:00Z"}]}


def test_availability_date_validation_and_room_precedence(api):
    r = api.room()
    for suffix in ["", "?date=", "?date=2026-02-29", "?date=2026-13-01", "?date=2026-3-02", "?date=2026-03-02T00:00:00Z", "?date=bad"]:
        error(api.call("GET", f"/rooms/{r}/availability{suffix}"), 422, "validation_error")
        error(api.call("GET", f"/rooms/missing/availability{suffix}"), 404, "not_found")
    assert ok(api.call("GET", f"/rooms/{r}/availability?date=2028-02-29"))["date"] == "2028-02-29"


def test_idempotency_failed_keys_and_independent_keys(api):
    r = api.room(capacity=2)
    h = {"Idempotency-Key": "reusable"}
    error(api.call("POST", "/bookings", raw=b"{", headers=h), 400, "bad_json")
    error(api.call("POST", "/bookings", payload(r, attendees=0), headers=h), 422, "validation_error")
    error(api.call("POST", "/bookings", payload("missing"), headers=h), 404, "not_found")
    error(api.call("POST", "/bookings", payload(r, attendees=3), headers=h), 422, "over_capacity")
    one_body = payload(r)
    one = ok(api.call("POST", "/bookings", one_body, headers=h), 201)
    # A different key is not a replay, even if the body is identical.
    error(api.call("POST", "/bookings", one_body, headers={"Idempotency-Key": "other"}), 409, "conflict")
    two = ok(api.call("POST", "/bookings", payload(r, "10:00", "11:00"),
                      headers={"Idempotency-Key": "other"}), 201)
    assert [one["id"], two["id"]] == ["b1", "b2"]
    error(api.call("POST", "/bookings", {**one_body, "room_id": "missing"}, headers=h),
          422, "idempotency_mismatch")
    assert ok(api.call("GET", "/bookings"))["bookings"] == [one, two]


def test_filters_across_dates_and_unaligned_window(api):
    r = api.room()
    # Cross midnight with a valid duration, and query with non-quarter-hour seconds.
    overnight = create(api, r, "2026-03-01T23:30:00Z", "2026-03-02T00:30:00Z")
    morning = create(api, r, "08:00", "08:15")
    window = {"from": "2026-03-02T00:29:59Z", "to": "2026-03-02T08:00:01Z"}
    assert ok(query(api, **window))["bookings"] == [overnight, morning]
    assert ok(query(api, **{"from": "2026-03-02T00:30:00Z", "to": ts("08:00")}))["bookings"] == []
    assert ok(api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")) == {
        "date": "2026-03-02", "free": [{"start": ts("08:15"), "end": ts("18:00")}]}


def test_availability_booking_crossing_midnight_and_day_boundaries(api):
    r = api.room()
    create(api, r, "2026-03-02T03:00:00Z", "2026-03-02T08:00:00Z")
    create(api, r, "2026-03-02T18:00:00Z", "2026-03-03T01:00:00Z")
    assert ok(api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")) == {
        "date": "2026-03-02", "free": [{"start": ts("08:00"), "end": ts("18:00")}]}
    create(api, r, "2026-03-02T08:00:00Z", "2026-03-02T16:00:00Z")
    create(api, r, "2026-03-02T16:00:00Z", "2026-03-02T18:00:00Z")
    assert ok(api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")) == {
        "date": "2026-03-02", "free": []}
