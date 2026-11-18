"""Regression suite for the bookings API, derived from SPEC.md.

Every test drives server.py as a black box over HTTP (via the `api` fixture in
helpers.py). Each test gets a fresh server, so state starts empty.
"""
import datetime as dt
import json
from urllib.parse import urlencode

import pytest

from helpers import api, code, ts  # noqa: F401  (api is a pytest fixture)

DAY = "2026-03-02"
RFC_OK = "application/json"


# ---------------------------------------------------------------- helpers

def make_room(a, name="Atlas", capacity=8):
    s, b, _ = a.call("POST", "/rooms", {"name": name, "capacity": capacity})
    assert s == 201, (s, b)
    return b["id"]


def book(a, room, start, end, title="sync", attendees=2, headers=None):
    return a.book(room, start, end, title=title, attendees=attendees, headers=headers)


def booking_ids(body):
    return [x["id"] for x in body["bookings"]]


def list_bookings(a, **params):
    q = urlencode(params)
    return a.call("GET", "/bookings" + (f"?{q}" if q else ""))


def availability(a, room, date=DAY):
    return a.call("GET", f"/rooms/{room}/availability?date={date}")


def free_window(start, end):
    return {"start": ts(start), "end": ts(end)}


def valid_booking(room, **over):
    body = {"room_id": room, "start": ts("09:00"), "end": ts("10:00"), "title": "sync", "attendees": 2}
    body.update(over)
    return body


def assert_error(resp, status, err_code):
    s, body, headers = resp
    assert s == status, (s, body)
    assert set(body.keys()) == {"error"}, body
    assert body["error"]["code"] == err_code, body
    assert isinstance(body["error"].get("message"), str), body
    assert headers["content-type"].startswith(RFC_OK)
    return body


# ---------------------------------------------------------------- general / routing

def test_entry_state_starts_empty(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


@pytest.mark.parametrize("path", ["/nope", "/rooms/r1/unknown", "/bookings/b1/x", "/", "/roomsx"])
def test_unknown_path_is_404(api, path):
    assert_error(api.call("GET", path), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("POST", "/rooms/r1/availability?date=2026-03-02"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
])
def test_unsupported_method_is_405(api, method, path):
    assert_error(api.call(method, path, body={"a": 1} if method != "DELETE" else None),
                 405, "method_not_allowed")


def test_405_is_not_404_for_existing_path_with_unknown_id(api):
    # The path shape exists, so the method check happens regardless of the id.
    assert_error(api.call("POST", "/rooms/nonexistent"), 405, "method_not_allowed")


def test_bad_json_is_400(api):
    assert_error(api.call("POST", "/rooms", raw=b"{not json"), 400, "bad_json")


def test_empty_body_is_400(api):
    assert_error(api.call("POST", "/rooms", raw=b""), 400, "bad_json")


@pytest.mark.parametrize("raw", [b"[]", b'"text"', b"42", b"null", b"true", b"[1, 2]"])
def test_valid_json_but_not_object_is_400(api, raw):
    assert_error(api.call("POST", "/rooms", raw=raw), 400, "bad_json")
    assert_error(api.call("POST", "/bookings", raw=raw), 400, "bad_json")


def test_invalid_utf8_body_is_400(api):
    assert_error(api.call("POST", "/rooms", raw=b"\xff\xfe{}"), 400, "bad_json")


def test_bad_json_on_bookings_is_400(api):
    assert_error(api.call("POST", "/bookings", raw=b"{"), 400, "bad_json")


def test_json_responses_have_content_type(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    checks = [
        api.call("GET", "/rooms"),
        api.call("GET", f"/rooms/{room}"),
        api.call("GET", "/bookings"),
        api.call("GET", "/bookings/b1"),
        api.call("GET", f"/rooms/{room}/availability?date={DAY}"),
        api.call("POST", "/rooms", {"name": "", "capacity": 1}),  # error body
        api.call("GET", "/nope"),
    ]
    for s, body, headers in checks:
        assert body is not None
        assert headers["content-type"].startswith(RFC_OK), headers


def test_delete_has_empty_body(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    s, body, headers = api.call("DELETE", "/bookings/b1")
    assert s == 204 and body is None
    assert "content-type" not in headers


def test_unknown_request_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 4, "colour": "red"})
    assert s == 201 and set(b.keys()) == {"id", "name", "capacity"}
    room = b["id"]
    s, b, _ = api.call("POST", "/bookings", valid_booking(room, extra=True, foo=[1]))
    assert s == 201 and set(b.keys()) == {"id", "room_id", "start", "end", "title", "attendees"}


# ---------------------------------------------------------------- rooms

def test_create_room_response(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}
    assert h["content-type"].startswith(RFC_OK)


def test_room_ids_sequential_in_creation_order(api):
    ids = [make_room(api, name=f"Room {i}") for i in range(5)]
    assert ids == ["r1", "r2", "r3", "r4", "r5"]
    s, b, _ = api.call("GET", "/rooms")
    assert [r["id"] for r in b["rooms"]] == ids
    assert [r["name"] for r in b["rooms"]] == [f"Room {i}" for i in range(5)]


def test_get_room(api):
    room = make_room(api, "Atlas", 8)
    assert api.call("GET", f"/rooms/{room}")[1] == {"id": room, "name": "Atlas", "capacity": 8}


def test_get_unknown_room_404(api):
    assert_error(api.call("GET", "/rooms/r99"), 404, "not_found")


@pytest.mark.parametrize("name,expected", [
    ("  Atlas  ", "Atlas"),
    ("\tBorealis\n", "Borealis"),
    ("A", "A"),
    ("x" * 50, "x" * 50),
    ("Room with spaces inside", "Room with spaces inside"),
])
def test_room_name_trimmed_and_stored(api, name, expected):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 1})
    assert s == 201 and b["name"] == expected
    assert api.call("GET", f"/rooms/{b['id']}")[1]["name"] == expected


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 51, " " * 3 + "y" * 51 + " "])
def test_room_name_length_validation(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("name", [None, 5, ["Atlas"], {"n": 1}, True])
def test_room_name_type_validation(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("capacity", [1, 500])
def test_room_capacity_bounds_accepted(api, capacity):
    s, b, _ = api.call("POST", "/rooms", {"name": f"c{capacity}", "capacity": capacity})
    assert s == 201 and b["capacity"] == capacity


@pytest.mark.parametrize("capacity", [0, -1, 501, 10_000, None, "8", 8.5, [8], True, False])
def test_room_capacity_validation(api, capacity):
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": capacity}), 422, "validation_error")


@pytest.mark.parametrize("body", [
    {"capacity": 8},
    {"name": "Atlas"},
    {},
])
def test_room_missing_fields(api, body):
    assert_error(api.call("POST", "/rooms", body), 422, "validation_error")


@pytest.mark.parametrize("second", ["atlas", "ATLAS", "  Atlas ", "aTlAs"])
def test_room_name_unique_ignoring_case_and_trim(api, second):
    make_room(api, "Atlas")
    assert_error(api.call("POST", "/rooms", {"name": second, "capacity": 3}), 409, "room_exists")


def test_room_duplicate_checked_after_validation_but_409_for_valid(api):
    make_room(api, "Atlas")
    # A duplicate name with otherwise valid fields is a conflict.
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 500}), 409, "room_exists")


def test_failed_room_creation_does_not_consume_id(api):
    make_room(api, "Atlas")
    assert api.call("POST", "/rooms", {"name": "Atlas", "capacity": 1})[0] == 409
    assert api.call("POST", "/rooms", {"name": "", "capacity": 1})[0] == 422
    s, b, _ = api.call("POST", "/rooms", {"name": "Borealis", "capacity": 1})
    assert s == 201 and b["id"] == "r2"


def test_rooms_with_different_names_are_independent(api):
    make_room(api, "Atlas")
    make_room(api, "Atlas Two")
    assert len(api.call("GET", "/rooms")[1]["rooms"]) == 2


# ---------------------------------------------------------------- booking creation

def test_create_booking_response_and_fetch(api):
    room = make_room(api)
    s, b, h = api.call("POST", "/bookings", {
        "room_id": room, "start": ts("09:00"), "end": ts("10:00"), "title": "Planning", "attendees": 4})
    assert s == 201
    assert b == {"id": "b1", "room_id": room, "start": ts("09:00"), "end": ts("10:00"),
                 "title": "Planning", "attendees": 4}
    assert h["content-type"].startswith(RFC_OK)
    assert api.call("GET", "/bookings/b1")[1] == b


def test_booking_ids_sequential_in_creation_order(api):
    room = make_room(api)
    ids = []
    for h in range(8, 12):
        s, b, _ = book(api, room, f"{h:02d}:00", f"{h:02d}:30")
        assert s == 201
        ids.append(b["id"])
    assert ids == ["b1", "b2", "b3", "b4"]


def test_booking_title_trimmed(api):
    room = make_room(api)
    s, b, _ = book(api, room, "09:00", "10:00", title="  Planning  ")
    assert s == 201 and b["title"] == "Planning"
    assert api.call("GET", "/bookings/b1")[1]["title"] == "Planning"


@pytest.mark.parametrize("title", ["x", "x" * 100, "  " + "y" * 98 + "  "])
def test_title_length_bounds_accepted(api, title):
    room = make_room(api)
    s, _, _ = book(api, room, "09:00", "10:00", title=title)
    assert s == 201


@pytest.mark.parametrize("title", ["", "   ", "\t", "z" * 101, "  " + "z" * 99 + "  x"])
def test_title_length_validation(api, title):
    room = make_room(api)
    assert_error(book(api, room, "09:00", "10:00", title=title), 422, "validation_error")


@pytest.mark.parametrize("title", [None, 3, ["a"]])
def test_title_type_validation(api, title):
    room = make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room, title=title)), 422, "validation_error")


@pytest.mark.parametrize("attendees", [0, -3, True, False, 2.5, "2", None, [2]])
def test_attendees_validation(api, attendees):
    room = make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room, attendees=attendees)), 422, "validation_error")


def test_attendees_equal_capacity_allowed(api):
    room = make_room(api, capacity=5)
    assert book(api, room, "09:00", "10:00", attendees=5)[0] == 201


def test_attendees_one_allowed(api):
    room = make_room(api, capacity=5)
    assert book(api, room, "09:00", "10:00", attendees=1)[0] == 201


def test_over_capacity(api):
    room = make_room(api, capacity=2)
    assert_error(book(api, room, "09:00", "10:00", attendees=3), 422, "over_capacity")


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_missing_field_is_validation_error(api, field):
    room = make_room(api)
    body = valid_booking(room)
    del body[field]
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("room_id", [None, 1, ["r1"]])
def test_room_id_type_validation(api, room_id):
    make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room_id)), 422, "validation_error")


@pytest.mark.parametrize("value", [
    "2026-03-02 09:00:00Z",
    "2026-03-02T09:00:00",
    "2026-03-02T09:00:00+00:00",
    "2026-03-02T09:00:00.000Z",
    "2026-03-02T09:00Z",
    "2026-03-02T09:00:00z",
    "2026-03-02T9:00:00Z",
    "2026-3-2T09:00:00Z",
    "2026-03-02T09:00:00Z ",
    " 2026-03-02T09:00:00Z",
    "2026-02-30T09:00:00Z",
    "2026-03-02T24:00:00Z",
    "2026-03-02T09:60:00Z",
    "not a time",
    "",
    1234567890,
    None,
])
def test_invalid_timestamp_format_rejected(api, value):
    room = make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room, start=value)), 422, "validation_error")
    assert_error(api.call("POST", "/bookings", valid_booking(room, end=value)), 422, "validation_error")


@pytest.mark.parametrize("value", ["2026-03-02T09:10:00Z", "2026-03-02T09:07:00Z", "2026-03-02T09:01:00Z",
                                   "2026-03-02T09:59:00Z"])
def test_start_minutes_must_be_quarter_hour(api, value):
    room = make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room, start=value)), 422, "validation_error")


@pytest.mark.parametrize("value", ["2026-03-02T09:05:00Z", "2026-03-02T09:50:00Z", "2026-03-02T10:20:00Z"])
def test_end_minutes_must_be_quarter_hour(api, value):
    room = make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room, end=value)), 422, "validation_error")


@pytest.mark.parametrize("value", ["2026-03-02T09:00:30Z", "2026-03-02T09:15:01Z", "2026-03-02T10:00:59Z"])
def test_seconds_must_be_zero(api, value):
    room = make_room(api)
    assert_error(api.call("POST", "/bookings", valid_booking(room, end=value)), 422, "validation_error")
    assert_error(api.call("POST", "/bookings", valid_booking(room, start=value)), 422, "validation_error")


@pytest.mark.parametrize("start,end", [
    ("09:00", "09:15"),
    ("09:15", "09:30"),
    ("09:30", "10:45"),
    ("23:45", "2026-03-03T00:15:00Z"),
])
def test_quarter_hour_alignments_accepted(api, start, end):
    room = make_room(api)
    s, _, _ = book(api, room, start, end)
    assert s == 201


@pytest.mark.parametrize("start,end", [
    ("10:00", "10:00"),
    ("10:00", "09:00"),
    ("10:00", "09:45"),
])
def test_end_must_be_after_start(api, start, end):
    room = make_room(api)
    assert_error(book(api, room, start, end), 422, "validation_error")


def test_booking_exactly_eight_hours_allowed(api):
    room = make_room(api)
    assert book(api, room, "09:00", "17:00")[0] == 201


def test_booking_longer_than_eight_hours_rejected(api):
    room = make_room(api)
    assert_error(book(api, room, "09:00", "17:15"), 422, "validation_error")


def test_booking_across_midnight_longer_than_eight_hours_rejected(api):
    room = make_room(api)
    assert_error(book(api, room, "20:00", "2026-03-03T05:00:00Z"), 422, "validation_error")


def test_booking_across_midnight_within_limit_allowed(api):
    room = make_room(api)
    assert book(api, room, "22:00", "2026-03-03T01:00:00Z")[0] == 201


def test_validation_precedes_room_lookup(api):
    # Unknown room + invalid body: validation wins.
    s, b, _ = api.call("POST", "/bookings", valid_booking("r999", attendees=0))
    assert_error((s, b, {"content-type": RFC_OK}), 422, "validation_error")


def test_unknown_room_is_404(api):
    assert_error(api.call("POST", "/bookings", valid_booking("r999")), 404, "not_found")


def test_unknown_room_checked_before_capacity_and_conflict(api):
    # Capacity is irrelevant when the room does not exist.
    assert_error(api.call("POST", "/bookings", valid_booking("r999", attendees=500)), 404, "not_found")


def test_over_capacity_checked_before_conflict(api):
    room = make_room(api, capacity=2)
    assert book(api, room, "09:00", "10:00", attendees=1)[0] == 201
    assert_error(book(api, room, "09:00", "10:00", attendees=3), 422, "over_capacity")


def test_failed_booking_does_not_consume_id(api):
    room = make_room(api, capacity=2)
    assert book(api, room, "09:00", "10:00", attendees=3)[0] == 422
    assert book(api, room, "09:00", "10:00", attendees=1)[0] == 201
    assert api.call("GET", "/bookings/b1")[0] == 200


# ---------------------------------------------------------------- overlaps

def test_overlap_at_start_conflicts(api):
    room = make_room(api)
    assert book(api, room, "09:00", "10:00")[0] == 201
    assert_error(book(api, room, "08:30", "09:30"), 409, "conflict")


def test_overlap_at_end_conflicts(api):
    room = make_room(api)
    assert book(api, room, "09:00", "10:00")[0] == 201
    assert_error(book(api, room, "09:30", "10:30"), 409, "conflict")


def test_new_contains_existing_conflicts(api):
    room = make_room(api)
    assert book(api, room, "10:00", "11:00")[0] == 201
    assert_error(book(api, room, "09:00", "12:00"), 409, "conflict")


def test_existing_contains_new_conflicts(api):
    room = make_room(api)
    assert book(api, room, "09:00", "12:00")[0] == 201
    assert_error(book(api, room, "10:00", "11:00"), 409, "conflict")


def test_identical_slot_conflicts(api):
    room = make_room(api)
    assert book(api, room, "09:00", "10:00")[0] == 201
    assert_error(book(api, room, "09:00", "10:00"), 409, "conflict")


def test_one_minute_style_overlap_by_quarter_hour_conflicts(api):
    room = make_room(api)
    assert book(api, room, "09:00", "09:30")[0] == 201
    assert_error(book(api, room, "09:15", "09:45"), 409, "conflict")


def test_back_to_back_allowed_both_directions(api):
    room = make_room(api)
    assert book(api, room, "10:00", "11:00")[0] == 201
    assert book(api, room, "09:00", "10:00")[0] == 201  # ends exactly when the other starts
    assert book(api, room, "11:00", "12:00")[0] == 201  # starts exactly when the other ends


def test_adjacent_slots_do_not_block_each_other_in_long_chain(api):
    room = make_room(api)
    for h in range(8, 18):
        assert book(api, room, f"{h:02d}:00", f"{h + 1:02d}:00")[0] == 201


def test_same_slot_different_rooms_allowed(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    assert book(api, r1, "09:00", "10:00")[0] == 201
    assert book(api, r2, "09:00", "10:00")[0] == 201


def test_cancelled_booking_does_not_conflict(api):
    room = make_room(api)
    assert book(api, room, "09:00", "10:00")[0] == 201
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert book(api, room, "09:30", "10:30")[0] == 201


def test_conflict_does_not_store_booking(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    assert book(api, room, "09:30", "10:30")[0] == 409
    s, b, _ = list_bookings(api, room_id=room)
    assert booking_ids(b) == ["b1"]


def test_delete_frees_slot_for_overlapping_booking(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = book(api, room, "09:00", "10:00")
    assert s == 201 and b["id"] == "b2"  # ids are never reused


# ---------------------------------------------------------------- get / delete bookings

def test_get_unknown_booking_404(api):
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_booking(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    s, body, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and body is None
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_unknown_booking_404(api):
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_delete_twice_second_is_404(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_deleted_id_not_reused(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = book(api, room, "11:00", "12:00")
    assert s == 201 and b["id"] == "b2"
    assert api.call("GET", "/bookings/b1")[0] == 404


def test_delete_does_not_affect_other_bookings(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    book(api, room, "11:00", "12:00")
    api.call("DELETE", "/bookings/b1")
    assert api.call("GET", "/bookings/b2")[1]["id"] == "b2"


# ---------------------------------------------------------------- idempotency

def test_idempotent_replay_returns_original_response_and_creates_nothing(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k1"}
    s1, b1, _ = book(api, room, "09:00", "10:00", title="Plan", attendees=3, headers=h)
    s2, b2, _ = book(api, room, "09:00", "10:00", title="Plan", attendees=3, headers=h)
    assert s1 == 201 and s2 == 201 and b1 == b2
    assert booking_ids(list_bookings(api)[1]) == ["b1"]


def test_idempotent_replay_does_not_consume_ids(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k1"}
    book(api, room, "09:00", "10:00", headers=h)
    book(api, room, "09:00", "10:00", headers=h)
    s, b, _ = book(api, room, "11:00", "12:00")
    assert s == 201 and b["id"] == "b2"


def test_idempotent_replay_ignores_key_order_and_whitespace(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k2"}
    raw1 = json.dumps({"room_id": room, "start": ts("09:00"), "end": ts("10:00"),
                       "title": "Plan", "attendees": 3}).encode()
    raw2 = b'{ "attendees" : 3,\n "title":"Plan", "end":"%s", "start":"%s", "room_id":"%s" }' % (
        ts("10:00").encode(), ts("09:00").encode(), room.encode())
    s1, b1, _ = api.call("POST", "/bookings", headers=h, raw=raw1)
    s2, b2, _ = api.call("POST", "/bookings", headers=h, raw=raw2)
    assert s1 == 201 and s2 == 201 and b1 == b2


def test_idempotent_replay_after_original_deleted_still_returns_original(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k3"}
    s1, b1, _ = book(api, room, "09:00", "10:00", headers=h)
    api.call("DELETE", "/bookings/b1")
    s2, b2, _ = book(api, room, "09:00", "10:00", headers=h)
    assert s2 == 201 and b2 == b1
    assert api.call("GET", "/bookings/b1")[0] == 404
    assert list_bookings(api)[1]["bookings"] == []


def test_same_key_different_body_is_mismatch(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k4"}
    assert book(api, room, "09:00", "10:00", headers=h)[0] == 201
    assert_error(book(api, room, "11:00", "12:00", headers=h), 422, "idempotency_mismatch")
    assert booking_ids(list_bookings(api)[1]) == ["b1"]


@pytest.mark.parametrize("change", [
    {"title": "Other"},
    {"attendees": 3},
    {"start": ts("09:15")},
    {"end": ts("10:15")},
])
def test_same_key_any_field_difference_is_mismatch(api, change):
    room = make_room(api)
    h = {"Idempotency-Key": "k5"}
    assert api.call("POST", "/bookings", valid_booking(room), headers=h)[0] == 201
    assert_error(api.call("POST", "/bookings", valid_booking(room, **change), headers=h),
                 422, "idempotency_mismatch")


def test_mismatch_keeps_original_remembered(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k6"}
    s1, b1, _ = book(api, room, "09:00", "10:00", headers=h)
    assert book(api, room, "11:00", "12:00", headers=h)[0] == 422
    s2, b2, _ = book(api, room, "09:00", "10:00", headers=h)
    assert s2 == 201 and b2 == b1


def test_mismatch_checked_before_other_validation(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k7"}
    book(api, room, "09:00", "10:00", headers=h)
    assert_error(api.call("POST", "/bookings", valid_booking(room, attendees=0), headers=h),
                 422, "idempotency_mismatch")


def test_failed_request_with_key_not_remembered_validation(api):
    room = make_room(api)
    h = {"Idempotency-Key": "k8"}
    assert_error(api.call("POST", "/bookings", valid_booking(room, attendees=0), headers=h),
                 422, "validation_error")
    s, b, _ = api.call("POST", "/bookings", valid_booking(room), headers=h)
    assert s == 201 and b["id"] == "b1"


def test_failed_request_with_key_not_remembered_conflict(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")  # b1
    h = {"Idempotency-Key": "k9"}
    assert_error(book(api, room, "09:30", "10:30", headers=h), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = book(api, room, "09:30", "10:30", headers=h)
    assert s == 201


def test_failed_request_with_key_not_remembered_unknown_room(api):
    h = {"Idempotency-Key": "k10"}
    assert_error(api.call("POST", "/bookings", valid_booking("r1"), headers=h), 404, "not_found")
    room = make_room(api)
    s, b, _ = api.call("POST", "/bookings", valid_booking(room), headers=h)
    assert s == 201 and b["id"] == "b1"


def test_failed_request_with_key_not_remembered_over_capacity(api):
    room = make_room(api, capacity=2)
    h = {"Idempotency-Key": "k11"}
    assert_error(book(api, room, "09:00", "10:00", attendees=5, headers=h), 422, "over_capacity")
    assert book(api, room, "09:00", "10:00", attendees=2, headers=h)[0] == 201


def test_different_keys_same_body_create_separate_bookings(api):
    room = make_room(api)
    assert book(api, room, "09:00", "10:00", headers={"Idempotency-Key": "a"})[0] == 201
    # Different key is an independent request: the slot is now taken.
    assert_error(book(api, room, "09:00", "10:00", headers={"Idempotency-Key": "b"}), 409, "conflict")


def test_request_without_key_is_not_deduplicated(api):
    room = make_room(api)
    assert book(api, room, "09:00", "10:00")[0] == 201
    assert_error(book(api, room, "09:00", "10:00"), 409, "conflict")


def test_key_is_per_booking_post_only(api):
    # A key sent on a GET is ignored; a key on POST /rooms does not interfere with bookings.
    room = make_room(api)
    h = {"Idempotency-Key": "shared"}
    assert api.call("GET", "/rooms", headers=h)[0] == 200
    assert api.call("POST", "/rooms", {"name": "Borealis", "capacity": 2}, headers=h)[0] == 201
    assert book(api, room, "09:00", "10:00", headers=h)[0] == 201


# ---------------------------------------------------------------- list bookings

@pytest.fixture
def seeded(api):
    """Room r1 and r2; b1..b3 in r1 on DAY (09-10, 10-11, 11-12); b4 in r2 at 09-10."""
    r1 = make_room(api, "Atlas", 8)
    r2 = make_room(api, "Borealis", 8)
    book(api, r1, "09:00", "10:00")
    book(api, r1, "10:00", "11:00")
    book(api, r1, "11:00", "12:00")
    book(api, r2, "09:00", "10:00")
    return api, r1, r2


def test_list_all_default_shape(seeded):
    api, r1, _ = seeded
    s, b, h = list_bookings(api)
    assert s == 200 and set(b.keys()) == {"bookings", "next_cursor"}
    assert h["content-type"].startswith(RFC_OK)
    assert b["next_cursor"] is None
    assert len(b["bookings"]) == 4
    for x in b["bookings"]:
        assert set(x.keys()) == {"id", "room_id", "start", "end", "title", "attendees"}


def test_list_order_by_start_then_creation(seeded):
    api, _, _ = seeded
    _, b, _ = list_bookings(api)
    # b1 and b4 both start 09:00; b1 was created first.
    assert booking_ids(b) == ["b1", "b4", "b2", "b3"]


def test_list_filter_room_id(seeded):
    api, r1, r2 = seeded
    _, b, _ = list_bookings(api, room_id=r1)
    assert booking_ids(b) == ["b1", "b2", "b3"]
    _, b, _ = list_bookings(api, room_id=r2)
    assert booking_ids(b) == ["b4"]


def test_list_filter_unknown_room_is_empty_not_error(seeded):
    api, _, _ = seeded
    s, b, _ = list_bookings(api, room_id="r999")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_empty_result(seeded):
    api, r1, _ = seeded
    s, b, _ = list_bookings(api, room_id=r1, **{"from": ts("13:00"), "to": ts("14:00")})
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_from_only_excludes_ending_exactly_at_from(seeded):
    api, _, _ = seeded
    _, b, _ = list_bookings(api, **{"from": ts("10:00")})
    # b1 ends exactly at 10:00 -> excluded. b4 ends 10:00 -> excluded.
    assert booking_ids(b) == ["b2", "b3"]


def test_list_to_only_excludes_starting_exactly_at_to(seeded):
    api, _, _ = seeded
    _, b, _ = list_bookings(api, to=ts("10:00"))
    assert booking_ids(b) == ["b1", "b4"]


def test_list_window_half_open(seeded):
    api, _, _ = seeded
    _, b, _ = list_bookings(api, **{"from": ts("10:00"), "to": ts("11:00")})
    assert booking_ids(b) == ["b2"]


def test_list_window_partial_overlap_included(seeded):
    api, _, _ = seeded
    _, b, _ = list_bookings(api, **{"from": ts("09:30"), "to": ts("10:30")})
    assert booking_ids(b) == ["b1", "b4", "b2"]


def test_list_window_from_equals_to_uses_overlap_rule(seeded):
    api, _, _ = seeded
    # Empty window: a booking is included if end > from and start < to, so bookings spanning 09:30 match.
    _, b, _ = list_bookings(api, **{"from": ts("09:30"), "to": ts("09:30")})
    assert booking_ids(b) == ["b1", "b4"]


def test_list_window_with_unaligned_bounds(seeded):
    api, _, _ = seeded
    s, b, _ = list_bookings(api, **{"from": "2026-03-02T10:07:13Z", "to": "2026-03-02T10:52:00Z"})
    assert s == 200 and booking_ids(b) == ["b2"]


def test_list_window_unaligned_from_inside_booking(seeded):
    api, _, _ = seeded
    _, b, _ = list_bookings(api, **{"from": "2026-03-02T11:30:00Z"})
    assert booking_ids(b) == ["b3"]


@pytest.mark.parametrize("params", [
    {"from": "2026-03-02"},
    {"from": "2026-03-02 10:00:00Z"},
    {"from": "garbage"},
    {"to": "2026-03-02T10:00:00"},
    {"to": ""},
    {"from": "2026-02-30T10:00:00Z"},
])
def test_list_invalid_window_is_422(seeded, params):
    api, _, _ = seeded
    assert_error(list_bookings(api, **params), 422, "validation_error")


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", ""])
def test_list_invalid_limit_422(seeded, limit):
    api, _, _ = seeded
    assert_error(list_bookings(api, limit=limit), 422, "validation_error")


@pytest.mark.parametrize("limit", ["1", "100"])
def test_list_limit_bounds_accepted(seeded, limit):
    api, _, _ = seeded
    s, b, _ = list_bookings(api, limit=limit)
    assert s == 200 and len(b["bookings"]) <= int(limit)


def test_list_invalid_cursor_422(seeded):
    api, _, _ = seeded
    assert_error(list_bookings(api, cursor="not-a-cursor-xyz"), 422, "validation_error")


def test_list_limit_and_next_cursor(seeded):
    api, _, _ = seeded
    s, b, _ = list_bookings(api, limit="2")
    assert booking_ids(b) == ["b1", "b4"]
    assert b["next_cursor"] is not None
    s, b2, _ = list_bookings(api, limit="2", cursor=b["next_cursor"])
    assert booking_ids(b2) == ["b2", "b3"]
    assert b2["next_cursor"] is None


def test_default_limit_is_20(api):
    room = make_room(api)
    for i in range(25):
        d = dt.date(2026, 3, 1) + dt.timedelta(days=i)
        assert book(api, room, f"{d.isoformat()}T09:00:00Z", f"{d.isoformat()}T10:00:00Z")[0] == 201
    _, b, _ = list_bookings(api)
    assert len(b["bookings"]) == 20
    assert b["next_cursor"] is not None


def test_limit_exactly_available_has_no_next_cursor(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    book(api, room, "10:00", "11:00")
    _, b, _ = list_bookings(api, limit="2")
    assert len(b["bookings"]) == 2 and b["next_cursor"] is None


def test_walk_all_pages_returns_each_exactly_once(api):
    room = make_room(api, capacity=8)
    other = make_room(api, "Other", 8)
    expected = []
    for i in range(23):
        d = dt.date(2026, 3, 1) + dt.timedelta(days=i % 7)
        r = room if i % 3 else other
        s, b, _ = book(api, r, f"{d.isoformat()}T{9 + i % 5:02d}:00:00Z",
                       f"{d.isoformat()}T{10 + i % 5:02d}:00:00Z")
        assert s == 201, (i, b)
        expected.append(b["id"])
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": "4"}
        if cursor:
            params["cursor"] = cursor
        s, b, _ = list_bookings(api, **params)
        assert s == 200
        seen += booking_ids(b)
        pages += 1
        cursor = b["next_cursor"]
        if cursor is None:
            break
        assert pages < 50
    assert sorted(seen) == sorted(expected)
    assert len(seen) == len(set(seen)) == 23
    starts = [x for x in api.call("GET", "/bookings?limit=100")[1]["bookings"]]
    assert seen == [x["id"] for x in starts]


def test_pagination_with_filters(seeded):
    api, r1, _ = seeded
    _, b, _ = list_bookings(api, room_id=r1, limit="1")
    assert booking_ids(b) == ["b1"]
    _, b, _ = list_bookings(api, room_id=r1, limit="1", cursor=b["next_cursor"])
    assert booking_ids(b) == ["b2"]
    _, b, _ = list_bookings(api, room_id=r1, limit="1", cursor=b["next_cursor"])
    assert booking_ids(b) == ["b3"]
    assert b["next_cursor"] is None


def test_pagination_ties_on_start_are_not_skipped(api):
    rooms = [make_room(api, f"R{i}", 4) for i in range(5)]
    for r in rooms:
        assert book(api, r, "09:00", "10:00")[0] == 201
    seen, cursor = [], None
    while True:
        params = {"limit": "2"}
        if cursor:
            params["cursor"] = cursor
        _, b, _ = list_bookings(api, **params)
        seen += booking_ids(b)
        cursor = b["next_cursor"]
        if not cursor:
            break
    assert seen == ["b1", "b2", "b3", "b4", "b5"]


def test_listing_after_delete_excludes_deleted(seeded):
    api, r1, _ = seeded
    api.call("DELETE", "/bookings/b2")
    _, b, _ = list_bookings(api, room_id=r1)
    assert booking_ids(b) == ["b1", "b3"]


# ---------------------------------------------------------------- availability

def test_availability_empty_day_is_whole_working_day(api):
    room = make_room(api)
    s, b, h = availability(api, room)
    assert s == 200 and h["content-type"].startswith(RFC_OK)
    assert b == {"date": DAY, "free": [free_window("08:00", "18:00")]}


def test_availability_gaps_between_bookings(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    book(api, room, "12:00", "13:30")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("08:00", "09:00"),
                         free_window("10:00", "12:00"),
                         free_window("13:30", "18:00")]


def test_availability_adjacent_bookings_leave_no_gap(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    book(api, room, "10:00", "11:00")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("08:00", "09:00"), free_window("11:00", "18:00")]


def test_availability_fully_booked_is_empty(api):
    room = make_room(api)
    book(api, room, "08:00", "13:00")
    book(api, room, "13:00", "18:00")
    _, b, _ = availability(api, room)
    assert b == {"date": DAY, "free": []}


def test_availability_single_booking_covering_day(api):
    room = make_room(api)
    book(api, room, "08:00", "16:00")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("16:00", "18:00")]


def test_availability_clips_booking_starting_before_working_day(api):
    room = make_room(api)
    book(api, room, "07:00", "09:00")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("09:00", "18:00")]


def test_availability_clips_booking_ending_after_working_day(api):
    room = make_room(api)
    book(api, room, "17:00", "19:00")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("08:00", "17:00")]


def test_availability_ignores_booking_entirely_outside_day(api):
    room = make_room(api)
    book(api, room, "06:00", "07:00")
    book(api, room, "18:00", "19:00")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("08:00", "18:00")]


def test_availability_bookings_spanning_whole_day_and_beyond(api):
    room = make_room(api)
    book(api, room, "07:00", "15:00")
    book(api, room, "15:00", "19:00")
    _, b, _ = availability(api, room)
    assert b["free"] == []


def test_availability_ignores_other_rooms(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    book(api, r2, "09:00", "10:00")
    _, b, _ = availability(api, r1)
    assert b["free"] == [free_window("08:00", "18:00")]


def test_availability_ignores_other_days(api):
    room = make_room(api)
    book(api, room, "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z")
    book(api, room, "2026-03-03T09:00:00Z", "2026-03-03T10:00:00Z")
    _, b, _ = availability(api, room, DAY)
    assert b["free"] == [free_window("08:00", "18:00")]


def test_availability_free_sorted_ascending(api):
    room = make_room(api)
    book(api, room, "15:00", "16:00")
    book(api, room, "09:00", "10:00")
    book(api, room, "12:00", "12:30")
    _, b, _ = availability(api, room)
    starts = [f["start"] for f in b["free"]]
    assert starts == sorted(starts)
    assert len(b["free"]) == 4


def test_availability_reflects_delete(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("08:00", "18:00")]


def test_availability_quarter_hour_gap(api):
    room = make_room(api)
    book(api, room, "08:00", "09:45")
    book(api, room, "10:00", "18:00")
    _, b, _ = availability(api, room)
    assert b["free"] == [free_window("09:45", "10:00")]


def test_availability_unknown_room_404(api):
    assert_error(api.call("GET", f"/rooms/r99/availability?date={DAY}"), 404, "not_found")


def test_availability_room_check_before_date_check(api):
    assert_error(api.call("GET", "/rooms/r99/availability?date=nope"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r99/availability"), 404, "not_found")


@pytest.mark.parametrize("query", [
    "",
    "?date=",
    "?date=2026-02-30",
    "?date=2026-13-01",
    "?date=2026-00-10",
    "?date=2026-03-32",
    "?date=2026/03/02",
    "?date=2026-3-2",
    "?date=02-03-2026",
    "?date=2026-03-02T09:00:00Z",
    "?date=tomorrow",
    "?day=2026-03-02",
])
def test_availability_invalid_date_422(api, query):
    room = make_room(api)
    assert_error(api.call("GET", f"/rooms/{room}/availability{query}"), 422, "validation_error")


def test_availability_leap_day(api):
    room = make_room(api)
    assert availability(api, room, "2028-02-29")[0] == 200
    assert_error(availability(api, room, "2026-02-29"), 422, "validation_error")


def test_availability_date_echoed(api):
    room = make_room(api)
    _, b, _ = availability(api, room, "2027-12-31")
    assert b["date"] == "2027-12-31"
    assert b["free"][0]["start"] == "2027-12-31T08:00:00Z"


def test_availability_timestamps_are_canonical_utc(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00")
    _, b, _ = availability(api, room)
    for f in b["free"]:
        for key in ("start", "end"):
            dt.datetime.strptime(f[key], "%Y-%m-%dT%H:%M:%SZ")
    assert set(b["free"][0].keys()) == {"start", "end"}


# ---------------------------------------------------------------- rooms: listing & isolation

def test_list_rooms_creation_order_and_shape(api):
    make_room(api, "Zeta", 3)
    make_room(api, "Alpha", 9)
    _, b, _ = api.call("GET", "/rooms")
    assert b["rooms"] == [{"id": "r1", "name": "Zeta", "capacity": 3},
                          {"id": "r2", "name": "Alpha", "capacity": 9}]


def test_bookings_reference_room_ids(api):
    room = make_room(api)
    _, b, _ = book(api, room, "09:00", "10:00")
    assert b["room_id"] == room == api.call("GET", "/rooms")[1]["rooms"][0]["id"]


def test_get_booking_shape(api):
    room = make_room(api)
    book(api, room, "09:00", "10:00", title="T", attendees=1)
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 200
    assert b == {"id": "b1", "room_id": room, "start": ts("09:00"), "end": ts("10:00"),
                 "title": "T", "attendees": 1}


def test_fresh_server_has_no_state(api):
    # Each fixture starts a new process; a fresh server must not know about prior rooms.
    assert api.call("GET", "/rooms")[1] == {"rooms": []}
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
