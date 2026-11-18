"""Black-box regression suite for the bookings API, written from SPEC.md.

Every test talks to a freshly started server.py over HTTP (via the `api` fixture
in helpers.py). Nothing here inspects the server's source or internals.
"""
import json
import re

import pytest

from helpers import api, code, ts  # noqa: F401  (api is a pytest fixture)


# ---------------------------------------------------------------- helpers

def make_room(api, name="Atlas", capacity=8):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": capacity})
    assert s == 201, (s, b)
    return b["id"]


def book_ok(api, room, start, end, title="sync", attendees=2, headers=None):
    s, b, _ = api.book(room, start, end, title=title, attendees=attendees, headers=headers)
    assert s == 201, (s, b)
    return b


def booking_body(room_id, start="09:00", end="10:00", title="sync", attendees=2):
    return {"room_id": room_id, "start": ts(start), "end": ts(end),
            "title": title, "attendees": attendees}


def assert_error(resp, status, err_code):
    s, b, h = resp
    assert s == status, (s, b)
    assert isinstance(b, dict) and set(b) == {"error"}, b
    assert isinstance(b["error"], dict)
    assert b["error"].get("code") == err_code, b
    assert isinstance(b["error"].get("message"), str)
    assert h["content-type"].startswith("application/json")


def list_bookings(api, query=""):
    s, b, _ = api.call("GET", "/bookings" + (("?" + query) if query else ""))
    assert s == 200, (s, b)
    return b


def walk_all_pages(api, query="", limit=None):
    ids, cursor = [], None
    for _ in range(1000):
        parts = [p for p in [query, f"limit={limit}" if limit else "",
                             f"cursor={cursor}" if cursor else ""] if p]
        b = list_bookings(api, "&".join(parts))
        ids.extend(x["id"] for x in b["bookings"])
        cursor = b["next_cursor"]
        if cursor is None:
            return ids
    raise AssertionError("pagination did not terminate")


def hhmm(minutes_of_day):
    return f"{minutes_of_day // 60:02d}:{minutes_of_day % 60:02d}"


def free(api, room, date="2026-03-02"):
    s, b, _ = api.call("GET", f"/rooms/{room}/availability?date={date}")
    assert s == 200, (s, b)
    assert b["date"] == date
    assert set(b) == {"date", "free"}
    return [(f["start"], f["end"]) for f in b["free"]]


# ---------------------------------------------------------------- generic HTTP behaviour

def test_unknown_path_is_404_with_error_body(api):
    assert_error(api.call("GET", "/nope"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r1/unknown"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/b1/extra"), 404, "not_found")
    assert_error(api.call("POST", "/bookings/b1/extra", {}), 404, "not_found")
    assert_error(api.call("GET", "/"), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/rooms/r1/availability?date=2026-03-02"),
])
def test_unsupported_method_is_405(api, method, path):
    assert_error(api.call(method, path), 405, "method_not_allowed")


def test_success_responses_are_json(api):
    s, b, h = api.call("POST", "/rooms", {"name": "A", "capacity": 1})
    assert s == 201 and h["content-type"].startswith("application/json")
    s, b, h = api.call("GET", "/rooms")
    assert s == 200 and h["content-type"].startswith("application/json")
    s, b, h = api.call("GET", "/bookings")
    assert s == 200 and h["content-type"].startswith("application/json")


def test_delete_204_has_empty_body(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None


@pytest.mark.parametrize("raw", [b"{not json", b"", b"[1,2]", b'"text"', b"42", b"null", b"true"])
def test_bad_json_bodies_are_400(api, raw):
    assert_error(api.call("POST", "/rooms", raw=raw), 400, "bad_json")
    assert_error(api.call("POST", "/bookings", raw=raw), 400, "bad_json")


def test_unknown_fields_are_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 4, "colour": "red"})
    assert s == 201 and b == {"id": "r1", "name": "Atlas", "capacity": 4}


# ---------------------------------------------------------------- rooms

def test_create_room_response_shape(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}


def test_room_ids_are_sequential_in_creation_order(api):
    ids = [make_room(api, name=f"Room {i}") for i in range(4)]
    assert ids == ["r1", "r2", "r3", "r4"]
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and [r["id"] for r in b["rooms"]] == ids


def test_list_rooms_empty_at_start(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}


def test_get_room_by_id(api):
    make_room(api, "Atlas", 8)
    make_room(api, "Borealis", 4)
    s, b, _ = api.call("GET", "/rooms/r2")
    assert s == 200 and b == {"id": "r2", "name": "Borealis", "capacity": 4}


def test_get_unknown_room_is_404(api):
    make_room(api)
    assert_error(api.call("GET", "/rooms/r9"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/zzz"), 404, "not_found")


@pytest.mark.parametrize("name", ["", "   ", "x" * 51, "\t\n"])
def test_room_name_invalid_lengths(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 8}), 422, "validation_error")


def test_room_name_boundaries(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 8})
    assert s == 201 and b["name"] == "x" * 50
    s, b, _ = api.call("POST", "/rooms", {"name": "y", "capacity": 8})
    assert s == 201


def test_room_name_is_stored_trimmed(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  Atlas  ", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    s, got, _ = api.call("GET", "/rooms/r1")
    assert got["name"] == "Atlas"


def test_room_name_length_counts_after_trim(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  " + "x" * 50 + "  ", "capacity": 8})
    assert s == 201 and b["name"] == "x" * 50


@pytest.mark.parametrize("capacity", [0, -1, 501, 10_000])
def test_room_capacity_out_of_range(api, capacity):
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": capacity}), 422, "validation_error")


@pytest.mark.parametrize("capacity", [1, 500])
def test_room_capacity_boundaries(api, capacity):
    s, b, _ = api.call("POST", "/rooms", {"name": f"A{capacity}", "capacity": capacity})
    assert s == 201 and b["capacity"] == capacity


@pytest.mark.parametrize("capacity", [True, False, "8", None, 8.5, [8]])
def test_room_capacity_wrong_type(api, capacity):
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": capacity}), 422, "validation_error")


@pytest.mark.parametrize("name", [True, 5, None, ["A"]])
def test_room_name_wrong_type(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 8}), 422, "validation_error")


@pytest.mark.parametrize("body", [
    {"capacity": 8},
    {"name": "A"},
    {},
])
def test_room_missing_fields(api, body):
    assert_error(api.call("POST", "/rooms", body), 422, "validation_error")


def test_duplicate_room_name_is_409(api):
    make_room(api, "Atlas")
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 2}), 409, "room_exists")


@pytest.mark.parametrize("dup", ["atlas", "ATLAS", "  Atlas ", " aTlAs\t"])
def test_duplicate_room_name_ignores_case_and_trim(api, dup):
    make_room(api, "Atlas")
    assert_error(api.call("POST", "/rooms", {"name": dup, "capacity": 2}), 409, "room_exists")
    s, b, _ = api.call("GET", "/rooms")
    assert len(b["rooms"]) == 1


def test_failed_room_creation_does_not_consume_id(api):
    make_room(api, "Atlas")
    assert api.call("POST", "/rooms", {"name": "Atlas", "capacity": 2})[0] == 409
    assert api.call("POST", "/rooms", {"name": "", "capacity": 2})[0] == 422
    assert make_room(api, "Borealis") == "r2"


def test_validation_precedes_duplicate_check(api):
    make_room(api, "Atlas")
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 0}), 422, "validation_error")


# ---------------------------------------------------------------- bookings: creation

def test_create_booking_response_shape(api):
    r = make_room(api)
    s, b, h = api.call("POST", "/bookings", booking_body(r, "09:00", "10:00", "Planning", 4))
    assert s == 201 and h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": r, "start": ts("09:00"), "end": ts("10:00"),
                 "title": "Planning", "attendees": 4}


def test_booking_ids_sequential(api):
    r = make_room(api)
    ids = [book_ok(api, r, f"{h:02d}:00", f"{h:02d}:30")["id"] for h in range(8, 12)]
    assert ids == ["b1", "b2", "b3", "b4"]


def test_get_booking_by_id(api):
    r = make_room(api)
    created = book_ok(api, r, "09:00", "10:00", title="Standup", attendees=3)
    s, got, _ = api.call("GET", "/bookings/b1")
    assert s == 200 and got == created


def test_get_unknown_booking_is_404(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert_error(api.call("GET", "/bookings/b2"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/bogus"), 404, "not_found")


def test_booking_title_is_stored_trimmed(api):
    r = make_room(api)
    b = book_ok(api, r, "09:00", "10:00", title="   Planning  ")
    assert b["title"] == "Planning"
    assert api.call("GET", "/bookings/b1")[1]["title"] == "Planning"


def test_booking_title_length_boundaries(api):
    r = make_room(api)
    assert book_ok(api, r, "09:00", "09:30", title="x" * 100)["title"] == "x" * 100
    assert book_ok(api, r, "10:00", "10:30", title="y")["title"] == "y"


@pytest.mark.parametrize("title", ["", "   ", "x" * 101, " " + "x" * 101 + " "])
def test_booking_title_invalid(api, title):
    r = make_room(api)
    assert_error(api.call("POST", "/bookings", booking_body(r, title=title)), 422, "validation_error")


@pytest.mark.parametrize("attendees", [0, -3])
def test_attendees_must_be_at_least_one(api, attendees):
    r = make_room(api)
    assert_error(api.book(r, "09:00", "10:00", attendees=attendees), 422, "validation_error")


@pytest.mark.parametrize("attendees", [True, False, "2", None, 2.5, [2]])
def test_attendees_wrong_type(api, attendees):
    r = make_room(api)
    assert_error(api.book(r, "09:00", "10:00", attendees=attendees), 422, "validation_error")


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field(api, field):
    r = make_room(api)
    body = booking_body(r)
    del body[field]
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("field,value", [
    ("room_id", 1), ("room_id", None), ("title", None), ("title", 7),
    ("start", None), ("end", 12345), ("attendees", None),
])
def test_booking_null_or_wrong_type_fields(api, field, value):
    r = make_room(api)
    body = booking_body(r)
    body[field] = value
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


def test_booking_unknown_fields_ignored(api):
    r = make_room(api)
    body = booking_body(r)
    body["organiser"] = "someone"
    s, b, _ = api.call("POST", "/bookings", body)
    assert s == 201 and "organiser" not in b


# ---- timestamp rules

@pytest.mark.parametrize("bad", [
    "2026-03-02T09:00:00+00:00",
    "2026-03-02T09:00:00",
    "2026-03-02 09:00:00Z",
    "2026-03-02t09:00:00Z",
    "2026-03-02T09:00Z",
    "2026-03-02T09:00:00.000Z",
    "2026-03-02T09:00:00z",
    "2026-3-2T09:00:00Z",
    "2026-03-02",
    "09:00",
    "not a time",
    "",
    "2026-02-30T09:00:00Z",
    "2026-13-01T09:00:00Z",
    "2026-03-02T24:00:00Z",
    "2026-03-02T09:60:00Z",
])
def test_invalid_start_timestamp_format(api, bad):
    r = make_room(api)
    body = booking_body(r)
    body["start"] = bad
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("bad", [
    "2026-03-02T10:00:00+00:00",
    "2026-03-02T10:00:00",
    "2026-03-02T10:00Z",
    "2026-02-29T10:00:00Z",  # 2026 is not a leap year
])
def test_invalid_end_timestamp_format(api, bad):
    r = make_room(api)
    body = booking_body(r)
    body["end"] = bad
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("minute", ["05", "10", "20", "29", "31", "44", "46", "59"])
def test_start_minutes_must_be_quarter_hour(api, minute):
    r = make_room(api)
    body = booking_body(r)
    body["start"] = f"2026-03-02T09:{minute}:00Z"
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("minute", ["05", "10", "20", "29", "31", "44", "46", "59"])
def test_end_minutes_must_be_quarter_hour(api, minute):
    r = make_room(api)
    body = booking_body(r)
    body["end"] = f"2026-03-02T10:{minute}:00Z"
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("start,end", [
    ("09:00", "09:15"), ("09:15", "09:30"), ("09:30", "09:45"), ("09:45", "10:00"),
])
def test_quarter_hour_bookings_allowed(api, start, end):
    r = make_room(api)
    assert book_ok(api, r, start, end)["start"] == ts(start)


def test_seconds_must_be_zero(api):
    r = make_room(api)
    body = booking_body(r)
    body["start"] = "2026-03-02T09:00:30Z"
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")
    body = booking_body(r)
    body["end"] = "2026-03-02T10:00:01Z"
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


def test_end_must_be_after_start(api):
    r = make_room(api)
    assert_error(api.call("POST", "/bookings", booking_body(r, "10:00", "09:00")), 422, "validation_error")


def test_zero_length_booking_rejected(api):
    r = make_room(api)
    assert_error(api.call("POST", "/bookings", booking_body(r, "10:00", "10:00")), 422, "validation_error")


def test_booking_of_exactly_eight_hours_allowed(api):
    r = make_room(api, capacity=8)
    b = book_ok(api, r, "08:00", "16:00")
    assert b["end"] == ts("16:00")


def test_booking_longer_than_eight_hours_rejected(api):
    r = make_room(api, capacity=8)
    assert_error(api.call("POST", "/bookings", booking_body(r, "08:00", "16:15")), 422, "validation_error")
    assert_error(api.call("POST", "/bookings", booking_body(r, "08:00", "18:00")), 422, "validation_error")


def test_booking_across_midnight_longer_than_eight_hours_rejected(api):
    r = make_room(api)
    body = booking_body(r)
    body["start"] = "2026-03-02T22:00:00Z"
    body["end"] = "2026-03-03T07:00:00Z"  # 9 hours
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


def test_booking_across_midnight_within_limit_allowed(api):
    r = make_room(api)
    body = booking_body(r)
    body["start"] = "2026-03-02T22:00:00Z"
    body["end"] = "2026-03-03T06:00:00Z"  # 8 hours
    s, b, _ = api.call("POST", "/bookings", body)
    assert s == 201 and b["end"] == "2026-03-03T06:00:00Z"


# ---- check ordering for bookings

def test_validation_precedes_unknown_room(api):
    body = booking_body("r99", "09:00", "10:00", title="")
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


def test_unknown_room_is_404(api):
    assert_error(api.call("POST", "/bookings", booking_body("r99")), 404, "not_found")


def test_unknown_room_precedes_capacity_check(api):
    body = booking_body("r99", attendees=10_000)
    assert_error(api.call("POST", "/bookings", body), 404, "not_found")


def test_over_capacity(api):
    r = make_room(api, capacity=2)
    assert_error(api.book(r, "09:00", "10:00", attendees=3), 422, "over_capacity")


def test_capacity_equal_is_allowed(api):
    r = make_room(api, capacity=2)
    assert book_ok(api, r, "09:00", "10:00", attendees=2)["attendees"] == 2


def test_over_capacity_precedes_conflict(api):
    r = make_room(api, capacity=2)
    book_ok(api, r, "09:00", "10:00", attendees=1)
    assert_error(api.book(r, "09:30", "10:30", attendees=5), 422, "over_capacity")


def test_conflict_on_overlap(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert_error(api.book(r, "09:30", "10:30"), 409, "conflict")


@pytest.mark.parametrize("start,end", [
    ("08:30", "09:30"),  # overlaps start
    ("09:30", "10:30"),  # overlaps end
    ("09:15", "09:45"),  # inside
    ("08:00", "11:00"),  # encloses
    ("09:00", "10:00"),  # identical
])
def test_conflict_variants(api, start, end):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert_error(api.book(r, start, end), 409, "conflict")


def test_back_to_back_is_not_a_conflict(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "10:00", "11:00")
    book_ok(api, r, "08:00", "09:00")


def test_same_slot_allowed_in_different_rooms(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    book_ok(api, r1, "09:00", "10:00")
    book_ok(api, r2, "09:00", "10:00")


def test_conflict_does_not_consume_booking_id(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert api.book(r, "09:30", "10:30")[0] == 409
    assert book_ok(api, r, "10:00", "11:00")["id"] == "b2"


def test_delete_frees_slot(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    book_ok(api, r, "09:00", "10:00")


def test_deleted_booking_ids_are_never_reused(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "11:00", "12:00")
    assert api.call("DELETE", "/bookings/b2")[0] == 204
    assert book_ok(api, r, "13:00", "14:00")["id"] == "b3"
    assert api.call("GET", "/bookings/b2")[0] == 404


def test_delete_unknown_or_twice_is_404(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert_error(api.call("DELETE", "/bookings/b99"), 404, "not_found")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_deleted_booking_disappears_from_lists(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "11:00", "12:00")
    api.call("DELETE", "/bookings/b1")
    assert [b["id"] for b in list_bookings(api)["bookings"]] == ["b2"]


# ---------------------------------------------------------------- idempotency

def test_idempotent_replay_returns_original_and_creates_nothing(api):
    r = make_room(api)
    hdr = {"Idempotency-Key": "k-1"}
    s1, b1, _ = api.book(r, "09:00", "10:00", title="A", attendees=2, headers=hdr)
    assert s1 == 201
    s2, b2, _ = api.book(r, "09:00", "10:00", title="A", attendees=2, headers=hdr)
    assert s2 == 201 and b2 == b1
    assert [b["id"] for b in list_bookings(api)["bookings"]] == ["b1"]
    # the replay consumed no id: the next genuinely new booking is b2
    assert book_ok(api, r, "10:00", "11:00")["id"] == "b2"


def test_idempotent_replay_ignores_json_key_order_and_whitespace(api):
    r = make_room(api)
    hdr = {"Idempotency-Key": "k-order"}
    raw1 = json.dumps(booking_body(r)).encode()
    body = booking_body(r)
    reordered = {k: body[k] for k in reversed(list(body))}
    raw2 = json.dumps(reordered, indent=4).encode()
    s1, b1, _ = api.call("POST", "/bookings", raw=raw1, headers=hdr)
    s2, b2, _ = api.call("POST", "/bookings", raw=raw2, headers=hdr)
    assert s1 == 201 and s2 == 201 and b1 == b2
    assert len(list_bookings(api)["bookings"]) == 1


def test_idempotent_replay_after_delete_still_returns_original(api):
    r = make_room(api)
    hdr = {"Idempotency-Key": "k-del"}
    _, b1, _ = api.book(r, "09:00", "10:00", headers=hdr)
    api.call("DELETE", "/bookings/b1")
    s, b2, _ = api.book(r, "09:00", "10:00", headers=hdr)
    assert s == 201 and b2 == b1
    assert list_bookings(api)["bookings"] == []


def test_idempotency_mismatch_is_422(api):
    r = make_room(api)
    hdr = {"Idempotency-Key": "k-mm"}
    assert api.book(r, "09:00", "10:00", title="A", headers=hdr)[0] == 201
    assert_error(api.book(r, "09:00", "10:00", title="B", headers=hdr), 422, "idempotency_mismatch")
    assert_error(api.book(r, "11:00", "12:00", title="A", headers=hdr), 422, "idempotency_mismatch")
    assert_error(api.book(r, "09:00", "10:00", title="A", attendees=3, headers=hdr), 422, "idempotency_mismatch")


def test_idempotency_mismatch_creates_nothing(api):
    r = make_room(api)
    hdr = {"Idempotency-Key": "k-none"}
    api.book(r, "09:00", "10:00", title="A", headers=hdr)
    api.book(r, "12:00", "13:00", title="other", headers=hdr)
    assert [b["id"] for b in list_bookings(api)["bookings"]] == ["b1"]


def test_keys_are_scoped_independently(api):
    r = make_room(api)
    api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k-a"})
    s, b2, _ = api.book(r, "11:00", "12:00", headers={"Idempotency-Key": "k-b"})
    assert s == 201 and b2["id"] == "b2"


def test_failed_request_with_key_is_not_remembered(api):
    r = make_room(api)
    hdr = {"Idempotency-Key": "k-fail"}
    assert_error(api.book(r, "09:00", "10:00", title="", headers=hdr), 422, "validation_error")
    s, b, _ = api.book(r, "09:00", "10:00", headers=hdr)
    assert s == 201 and b["id"] == "b1"


def test_conflict_with_key_is_not_remembered(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    hdr = {"Idempotency-Key": "k-conflict"}
    assert_error(api.book(r, "09:30", "10:30", headers=hdr), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:30", "10:30", headers=hdr)
    assert s == 201


def test_over_capacity_with_key_is_not_remembered(api):
    r = make_room(api, capacity=1)
    hdr = {"Idempotency-Key": "k-cap"}
    assert_error(api.book(r, "09:00", "10:00", attendees=5, headers=hdr), 422, "over_capacity")
    s, b, _ = api.book(r, "09:00", "10:00", attendees=1, headers=hdr)
    assert s == 201


def test_request_without_key_is_not_idempotent(api):
    r = make_room(api)
    assert book_ok(api, r, "09:00", "09:30")["id"] == "b1"
    assert_error(api.book(r, "09:00", "09:30"), 409, "conflict")


# ---------------------------------------------------------------- listing bookings

def test_list_bookings_empty(api):
    assert list_bookings(api) == {"bookings": [], "next_cursor": None}


def test_list_sorted_by_start_then_creation(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    book_ok(api, r1, "11:00", "12:00")           # b1
    book_ok(api, r2, "09:00", "10:00")           # b2
    book_ok(api, r1, "09:00", "10:00")           # b3: same start as b2, created later
    book_ok(api, r2, "08:00", "09:00")           # b4
    assert [b["id"] for b in list_bookings(api)["bookings"]] == ["b4", "b2", "b3", "b1"]


def test_list_response_shape(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and set(b) == {"bookings", "next_cursor"}
    assert b["next_cursor"] is None
    assert b["bookings"][0] == api.call("GET", "/bookings/b1")[1]


def test_filter_by_room_id(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    book_ok(api, r1, "09:00", "10:00")
    book_ok(api, r2, "09:00", "10:00")
    book_ok(api, r1, "11:00", "12:00")
    got = list_bookings(api, f"room_id={r1}")["bookings"]
    assert [b["id"] for b in got] == ["b1", "b3"]
    assert all(b["room_id"] == r1 for b in got)


def test_filter_by_unknown_room_is_empty(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert list_bookings(api, "room_id=r999") == {"bookings": [], "next_cursor": None}


def test_filter_from_only(api):
    r = make_room(api)
    book_ok(api, r, "08:00", "09:00")  # ends exactly at from -> excluded
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "13:00", "14:00")
    got = list_bookings(api, f"from={ts('09:00')}")["bookings"]
    assert [b["id"] for b in got] == ["b2", "b3"]


def test_filter_to_only(api):
    r = make_room(api)
    book_ok(api, r, "08:00", "09:00")
    book_ok(api, r, "10:00", "11:00")  # starts exactly at to -> excluded
    got = list_bookings(api, f"to={ts('10:00')}")["bookings"]
    assert [b["id"] for b in got] == ["b1"]


def test_filter_window_includes_partial_overlaps(api):
    r = make_room(api)
    book_ok(api, r, "08:00", "09:30")   # overlaps window start
    book_ok(api, r, "09:30", "10:30")   # inside
    book_ok(api, r, "11:30", "12:00")   # outside
    got = list_bookings(api, f"from={ts('09:00')}&to={ts('10:00')}")["bookings"]
    assert [b["id"] for b in got] == ["b1", "b2"]


def test_filter_window_not_aligned_to_quarter_hours(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    got = list_bookings(api, "from=2026-03-02T09:20:00Z&to=2026-03-02T09:40:00Z")["bookings"]
    assert [b["id"] for b in got] == ["b1"]


def test_filter_window_half_open_boundaries(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert list_bookings(api, f"from={ts('10:00')}")["bookings"] == []
    assert list_bookings(api, f"to={ts('09:00')}")["bookings"] == []
    got = list_bookings(api, "to=2026-03-02T09:00:01Z")["bookings"]
    assert [b["id"] for b in got] == ["b1"]


def test_filter_combined_room_and_window(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    book_ok(api, r1, "09:00", "10:00")
    book_ok(api, r2, "09:00", "10:00")
    book_ok(api, r1, "12:00", "13:00")
    got = list_bookings(api, f"room_id={r1}&from={ts('08:00')}&to={ts('11:00')}")["bookings"]
    assert [b["id"] for b in got] == ["b1"]


@pytest.mark.parametrize("query", [
    "from=2026-03-02",
    "from=garbage",
    "from=2026-03-02T09:00:00%2B00:00",
    "to=2026-13-01T00:00:00Z",
    "to=2026-03-02T09:00",
    "limit=0",
    "limit=101",
    "limit=abc",
    "limit=-1",
    "limit=1.5",
    "cursor=%%%not-a-cursor%%%",
])
def test_invalid_list_query_is_422(api, query):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert_error(api.call("GET", "/bookings?" + query), 422, "validation_error")


def test_limit_boundaries_accepted(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    assert list_bookings(api, "limit=1")["next_cursor"] is None
    assert list_bookings(api, "limit=100")["bookings"][0]["id"] == "b1"


def test_default_page_size_is_20(api):
    r = make_room(api)
    for i in range(25):
        m = 8 * 60 + i * 15
        book_ok(api, r, hhmm(m), hhmm(m + 15))
    first = list_bookings(api)
    assert len(first["bookings"]) == 20
    assert first["next_cursor"] is not None
    second = list_bookings(api, f"cursor={first['next_cursor']}")
    assert len(second["bookings"]) == 5 and second["next_cursor"] is None


def test_pagination_walks_every_booking_exactly_once(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    created = []
    for i in range(9):
        room = r1 if i % 2 == 0 else r2
        m = 8 * 60 + i * 15
        created.append(book_ok(api, room, hhmm(m), hhmm(m + 15))["id"])
    # same start as an earlier booking (different room): creation-order tie-break
    created.append(book_ok(api, r2, "08:00", "08:15")["id"])
    expected = [b["id"] for b in list_bookings(api, "limit=100")["bookings"]]
    assert sorted(expected) == sorted(created)
    for limit in (1, 2, 3, 4, 7):
        assert walk_all_pages(api, limit=limit) == expected


def test_pagination_with_room_filter_keeps_filter(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    for i in range(6):
        book_ok(api, r1, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
        book_ok(api, r2, f"{8 + i:02d}:30", f"{8 + i:02d}:45")
    expected = [b["id"] for b in list_bookings(api, f"room_id={r2}&limit=100")["bookings"]]
    assert len(expected) == 6
    assert walk_all_pages(api, query=f"room_id={r2}", limit=4) == expected


def test_pagination_with_window_filter(api):
    r = make_room(api)
    for i in range(6):
        book_ok(api, r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    query = f"from={ts('10:00')}&to={ts('14:00')}"
    expected = [b["id"] for b in list_bookings(api, query + "&limit=100")["bookings"]]
    assert expected == ["b3", "b4", "b5", "b6"]
    assert walk_all_pages(api, query=query, limit=3) == expected


def test_next_cursor_absent_on_last_page(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "10:00", "11:00")
    first = list_bookings(api, "limit=1")
    assert first["next_cursor"] is not None
    last = list_bookings(api, f"limit=1&cursor={first['next_cursor']}")
    assert [b["id"] for b in last["bookings"]] == ["b2"]
    assert last["next_cursor"] is None


def test_exact_fit_page_has_null_cursor(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "10:00", "11:00")
    first = list_bookings(api, "limit=2")
    assert len(first["bookings"]) == 2 and first["next_cursor"] is None


# ---------------------------------------------------------------- availability

def test_availability_empty_day_is_whole_working_day(api):
    r = make_room(api)
    assert free(api, r) == [(ts("08:00"), ts("18:00"))]


def test_availability_gaps_around_bookings(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "12:00", "13:30")
    assert free(api, r) == [
        (ts("08:00"), ts("09:00")),
        (ts("10:00"), ts("12:00")),
        (ts("13:30"), ts("18:00")),
    ]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    book_ok(api, r, "10:00", "11:00")
    assert free(api, r) == [(ts("08:00"), ts("09:00")), (ts("11:00"), ts("18:00"))]


def test_availability_fully_booked_day_is_empty(api):
    r = make_room(api)
    book_ok(api, r, "08:00", "13:00")
    book_ok(api, r, "13:00", "18:00")
    assert free(api, r) == []


def test_availability_clips_bookings_partly_outside_working_day(api):
    r = make_room(api)
    # ends after 08:00: only 08:00-09:00 is inside the day
    body = booking_body(r)
    body["start"] = "2026-03-02T01:00:00Z"
    body["end"] = "2026-03-02T09:00:00Z"
    assert api.call("POST", "/bookings", body)[0] == 201
    # starts before 18:00 and ends after it: only 17:00-18:00 is inside the day
    body = booking_body(r)
    body["start"] = "2026-03-02T17:00:00Z"
    body["end"] = "2026-03-02T23:00:00Z"
    assert api.call("POST", "/bookings", body)[0] == 201
    assert free(api, r) == [(ts("09:00"), ts("17:00"))]


def test_availability_ignores_bookings_on_other_days(api):
    r = make_room(api)
    body = booking_body(r)
    body["start"] = "2026-03-01T09:00:00Z"
    body["end"] = "2026-03-01T10:00:00Z"
    assert api.call("POST", "/bookings", body)[0] == 201
    assert free(api, r, "2026-03-02") == [(ts("08:00"), ts("18:00"))]
    assert free(api, r, "2026-03-01") == [
        (ts("2026-03-01T08:00:00Z"), ts("2026-03-01T09:00:00Z")),
        (ts("2026-03-01T10:00:00Z"), ts("2026-03-01T18:00:00Z")),
    ]


def test_availability_is_per_room(api):
    r1 = make_room(api, "Atlas")
    r2 = make_room(api, "Borealis")
    book_ok(api, r1, "09:00", "10:00")
    assert free(api, r2) == [(ts("08:00"), ts("18:00"))]


def test_availability_updates_after_delete(api):
    r = make_room(api)
    book_ok(api, r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert free(api, r) == [(ts("08:00"), ts("18:00"))]


def test_availability_quarter_hour_gap(api):
    r = make_room(api)
    book_ok(api, r, "08:15", "08:30")
    assert free(api, r) == [(ts("08:00"), ts("08:15")), (ts("08:30"), ts("18:00"))]


@pytest.mark.parametrize("date", [
    "", "2026-02-30", "2026-13-01", "2026-00-10", "2026-03-32", "2026-3-2",
    "03-02-2026", "2026/03/02", "2026-03-02T00:00:00Z", "garbage",
])
def test_availability_invalid_date_is_422(api, date):
    r = make_room(api)
    assert_error(api.call("GET", f"/rooms/{r}/availability?date={date}"), 422, "validation_error")


def test_availability_missing_date_is_422(api):
    r = make_room(api)
    assert_error(api.call("GET", f"/rooms/{r}/availability"), 422, "validation_error")


def test_availability_leap_day_accepted(api):
    r = make_room(api)
    assert free(api, r, "2028-02-29") == [(ts("2028-02-29T08:00:00Z"), ts("2028-02-29T18:00:00Z"))]


def test_availability_unknown_room_is_404(api):
    assert_error(api.call("GET", "/rooms/r42/availability?date=2026-03-02"), 404, "not_found")


def test_availability_room_check_precedes_date_check(api):
    assert_error(api.call("GET", "/rooms/r42/availability?date=garbage"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r42/availability"), 404, "not_found")
