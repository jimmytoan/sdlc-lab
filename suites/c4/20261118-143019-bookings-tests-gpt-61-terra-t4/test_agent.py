"""Black-box regression tests for the bookings HTTP contract."""
import json
from urllib.parse import urlencode

import pytest

from helpers import api, ts  # noqa: F401


def request(api, method, path, body=None, **kwargs):
    status, data, headers = api.call(method, path, body, **kwargs)
    if data is not None:
        assert headers.get("content-type", "").split(";")[0] == "application/json"
    return status, data


def error(response, status, code):
    actual, body = response
    assert actual == status, body
    assert isinstance(body, dict) and isinstance(body.get("error"), dict)
    assert body["error"].get("code") == code
    assert isinstance(body["error"].get("message"), str) and body["error"]["message"]


def booking(room="r1", start="09:00", end="10:00", title="sync", attendees=2, **extra):
    return {"room_id": room, "start": ts(start), "end": ts(end),
            "title": title, "attendees": attendees, **extra}


def post_booking(api, body, headers=None):
    return request(api, "POST", "/bookings", body, headers=headers)


def query(path, **params):
    return path + "?" + urlencode(params)


def test_routing_and_method_errors(api):
    for method, path in [("GET", "/missing"), ("POST", "/missing"),
                         ("GET", "/rooms/r1/unknown"), ("GET", "/bookings/b1/extra")]:
        error(request(api, method, path), 404, "not_found")
    for method, path in [("DELETE", "/rooms"), ("PUT", "/rooms"),
                         ("POST", "/rooms/r999"), ("DELETE", "/rooms/r999"),
                         ("POST", "/rooms/r999/availability"),
                         ("PUT", "/bookings"), ("POST", "/bookings/b999"),
                         ("PATCH", "/bookings/b999")]:
        error(request(api, method, path, raw=b"not json"), 405, "method_not_allowed")
    for path in ["/rooms/nope", "/bookings/nope"]:
        error(request(api, "GET", path), 404, "not_found")
    error(request(api, "DELETE", "/bookings/nope"), 404, "not_found")


@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
@pytest.mark.parametrize("raw", [b"", b"{", b"null", b"true", b"42", b'"hello"', b"[]", b"\xff"])
def test_bad_json(api, path, raw):
    error(request(api, "POST", path, raw=raw), 400, "bad_json")


def test_rooms_creation_retrieval_uniqueness_and_order(api):
    assert request(api, "GET", "/rooms") == (200, {"rooms": []})
    s, first = request(api, "POST", "/rooms", {"name": "  Atlas \t", "capacity": 1, "ignored": 42})
    assert (s, first) == (201, {"id": "r1", "name": "Atlas", "capacity": 1})
    error(request(api, "POST", "/rooms", {"name": " aTLAS ", "capacity": 500}), 409, "room_exists")
    # Validation precedes uniqueness, and a failed attempt does not consume an id.
    error(request(api, "POST", "/rooms", {"name": "atlas", "capacity": 0}), 422, "validation_error")
    s, second = request(api, "POST", "/rooms", {"name": "x" * 50, "capacity": 500})
    assert (s, second) == (201, {"id": "r2", "name": "x" * 50, "capacity": 500})
    assert request(api, "GET", "/rooms") == (200, {"rooms": [first, second]})
    assert request(api, "GET", "/rooms/r1") == (200, first)
    assert request(api, "GET", "/rooms/r2") == (200, second)


@pytest.mark.parametrize("body", [
    {}, {"capacity": 1}, {"name": "A"},
    *[{"name": n, "capacity": 1} for n in [None, True, 1, [], "", " \t ", "x" * 51]],
    *[{"name": "A", "capacity": c} for c in [None, True, False, "1", 1.0, [], 0, -1, 501]],
])
def test_invalid_rooms(api, body):
    error(request(api, "POST", "/rooms", body), 422, "validation_error")
    assert request(api, "GET", "/rooms") == (200, {"rooms": []})


INVALID_TIMESTAMPS = [
    "2026-03-02T09:00Z", "2026-03-02T09:00:00+00:00", "2026-03-02 09:00:00Z",
    "2026-3-02T09:00:00Z", "2026-03-02T09:00:00.000Z", "2026-03-02T09:00:00z",
    "2026-02-29T09:00:00Z", "2026-13-01T09:00:00Z", "2026-03-02T24:00:00Z",
    "2026-03-02T09:60:00Z", "2026-03-02T09:00:60Z", "", None, 1, True,
]


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("value", INVALID_TIMESTAMPS + ["2026-03-02T09:01:00Z", "2026-03-02T09:15:01Z"])
def test_booking_timestamp_validation(api, field, value):
    api.room()
    body = booking()
    body[field] = value
    error(post_booking(api, body), 422, "validation_error")


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_fields(api, field):
    api.room()
    body = booking()
    del body[field]
    error(post_booking(api, body), 422, "validation_error")


@pytest.mark.parametrize("field,value", [
    ("room_id", None), ("room_id", 1), ("room_id", True),
    ("title", None), ("title", 5), ("title", ""), ("title", " \t"), ("title", "x" * 101),
    ("attendees", None), ("attendees", True), ("attendees", False),
    ("attendees", "1"), ("attendees", 1.0), ("attendees", 0), ("attendees", -1),
])
def test_booking_field_validation(api, field, value):
    api.room()
    body = booking()
    body[field] = value
    error(post_booking(api, body), 422, "validation_error")


@pytest.mark.parametrize("start,end", [("09:00", "09:00"), ("10:00", "09:00"), ("09:00", "17:15")])
def test_booking_duration_invalid(api, start, end):
    api.room()
    error(post_booking(api, booking(start=start, end=end)), 422, "validation_error")


def test_booking_boundaries_and_check_order(api):
    r = api.room(capacity=2)
    # Validation is performed before room existence, then capacity before conflict.
    error(post_booking(api, booking(room="missing", attendees=0)), 422, "validation_error")
    error(post_booking(api, booking(room="missing", attendees=999)), 404, "not_found")
    s, b = post_booking(api, booking(r, "08:00", "16:00", "  " + "x" * 100 + "  ", 2, ignored=True))
    assert s == 201 and b == {"id": "b1", "room_id": r, "start": ts("08:00"), "end": ts("16:00"), "title": "x" * 100, "attendees": 2}
    error(post_booking(api, booking(r, "09:00", "10:00", attendees=3)), 422, "over_capacity")
    error(post_booking(api, booking(r, "09:00", "10:00", attendees=2)), 409, "conflict")
    s, b = post_booking(api, booking(r, "16:00", "16:15", "x", 1))
    assert s == 201 and b["id"] == "b2"


def test_overlap_shapes_adjacency_rooms_and_delete(api):
    r1 = api.room("One")
    r2 = api.room("Two")
    s, original = post_booking(api, booking(r1, "10:00", "11:00"))
    assert s == 201
    for start, end in [("10:00", "11:00"), ("09:00", "12:00"), ("10:15", "10:45"),
                       ("09:45", "10:15"), ("10:45", "11:15")]:
        error(post_booking(api, booking(r1, start, end)), 409, "conflict")
    assert post_booking(api, booking(r1, "09:00", "10:00"))[0] == 201
    assert post_booking(api, booking(r1, "11:00", "12:00"))[0] == 201
    assert post_booking(api, booking(r2, "10:00", "11:00"))[0] == 201
    assert request(api, "GET", "/bookings/b1") == (200, original)
    assert request(api, "DELETE", "/bookings/b1") == (204, None)
    error(request(api, "GET", "/bookings/b1"), 404, "not_found")
    error(request(api, "DELETE", "/bookings/b1"), 404, "not_found")
    s, replacement = post_booking(api, booking(r1, "10:00", "11:00"))
    assert s == 201 and replacement["id"] == "b5"
    assert [b["id"] for b in request(api, "GET", "/bookings")[1]["bookings"]] == ["b2", "b4", "b5", "b3"]


def test_idempotency_replay_mismatch_and_deletion(api):
    r = api.room()
    body = booking(r, title="  Meeting  ", extra={"x": [1, 2]})
    headers = {"Idempotency-Key": "key-one"}
    s, saved = post_booking(api, body, headers)
    assert s == 201 and saved["title"] == "Meeting"
    # Object key order and JSON whitespace do not affect equality.
    raw = json.dumps(dict(reversed(list(body.items()))), indent=2).encode()
    assert request(api, "POST", "/bookings", headers=headers, raw=raw) == (201, saved)
    changed = {**body, "extra": {"x": [1, 3]}}
    error(post_booking(api, changed, headers), 422, "idempotency_mismatch")
    # Mismatch takes precedence even over ordinary validation and conflict checks.
    error(post_booking(api, {}, headers), 422, "idempotency_mismatch")
    assert request(api, "DELETE", "/bookings/b1") == (204, None)
    assert post_booking(api, body, headers) == (201, saved)
    error(request(api, "GET", "/bookings/b1"), 404, "not_found")
    assert request(api, "GET", "/bookings")[1]["bookings"] == []
    s, new = post_booking(api, body, {"Idempotency-Key": "different"})
    assert s == 201 and new["id"] == "b2"


def test_idempotency_failures_not_remembered_and_empty_key(api):
    r = api.room(capacity=2)
    h = {"Idempotency-Key": "retry"}
    error(post_booking(api, booking(r, attendees=3), h), 422, "over_capacity")
    s, first = post_booking(api, booking(r), h)
    assert s == 201 and first["id"] == "b1"
    # A second key with a conflicting request is not reserved by the failure.
    h2 = {"Idempotency-Key": "retry-conflict"}
    error(post_booking(api, booking(r), h2), 409, "conflict")
    s, second = post_booking(api, booking(r, "10:00", "11:00"), h2)
    assert s == 201 and second["id"] == "b2"
    for start, end in [("11:00", "12:00"), ("12:00", "13:00")]:
        assert post_booking(api, booking(r, start, end), {"Idempotency-Key": ""})[0] == 201


def test_listing_filters_overlap_and_sort(api):
    r1 = api.room("One")
    r2 = api.room("Two")
    created = []
    for r, start, end in [(r1, "11:00", "12:00"), (r1, "09:00", "10:00"),
                          (r2, "09:00", "10:30"), (r1, "10:00", "11:00"),
                          (r2, "12:00", "13:00")]:
        s, b = post_booking(api, booking(r, start, end))
        assert s == 201
        created.append(b)
    def listed(**params):
        status, data = request(api, "GET", query("/bookings", **params))
        assert status == 200 and data["next_cursor"] is None
        return [b["id"] for b in data["bookings"]]
    assert listed() == ["b2", "b3", "b4", "b1", "b5"]
    assert listed(room_id=r1) == ["b2", "b4", "b1"]
    assert listed(room_id="unknown") == []
    assert listed(**{"from": ts("10:00")}) == ["b3", "b4", "b1", "b5"]
    assert listed(to=ts("11:00")) == ["b2", "b3", "b4"]
    assert listed(**{"from": ts("10:00"), "to": ts("11:00"), "room_id": r1}) == ["b4"]
    assert listed(**{"from": "2026-03-02T10:00:01Z", "to": "2026-03-02T10:00:02Z"}) == ["b3", "b4"]
    assert listed(**{"from": ts("13:00")}) == []


@pytest.mark.parametrize("param,value", [
    *[(p, v) for p in ("from", "to") for v in ["", "2026-02-30T10:00:00Z", "2026-03-02", "2026-03-02T10:00:00+00:00"]],
    *[("limit", v) for v in ["", "0", "101", "-1", "+1", "1.0", "true", "abc"]],
    *[("cursor", v) for v in ["", "garbage", "c2026-02-30T09:00:00Z_1", "c2026-03-02T09:00:00Z_x"]],
])
def test_invalid_listing_parameters(api, param, value):
    error(request(api, "GET", query("/bookings", **{param: value})), 422, "validation_error")


def test_pagination_default_limit_ties_and_filters(api):
    # Equal starts in different rooms exercise the creation-order tiebreaker.
    for i in range(25):
        r = api.room(f"Room {i}")
        assert post_booking(api, booking(r, "09:00", "10:00"))[0] == 201
    s, page = request(api, "GET", "/bookings")
    assert s == 200 and [b["id"] for b in page["bookings"]] == [f"b{i}" for i in range(1, 21)]
    assert isinstance(page["next_cursor"], str) and page["next_cursor"]
    s, last = request(api, "GET", query("/bookings", cursor=page["next_cursor"]))
    assert s == 200 and [b["id"] for b in last["bookings"]] == [f"b{i}" for i in range(21, 26)]
    assert last["next_cursor"] is None
    filters = {"from": ts("08:00"), "to": ts("10:00"), "limit": 7}
    seen, cursor = [], None
    while True:
        params = dict(filters)
        if cursor is not None:
            params["cursor"] = cursor
        s, page = request(api, "GET", query("/bookings", **params))
        assert s == 200 and len(page["bookings"]) <= 7
        seen.extend(b["id"] for b in page["bookings"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert isinstance(cursor, str) and cursor and len(seen) < 25
    assert seen == [f"b{i}" for i in range(1, 26)]
    s, page = request(api, "GET", query("/bookings", limit=100))
    assert s == 200 and len(page["bookings"]) == 25 and page["next_cursor"] is None
    s, page = request(api, "GET", query("/bookings", room_id="r1", limit=1))
    assert s == 200 and [b["id"] for b in page["bookings"]] == ["b1"] and page["next_cursor"] is None


def test_availability_gaps_clipping_and_other_rooms(api):
    r = api.room("One")
    other = api.room("Two")
    path = f"/rooms/{r}/availability?date=2026-03-02"
    def availability():
        s, data = request(api, "GET", path)
        assert s == 200 and data["date"] == "2026-03-02"
        return data["free"]
    def gap(start, end):
        return {"start": ts(start), "end": ts(end)}
    assert availability() == [gap("08:00", "18:00")]
    for start, end in [("07:00", "08:30"), ("09:00", "10:00"), ("10:00", "10:30"),
                       ("12:00", "13:00"), ("17:30", "19:00"), ("20:00", "21:00")]:
        assert post_booking(api, booking(r, start, end))[0] == 201
    assert post_booking(api, booking(other, "10:30", "12:00"))[0] == 201
    assert availability() == [gap("08:30", "09:00"), gap("10:30", "12:00"), gap("13:00", "17:30")]
    assert request(api, "DELETE", "/bookings/b3") == (204, None)
    assert availability() == [gap("08:30", "09:00"), gap("10:00", "12:00"), gap("13:00", "17:30")]
    assert request(api, "GET", f"/rooms/{r}/availability?date=2026-03-03")[1]["free"] == [
        {"start": "2026-03-03T08:00:00Z", "end": "2026-03-03T18:00:00Z"}]


def test_availability_fully_booked_and_cross_midnight(api):
    r = api.room()
    assert post_booking(api, booking(r, "08:00", "16:00"))[0] == 201
    assert post_booking(api, booking(r, "16:00", "18:00"))[0] == 201
    assert request(api, "GET", f"/rooms/{r}/availability?date=2026-03-02")[1]["free"] == []
    r2 = api.room("Other")
    assert post_booking(api, booking(r2, "2026-03-01T23:00:00Z", "2026-03-02T08:15:00Z"))[0] == 422  # over 8 hours
    assert post_booking(api, booking(r2, "2026-03-02T23:00:00Z", "2026-03-03T07:00:00Z"))[0] == 201
    assert request(api, "GET", f"/rooms/{r2}/availability?date=2026-03-03")[1]["free"] == [
        {"start": "2026-03-03T08:00:00Z", "end": "2026-03-03T18:00:00Z"}]


@pytest.mark.parametrize("date", [None, "", "2026-02-29", "2026-13-01", "2026-03-32", "2026-3-02", "2026-03-02T00:00:00Z", "nonsense"])
def test_availability_date_validation_and_room_precedence(api, date):
    r = api.room()
    suffix = "" if date is None else "?" + urlencode({"date": date})
    error(request(api, "GET", f"/rooms/{r}/availability{suffix}"), 422, "validation_error")
    error(request(api, "GET", f"/rooms/unknown/availability{suffix}"), 404, "not_found")


def test_availability_leap_day(api):
    r = api.room()
    assert request(api, "GET", f"/rooms/{r}/availability?date=2024-02-29") == (200, {
        "date": "2024-02-29", "free": [{"start": "2024-02-29T08:00:00Z", "end": "2024-02-29T18:00:00Z"}]})


def test_filtered_pagination_across_start_times(api):
    r = api.room("One")
    other = api.room("Two")
    # Creation order differs from chronological order; filtered-out items must not
    # consume page slots or change the next cursor's position.
    for room, start, end in [(r, "12:00", "12:15"), (other, "09:15", "09:30"),
                             (r, "09:00", "09:15"), (r, "10:30", "10:45"),
                             (r, "09:15", "09:30"), (r, "11:45", "12:00"),
                             (other, "11:00", "11:15")]:
        assert post_booking(api, booking(room, start, end))[0] == 201
    params = {"room_id": r, "from": ts("09:15"), "to": ts("12:00"), "limit": 1}
    ids = []
    while True:
        s, page = request(api, "GET", query("/bookings", **params))
        assert s == 200 and len(page["bookings"]) == 1
        ids.append(page["bookings"][0]["id"])
        if page["next_cursor"] is None:
            break
        assert len(ids) < 4
        params["cursor"] = page["next_cursor"]
    assert ids == ["b5", "b4", "b6"]


def test_idempotency_validation_and_unknown_room_failures_can_be_retried(api):
    api.room()
    h = {"Idempotency-Key": "recover"}
    error(post_booking(api, {"room_id": "r1"}, h), 422, "validation_error")
    error(post_booking(api, booking("missing"), h), 404, "not_found")
    error(request(api, "POST", "/bookings", headers=h, raw=b"{"), 400, "bad_json")
    s, saved = post_booking(api, booking(), h)
    assert s == 201 and saved["id"] == "b1"
    assert post_booking(api, booking(), h) == (201, saved)
