"""Thorough pytest test suite for bookings API to guard against regressions."""
import pytest
from helpers import api, code, ts


def test_content_type_and_error_format(api):
    # Error response content-type and body structure
    s, b, h = api.call("POST", "/rooms", {"name": "", "capacity": 8})
    assert s == 422
    assert h["content-type"].startswith("application/json")
    assert "error" in b
    assert "code" in b["error"]
    assert "message" in b["error"]
    assert code(b) == "validation_error"


def test_bad_json_and_body_types(api):
    # Not valid JSON
    s, b, h = api.call("POST", "/rooms", raw=b"not-json")
    assert s == 400
    assert code(b) == "bad_json"

    # Valid JSON but not an object (e.g. list)
    s, b, _ = api.call("POST", "/rooms", raw=b"[1, 2, 3]")
    assert s == 400
    assert code(b) == "bad_json"

    # Valid JSON but scalar
    s, b, _ = api.call("POST", "/rooms", raw=b"123")
    assert s == 400
    assert code(b) == "bad_json"


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8, "unknown_field": "ignored"})
    assert s == 201
    assert b["name"] == "Atlas"
    assert b["capacity"] == 8
    assert "unknown_field" not in b


def test_boolean_not_accepted_for_integer(api):
    # Capacity as boolean
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": True})
    assert s == 422
    assert code(b) == "validation_error"

    # Attendees as boolean
    r = api.room()
    s, b, _ = api.book(r, "09:00", "10:00", attendees=True)
    assert s == 422
    assert code(b) == "validation_error"


def test_not_found_and_method_not_allowed(api):
    # 404 unknown path
    s, b, _ = api.call("GET", "/nonexistent")
    assert s == 404
    assert code(b) == "not_found"

    # 405 method not allowed
    s, b, _ = api.call("PUT", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 405
    assert code(b) == "method_not_allowed"

    s, b, _ = api.call("DELETE", "/rooms")
    assert s == 405
    assert code(b) == "method_not_allowed"


def test_room_name_and_capacity_validation(api):
    # Missing name
    s, b, _ = api.call("POST", "/rooms", {"capacity": 8})
    assert s == 422 and code(b) == "validation_error"

    # Missing capacity
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas"})
    assert s == 422 and code(b) == "validation_error"

    # Name too short / empty / whitespace
    for name in ["", "   "]:
        s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 8})
        assert s == 422 and code(b) == "validation_error"

    # Name too long (> 50 chars)
    s, b, _ = api.call("POST", "/rooms", {"name": "a" * 51, "capacity": 8})
    assert s == 422 and code(b) == "validation_error"

    # Name exactly 50 chars works
    s, b, _ = api.call("POST", "/rooms", {"name": "a" * 50, "capacity": 8})
    assert s == 201

    # Capacity out of bounds (< 1 or > 500)
    for cap in [0, -1, 501, 1000]:
        s, b, _ = api.call("POST", "/rooms", {"name": f"Room {cap}", "capacity": cap})
        assert s == 422 and code(b) == "validation_error"


def test_room_exists_conflict(api):
    api.room("Atlas", 8)
    # Exact duplicate name
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 10})
    assert s == 409 and code(b) == "room_exists"

    # Case-insensitive and trimmed duplicate check
    s, b, _ = api.call("POST", "/rooms", {"name": "  atlas  ", "capacity": 10})
    assert s == 409 and code(b) == "room_exists"


def test_room_get_by_id(api):
    r1 = api.room("Atlas", 8)
    s, b, _ = api.call("GET", f"/rooms/{r1}")
    assert s == 200
    assert b == {"id": r1, "name": "Atlas", "capacity": 8}

    # Unknown room GET
    s, resp_404, _ = api.call("GET", "/rooms/r999")
    assert s == 404 and code(resp_404) == "not_found"


def test_booking_timestamp_validation(api):
    r = api.room()
    # Invalid timestamp formats
    for bad_ts in ["2026-03-02 09:00:00Z", "2026-03-02T09:00:00", "09:00:00Z", "bad"]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": bad_ts, "end": ts("10:00"), "title": "Test", "attendees": 2
        })
        assert s == 422 and code(b) == "validation_error"


def test_booking_alignment_and_duration(api):
    r = api.room()
    # Bad minute alignment (not 00, 15, 30, 45)
    s, b, _ = api.book(r, "09:10", "10:00")
    assert s == 422 and code(b) == "validation_error"

    # Non-zero seconds
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": "2026-03-02T09:00:01Z", "end": "2026-03-02T10:00:00Z", "title": "Test", "attendees": 2
    })
    assert s == 422 and code(b) == "validation_error"

    # End <= start
    s, b, _ = api.book(r, "10:00", "09:00")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.book(r, "10:00", "10:00")
    assert s == 422 and code(b) == "validation_error"

    # Duration > 8 hours
    s, b, _ = api.book(r, "09:00", "18:00")  # 9 hours
    assert s == 422 and code(b) == "validation_error"

    # Duration exactly 8 hours works
    s, b, _ = api.book(r, "09:00", "17:00")  # 8 hours
    assert s == 201


def test_booking_title_and_attendees_validation(api):
    r = api.room()
    # Title validation
    for title in ["", "   ", "a" * 101]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": title, "attendees": 2
        })
        assert s == 422 and code(b) == "validation_error"

    # Attendees validation (< 1)
    s, b, _ = api.book(r, "09:00", "10:00", attendees=0)
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.book(r, "09:00", "10:00", attendees=-2)
    assert s == 422 and code(b) == "validation_error"


def test_booking_check_order_room_before_capacity(api):
    # Room check (404) comes before capacity check (over_capacity)
    # If room does not exist, even if attendees are massive, it should be 404 not_found.
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": "r999", "start": ts("09:00"), "end": ts("10:00"), "title": "Test", "attendees": 9999
    })
    assert s == 404 and code(b) == "not_found"


def test_booking_id_never_reused(api):
    r = api.room()
    _, b1, _ = api.book(r, "09:00", "10:00", title="B1")
    assert b1["id"] == "b1"
    api.call("DELETE", "/bookings/b1")

    _, b2, _ = api.book(r, "11:00", "12:00", title="B2")
    assert b2["id"] == "b2"  # ID never reused, even after delete


def test_booking_crud_and_not_found(api):
    r = api.room()
    _, b, _ = api.book(r, "09:00", "10:00", title="Meet")

    # GET existing booking
    s, got, _ = api.call("GET", "/bookings/b1")
    assert s == 200 and got == b

    # GET non-existent booking
    s, resp_404, _ = api.call("GET", "/bookings/b999")
    assert s == 404 and code(resp_404) == "not_found"

    # DELETE non-existent booking
    s, resp_del404, _ = api.call("DELETE", "/bookings/b999")
    assert s == 404 and code(resp_del404) == "not_found"

    # DELETE existing booking twice
    s, _, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204
    s, resp_del2, _ = api.call("DELETE", "/bookings/b1")
    assert s == 404 and code(resp_del2) == "not_found"


def test_idempotency_key(api):
    r = api.room()
    body = {"room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Sync", "attendees": 2}

    # First request with idempotency key succeeds
    s1, b1, _ = api.call("POST", "/bookings", body, headers={"Idempotency-Key": "key-123"})
    assert s1 == 201

    # Second request with same key and equal body (different whitespace/key order) returns original 201 without creating new booking
    body_eq = {"room_id": r, "attendees": 2, "title": "Sync", "start": ts("09:00"), "end": ts("10:00")}
    s2, b2, _ = api.call("POST", "/bookings", body_eq, headers={"Idempotency-Key": "key-123"})
    assert s2 == 201
    assert b2 == b1

    # List bookings should only show 1 booking created
    _, list_resp, _ = api.call("GET", "/bookings")
    assert len(list_resp["bookings"]) == 1

    # Request with same key but different body -> 422 idempotency_mismatch
    body_diff = {"room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Different", "attendees": 2}
    s3, b3, _ = api.call("POST", "/bookings", body_diff, headers={"Idempotency-Key": "key-123"})
    assert s3 == 422 and code(b3) == "idempotency_mismatch"

    # Failed requests are not remembered; key can be used again after failure
    api.book(r, "11:00", "12:00")
    s_fail, b_fail, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": ts("11:30"), "end": ts("12:30"), "title": "Conflict", "attendees": 2
    }, headers={"Idempotency-Key": "key-fail"})
    assert s_fail == 409

    # Now use key-fail with a valid request -> should succeed (201)
    s_retry, b_retry, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": ts("13:00"), "end": ts("14:00"), "title": "Valid Retry", "attendees": 2
    }, headers={"Idempotency-Key": "key-fail"})
    assert s_retry == 201


def test_list_bookings_filters_and_pagination(api):
    r1 = api.room("Room 1", 10)
    r2 = api.room("Room 2", 10)

    api.book(r1, "09:00", "10:00", title="A")
    api.book(r1, "10:00", "11:00", title="B")
    api.book(r2, "09:30", "10:30", title="C")

    # Filter by room_id
    _, b_r1, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert len(b_r1["bookings"]) == 2
    assert [x["title"] for x in b_r1["bookings"]] == ["A", "B"]

    # Filter by unknown room_id gives empty list, not error
    _, b_unk, _ = api.call("GET", "/bookings?room_id=r999")
    assert b_unk["bookings"] == []

    # Filter by from / to timestamps (overlap window [from, to))
    # Window 09:15 to 10:00 should overlap A (09:00-10:00) and C (09:30-10:30) but not B (10:00-11:00)
    _, b_time, _ = api.call("GET", f"/bookings?from={ts('09:15')}&to={ts('10:00')}")
    assert len(b_time["bookings"]) == 2
    assert {x["title"] for x in b_time["bookings"]} == {"A", "C"}

    # Invalid from, to, limit, cursor -> 422 validation_error
    for q in ["from=bad", "to=bad", "limit=0", "limit=101", "limit=abc", "cursor=invalid"]:
        s, b, _ = api.call("GET", f"/bookings?{q}")
        assert s == 422 and code(b) == "validation_error"

    # Pagination with limit
    for i in range(5):
        api.book(r1, f"{12+i}:00", f"{13+i}:00", title=f"P{i}")

    s, page1, _ = api.call("GET", f"/bookings?room_id={r1}&limit=3")
    assert s == 200
    assert len(page1["bookings"]) == 3
    assert page1["next_cursor"] is not None

    s, page2, _ = api.call("GET", f"/bookings?room_id={r1}&limit=3&cursor={page1['next_cursor']}")
    assert s == 200
    assert len(page2["bookings"]) == 3
    assert page2["next_cursor"] is not None

    s, page3, _ = api.call("GET", f"/bookings?room_id={r1}&limit=3&cursor={page2['next_cursor']}")
    assert s == 200
    assert len(page3["bookings"]) == 1
    assert page3["next_cursor"] is None


def test_availability_rules(api):
    # Room check comes before date check (unknown room + invalid date -> 404 not_found)
    s, b, _ = api.call("GET", "/rooms/r999/availability?date=bad-date")
    assert s == 404 and code(b) == "not_found"

    r = api.room()
    # Missing date -> 422 validation_error
    s, b, _ = api.call("GET", f"/rooms/{r}/availability")
    assert s == 422 and code(b) == "validation_error"

    # Invalid date -> 422 validation_error
    for d in ["2026-02-30", "not-a-date", "2026/03/02"]:
        s, b, _ = api.call("GET", f"/rooms/{r}/availability?date={d}")
        assert s == 422 and code(b) == "validation_error"

    # Availability free slots calculation
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")

    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["date"] == "2026-03-02"
    assert b["free"] == [
        {"start": ts("08:00"), "end": ts("09:00")},
        {"start": ts("10:00"), "end": ts("11:00")},
        {"start": ts("12:00"), "end": ts("18:00")},
    ]

    # Booking partly outside working day clipped to it
    r2 = api.room("Room 2", 10)
    api.book(r2, "07:30", "08:30")
    s, b2, _ = api.call("GET", f"/rooms/{r2}/availability?date=2026-03-02")
    assert s == 200
    assert b2["free"] == [{"start": ts("08:30"), "end": ts("18:00")}]
