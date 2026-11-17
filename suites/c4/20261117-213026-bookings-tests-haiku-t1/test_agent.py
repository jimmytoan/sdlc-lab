"""Regression suite for the bookings API, driven as a black box over HTTP.

Every expectation here is taken from SPEC.md. Each test starts a fresh server
(via the `api` fixture from helpers.py), so tests do not share state.
"""
import json
from urllib.parse import urlencode

import pytest

from helpers import api, code, ts  # noqa: F401  (api is a pytest fixture)

DAY = "2026-03-02"
OTHER_DAY = "2026-03-03"


# ---------------------------------------------------------------- helpers

def T(hhmm, day=DAY):
    """Full UTC timestamp for a wall-clock time on a day."""
    return f"{day}T{hhmm}:00Z"


def assert_error(resp, status, err_code):
    s, b, h = resp
    assert s == status, (s, b)
    assert isinstance(b, dict) and set(b) == {"error"}, b
    assert set(b["error"]) >= {"code", "message"}, b
    assert isinstance(b["error"]["message"], str)
    assert b["error"]["code"] == err_code, b
    assert h["content-type"].startswith("application/json")


def walk(api, path="/bookings", **params):
    """Follow next_cursor until exhausted. Returns (all bookings, number of pages)."""
    out, pages, cursor = [], 0, None
    while True:
        q = dict(params)
        if cursor is not None:
            q["cursor"] = cursor
        s, b, _ = api.call("GET", f"{path}?{urlencode(q)}")
        assert s == 200, b
        out.extend(b["bookings"])
        pages += 1
        cursor = b["next_cursor"]
        if cursor is None:
            return out, pages
        assert pages < 500, "pagination does not terminate"


def free_of(api, room_id, date=DAY):
    s, b, _ = api.call("GET", f"/rooms/{room_id}/availability?date={date}")
    assert s == 200, b
    return [(f["start"], f["end"]) for f in b["free"]]


def ids_of(api, query=""):
    s, b, _ = api.call("GET", "/bookings" + (f"?{query}" if query else ""))
    assert s == 200, b
    return [x["id"] for x in b["bookings"]]


ROOM_BODY_KEYS = {"id", "name", "capacity"}
BOOKING_KEYS = {"id", "room_id", "start", "end", "title", "attendees"}


# ---------------------------------------------------------------- general

def test_state_starts_empty(api):
    assert api.call("GET", "/rooms")[1] == {"rooms": []}
    assert api.call("GET", "/bookings")[1] == {"bookings": [], "next_cursor": None}


@pytest.mark.parametrize("path", ["/", "/nope", "/rooms/r1/nope", "/bookings/b1/extra", "/availability", "/rooms/r1/availability/x"])
def test_unknown_path_is_404(api, path):
    assert_error(api.call("GET", path), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/rooms"),
    ("PUT", "/rooms"),
    ("PATCH", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("POST", "/rooms/r1/availability"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
])
def test_unsupported_method_is_405(api, method, path):
    api.room()  # r1 exists, so the path itself is real
    assert_error(api.call(method, path), 405, "method_not_allowed")


@pytest.mark.parametrize("method,path", [("POST", "/rooms"), ("POST", "/bookings")])
@pytest.mark.parametrize("raw", [
    b"{", b"not json", b"", b"[1, 2]", b'"text"', b"42", b"null", b"true", b"\xff\xfe{}",
])
def test_bad_json_is_400(api, method, path, raw):
    assert_error(api.call(method, path, raw=raw), 400, "bad_json")


def test_unknown_fields_are_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8, "colour": "red"})
    assert s == 201 and set(b) == ROOM_BODY_KEYS
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": b["id"], "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": 1, "extra": [1],
    })
    assert s == 201 and set(b) == BOOKING_KEYS


@pytest.mark.parametrize("field,value", [("capacity", True), ("capacity", False)])
def test_boolean_is_never_an_integer_for_rooms(api, field, value):
    body = {"name": "Atlas", "capacity": 8}
    body[field] = value
    assert_error(api.call("POST", "/rooms", body), 422, "validation_error")


@pytest.mark.parametrize("value", [True, False])
def test_boolean_is_never_an_integer_for_attendees(api, value):
    r = api.room()
    assert_error(api.call("POST", "/bookings", {
        "room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": value,
    }), 422, "validation_error")


def test_success_responses_have_json_content_type(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert h["content-type"].startswith("application/json")
    s, b, h = api.call("GET", "/rooms")
    assert h["content-type"].startswith("application/json")


def test_delete_returns_empty_204(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None


# ---------------------------------------------------------------- rooms

def test_create_room_response(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}


def test_room_ids_are_sequential(api):
    assert [api.room(f"Room {i}") for i in range(3)] == ["r1", "r2", "r3"]


def test_get_room(api):
    rid = api.room("Atlas", 8)
    s, b, _ = api.call("GET", f"/rooms/{rid}")
    assert s == 200 and b == {"id": rid, "name": "Atlas", "capacity": 8}


def test_get_unknown_room_is_404(api):
    api.room()
    assert_error(api.call("GET", "/rooms/r99"), 404, "not_found")


def test_list_rooms_in_creation_order(api):
    api.room("Zeta", 2)
    api.room("Alpha", 3)
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200
    assert b == {"rooms": [{"id": "r1", "name": "Zeta", "capacity": 2},
                           {"id": "r2", "name": "Alpha", "capacity": 3}]}


def test_room_name_is_trimmed(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "   Atlas  ", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    assert api.call("GET", "/rooms/r1")[1]["name"] == "Atlas"


@pytest.mark.parametrize("name,ok", [
    ("A", True),
    ("x" * 50, True),
    ("  " + "x" * 50 + "  ", True),  # 50 characters after trimming
    ("x" * 51, False),
    ("", False),
    ("   ", False),
    ("\t\n", False),
    (None, False),
    (12, False),
    (["Atlas"], False),
])
def test_room_name_rules(api, name, ok):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 8})
    if ok:
        assert s == 201, b
    else:
        assert_error((s, b, _), 422, "validation_error")


def test_room_name_missing_is_422(api):
    assert_error(api.call("POST", "/rooms", {"capacity": 8}), 422, "validation_error")


@pytest.mark.parametrize("capacity,ok", [
    (1, True), (500, True), (8, True),
    (0, False), (-1, False), (501, False), (10**6, False),
    ("8", False), (8.0, False), (8.5, False), (None, False),
])
def test_room_capacity_rules(api, capacity, ok):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": capacity})
    if ok:
        assert s == 201, b
        assert b["capacity"] == capacity
    else:
        assert_error((s, b, _), 422, "validation_error")


def test_room_capacity_missing_is_422(api):
    assert_error(api.call("POST", "/rooms", {"name": "Atlas"}), 422, "validation_error")


@pytest.mark.parametrize("other", ["atlas", "ATLAS", "  Atlas ", "aTlAs"])
def test_duplicate_room_name_ignores_case_and_trimming(api, other):
    api.room("Atlas")
    assert_error(api.call("POST", "/rooms", {"name": other, "capacity": 4}), 409, "room_exists")


def test_similar_room_names_are_not_duplicates(api):
    api.room("Atlas")
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas 2", "capacity": 4})
    assert s == 201


def test_failed_room_creation_does_not_consume_an_id(api):
    api.room("Atlas")
    assert api.call("POST", "/rooms", {"name": "atlas", "capacity": 4})[0] == 409
    assert api.call("POST", "/rooms", {"name": "", "capacity": 4})[0] == 422
    s, b, _ = api.call("POST", "/rooms", {"name": "Borealis", "capacity": 4})
    assert s == 201 and b["id"] == "r2"


# ---------------------------------------------------------------- booking creation

def test_create_booking_response_exact(api):
    r = api.room(capacity=8)
    s, b, h = api.book(r, "09:00", "10:00", title="Planning", attendees=4)
    assert s == 201
    assert h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": r, "start": T("09:00"), "end": T("10:00"),
                 "title": "Planning", "attendees": 4}


def test_get_booking_matches_created(api):
    r = api.room()
    _, created, _ = api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 200 and b == created


def test_booking_ids_are_sequential(api):
    r = api.room()
    ids = [api.book(r, f"{h:02d}:00", f"{h:02d}:30")[1]["id"] for h in range(9, 13)]
    assert ids == ["b1", "b2", "b3", "b4"]


def test_booking_title_is_trimmed(api):
    r = api.room()
    s, b, _ = api.book(r, "09:00", "10:00", title="  Planning  ")
    assert s == 201 and b["title"] == "Planning"


@pytest.mark.parametrize("title,ok", [
    ("x", True),
    ("x" * 100, True),
    ("  " + "x" * 100 + "  ", True),
    ("x" * 101, False),
    ("", False),
    ("   ", False),
    (None, False),
    (42, False),
])
def test_booking_title_rules(api, title, ok):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": T("09:00"), "end": T("10:00"), "title": title, "attendees": 2})
    if ok:
        assert s == 201, b
    else:
        assert_error((s, b, _), 422, "validation_error")


@pytest.mark.parametrize("attendees,ok", [
    (1, True), (8, True), (0, False), (-3, False), ("4", False), (4.0, False), (None, False),
])
def test_booking_attendee_type_and_minimum(api, attendees, ok):
    r = api.room(capacity=8)
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": attendees})
    if ok:
        assert s == 201, b
    else:
        assert_error((s, b, _), 422, "validation_error")


@pytest.mark.parametrize("missing", ["room_id", "start", "end", "title", "attendees"])
def test_missing_booking_field_is_422(api, missing):
    r = api.room()
    body = {"room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": 2}
    del body[missing]
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("field,value", [
    ("room_id", 1), ("room_id", None), ("start", 900), ("start", None),
    ("end", ["2026-03-02T10:00:00Z"]), ("title", 7),
])
def test_wrong_type_booking_field_is_422(api, field, value):
    r = api.room()
    body = {"room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": 2}
    body[field] = value
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("bad", [
    "2026-03-02T09:00:00",           # no zone designator
    "2026-03-02 09:00:00Z",          # space instead of T
    "2026-03-02T09:00Z",             # no seconds
    "2026-03-02T09:00:00.000Z",      # fractional seconds
    "2026-03-02T09:00:00+00:00",     # offset form
    "2026-03-02T09:00:00z",          # lower-case designator
    "2026-02-30T09:00:00Z",          # not a real date
    "2026-03-02T24:00:00Z",          # hour out of range
    "2026-13-01T09:00:00Z",          # month out of range
    "09:00",                         # time only
    "",
    "not a time",
    "2026-03-02T09:10:00Z",          # minutes not a quarter hour
    "2026-03-02T09:05:00Z",
    "2026-03-02T09:00:30Z",          # non-zero seconds
])
@pytest.mark.parametrize("field", ["start", "end"])
def test_invalid_timestamp_forms_are_422(api, field, bad):
    r = api.room()
    body = {"room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": 2}
    body[field] = bad
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("minute", ["00", "15", "30", "45"])
def test_quarter_hour_alignment_accepted(api, minute):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": T(f"09:{minute}"), "end": T(f"10:{minute}"), "title": "x", "attendees": 1})
    assert s == 201, b
    assert b["start"] == T(f"09:{minute}")


@pytest.mark.parametrize("start,end", [
    ("10:00", "10:00"),   # equal
    ("10:00", "09:00"),   # reversed
    ("10:00", "09:45"),
])
def test_end_must_be_after_start(api, start, end):
    r = api.room()
    assert_error(api.book(r, start, end), 422, "validation_error")


def test_exactly_eight_hours_is_allowed(api):
    r = api.room()
    s, b, _ = api.book(r, "09:00", "17:00")
    assert s == 201, b


def test_longer_than_eight_hours_is_rejected(api):
    r = api.room()
    assert_error(api.book(r, "09:00", "17:15"), 422, "validation_error")


def test_booking_may_cross_midnight(api):
    r = api.room()
    s, b, _ = api.book(r, "22:00", T("01:00", day=OTHER_DAY))
    assert s == 201 and b["end"] == T("01:00", day=OTHER_DAY)


def test_over_capacity(api):
    r = api.room(capacity=4)
    assert_error(api.book(r, "09:00", "10:00", attendees=5), 422, "over_capacity")


def test_attendees_equal_to_capacity_is_allowed(api):
    r = api.room(capacity=4)
    assert api.book(r, "09:00", "10:00", attendees=4)[0] == 201


def test_unknown_room_is_404(api):
    api.room()
    assert_error(api.book("r99", "09:00", "10:00"), 404, "not_found")


def test_missing_room_id_is_validation_error_not_404(api):
    body = {"start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": 1}
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


def test_validation_is_checked_before_room_existence(api):
    assert_error(api.book("r99", "09:10", "10:00"), 422, "validation_error")
    assert_error(api.book("r99", "09:00", "10:00", attendees=0), 422, "validation_error")


def test_room_existence_is_checked_before_capacity(api):
    assert_error(api.book("r99", "09:00", "10:00", attendees=1000), 404, "not_found")


def test_capacity_is_checked_before_conflict(api):
    r = api.room(capacity=2)
    assert api.book(r, "09:00", "10:00", attendees=1)[0] == 201
    assert_error(api.book(r, "09:30", "10:30", attendees=3), 422, "over_capacity")


def test_failed_booking_does_not_consume_an_id(api):
    r = api.room(capacity=2)
    api.book(r, "09:00", "10:00")
    assert api.book(r, "09:30", "10:30")[0] == 409
    assert api.book(r, "11:00", "12:00", attendees=9)[0] == 422
    s, b, _ = api.book(r, "11:00", "12:00")
    assert s == 201 and b["id"] == "b2"


# ---------------------------------------------------------------- conflicts

@pytest.mark.parametrize("start,end", [
    ("09:00", "10:00"),   # identical
    ("08:00", "09:30"),   # overlaps start
    ("09:30", "10:30"),   # overlaps end
    ("08:00", "11:00"),   # contains existing
    ("09:15", "09:45"),   # contained by existing
])
def test_overlapping_booking_is_409(api, start, end):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert_error(api.book(r, start, end), 409, "conflict")


def test_back_to_back_is_allowed_on_both_sides(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert api.book(r, "10:00", "11:00")[0] == 201
    assert api.book(r, "08:00", "09:00")[0] == 201


def test_fits_between_two_bookings(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")
    assert api.book(r, "10:00", "11:00")[0] == 201
    assert_error(api.book(r, "09:30", "11:30"), 409, "conflict")


def test_same_slot_in_different_rooms_is_allowed(api):
    a = api.room("Atlas")
    b = api.room("Borealis")
    assert api.book(a, "09:00", "10:00")[0] == 201
    assert api.book(b, "09:00", "10:00")[0] == 201


def test_conflict_applies_per_day(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": T("09:00", OTHER_DAY), "end": T("10:00", OTHER_DAY), "title": "x", "attendees": 1})
    assert s == 201, b


def test_conflict_only_with_live_bookings(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert api.book(r, "09:30", "10:30")[0] == 201


# ---------------------------------------------------------------- get / delete

def test_get_unknown_booking_is_404(api):
    api.room()
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_frees_slot_and_hides_booking(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert ids_of(api) == []
    assert api.book(r, "09:00", "10:00")[0] == 201


def test_delete_unknown_booking_is_404(api):
    api.room()
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    assert_error(api.call("DELETE", "/bookings/b77"), 404, "not_found")


def test_delete_twice_is_404_second_time(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_ids_are_never_reused_after_delete(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:00", "10:00")
    assert s == 201 and b["id"] == "b2"
    assert api.call("GET", "/bookings/b1")[0] == 404


def test_other_bookings_survive_a_delete(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.call("GET", "/bookings/b2")
    assert s == 200 and b["start"] == T("11:00")


# ---------------------------------------------------------------- listing

def test_list_empty(api):
    api.room()
    assert api.call("GET", "/bookings")[1] == {"bookings": [], "next_cursor": None}


def test_list_items_have_booking_shape(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    _, b, _ = api.call("GET", "/bookings")
    assert set(b["bookings"][0]) == BOOKING_KEYS


def test_list_sorted_by_start_then_creation(api):
    a = api.room("Atlas")
    b_room = api.room("Borealis")
    c = api.room("Cygnus")
    api.book(a, "10:00", "11:00")       # b1, start 10:00
    api.book(c, "09:00", "10:00")       # b2, start 09:00
    api.book(b_room, "09:00", "10:00")  # b3, start 09:00 (same start, created later)
    api.book(a, "08:00", "08:30")       # b4
    assert ids_of(api) == ["b4", "b2", "b3", "b1"]


def test_list_filters_by_room(api):
    a = api.room("Atlas")
    b_room = api.room("Borealis")
    api.book(a, "09:00", "10:00")
    api.book(b_room, "09:00", "10:00")
    api.book(a, "11:00", "12:00")
    assert ids_of(api, f"room_id={a}") == ["b1", "b3"]
    assert ids_of(api, f"room_id={b_room}") == ["b2"]


def test_list_unknown_room_is_empty_not_error(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings?room_id=r99")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


@pytest.mark.parametrize("frm,to,included", [
    (T("10:00"), None, False),          # booking ends exactly at from: not after from
    (T("09:59"), None, True),
    ("2026-03-02T10:01:00Z", None, False),
    (None, T("09:00"), False),          # booking starts exactly at to: not before to
    (None, T("09:01"), True),
    (T("08:30"), T("09:30"), True),
    ("2026-03-02T09:10:00Z", "2026-03-02T09:20:00Z", True),   # inside booking, unaligned window
    ("2026-03-02T10:10:00Z", "2026-03-02T10:20:00Z", False),  # after booking, unaligned window
    (T("12:00"), T("13:00"), False),
])
def test_list_window_overlap_semantics(api, frm, to, included):
    r = api.room()
    api.book(r, "09:00", "10:00")
    q = {}
    if frm:
        q["from"] = frm
    if to:
        q["to"] = to
    assert ids_of(api, urlencode(q)) == (["b1"] if included else [])


def test_list_window_is_half_open_on_both_ends(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    # [10:00, 10:00) would be empty; [09:00,10:00) covers only the first booking
    assert ids_of(api, urlencode({"from": T("09:00"), "to": T("10:00")})) == ["b1"]
    assert ids_of(api, urlencode({"from": T("10:00"), "to": T("11:00")})) == ["b2"]


def test_list_from_or_to_alone(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")
    assert ids_of(api, urlencode({"from": T("10:30")})) == ["b2"]
    assert ids_of(api, urlencode({"to": T("10:30")})) == ["b1"]


def test_list_room_and_window_combined(api):
    a = api.room("Atlas")
    b_room = api.room("Borealis")
    api.book(a, "09:00", "10:00")
    api.book(b_room, "09:00", "10:00")
    api.book(a, "13:00", "14:00")
    q = urlencode({"room_id": a, "from": T("08:00"), "to": T("12:00")})
    assert ids_of(api, q) == ["b1"]


@pytest.mark.parametrize("query", [
    "limit=0", "limit=101", "limit=-1", "limit=abc", "limit=", "limit=1.5",
    "from=yesterday", "from=2026-03-02", "from=2026-03-02T09:00:00%2B00:00",
    "to=2026-03-02T25:00:00Z", "to=20260302",
    "cursor=garbage", "cursor=c2026-03-02T09:00:00Z", "cursor=c2026-02-30T09:00:00Z_1",
])
def test_list_invalid_parameters_are_422(api, query):
    api.room()
    assert_error(api.call("GET", f"/bookings?{query}"), 422, "validation_error")


@pytest.mark.parametrize("limit", [1, 100])
def test_list_limit_bounds_accepted(api, limit):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/bookings?limit={limit}")
    assert s == 200 and len(b["bookings"]) == 1


def test_default_page_size_is_20(api):
    r = api.room()
    for i in range(21):  # distinct days, so no conflicts
        day = f"2026-04-{i + 1:02d}"
        assert api.call("POST", "/bookings", {
            "room_id": r, "start": T("09:00", day), "end": T("10:00", day),
            "title": "x", "attendees": 1})[0] == 201
    s, b, _ = api.call("GET", f"/bookings?room_id={r}")
    assert s == 200 and len(b["bookings"]) == 20 and b["next_cursor"] is not None
    s, b2, _ = api.call("GET", f"/bookings?room_id={r}&cursor={b['next_cursor']}")
    assert s == 200 and len(b2["bookings"]) == 1 and b2["next_cursor"] is None


def test_walk_pages_returns_every_booking_once(api):
    r = api.room()
    created = []
    for i in range(7):
        created.append(api.book(r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")[1]["id"])
    items, pages = walk(api, limit=3)
    assert pages == 3
    assert [x["id"] for x in items] == created


def test_walk_pages_when_size_is_exact_multiple(api):
    r = api.room()
    for i in range(6):
        api.book(r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    items, pages = walk(api, limit=3)
    assert pages == 2 and len(items) == 6


def test_last_page_has_null_cursor(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    assert b["next_cursor"] is None and len(b["bookings"]) == 2


def test_pagination_with_ties_on_start(api):
    rooms = [api.room(f"Room {i}") for i in range(4)]
    created = [api.book(rid, "09:00", "10:00")[1]["id"] for rid in rooms]
    items, pages = walk(api, limit=1)
    assert [x["id"] for x in items] == created and pages == 4


def test_pagination_respects_filters(api):
    a = api.room("Atlas")
    b_room = api.room("Borealis")
    for i in range(5):
        api.book(a, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
        api.book(b_room, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    items, _ = walk(api, room_id=a, limit=2)
    assert len(items) == 5 and all(x["room_id"] == a for x in items)


def test_pagination_respects_window(api):
    r = api.room()
    for i in range(6):
        api.book(r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    items, _ = walk(api, **{"from": T("10:00"), "to": T("13:00")}, limit=1)
    assert [x["start"] for x in items] == [T("10:00"), T("11:00"), T("12:00")]


def test_same_cursor_returns_same_page(api):
    r = api.room()
    for i in range(4):
        api.book(r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    _, first, _ = api.call("GET", "/bookings?limit=2")
    c = first["next_cursor"]
    a = api.call("GET", f"/bookings?limit=2&cursor={c}")[1]
    b = api.call("GET", f"/bookings?limit=2&cursor={c}")[1]
    assert a == b and len(a["bookings"]) == 2


# ---------------------------------------------------------------- availability

def test_availability_empty_day_is_whole_working_day(api):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date={DAY}")
    assert s == 200
    assert b == {"date": DAY, "free": [{"start": T("08:00"), "end": T("18:00")}]}


def test_availability_gaps_around_booking(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert free_of(api, r) == [(T("08:00"), T("09:00")), (T("10:00"), T("18:00"))]


def test_availability_free_item_shape(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    _, b, _ = api.call("GET", f"/rooms/{r}/availability?date={DAY}")
    assert set(b) == {"date", "free"}
    assert all(set(f) == {"start", "end"} for f in b["free"])


def test_availability_multiple_gaps_ascending(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "12:00", "13:00")
    api.book(r, "15:00", "16:00")
    assert free_of(api, r) == [
        (T("08:00"), T("09:00")), (T("10:00"), T("12:00")),
        (T("13:00"), T("15:00")), (T("16:00"), T("18:00")),
    ]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    assert free_of(api, r) == [(T("08:00"), T("09:00")), (T("11:00"), T("18:00"))]


def test_availability_fully_booked_is_empty(api):
    r = api.room()
    api.book(r, "08:00", "13:00")
    api.book(r, "13:00", "18:00")
    assert free_of(api, r) == []


def test_availability_clips_bookings_that_start_before_working_day(api):
    r = api.room()
    api.book(r, "07:00", "09:00")
    assert free_of(api, r) == [(T("09:00"), T("18:00"))]


def test_availability_clips_bookings_that_end_after_working_day(api):
    r = api.room()
    api.book(r, "17:00", "19:00")
    assert free_of(api, r) == [(T("08:00"), T("17:00"))]


def test_availability_ignores_bookings_outside_working_day(api):
    r = api.room()
    api.book(r, "06:00", "07:00")
    api.book(r, "18:00", "19:00")
    assert free_of(api, r) == [(T("08:00"), T("18:00"))]


def test_availability_booking_spanning_whole_day_edges(api):
    r = api.room()
    api.book(r, "07:00", "08:00")  # ends exactly at open
    assert free_of(api, r) == [(T("08:00"), T("18:00"))]


def test_availability_booking_from_previous_day_clipped(api):
    r = api.room()
    # 23:00 on the 1st to 07:00 on the 2nd: 8 hours, entirely before the working day of the 2nd
    s, _, _ = api.call("POST", "/bookings", {"room_id": r, "start": T("23:00", "2026-03-01"),
                                            "end": T("07:00", DAY), "title": "late", "attendees": 1})
    assert s == 201
    assert free_of(api, r, DAY) == [(T("08:00"), T("18:00"))]


def test_availability_booking_running_into_next_day_clipped(api):
    r = api.room()
    api.book(r, "17:00", T("01:00", day=OTHER_DAY))
    assert free_of(api, r, DAY) == [(T("08:00"), T("17:00"))]


def test_availability_ignores_other_rooms(api):
    a = api.room("Atlas")
    b_room = api.room("Borealis")
    api.book(b_room, "09:00", "10:00")
    assert free_of(api, a) == [(T("08:00"), T("18:00"))]


def test_availability_ignores_other_days(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("POST", "/bookings", {"room_id": r, "start": T("09:00", OTHER_DAY),
                                  "end": T("10:00", OTHER_DAY), "title": "x", "attendees": 1})
    assert free_of(api, r, DAY) == [(T("08:00"), T("09:00")), (T("10:00"), T("18:00"))]


def test_availability_reflects_deletion(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert free_of(api, r) == [(T("08:00"), T("18:00"))]


def test_availability_echoes_date(api):
    r = api.room()
    _, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-07-15")
    assert b["date"] == "2026-07-15"


def test_availability_leap_day(api):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2028-02-29")
    assert s == 200 and b["date"] == "2028-02-29"


@pytest.mark.parametrize("query", [
    "", "date=", "date", "date=2026-02-30", "date=2026-02-29", "date=2026-13-01",
    "date=2026-3-2", "date=2026/03/02", "date=not-a-date", "date=2026-03-02T00:00:00Z",
    "date=03-02-2026", "date=20260302",
])
def test_availability_invalid_date_is_422(api, query):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability?{query}"), 422, "validation_error")


def test_availability_missing_date_is_422(api):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability"), 422, "validation_error")


def test_availability_unknown_room_is_404(api):
    api.room()
    assert_error(api.call("GET", "/rooms/r99/availability?date=2026-03-02"), 404, "not_found")


def test_availability_room_check_precedes_date_check(api):
    assert_error(api.call("GET", "/rooms/r99/availability?date=bogus"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r99/availability"), 404, "not_found")


# ---------------------------------------------------------------- idempotency

def test_replay_returns_original_response_without_creating(api):
    r = api.room()
    s1, b1, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k1"})
    assert s1 == 201
    s2, b2, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k1"})
    assert s2 == 201 and b2 == b1
    assert ids_of(api) == ["b1"]
    # the next genuinely new booking still gets b2
    assert api.book(r, "11:00", "12:00")[1]["id"] == "b2"


def test_replay_ignores_key_order_and_whitespace(api):
    r = api.room()
    first = {"room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "x", "attendees": 2}
    s1, b1, _ = api.call("POST", "/bookings", first, headers={"Idempotency-Key": "k2"})
    assert s1 == 201
    reordered = ('{ "attendees" : 2,\n "title":"x", "end": "%s", "start":"%s",   "room_id": "%s" }'
                 % (T("10:00"), T("09:00"), r))
    s2, b2, _ = api.call("POST", "/bookings", raw=reordered.encode(),
                         headers={"Idempotency-Key": "k2"})
    assert s2 == 201 and b2 == b1
    assert ids_of(api) == ["b1"]


def test_replay_with_different_body_is_mismatch(api):
    r = api.room()
    api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k3"})
    assert_error(api.book(r, "09:00", "10:00", title="other", headers={"Idempotency-Key": "k3"}),
                 422, "idempotency_mismatch")
    assert_error(api.book(r, "11:00", "12:00", headers={"Idempotency-Key": "k3"}),
                 422, "idempotency_mismatch")
    assert ids_of(api) == ["b1"]


def test_replay_with_extra_field_is_a_different_body(api):
    r = api.room()
    api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k4"})
    body = {"room_id": r, "start": T("09:00"), "end": T("10:00"), "title": "sync",
            "attendees": 2, "note": "extra"}
    assert_error(api.call("POST", "/bookings", body, headers={"Idempotency-Key": "k4"}),
                 422, "idempotency_mismatch")


def test_replay_with_different_room_is_mismatch(api):
    a = api.room("Atlas")
    b_room = api.room("Borealis")
    api.book(a, "09:00", "10:00", headers={"Idempotency-Key": "k5"})
    assert_error(api.book(b_room, "09:00", "10:00", headers={"Idempotency-Key": "k5"}),
                 422, "idempotency_mismatch")


def test_failed_request_is_not_remembered(api):
    r = api.room()
    api.book(r, "09:00", "10:00")  # b1 occupies the slot
    assert_error(api.book(r, "09:30", "10:30", headers={"Idempotency-Key": "k6"}), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:30", "10:30", headers={"Idempotency-Key": "k6"})
    assert s == 201 and b["start"] == T("09:30")


def test_failed_validation_is_not_remembered(api):
    r = api.room(capacity=2)
    assert_error(api.book(r, "09:00", "10:00", attendees=5, headers={"Idempotency-Key": "k7"}),
                 422, "over_capacity")
    assert_error(api.book(r, "09:00", "09:10", headers={"Idempotency-Key": "k7"}),
                 422, "validation_error")
    s, b, _ = api.book(r, "09:00", "10:00", attendees=2, headers={"Idempotency-Key": "k7"})
    assert s == 201 and b["attendees"] == 2


def test_failed_room_lookup_is_not_remembered(api):
    assert_error(api.book("r1", "09:00", "10:00", headers={"Idempotency-Key": "k8"}), 404, "not_found")
    r = api.room()
    s, b, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k8"})
    assert s == 201


def test_replay_after_delete_creates_nothing(api):
    r = api.room()
    _, b1, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k9"})
    api.call("DELETE", "/bookings/b1")
    s, b2, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k9"})
    assert s == 201 and b2 == b1
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert ids_of(api) == []


def test_different_keys_with_same_body_are_separate_requests(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "a"})[0] == 201
    assert_error(api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "b"}), 409, "conflict")


def test_keyless_requests_are_not_deduplicated(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert_error(api.book(r, "09:00", "10:00"), 409, "conflict")


def test_replay_with_invalid_different_body_is_still_mismatch(api):
    r = api.room(capacity=4)
    api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k10"})
    # the new body is itself invalid (zero attendees, bad alignment), but the key matches a stored
    # request with a different body, so the answer is a mismatch
    assert_error(api.book(r, "09:10", "10:00", attendees=0, headers={"Idempotency-Key": "k10"}),
                 422, "idempotency_mismatch")
    assert ids_of(api) == ["b1"]
