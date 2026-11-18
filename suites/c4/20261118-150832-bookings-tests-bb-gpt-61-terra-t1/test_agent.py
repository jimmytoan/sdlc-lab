"""Black-box tests for the booking API contract."""
import json
from urllib.parse import urlencode

from helpers import api, ts  # noqa: F401


def call(api, method, path, body=None, **kwargs):
    status, result, headers = api.call(method, path, body, **kwargs)
    if result is not None:
        assert headers.get("content-type", "").lower().startswith("application/json")
    return status, result


def error(api, method, path, status, code, body=None, **kwargs):
    actual, result = call(api, method, path, body, **kwargs)
    assert actual == status, (method, path, body, actual, result)
    assert isinstance(result, dict) and isinstance(result.get("error"), dict)
    assert result["error"].get("code") == code
    assert isinstance(result["error"].get("message"), str) and result["error"]["message"].strip()


def booking(room="r1", start="09:00", end="10:00", title="sync", attendees=2):
    return {"room_id": room, "start": ts(start), "end": ts(end),
            "title": title, "attendees": attendees}


def create(api, payload, headers=None):
    status, result = call(api, "POST", "/bookings", payload, headers=headers)
    assert status == 201, result
    return result


def listing(api, **params):
    path = "/bookings" + ("?" + urlencode(params) if params else "")
    status, result = call(api, "GET", path)
    assert status == 200, result
    assert set(result) == {"bookings", "next_cursor"}
    return result


def availability(api, room, date="2026-03-02"):
    status, result = call(api, "GET", f"/rooms/{room}/availability?" + urlencode({"date": date}))
    assert status == 200, result
    return result


def gap(start, end):
    return {"start": ts(start), "end": ts(end)}


def test_routes_methods_and_errors(api):
    api.room()
    create(api, booking())
    for path in ["/not-a-route", "/rooms/r1/not-a-route", "/bookings/b1/extra"]:
        for method in ("GET", "POST", "DELETE"):
            error(api, method, path, 404, "not_found")
    for method, path in [("PUT", "/rooms"), ("DELETE", "/rooms"),
                         ("POST", "/rooms/r1"), ("DELETE", "/rooms/r1"),
                         ("PUT", "/bookings"), ("DELETE", "/bookings"),
                         ("POST", "/bookings/b1"), ("PUT", "/bookings/b1"),
                         ("POST", "/rooms/r1/availability")]:
        error(api, method, path, 405, "method_not_allowed")
    for path in ("/rooms/r99", "/bookings/b99"):
        error(api, "GET", path, 404, "not_found")
    error(api, "DELETE", "/bookings/b99", 404, "not_found")


def test_json_body_rules(api):
    for path in ("/rooms", "/bookings"):
        for raw in (b"", b"{", b"no", b"null", b"true", b"42", b'"text"', b"[]"):
            error(api, "POST", path, 400, "bad_json", raw=raw)
        error(api, "POST", path, 422, "validation_error", body={})


def test_rooms_creation_lookup_and_duplicates(api):
    assert call(api, "GET", "/rooms") == (200, {"rooms": []})
    status, first = call(api, "POST", "/rooms", {"name": "  Atlas  ", "capacity": 1, "ignored": 3})
    assert status == 201 and first == {"id": "r1", "name": "Atlas", "capacity": 1}
    for name in ("atlas", " ATLAS ", "aTlAs"):
        error(api, "POST", "/rooms", 409, "room_exists", {"name": name, "capacity": 5})
    status, second = call(api, "POST", "/rooms", {"name": "x" * 50, "capacity": 500})
    assert status == 201 and second == {"id": "r2", "name": "x" * 50, "capacity": 500}
    assert call(api, "GET", "/rooms") == (200, {"rooms": [first, second]})
    assert call(api, "GET", "/rooms/r1") == (200, first)
    assert call(api, "GET", "/rooms/r2") == (200, second)


def test_room_validation_boundaries_and_types(api):
    bad = [{}, {"name": "A"}, {"capacity": 2}]
    bad += [{"name": name, "capacity": 2} for name in
            (None, "", " \t ", "x" * 51, 4, True, [], {})]
    bad += [{"name": "A", "capacity": capacity} for capacity in
            (None, 0, -1, 501, True, False, 1.5, "2", [], {})]
    for payload in bad:
        error(api, "POST", "/rooms", 422, "validation_error", payload)
    assert api.room("A", 1) == "r1"
    error(api, "POST", "/rooms", 422, "validation_error", {"name": "a", "capacity": 0})


def test_booking_validation_fields_and_timestamps(api):
    api.room(capacity=10)
    good = booking()
    bad = []
    for field in good:
        item = good.copy()
        del item[field]
        bad.append(item)
    for field, values in {
        "room_id": [None, 1, True, [], {}],
        "start": [None, 1, True, "2026-03-02", "2026-03-02T09:00Z",
                  "2026-03-02T09:00:00+00:00", "2026-03-02 09:00:00Z",
                  "2026-3-02T09:00:00Z", "2026-03-02T09:00:00.000Z",
                  "2026-02-30T09:00:00Z", "2026-03-02T24:00:00Z",
                  "2026-03-02T09:01:00Z", "2026-03-02T09:15:01Z"],
        "end": [None, [], "2026-03-02T10:30:01Z", "2026-03-02T10:31:00Z",
                "2026-03-02T09:00:00Z", "2026-03-02T08:45:00Z",
                "2026-03-02T17:15:00Z"],
        "title": [None, 1, True, "", "  \t", "x" * 101, [], {}],
        "attendees": [None, 0, -1, True, False, 1.0, "1", [], {}],
    }.items():
        for value in values:
            bad.append({**good, field: value})
    for payload in bad:
        error(api, "POST", "/bookings", 422, "validation_error", payload)
    result = create(api, {**good, "start": ts("09:15"), "end": ts("17:15"),
                          "title": "  " + "x" * 100 + "  ", "attendees": 1, "extra": 4})
    assert result == {"id": "b1", "room_id": "r1", "start": ts("09:15"),
                      "end": ts("17:15"), "title": "x" * 100, "attendees": 1}


def test_booking_check_order_and_conflict_geometry(api):
    r1 = api.room(capacity=2)
    r2 = api.room("Other", capacity=5)
    initial = create(api, booking(r1, "10:00", "11:00", attendees=2))
    error(api, "POST", "/bookings", 422, "validation_error", booking("missing", attendees=0))
    error(api, "POST", "/bookings", 404, "not_found", booking("missing", attendees=999))
    error(api, "POST", "/bookings", 422, "over_capacity", booking(r1, "10:00", "11:00", attendees=3))
    for start, end in [("10:00", "11:00"), ("09:45", "10:15"),
                       ("10:45", "11:15"), ("09:00", "12:00"), ("10:15", "10:45")]:
        error(api, "POST", "/bookings", 409, "conflict", booking(r1, start, end))
    assert create(api, booking(r1, "09:00", "10:00"))["id"] == "b2"
    assert create(api, booking(r1, "11:00", "12:00"))["id"] == "b3"
    assert create(api, booking(r2, "10:00", "11:00"))["id"] == "b4"
    assert call(api, "GET", "/bookings/b1") == (200, initial)


def test_delete_and_monotonic_ids(api):
    r = api.room()
    b1 = create(api, booking(r))
    assert call(api, "DELETE", "/bookings/b1") == (204, None)
    error(api, "GET", "/bookings/b1", 404, "not_found")
    error(api, "DELETE", "/bookings/b1", 404, "not_found")
    b2 = create(api, booking(r))
    assert b1["id"] == "b1" and b2["id"] == "b2"
    assert listing(api)["bookings"] == [b2]


def test_idempotency_replay_mismatch_and_failures(api):
    r = api.room(capacity=2)
    headers = {"Idempotency-Key": "client-key"}
    original = {**booking(r), "title": "  Team  ", "extra": "ignored"}
    first = create(api, original, headers)
    assert first["title"] == "Team"
    # JSON object order and whitespace are irrelevant, but ignored fields and
    # untrimmed strings still belong to the original parsed request body.
    raw = json.dumps(original, indent=2, sort_keys=True).encode()
    assert call(api, "POST", "/bookings", headers=headers, raw=raw) == (201, first)
    for changed in ({**original, "title": "Team"}, {**original, "extra": "different"},
                    {**original, "end": ts("10:15")}, {"unexpected": 1}):
        error(api, "POST", "/bookings", 422, "idempotency_mismatch", changed, headers=headers)
    assert listing(api)["bookings"] == [first]
    failed_key = {"Idempotency-Key": "failed-key"}
    error(api, "POST", "/bookings", 400, "bad_json", headers=failed_key, raw=b"{")
    error(api, "POST", "/bookings", 422, "validation_error", {}, headers=failed_key)
    error(api, "POST", "/bookings", 404, "not_found", booking("missing"), headers=failed_key)
    error(api, "POST", "/bookings", 422, "over_capacity", booking(r, attendees=3), headers=failed_key)
    error(api, "POST", "/bookings", 409, "conflict", booking(r), headers=failed_key)
    second = create(api, booking(r, "10:00", "11:00"), failed_key)
    assert second["id"] == "b2"
    assert call(api, "POST", "/bookings", booking(r, "10:00", "11:00"), headers=failed_key) == (201, second)
    assert call(api, "DELETE", "/bookings/b1") == (204, None)
    assert call(api, "POST", "/bookings", original, headers=headers) == (201, first)
    assert listing(api)["bookings"] == [second]
    assert create(api, booking(r))["id"] == "b3"


def test_listing_filters_order_and_half_open_windows(api):
    r1, r2 = api.room(), api.room("Other")
    a = create(api, booking(r1, "11:00", "12:00"))
    b = create(api, booking(r1, "09:00", "10:00"))
    c = create(api, booking(r2, "09:00", "09:30"))
    d = create(api, booking(r1, "10:00", "11:00"))
    assert listing(api)["bookings"] == [b, c, d, a]
    assert listing(api, room_id=r1)["bookings"] == [b, d, a]
    assert listing(api, room_id="unknown")["bookings"] == []
    assert listing(api, **{"from": ts("10:00")})["bookings"] == [d, a]
    assert listing(api, to=ts("10:00"))["bookings"] == [b, c]
    assert listing(api, **{"from": "2026-03-02T09:15:01Z", "to": "2026-03-02T10:00:01Z"})["bookings"] == [b, c, d]
    assert listing(api, room_id=r1, **{"from": ts("09:30"), "to": ts("11:00")})["bookings"] == [b, d]
    assert listing(api, **{"from": ts("12:00")})["bookings"] == []


def test_listing_invalid_parameters(api):
    api.room()
    for key, values in {
        "from": ["not-a-date", "2026-02-30T00:00:00Z", "2026-03-02", "2026-03-02T00:00:00+00:00"],
        "to": ["bad", "2026-13-01T00:00:00Z", "2026-03-02T00:00Z"],
        "limit": ["0", "-1", "101", "1.5", "true", "abc"],
        "cursor": ["garbage", "not-a-valid-cursor!"],
    }.items():
        for value in values:
            error(api, "GET", "/bookings?" + urlencode({key: value}), 422, "validation_error")


def test_pagination_default_limits_and_filter_continuity(api):
    r1, r2 = api.room(), api.room("Other")
    expected = []
    for hour in range(8, 20):
        for room in (r1, r2):
            expected.append(create(api, booking(room, f"{hour:02}:00", f"{hour:02}:30")))
    page = listing(api)
    assert page["bookings"] == expected[:20] and isinstance(page["next_cursor"], str)
    assert listing(api, cursor=page["next_cursor"]) == {"bookings": expected[20:], "next_cursor": None}
    assert listing(api, limit=100) == {"bookings": expected, "next_cursor": None}
    filters = {"room_id": r2, "from": ts("09:00"), "to": ts("17:00"), "limit": 3}
    want = [b for b in expected if b["room_id"] == r2 and ts("09:00") <= b["start"] < ts("17:00")]
    found, cursor, seen = [], None, set()
    while True:
        page = listing(api, **filters, **({"cursor": cursor} if cursor else {}))
        assert len(page["bookings"]) <= 3
        found.extend(page["bookings"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert isinstance(cursor, str) and cursor and cursor not in seen
        seen.add(cursor)
        assert len(seen) < 20
    assert found == want
    single = listing(api, limit=1)
    assert single["bookings"] == expected[:1] and single["next_cursor"]


def test_availability_clipping_merging_and_date_isolation(api):
    r1, r2 = api.room(), api.room("Other")
    assert availability(api, r1) == {"date": "2026-03-02", "free": [gap("08:00", "18:00")]}
    for start, end in [("06:00", "08:30"), ("09:00", "10:00"),
                       ("10:00", "11:00"), ("17:00", "19:00"),
                       ("20:00", "21:00")]:
        create(api, booking(r1, start, end))
    create(api, booking(r2, "11:00", "17:00"))
    assert availability(api, r1) == {"date": "2026-03-02", "free": [gap("08:30", "09:00"), gap("11:00", "17:00")]}
    assert availability(api, r2) == {"date": "2026-03-02", "free": [gap("08:00", "11:00"), gap("17:00", "18:00")]}
    assert availability(api, r1, "2026-03-03") == {"date": "2026-03-03", "free": [
        {"start": "2026-03-03T08:00:00Z", "end": "2026-03-03T18:00:00Z"}]}
    assert call(api, "DELETE", "/bookings/b2") == (204, None)
    assert availability(api, r1)["free"] == [gap("08:30", "10:00"), gap("11:00", "17:00")]


def test_availability_full_day_and_cross_midnight(api):
    r = api.room()
    create(api, booking(r, "08:00", "16:00"))
    create(api, booking(r, "16:00", "18:00"))
    assert availability(api, r)["free"] == []
    r2 = api.room("Night")
    create(api, booking(r2, "2026-03-01T23:00:00Z", "2026-03-02T07:00:00Z"))
    create(api, booking(r2, "2026-03-02T17:30:00Z", "2026-03-03T01:00:00Z"))
    assert availability(api, r2)["free"] == [gap("08:00", "17:30")]


def test_availability_validation_and_room_precedence(api):
    r = api.room()
    error(api, "GET", "/rooms/missing/availability", 404, "not_found")
    error(api, "GET", "/rooms/missing/availability?date=bad", 404, "not_found")
    error(api, "GET", f"/rooms/{r}/availability", 422, "validation_error")
    for date in ("bad", "2026-02-29", "2026-04-31", "2026-13-01", "2026-3-02", "2026-03-02T00:00:00Z"):
        error(api, "GET", f"/rooms/{r}/availability?" + urlencode({"date": date}), 422, "validation_error")
    assert availability(api, r, "2024-02-29")["date"] == "2024-02-29"


def test_across_midnight_order_and_availability(api):
    r = api.room()
    later = create(api, booking(r, "2026-03-03T00:00:00Z", "2026-03-03T00:15:00Z"))
    before = create(api, booking(r, "2026-03-02T23:30:00Z", "2026-03-02T23:45:00Z"))
    overnight = create(api, booking(r, "2026-03-03T01:00:00Z", "2026-03-03T09:00:00Z"))
    assert listing(api)["bookings"] == [before, later, overnight]
    assert listing(api, **{"from": "2026-03-03T08:30:00Z", "to": "2026-03-03T08:45:00Z"})["bookings"] == [overnight]
    assert availability(api, r, "2026-03-03") == {"date": "2026-03-03", "free": [
        {"start": "2026-03-03T09:00:00Z", "end": "2026-03-03T18:00:00Z"}]}
    assert listing(api, limit=3)["next_cursor"] is None
