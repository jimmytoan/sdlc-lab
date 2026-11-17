"""Black-box regression tests for the booking API contract."""
import json
from urllib.parse import urlencode

from helpers import api, ts  # noqa: F401


def expect(result, status, error=None):
    actual, body, headers = result
    assert actual == status, (actual, body, status)
    if body is not None:
        assert headers.get("content-type", "").lower().startswith("application/json")
    if error is not None:
        assert isinstance(body, dict) and set(body) == {"error"}
        assert body["error"]["code"] == error
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body


def booking(room="r1", start="09:00", end="10:00", title="sync", attendees=2, **extra):
    return {"room_id": room, "start": ts(start), "end": ts(end),
            "title": title, "attendees": attendees, **extra}


def get_list(api, **query):
    path = "/bookings" + ("?" + urlencode(query) if query else "")
    return expect(api.call("GET", path), 200)


def ids(page):
    return [b["id"] for b in page["bookings"]]


def test_routing_and_json_errors(api):
    for path in ("/missing", "/rooms/r1/nope", "/bookings/b1/extra", "/rooms/", "/bookings/"):
        expect(api.call("GET", path), 404, "not_found")
    for method, path in [("PUT", "/rooms"), ("DELETE", "/rooms"),
                         ("POST", "/rooms/r999"), ("DELETE", "/rooms/r999"),
                         ("POST", "/rooms/r999/availability"),
                         ("PUT", "/bookings"), ("PATCH", "/bookings/b999"),
                         ("POST", "/bookings/b999")]:
        expect(api.call(method, path, raw=b"{"), 405, "method_not_allowed")
    for path in ("/rooms", "/bookings"):
        for raw in (b"", b"{", b"not json", b"null", b"[]", b'"text"', b"123", b"true", b'\xff'):
            expect(api.call("POST", path, raw=raw), 400, "bad_json")
    expect(api.call("GET", "/rooms/r999"), 404, "not_found")
    expect(api.call("GET", "/bookings/b999"), 404, "not_found")
    expect(api.call("DELETE", "/bookings/b999"), 404, "not_found")


def test_room_validation_and_creation(api):
    assert expect(api.call("GET", "/rooms"), 200) == {"rooms": []}
    invalid = [
        {}, {"capacity": 1}, {"name": "a"},
        *[{"name": name, "capacity": 1} for name in (None, True, 42, [], {}, "", " \t ", "x" * 51)],
        *[{"name": "room", "capacity": cap} for cap in (None, True, False, 0, -1, 501, 1.0, "1", [], {})],
    ]
    for body in invalid:
        expect(api.call("POST", "/rooms", body), 422, "validation_error")
    r1 = expect(api.call("POST", "/rooms", {"name": "  Atlas \t", "capacity": 1, "extra": 7}), 201)
    assert r1 == {"id": "r1", "name": "Atlas", "capacity": 1}
    for name in ("atlas", " ATLAS ", "aTlAs"):
        expect(api.call("POST", "/rooms", {"name": name, "capacity": 5}), 409, "room_exists")
    # Validation precedes duplicate detection.
    expect(api.call("POST", "/rooms", {"name": "atlas", "capacity": 0}), 422, "validation_error")
    r2 = expect(api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 500}), 201)
    assert r2 == {"id": "r2", "name": "x" * 50, "capacity": 500}
    assert expect(api.call("GET", "/rooms/r1"), 200) == r1
    assert expect(api.call("GET", "/rooms/r2"), 200) == r2
    assert expect(api.call("GET", "/rooms"), 200) == {"rooms": [r1, r2]}


def test_booking_field_validation(api):
    api.room(capacity=500)
    valid = booking()
    for field in valid:
        body = dict(valid)
        del body[field]
        expect(api.call("POST", "/bookings", body), 422, "validation_error")
    bad_values = {
        "room_id": [None, 1, True, [], {}],
        "start": [None, 1, True, "2026-03-02", "2026-03-02T09:00Z", "2026-03-02T09:00:00+00:00",
                  "2026-03-02T09:00:00.000Z", "2026-3-02T09:00:00Z", "2026-02-30T09:00:00Z",
                  "2026-03-02T24:00:00Z", "2026-03-02t09:00:00z", "2026-03-02T09:01:00Z",
                  "2026-03-02T09:15:01Z"],
        "end": [None, [], "2026-03-02T10:14:00Z", "2026-03-02T10:00:01Z",
                "2026-03-02T09:00:00Z", "2026-03-02T08:45:00Z", "2026-03-02T17:15:00Z"],
        "title": [None, 1, True, [], "", " \n\t", "x" * 101],
        "attendees": [None, True, False, 0, -1, 1.0, "1", [], {}],
    }
    for field, values in bad_values.items():
        for value in values:
            expect(api.call("POST", "/bookings", {**valid, field: value}), 422, "validation_error")
    # Boundary values and duration across midnight are valid.
    created = expect(api.call("POST", "/bookings", booking(start="09:15", end="17:15", title="  " + "x" * 100 + "  ", attendees=500, ignored=True)), 201)
    assert created == {"id": "b1", "room_id": "r1", "start": ts("09:15"), "end": ts("17:15"), "title": "x" * 100, "attendees": 500}
    next_day = booking(start="2026-03-02T23:45:00Z", end="2026-03-03T00:30:00Z", title=" a ", attendees=1)
    result = expect(api.call("POST", "/bookings", next_day), 201)
    assert result == {**next_day, "id": "b2", "title": "a"}


def test_booking_rule_precedence_and_overlaps(api):
    r1 = api.room(capacity=2)
    r2 = api.room("Other", 4)
    first = expect(api.call("POST", "/bookings", booking(r1, "10:00", "11:00")), 201)
    assert first["id"] == "b1"
    expect(api.call("POST", "/bookings", booking("missing", attendees=0)), 422, "validation_error")
    expect(api.call("POST", "/bookings", booking("missing", attendees=999)), 404, "not_found")
    expect(api.call("POST", "/bookings", booking(r1, "10:15", "10:45", attendees=3)), 422, "over_capacity")
    for start, end in [("09:45", "10:15"), ("10:45", "11:15"), ("10:15", "10:45"),
                       ("09:45", "11:15"), ("10:00", "11:00")]:
        expect(api.call("POST", "/bookings", booking(r1, start, end)), 409, "conflict")
    assert expect(api.call("POST", "/bookings", booking(r1, "09:00", "10:00")), 201)["id"] == "b2"
    assert expect(api.call("POST", "/bookings", booking(r1, "11:00", "12:00")), 201)["id"] == "b3"
    assert expect(api.call("POST", "/bookings", booking(r2, "10:00", "11:00")), 201)["id"] == "b4"
    assert expect(api.call("DELETE", "/bookings/b1"), 204) is None
    expect(api.call("GET", "/bookings/b1"), 404, "not_found")
    expect(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    assert expect(api.call("POST", "/bookings", booking(r1, "10:00", "11:00")), 201)["id"] == "b5"


def test_idempotency_replay_mismatch_and_failures(api):
    api.room(capacity=2)
    body = booking(title="  team  ", extra={"a": 1})
    header = {"Idempotency-Key": "key-1"}
    original = expect(api.call("POST", "/bookings", body, headers=header), 201)
    # Different JSON serialization and member order, but equal parsed object.
    raw = json.dumps(dict(reversed(list(body.items()))), indent=2).encode()
    assert expect(api.call("POST", "/bookings", raw=raw, headers=header), 201) == original
    for changed in ({**body, "title": "team"}, {**body, "extra": {"a": 2}},
                    {**body, "room_id": "missing"}, {**body, "attendees": 0}):
        expect(api.call("POST", "/bookings", changed, headers=header), 422, "idempotency_mismatch")
    assert ids(get_list(api)) == ["b1"]
    # Failed requests do not reserve the key, even when failure is a conflict.
    fail_key = {"Idempotency-Key": "retry"}
    expect(api.call("POST", "/bookings", booking(attendees=3), headers=fail_key), 422, "over_capacity")
    expect(api.call("POST", "/bookings", booking(), headers=fail_key), 409, "conflict")
    second = expect(api.call("POST", "/bookings", booking(start="10:00", end="11:00"), headers=fail_key), 201)
    assert second["id"] == "b2"
    assert expect(api.call("DELETE", "/bookings/b1"), 204) is None
    assert expect(api.call("POST", "/bookings", body, headers=header), 201) == original
    expect(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert expect(api.call("POST", "/bookings", body), 201)["id"] == "b3"


def test_list_filters_order_and_time_boundaries(api):
    a, b = api.room(), api.room("B")
    for room, start, end in [(a, "11:00", "12:00"), (a, "09:00", "10:00"),
                             (b, "09:00", "09:30"), (a, "10:00", "11:00"),
                             (b, "12:00", "13:00")]:
        expect(api.call("POST", "/bookings", booking(room, start, end)), 201)
    page = get_list(api)
    assert ids(page) == ["b2", "b3", "b4", "b1", "b5"] and page["next_cursor"] is None
    assert ids(get_list(api, room_id=a)) == ["b2", "b4", "b1"]
    assert ids(get_list(api, room_id="unknown")) == []
    assert ids(get_list(api, **{"from": ts("10:00")})) == ["b4", "b1", "b5"]
    assert ids(get_list(api, to=ts("10:00"))) == ["b2", "b3"]
    assert ids(get_list(api, **{"from": "2026-03-02T09:29:59Z", "to": "2026-03-02T10:00:01Z"})) == ["b2", "b3", "b4"]
    assert ids(get_list(api, room_id=a, **{"from": ts("09:30"), "to": ts("11:00")})) == ["b2", "b4"]


def test_list_invalid_queries(api):
    for name in ("from", "to"):
        for value in ("", "2026-02-30T00:00:00Z", "2026-03-02", "2026-03-02T09:00:00+00:00", "nonsense"):
            expect(api.call("GET", "/bookings?" + urlencode({name: value})), 422, "validation_error")
    for value in ("", "0", "101", "-1", "1.5", "true", "abc"):
        expect(api.call("GET", "/bookings?" + urlencode({"limit": value})), 422, "validation_error")
    for value in ("", "bogus", "c2026-02-30T09:00:00Z_1"):
        expect(api.call("GET", "/bookings?" + urlencode({"cursor": value})), 422, "validation_error")
    assert get_list(api, limit=1) == {"bookings": [], "next_cursor": None}
    assert get_list(api, limit=100) == {"bookings": [], "next_cursor": None}


def test_pagination_default_ties_and_filters(api):
    # Equal start times in different rooms force creation-order tie breaking across pages.
    a, b = api.room(), api.room("B")
    expected = []
    for day in range(1, 13):
        for room in (a, b):
            start = f"2026-04-{day:02d}T09:00:00Z"
            end = f"2026-04-{day:02d}T10:00:00Z"
            created = expect(api.call("POST", "/bookings", booking(room, start, end)), 201)
            expected.append(created)
    page = get_list(api)
    assert page["bookings"] == expected[:20] and isinstance(page["next_cursor"], str)
    page2 = get_list(api, cursor=page["next_cursor"])
    assert page2["bookings"] == expected[20:] and page2["next_cursor"] is None
    for filters, wanted in [({}, expected), ({"room_id": b}, expected[1::2]),
                            ({"from": "2026-04-04T09:30:00Z", "to": "2026-04-09T09:00:00Z"}, expected[6:16])]:
        found, cursor = [], None
        while True:
            query = {**filters, "limit": 3}
            if cursor is not None:
                query["cursor"] = cursor
            page = get_list(api, **query)
            assert 0 < len(page["bookings"]) <= 3
            found.extend(page["bookings"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor
            assert len(found) < len(wanted)
        assert found == wanted


def test_availability_clipping_adjacency_and_deletion(api):
    r, other = api.room(), api.room("Other")
    path = f"/rooms/{r}/availability?date=2026-03-02"
    def free(*pairs):
        return {"date": "2026-03-02", "free": [{"start": ts(s), "end": ts(e)} for s, e in pairs]}
    assert expect(api.call("GET", path), 200) == free(("08:00", "18:00"))
    for room, start, end in [(r, "07:00", "08:30"), (r, "09:00", "10:00"),
                             (r, "10:00", "11:00"), (r, "17:30", "19:00"),
                             (other, "11:00", "17:30"),
                             (r, "2026-03-01T23:00:00Z", "2026-03-02T01:00:00Z")]:
        expect(api.call("POST", "/bookings", booking(room, start, end)), 201)
    assert expect(api.call("GET", path), 200) == free(("08:30", "09:00"), ("11:00", "17:30"))
    expect(api.call("DELETE", "/bookings/b2"), 204)
    assert expect(api.call("GET", path), 200) == free(("08:30", "10:00"), ("11:00", "17:30"))
    assert expect(api.call("GET", f"/rooms/{r}/availability?date=2026-03-03"), 200) == {
        "date": "2026-03-03", "free": [{"start": "2026-03-03T08:00:00Z", "end": "2026-03-03T18:00:00Z"}]}


def test_availability_full_and_invalid_dates(api):
    r = api.room()
    for query in ("", "?date=", "?date=2026-02-29", "?date=2026-13-01", "?date=2026-3-02", "?date=20260302", "?date=2026-03-02T00:00:00Z"):
        expect(api.call("GET", f"/rooms/{r}/availability" + query), 422, "validation_error")
        expect(api.call("GET", "/rooms/missing/availability" + query), 404, "not_found")
    expect(api.call("POST", "/bookings", booking(r, "08:00", "16:00")), 201)
    expect(api.call("POST", "/bookings", booking(r, "16:00", "18:00")), 201)
    assert expect(api.call("GET", f"/rooms/{r}/availability?date=2026-03-02"), 200) == {"date": "2026-03-02", "free": []}
    assert expect(api.call("GET", f"/rooms/{r}/availability?date=2024-02-29"), 200) == {
        "date": "2024-02-29", "free": [{"start": "2024-02-29T08:00:00Z", "end": "2024-02-29T18:00:00Z"}]}


def test_independent_idempotency_keys_and_unremembered_errors(api):
    api.room()
    key = {"Idempotency-Key": "recover"}
    expect(api.call("POST", "/bookings", raw=b"{", headers=key), 400, "bad_json")
    expect(api.call("POST", "/bookings", booking(attendees=0), headers=key), 422, "validation_error")
    expect(api.call("POST", "/bookings", booking(room="missing"), headers=key), 404, "not_found")
    body = booking()
    created = expect(api.call("POST", "/bookings", body, headers=key), 201)
    expect(api.call("POST", "/bookings", body, headers={"Idempotency-Key": "other"}), 409, "conflict")
    # The other key was not saved by the conflict, and can now create a different booking.
    other_body = booking(start="10:00", end="11:00")
    other = expect(api.call("POST", "/bookings", other_body, headers={"Idempotency-Key": "other"}), 201)
    assert created["id"] == "b1" and other["id"] == "b2"
    assert expect(api.call("POST", "/bookings", other_body, headers={"Idempotency-Key": "other"}), 201) == other


def test_pagination_one_at_a_time_and_exact_limit(api):
    r = api.room()
    created = [expect(api.call("POST", "/bookings", booking(r, start, end)), 201)
               for start, end in [("09:00", "10:00"), ("10:00", "11:00"), ("11:00", "12:00")]]
    exact = get_list(api, limit=3)
    assert exact == {"bookings": created, "next_cursor": None}
    assert get_list(api, limit=100) == exact
    page = get_list(api, limit=1)
    for index in range(3):
        assert page["bookings"] == [created[index]]
        if index < 2:
            assert isinstance(page["next_cursor"], str) and page["next_cursor"]
            page = get_list(api, limit=1, cursor=page["next_cursor"])
        else:
            assert page["next_cursor"] is None
