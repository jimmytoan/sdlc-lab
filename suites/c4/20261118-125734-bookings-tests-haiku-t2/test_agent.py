"""Regression suite for the bookings API, written against SPEC.md.

Every test drives server.py as a black box over HTTP (see helpers.py). Each test gets a fresh server,
so state never leaks between tests.
"""
import json
import re

import pytest

from helpers import Api, api, code, ts  # noqa: F401  (api is a fixture)

DAY = "2026-03-02"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
BOOKING_KEYS = {"id", "room_id", "start", "end", "title", "attendees"}
ROOM_KEYS = {"id", "name", "capacity"}


# ---------------------------------------------------------------- helpers

def at(minutes, day=DAY):
    """UTC timestamp for `minutes` after midnight on `day`. May exceed 24h only if the caller avoids it."""
    h, m = divmod(minutes, 60)
    return f"{day}T{h:02d}:{m:02d}:00Z"


def hm(hh, mm=0, day=DAY):
    return at(hh * 60 + mm, day)


def assert_error(resp, status, err_code):
    s, body, headers = resp
    assert s == status, (s, body)
    assert isinstance(body, dict) and set(body) == {"error"}, body
    assert body["error"]["code"] == err_code, body
    assert isinstance(body["error"].get("message"), str)
    assert headers["content-type"].startswith("application/json")


def booking_body(room_id, **over):
    body = {"room_id": room_id, "start": hm(9), "end": hm(10), "title": "sync", "attendees": 2}
    body.update(over)
    return body


def post_booking(api, room_id, headers=None, **over):
    return api.call("POST", "/bookings", booking_body(room_id, **over), headers=headers)


def post_room(api, name="Atlas", capacity=8):
    return api.call("POST", "/rooms", {"name": name, "capacity": capacity})


def list_bookings(api, query=""):
    s, b, _ = api.call("GET", "/bookings" + (("?" + query) if query else ""))
    assert s == 200, (s, b)
    return b


def ids(bookings_body):
    return [x["id"] for x in bookings_body["bookings"]]


def walk_pages(api, query, limit):
    """Follow next_cursor until exhausted; return all bookings in order and the number of pages."""
    out, pages, cursor = [], 0, None
    while True:
        q = f"{query}&limit={limit}" if query else f"limit={limit}"
        if cursor is not None:
            q += f"&cursor={cursor}"
        page = list_bookings(api, q)
        pages += 1
        out.extend(page["bookings"])
        cursor = page["next_cursor"]
        if cursor is None:
            return out, pages
        assert pages < 1000, "pagination does not terminate"


@pytest.fixture
def room(api):
    """A room with capacity 8 that exists as r1."""
    s, b, _ = post_room(api, "Main", 8)
    assert s == 201
    return b["id"]


# ---------------------------------------------------------------- generic HTTP rules

@pytest.mark.parametrize("method,path", [
    ("GET", "/nope"),
    ("POST", "/nope"),
    ("DELETE", "/nope"),
    ("GET", "/"),
    ("GET", "/rooms/r1/nope"),
    ("GET", "/rooms/r1/availability/extra"),
    ("GET", "/bookings/b1/extra"),
    ("GET", "/rooms/zz"),
    ("GET", "/bookings/zz"),
])
def test_unknown_path_or_id_is_404(api, method, path):
    s, b, h = api.call(method, path)
    assert s == 404
    assert code(b) == "not_found"
    assert h["content-type"].startswith("application/json")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("PATCH", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("POST", "/rooms/r1/availability"),
    ("DELETE", "/rooms/r1/availability"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
])
def test_existing_path_wrong_method_is_405(api, method, path):
    post_room(api, "Main", 8)  # r1
    api.book("r1", "09:00", "10:00")  # b1
    s, b, h = api.call(method, path, {"name": "x", "capacity": 1} if method != "DELETE" else None)
    assert s == 405
    assert code(b) == "method_not_allowed"
    assert h["content-type"].startswith("application/json")


@pytest.mark.parametrize("raw", [
    b"",
    b"{",
    b"not json",
    b"{'name': 'x'}",
    b"[]",
    b"[1, 2]",
    b'"text"',
    b"42",
    b"null",
    b"true",
    b"\xff\xfe\x00",
])
@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
def test_bad_json_body_is_400(api, room, path, raw):
    s, b, h = api.call("POST", path, raw=raw)
    assert s == 400
    assert code(b) == "bad_json"
    assert h["content-type"].startswith("application/json")


def test_valid_json_object_is_not_bad_json(api):
    s, b, _ = api.call("POST", "/rooms", raw=b'  {"name": "Atlas", "capacity": 3}  \n')
    assert s == 201 and b["name"] == "Atlas"


def test_unknown_body_fields_are_ignored_for_rooms(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 3, "colour": "red", "x": [1]})
    assert s == 201
    assert set(b) == ROOM_KEYS


def test_unknown_body_fields_are_ignored_for_bookings(api, room):
    s, b, _ = post_booking(api, room, colour="red", extra={"a": 1})
    assert s == 201
    assert set(b) == BOOKING_KEYS


def test_error_bodies_have_code_and_message(api):
    s, b, _ = api.call("GET", "/rooms/nope")
    assert s == 404
    assert set(b) == {"error"} and set(b["error"]) >= {"code", "message"}
    assert isinstance(b["error"]["message"], str)


def test_json_content_type_on_success_responses(api, room):
    for path in ("/rooms", f"/rooms/{room}", "/bookings", f"/rooms/{room}/availability?date={DAY}"):
        s, b, h = api.call("GET", path)
        assert s == 200
        assert h["content-type"].startswith("application/json"), path
    s, b, h = post_booking(api, room)
    assert s == 201 and h["content-type"].startswith("application/json")


def test_delete_returns_204_with_empty_body(api, room):
    post_booking(api, room)
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204
    assert b is None


# ---------------------------------------------------------------- rooms

def test_create_room_response_shape(api):
    s, b, h = post_room(api, "Atlas", 8)
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}
    assert h["content-type"].startswith("application/json")


def test_room_ids_follow_creation_order(api):
    for i, name in enumerate(["A", "B", "C"], start=1):
        assert post_room(api, name)[1]["id"] == f"r{i}"


def test_failed_room_creation_does_not_consume_an_id(api):
    assert post_room(api, "")[0] == 422
    assert post_room(api, "A")[1]["id"] == "r1"
    assert post_room(api, "a")[0] == 409
    assert post_room(api, "B")[1]["id"] == "r2"


def test_list_rooms_in_creation_order(api):
    assert api.call("GET", "/rooms")[1] == {"rooms": []}
    post_room(api, "Zeta", 2)
    post_room(api, "Alpha", 3)
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200
    assert b == {"rooms": [{"id": "r1", "name": "Zeta", "capacity": 2},
                           {"id": "r2", "name": "Alpha", "capacity": 3}]}


def test_get_room_by_id(api):
    post_room(api, "Atlas", 8)
    s, b, _ = api.call("GET", "/rooms/r1")
    assert s == 200 and b == {"id": "r1", "name": "Atlas", "capacity": 8}


@pytest.mark.parametrize("name", ["x" * 50, "  " + "x" * 50 + "  ", "é" * 50])
def test_room_name_length_boundaries_accepted(api, name):
    s, b, _ = post_room(api, name)
    assert s == 201
    assert b["name"] == name.strip()


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 51, "  " + "x" * 51 + "  ", None, 5, ["a"], {"a": 1}])
def test_invalid_room_name_is_422(api, name):
    body = {"capacity": 4}
    if name is not None:
        body["name"] = name
    s, b, _ = api.call("POST", "/rooms", body)
    assert s == 422
    assert code(b) == "validation_error"


def test_missing_room_name_is_422(api):
    s, b, _ = api.call("POST", "/rooms", {"capacity": 4})
    assert s == 422 and code(b) == "validation_error"


def test_room_name_is_stored_trimmed(api):
    s, b, _ = post_room(api, "   Atlas  ")
    assert s == 201 and b["name"] == "Atlas"
    assert api.call("GET", "/rooms/r1")[1]["name"] == "Atlas"


@pytest.mark.parametrize("second", ["atlas", "ATLAS", "  Atlas ", "aTlAs"])
def test_duplicate_room_name_ignores_case_and_surrounding_space(api, second):
    assert post_room(api, "Atlas")[0] == 201
    s, b, _ = post_room(api, second)
    assert s == 409
    assert code(b) == "room_exists"


def test_same_name_inside_different_text_is_not_duplicate(api):
    assert post_room(api, "Atlas")[0] == 201
    assert post_room(api, "Atlas 2")[0] == 201


@pytest.mark.parametrize("capacity", [1, 500])
def test_capacity_boundaries_accepted(api, capacity):
    s, b, _ = post_room(api, "Room", capacity)
    assert s == 201 and b["capacity"] == capacity


@pytest.mark.parametrize("capacity", [0, -1, 501, 10000, "8", "", None, True, False, 8.5, [8], {"n": 8}])
def test_invalid_capacity_is_422(api, capacity):
    body = {"name": "Room"}
    if capacity is not None:
        body["capacity"] = capacity
    s, b, _ = api.call("POST", "/rooms", body)
    assert s == 422
    assert code(b) == "validation_error"


def test_missing_capacity_is_422(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Room"})
    assert s == 422 and code(b) == "validation_error"


def test_get_unknown_room_is_404(api):
    s, b, _ = api.call("GET", "/rooms/r1")
    assert s == 404 and code(b) == "not_found"


# ---------------------------------------------------------------- booking validation

def test_create_booking_response_shape(api, room):
    s, b, h = api.call("POST", "/bookings", booking_body(room, title="Planning", attendees=4))
    assert s == 201
    assert b == {"id": "b1", "room_id": room, "start": hm(9), "end": hm(10), "title": "Planning", "attendees": 4}
    assert h["content-type"].startswith("application/json")
    assert api.call("GET", "/bookings/b1")[1] == b


def test_booking_ids_follow_creation_order(api, room):
    for i in range(1, 4):
        s, b, _ = post_booking(api, room, start=hm(8 + i), end=hm(9 + i))
        assert s == 201 and b["id"] == f"b{i}"


def test_failed_booking_does_not_consume_an_id(api, room):
    assert post_booking(api, room)[0] == 201  # b1
    assert post_booking(api, room)[0] == 409  # conflict, not stored
    assert post_booking(api, room, start=hm(11), end=hm(12))[1]["id"] == "b2"


def test_booking_timestamps_are_stored_in_canonical_form(api, room):
    s, b, _ = post_booking(api, room, start="2026-03-02T09:00:00Z", end="2026-03-02T10:00:00Z")
    assert s == 201
    assert TS_RE.match(b["start"]) and TS_RE.match(b["end"])


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_missing_booking_field_is_422(api, room, field):
    body = booking_body(room)
    del body[field]
    s, b, _ = api.call("POST", "/bookings", body)
    assert s == 422
    assert code(b) == "validation_error"


@pytest.mark.parametrize("over", [
    {"room_id": 1},
    {"room_id": None},
    {"room_id": ["r1"]},
    {"title": 5},
    {"title": None},
    {"title": ["x"]},
    {"attendees": "2"},
    {"attendees": 2.5},
    {"attendees": None},
    {"attendees": True},
    {"attendees": False},
    {"start": 900},
    {"start": None},
    {"end": ["x"]},
])
def test_wrong_types_are_422(api, room, over):
    body = booking_body(room)
    body.update(over)
    s, b, _ = api.call("POST", "/bookings", body)
    assert s == 422
    assert code(b) == "validation_error"


@pytest.mark.parametrize("bad", [
    "2026-03-02T09:00:00",          # no Z
    "2026-03-02T09:00:00+00:00",    # offset instead of Z
    "2026-03-02T09:00:00.000Z",     # fractional seconds
    "2026-03-02T09:00Z",            # no seconds
    "2026-03-02 09:00:00Z",         # space instead of T
    "2026-03-02t09:00:00Z",         # lower-case t
    "2026-03-02",                   # date only
    "09:00",                        # time only
    "2026-02-30T09:00:00Z",         # not a real date
    "2026-13-02T09:00:00Z",         # month 13
    "2026-03-02T24:00:00Z",         # hour 24
    "2026-03-02T09:60:00Z",         # minute 60
    "2026-3-2T09:00:00Z",           # not zero padded
    "",
    "tomorrow",
])
def test_malformed_timestamp_is_422(api, room, bad):
    s, b, _ = post_booking(api, room, start=bad)
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = post_booking(api, room, start=hm(9), end=bad)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("bad", ["2026-03-02T09:07:00Z", "2026-03-02T09:01:00Z", "2026-03-02T09:14:00Z",
                                 "2026-03-02T09:46:00Z", "2026-03-02T09:59:00Z", "2026-03-02T09:00:30Z",
                                 "2026-03-02T09:00:01Z"])
def test_start_must_be_quarter_hour_aligned(api, room, bad):
    s, b, _ = post_booking(api, room, start=bad, end=hm(11))
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = post_booking(api, room, start=hm(8), end=bad)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("minutes", [0, 15, 30, 45])
def test_quarter_hour_starts_accepted(api, room, minutes):
    s, b, _ = post_booking(api, room, start=hm(9, minutes), end=hm(9, minutes + 15) if minutes < 45 else hm(10))
    assert s == 201, b


def test_end_must_be_after_start(api, room):
    s, b, _ = post_booking(api, room, start=hm(10), end=hm(9))
    assert s == 422 and code(b) == "validation_error"


def test_zero_length_booking_is_422(api, room):
    s, b, _ = post_booking(api, room, start=hm(9), end=hm(9))
    assert s == 422 and code(b) == "validation_error"


def test_booking_of_exactly_8_hours_is_accepted(api, room):
    s, b, _ = post_booking(api, room, start=hm(8), end=hm(16))
    assert s == 201


def test_booking_longer_than_8_hours_is_422(api, room):
    s, b, _ = post_booking(api, room, start=hm(8), end=hm(16, 15))
    assert s == 422 and code(b) == "validation_error"


def test_booking_across_midnight_longer_than_8_hours_is_422(api, room):
    s, b, _ = post_booking(api, room, start=hm(22), end=at(7 * 60 + 15, day="2026-03-03"))
    assert s == 422 and code(b) == "validation_error"


def test_booking_across_midnight_of_8_hours_is_accepted(api, room):
    s, b, _ = post_booking(api, room, start=hm(23), end=at(7 * 60, day="2026-03-03"))
    assert s == 201


@pytest.mark.parametrize("title", ["", "   ", "\t \n", "x" * 101, " " + "x" * 101 + " "])
def test_invalid_title_is_422(api, room, title):
    s, b, _ = post_booking(api, room, title=title)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("title", ["x", "x" * 100, "  " + "x" * 100 + "  "])
def test_title_length_boundaries_accepted(api, room, title):
    s, b, _ = post_booking(api, room, title=title)
    assert s == 201
    assert b["title"] == title.strip()


def test_title_is_stored_trimmed(api, room):
    s, b, _ = post_booking(api, room, title="  Planning  ")
    assert s == 201 and b["title"] == "Planning"
    assert api.call("GET", "/bookings/b1")[1]["title"] == "Planning"


@pytest.mark.parametrize("attendees", [0, -1, -100])
def test_attendees_below_one_is_422(api, room, attendees):
    s, b, _ = post_booking(api, room, attendees=attendees)
    assert s == 422 and code(b) == "validation_error"


def test_attendees_equal_to_capacity_is_accepted(api):
    room = post_room(api, "Small", 3)[1]["id"]
    s, b, _ = post_booking(api, room, attendees=3)
    assert s == 201


def test_attendees_over_capacity_is_422_over_capacity(api):
    room = post_room(api, "Small", 3)[1]["id"]
    s, b, _ = post_booking(api, room, attendees=4)
    assert s == 422 and code(b) == "over_capacity"


def test_unknown_room_is_404(api):
    s, b, _ = post_booking(api, "r99")
    assert s == 404 and code(b) == "not_found"


def test_validation_is_checked_before_room_existence(api):
    s, b, _ = post_booking(api, "r99", start=hm(9, 7))
    assert s == 422 and code(b) == "validation_error"


def test_validation_is_checked_before_capacity(api):
    room = post_room(api, "Small", 2)[1]["id"]
    s, b, _ = post_booking(api, room, attendees=5, start=hm(10), end=hm(9))
    assert s == 422 and code(b) == "validation_error"


def test_room_existence_is_checked_before_conflict(api):
    s, b, _ = post_booking(api, "r99")
    assert s == 404


def test_capacity_is_checked_before_conflict(api):
    room = post_room(api, "Small", 2)[1]["id"]
    assert post_booking(api, room, attendees=2)[0] == 201
    s, b, _ = post_booking(api, room, attendees=3)
    assert s == 422 and code(b) == "over_capacity"


# ---------------------------------------------------------------- conflicts

@pytest.mark.parametrize("start,end", [
    (hm(9, 30), hm(10, 30)),   # overlaps the end
    (hm(8, 30), hm(9, 30)),    # overlaps the start
    (hm(9, 15), hm(9, 45)),    # contained
    (hm(8), hm(11)),           # contains the existing booking
    (hm(9), hm(10)),           # identical
    (hm(9, 45), hm(12)),       # overlap by one quarter hour
])
def test_overlapping_booking_is_409_conflict(api, room, start, end):
    assert post_booking(api, room, start=hm(9), end=hm(10))[0] == 201
    s, b, _ = post_booking(api, room, start=start, end=end)
    assert s == 409 and code(b) == "conflict"


def test_back_to_back_bookings_are_allowed_both_ways(api, room):
    assert post_booking(api, room, start=hm(10), end=hm(11))[0] == 201
    assert post_booking(api, room, start=hm(9), end=hm(10))[0] == 201
    assert post_booking(api, room, start=hm(11), end=hm(12))[0] == 201


def test_bookings_in_different_rooms_do_not_conflict(api):
    a = post_room(api, "A", 8)[1]["id"]
    b_ = post_room(api, "B", 8)[1]["id"]
    assert post_booking(api, a)[0] == 201
    assert post_booking(api, b_)[0] == 201


def test_conflict_with_deleted_booking_is_not_reported(api, room):
    post_booking(api, room)
    api.call("DELETE", "/bookings/b1")
    assert post_booking(api, room)[0] == 201


def test_conflict_is_detected_after_a_delete_of_another_booking(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))        # b1
    post_booking(api, room, start=hm(10), end=hm(11))       # b2
    api.call("DELETE", "/bookings/b2")
    assert post_booking(api, room, start=hm(9, 30), end=hm(10, 30))[0] == 409


# ---------------------------------------------------------------- get / delete

def test_get_booking(api, room):
    _, created, _ = post_booking(api, room)
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 200 and b == created


def test_get_unknown_booking_is_404(api, room):
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"


def test_delete_frees_the_slot(api, room):
    post_booking(api, room)
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204
    assert api.call("GET", "/bookings/b1")[0] == 404
    assert post_booking(api, room)[0] == 201


def test_delete_twice_is_404(api, room):
    post_booking(api, room)
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"


def test_delete_unknown_booking_is_404(api, room):
    s, b, _ = api.call("DELETE", "/bookings/b42")
    assert s == 404 and code(b) == "not_found"


def test_deleted_booking_is_not_listed(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    post_booking(api, room, start=hm(10), end=hm(11))
    api.call("DELETE", "/bookings/b1")
    assert ids(list_bookings(api)) == ["b2"]


def test_ids_are_never_reused_after_delete(api, room):
    post_booking(api, room)                       # b1
    api.call("DELETE", "/bookings/b1")
    s, b, _ = post_booking(api, room)
    assert s == 201 and b["id"] == "b2"
    assert api.call("GET", "/bookings/b1")[0] == 404


def test_deleting_one_booking_does_not_affect_others(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    post_booking(api, room, start=hm(11), end=hm(12))
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.call("GET", "/bookings/b2")
    assert s == 200 and b["start"] == hm(11)


# ---------------------------------------------------------------- idempotency

def test_same_key_same_body_returns_original_response_without_creating(api, room):
    h = {"Idempotency-Key": "abc"}
    s1, b1, _ = post_booking(api, room, headers=h)
    s2, b2, _ = post_booking(api, room, headers=h)
    assert s1 == 201 and s2 == 201
    assert b2 == b1 and b2["id"] == "b1"
    assert len(list_bookings(api)["bookings"]) == 1
    assert post_booking(api, room, start=hm(11), end=hm(12))[1]["id"] == "b2"


def test_idempotent_replay_ignores_key_order_and_whitespace(api, room):
    h = {"Idempotency-Key": "k-order"}
    first = booking_body(room, title="Planning", attendees=3)
    s1, b1, _ = api.call("POST", "/bookings", first, headers=h)
    reordered = json.dumps(
        {"attendees": 3, "title": "Planning", "end": hm(10), "start": hm(9), "room_id": room}, indent=4)
    s2, b2, _ = api.call("POST", "/bookings", raw=reordered.encode(), headers=h)
    assert s1 == 201 and s2 == 201 and b2 == b1
    assert len(list_bookings(api)["bookings"]) == 1


def test_idempotent_replay_survives_deletion_without_recreating(api, room):
    h = {"Idempotency-Key": "gone"}
    assert post_booking(api, room, headers=h)[0] == 201
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = post_booking(api, room, headers=h)
    assert s == 201 and b["id"] == "b1"
    assert api.call("GET", "/bookings/b1")[0] == 404
    assert list_bookings(api) == {"bookings": [], "next_cursor": None}


def test_same_key_different_body_is_422_mismatch(api, room):
    h = {"Idempotency-Key": "abc"}
    assert post_booking(api, room, headers=h)[0] == 201
    s, b, _ = post_booking(api, room, headers=h, title="Other")
    assert s == 422 and code(b) == "idempotency_mismatch"
    assert len(list_bookings(api)["bookings"]) == 1


@pytest.mark.parametrize("over", [
    {"attendees": 3},
    {"start": hm(9, 15)},
    {"end": hm(11)},
    {"start": hm(12), "end": hm(13)},
])
def test_same_key_changed_field_is_mismatch(api, room, over):
    h = {"Idempotency-Key": "abc"}
    assert post_booking(api, room, headers=h)[0] == 201
    s, b, _ = post_booking(api, room, headers=h, **over)
    assert s == 422 and code(b) == "idempotency_mismatch"


def test_same_key_unknown_extra_field_difference_is_mismatch(api, room):
    h = {"Idempotency-Key": "abc"}
    assert post_booking(api, room, headers=h)[0] == 201
    s, b, _ = post_booking(api, room, headers=h, colour="red")
    assert s == 422 and code(b) == "idempotency_mismatch"


def test_mismatch_does_not_change_the_original_replay(api, room):
    h = {"Idempotency-Key": "abc"}
    _, original, _ = post_booking(api, room, headers=h)
    assert post_booking(api, room, headers=h, title="Other")[0] == 422
    s, again, _ = post_booking(api, room, headers=h)
    assert s == 201 and again == original


def test_failed_request_with_key_is_not_remembered_validation(api, room):
    h = {"Idempotency-Key": "retry"}
    s, b, _ = post_booking(api, room, headers=h, start=hm(9, 7))
    assert s == 422
    s, b, _ = post_booking(api, room, headers=h)
    assert s == 201 and b["id"] == "b1"


def test_failed_request_with_key_is_not_remembered_conflict(api, room):
    h = {"Idempotency-Key": "retry"}
    post_booking(api, room, start=hm(9), end=hm(10))  # b1 occupies the slot
    s, b, _ = post_booking(api, room, headers=h, start=hm(9, 30), end=hm(10, 30))
    assert s == 409
    api.call("DELETE", "/bookings/b1")
    s, b, _ = post_booking(api, room, headers=h, start=hm(9, 30), end=hm(10, 30))
    assert s == 201 and b["start"] == hm(9, 30)


def test_failed_request_with_key_is_not_remembered_unknown_room(api, room):
    h = {"Idempotency-Key": "retry"}
    assert post_booking(api, "r99", headers=h)[0] == 404
    s, b, _ = post_booking(api, room, headers=h)
    assert s == 201


def test_failed_request_does_not_cause_mismatch_for_a_new_body(api, room):
    h = {"Idempotency-Key": "retry"}
    assert post_booking(api, room, headers=h, attendees=0)[0] == 422
    s, b, _ = post_booking(api, room, headers=h, title="Different")
    assert s == 201 and b["title"] == "Different"


def test_same_body_with_different_keys_creates_separate_requests(api, room):
    assert post_booking(api, room, headers={"Idempotency-Key": "one"})[0] == 201
    s, b, _ = post_booking(api, room, headers={"Idempotency-Key": "two"})
    assert s == 409 and code(b) == "conflict"


def test_no_key_means_no_idempotency(api, room):
    assert post_booking(api, room)[0] == 201
    s, b, _ = post_booking(api, room)
    assert s == 409 and code(b) == "conflict"


def test_idempotency_does_not_bypass_capacity_check_for_new_keys(api):
    room = post_room(api, "Small", 2)[1]["id"]
    s, b, _ = post_booking(api, room, headers={"Idempotency-Key": "x"}, attendees=5)
    assert s == 422 and code(b) == "over_capacity"


# ---------------------------------------------------------------- list / get bookings

def test_list_bookings_empty(api):
    assert list_bookings(api) == {"bookings": [], "next_cursor": None}


def test_list_bookings_default_order_is_start_then_creation(api):
    a = post_room(api, "A", 8)[1]["id"]
    b_ = post_room(api, "B", 8)[1]["id"]
    post_booking(api, a, start=hm(11), end=hm(12))   # b1 starts 11:00
    post_booking(api, b_, start=hm(9), end=hm(10))   # b2 starts 09:00, room B
    post_booking(api, a, start=hm(9), end=hm(10))    # b3 starts 09:00, room A (tie: later creation)
    post_booking(api, b_, start=hm(8), end=hm(9))    # b4 starts 08:00
    assert ids(list_bookings(api)) == ["b4", "b2", "b3", "b1"]


def test_list_bookings_shape(api, room):
    post_booking(api, room)
    body = list_bookings(api)
    assert set(body) == {"bookings", "next_cursor"}
    assert set(body["bookings"][0]) == BOOKING_KEYS
    assert body["next_cursor"] is None


def test_list_filter_by_room(api):
    a = post_room(api, "A", 8)[1]["id"]
    b_ = post_room(api, "B", 8)[1]["id"]
    post_booking(api, a, start=hm(9), end=hm(10))
    post_booking(api, b_, start=hm(9), end=hm(10))
    post_booking(api, a, start=hm(11), end=hm(12))
    got = list_bookings(api, f"room_id={a}")
    assert ids(got) == ["b1", "b3"]
    assert all(x["room_id"] == a for x in got["bookings"])


def test_list_filter_by_unknown_room_is_empty_not_error(api, room):
    post_booking(api, room)
    s, b, _ = api.call("GET", "/bookings?room_id=r99")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_filter_by_room_with_no_bookings_is_empty(api, room):
    other = post_room(api, "Other", 8)[1]["id"]
    post_booking(api, room)
    assert list_bookings(api, f"room_id={other}")["bookings"] == []


def test_window_includes_booking_overlapping_from(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    assert ids(list_bookings(api, f"from={hm(9, 30)}")) == ["b1"]


def test_window_excludes_booking_ending_exactly_at_from(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    assert ids(list_bookings(api, f"from={hm(10)}")) == []


def test_window_excludes_booking_starting_exactly_at_to(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    assert ids(list_bookings(api, f"to={hm(9)}")) == []


def test_window_includes_booking_starting_just_before_to(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    assert ids(list_bookings(api, f"to={at(9 * 60 + 1)}")) == ["b1"]


def test_window_includes_booking_ending_just_after_from(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    assert ids(list_bookings(api, f"from={at(10 * 60 - 1)}")) == ["b1"]


def test_window_from_and_to_together(api, room):
    post_booking(api, room, start=hm(8), end=hm(9))      # b1 before window
    post_booking(api, room, start=hm(9), end=hm(10))     # b2 inside
    post_booking(api, room, start=hm(10), end=hm(11))    # b3 inside
    post_booking(api, room, start=hm(11), end=hm(12))    # b4 after window
    got = list_bookings(api, f"from={hm(9)}&to={hm(11)}")
    assert ids(got) == ["b2", "b3"]


def test_window_spanning_booking_is_included(api, room):
    post_booking(api, room, start=hm(8), end=hm(16))
    assert ids(list_bookings(api, f"from={hm(10)}&to={hm(11)}")) == ["b1"]


def test_window_does_not_need_quarter_hour_alignment(api, room):
    post_booking(api, room, start=hm(9), end=hm(9, 30))
    assert ids(list_bookings(api, f"from={hm(9, 7)}&to={hm(9, 22)}")) == ["b1"]
    assert ids(list_bookings(api, f"from={hm(9, 31)}")) == []


@pytest.mark.parametrize("query", [
    "from=2026-03-02",
    "from=2026-03-02T09:00",
    "from=garbage",
    "from=",
    "to=2026-03-02T25:00:00Z",
    "to=tomorrow",
    "to=2026-03-02T09:00:00",
])
def test_invalid_window_timestamp_is_422(api, room, query):
    s, b, _ = api.call("GET", "/bookings?" + query)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", ""])
def test_invalid_limit_is_422(api, room, limit):
    s, b, _ = api.call("GET", f"/bookings?limit={limit}")
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("limit", ["1", "100"])
def test_limit_boundaries_accepted(api, room, limit):
    post_booking(api, room)
    s, b, _ = api.call("GET", f"/bookings?limit={limit}")
    assert s == 200 and len(b["bookings"]) == 1


@pytest.mark.parametrize("cursor", ["garbage", "12345", "c", "2026-03-02T09:00:00Z"])
def test_invalid_cursor_is_422(api, room, cursor):
    post_booking(api, room)
    s, b, _ = api.call("GET", f"/bookings?cursor={cursor}")
    assert s == 422 and code(b) == "validation_error"


def make_slots(api, room, n):
    """n back-to-back 15-minute bookings starting 08:00 in one room, so each is one distinct slot."""
    for i in range(n):
        s, b, _ = post_booking(api, room, start=at(8 * 60 + 15 * i), end=at(8 * 60 + 15 * (i + 1)))
        assert s == 201, b


def test_default_limit_is_20_with_next_cursor(api, room):
    make_slots(api, room, 21)
    first = list_bookings(api)
    assert len(first["bookings"]) == 20
    assert isinstance(first["next_cursor"], str) and first["next_cursor"]
    second = list_bookings(api, f"cursor={first['next_cursor']}")
    assert ids(second) == ["b21"]
    assert second["next_cursor"] is None


def test_exactly_default_limit_has_no_next_cursor(api, room):
    make_slots(api, room, 20)
    body = list_bookings(api)
    assert len(body["bookings"]) == 20
    assert body["next_cursor"] is None


def test_walking_pages_returns_every_booking_exactly_once_in_order(api, room):
    make_slots(api, room, 25)
    got, pages = walk_pages(api, "", limit=7)
    assert pages == 4
    assert [x["id"] for x in got] == [f"b{i}" for i in range(1, 26)]


def test_walking_pages_with_ties_is_stable_and_complete(api):
    rooms = [post_room(api, f"R{i}", 8)[1]["id"] for i in range(7)]
    for r in rooms:  # all start at 09:00 in different rooms: tie broken by creation order
        assert post_booking(api, r)[0] == 201
    got, _ = walk_pages(api, "", limit=2)
    assert [x["id"] for x in got] == [f"b{i}" for i in range(1, 8)]


def test_walking_pages_with_filters(api):
    a = post_room(api, "A", 8)[1]["id"]
    b_ = post_room(api, "B", 8)[1]["id"]
    for i in range(6):
        post_booking(api, a, start=at(8 * 60 + 15 * i), end=at(8 * 60 + 15 * (i + 1)))
        post_booking(api, b_, start=at(8 * 60 + 15 * i), end=at(8 * 60 + 15 * (i + 1)))
    got, _ = walk_pages(api, f"room_id={a}", limit=4)
    assert [x["id"] for x in got] == ["b1", "b3", "b5", "b7", "b9", "b11"]
    assert all(x["room_id"] == a for x in got)


def test_walking_pages_with_window_filter(api, room):
    make_slots(api, room, 10)
    got, _ = walk_pages(api, f"from={at(8 * 60 + 30)}&to={at(9 * 60 + 30)}", limit=2)
    assert [x["id"] for x in got] == ["b3", "b4", "b5", "b6"]


def test_limit_larger_than_result_returns_everything_without_cursor(api, room):
    make_slots(api, room, 3)
    body = list_bookings(api, "limit=100")
    assert ids(body) == ["b1", "b2", "b3"] and body["next_cursor"] is None


def test_get_booking_matches_list_entry(api, room):
    post_booking(api, room, title="  Hi  ")
    listed = list_bookings(api)["bookings"][0]
    assert listed == api.call("GET", "/bookings/b1")[1]
    assert listed["title"] == "Hi"


# ---------------------------------------------------------------- availability

def free_of(api, room, date=DAY):
    s, b, h = api.call("GET", f"/rooms/{room}/availability?date={date}")
    assert s == 200, (s, b)
    assert h["content-type"].startswith("application/json")
    assert set(b) == {"date", "free"}
    assert b["date"] == date
    for gap in b["free"]:
        assert set(gap) == {"start", "end"}
    return [(g["start"], g["end"]) for g in b["free"]]


def test_empty_room_is_free_all_working_day(api, room):
    assert free_of(api, room) == [(hm(8), hm(18))]


def test_availability_gaps_around_a_booking(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    assert free_of(api, room) == [(hm(8), hm(9)), (hm(10), hm(18))]


def test_availability_multiple_gaps_ascending(api, room):
    post_booking(api, room, start=hm(15), end=hm(15, 15))
    post_booking(api, room, start=hm(9), end=hm(10))
    post_booking(api, room, start=hm(12), end=hm(13))
    assert free_of(api, room) == [(hm(8), hm(9)), (hm(10), hm(12)), (hm(13), hm(15)), (hm(15, 15), hm(18))]


def test_adjacent_bookings_leave_no_gap(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    post_booking(api, room, start=hm(10), end=hm(11))
    assert free_of(api, room) == [(hm(8), hm(9)), (hm(11), hm(18))]


def test_fully_booked_day_gives_empty_list(api, room):
    post_booking(api, room, start=hm(8), end=hm(13))
    post_booking(api, room, start=hm(13), end=hm(18))
    assert free_of(api, room) == []


def test_booking_partly_before_working_day_is_clipped(api, room):
    post_booking(api, room, start=hm(7), end=hm(9))
    assert free_of(api, room) == [(hm(9), hm(18))]


def test_booking_partly_after_working_day_is_clipped(api, room):
    post_booking(api, room, start=hm(17), end=hm(18))
    assert free_of(api, room) == [(hm(8), hm(17))]


def test_booking_spanning_working_day_edges_is_clipped(api, room):
    post_booking(api, room, start=hm(7), end=hm(15))  # 7:00-15:00
    post_booking(api, room, start=hm(15), end=hm(19))                       # 15:00-19:00
    assert free_of(api, room) == []


def test_booking_outside_working_day_has_no_effect(api, room):
    post_booking(api, room, start=hm(6), end=hm(7))
    post_booking(api, room, start=hm(7), end=hm(8))   # ends exactly at 08:00
    post_booking(api, room, start=hm(18), end=hm(19))  # starts exactly at 18:00
    assert free_of(api, room) == [(hm(8), hm(18))]


def test_availability_only_counts_the_requested_day(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    post_booking(api, room, start=at(9 * 60, day="2026-03-03"), end=at(10 * 60, day="2026-03-03"))
    assert free_of(api, room, "2026-03-02") == [(hm(8), hm(9)), (hm(10), hm(18))]
    assert free_of(api, room, "2026-03-03") == [(at(8 * 60, "2026-03-03"), at(9 * 60, "2026-03-03")),
                                                (at(10 * 60, "2026-03-03"), at(18 * 60, "2026-03-03"))]
    assert free_of(api, room, "2026-03-04") == [(at(8 * 60, "2026-03-04"), at(18 * 60, "2026-03-04"))]


def test_availability_ignores_other_rooms(api, room):
    other = post_room(api, "Other", 8)[1]["id"]
    post_booking(api, other, start=hm(9), end=hm(10))
    assert free_of(api, room) == [(hm(8), hm(18))]


def test_availability_restored_after_delete(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    api.call("DELETE", "/bookings/b1")
    assert free_of(api, room) == [(hm(8), hm(18))]


def test_availability_accepts_leap_day(api):
    room = post_room(api, "Leap", 8)[1]["id"]
    assert free_of(api, room, "2024-02-29") == [(at(8 * 60, "2024-02-29"), at(18 * 60, "2024-02-29"))]


@pytest.mark.parametrize("query", [
    "",
    "?date=",
    "?date=2026-02-30",
    "?date=2026-02-29",
    "?date=2026-13-01",
    "?date=2026-00-10",
    "?date=2026-3-2",
    "?date=03/02/2026",
    "?date=2026-03-02T00:00:00Z",
    "?date=yesterday",
    "?day=2026-03-02",
])
def test_invalid_or_missing_date_is_422(api, room, query):
    s, b, _ = api.call("GET", f"/rooms/{room}/availability{query}")
    assert s == 422 and code(b) == "validation_error"


def test_unknown_room_is_404(api):
    s, b, _ = api.call("GET", f"/rooms/r99/availability?date={DAY}")
    assert s == 404 and code(b) == "not_found"


def test_unknown_room_with_invalid_date_is_404_room_checked_first(api):
    s, b, _ = api.call("GET", "/rooms/r99/availability?date=garbage")
    assert s == 404 and code(b) == "not_found"


def test_booking_ending_exactly_at_working_day_start_is_irrelevant(api, room):
    post_booking(api, room, start=hm(6), end=hm(8))
    assert free_of(api, room) == [(hm(8), hm(18))]


def test_single_quarter_hour_gap_is_reported(api, room):
    post_booking(api, room, start=hm(9), end=hm(10))
    post_booking(api, room, start=hm(10, 15), end=hm(11))
    assert free_of(api, room) == [(hm(8), hm(9)), (hm(10), hm(10, 15)), (hm(11), hm(18))]
