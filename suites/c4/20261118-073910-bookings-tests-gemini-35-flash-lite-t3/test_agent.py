"""Thorough test suite for the bookings API guarding against regressions."""
import json
import pytest
from helpers import api, code, ts


def test_content_type_and_bad_json(api):
    # Content-Type header on success
    s, _, h = api.call("POST", "/rooms", {"name": "Room A", "capacity": 10})
    assert s == 201
    assert h["content-type"].startswith("application/json")

    # Content-Type header on error
    s, b, h = api.call("POST", "/rooms", {"name": "", "capacity": 10})
    assert s == 422
    assert h["content-type"].startswith("application/json")

    # Bad JSON (malformed)
    s, b, h = api.call("POST", "/rooms", raw=b"not json")
    assert s == 400
    assert code(b) == "bad_json"

    # Valid JSON but not an object (e.g., a list or string)
    s, b, _ = api.call("POST", "/rooms", raw=b"[1, 2, 3]")
    assert s == 400
    assert code(b) == "bad_json"

    s, b, _ = api.call("POST", "/rooms", raw=b'"just a string"')
    assert s == 400
    assert code(b) == "bad_json"


def test_not_found_and_method_not_allowed(api):
    # Non-existent path
    s, b, _ = api.call("GET", "/nonexistent")
    assert s == 404
    assert code(b) == "not_found"

    # Method not allowed (e.g. POST on /rooms/{id})
    r = api.room()
    s, b, _ = api.call("POST", f"/rooms/{r}")
    assert s == 405
    assert code(b) == "method_not_allowed"


def test_json_booleans_not_accepted_as_integers(api):
    # Boolean for room capacity
    s, b, _ = api.call("POST", "/rooms", {"name": "Room B", "capacity": True})
    assert s == 422
    assert code(b) == "validation_error"

    # Boolean for attendees in booking
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Test",
        "attendees": True
    })
    assert s == 422
    assert code(b) == "validation_error"

    # Boolean for limit in GET /bookings
    s, b, _ = api.call("GET", "/bookings?limit=true")
    assert s == 422
    assert code(b) == "validation_error"


def test_room_name_and_capacity_edge_cases(api):
    # Name too short / empty / whitespace
    for name in ["", "   "]:
        s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 10})
        assert s == 422
        assert code(b) == "validation_error"

    # Name too long (>50 chars)
    s, b, _ = api.call("POST", "/rooms", {"name": "a" * 51, "capacity": 10})
    assert s == 422
    assert code(b) == "validation_error"

    # Name stored trimmed
    s, b, _ = api.call("POST", "/rooms", {"name": "  Trimmed Room  ", "capacity": 10})
    assert s == 201
    assert b["name"] == "Trimmed Room"

    # Capacity out of bounds (<1, >500, non-int)
    for cap in [0, 501, 10.5, "10"]:
        s, b, _ = api.call("POST", "/rooms", {"name": "Room C", "capacity": cap})
        assert s == 422
        assert code(b) == "validation_error"

    # Duplicate room name (case-insensitive, after trimming)
    api.room("Alpha", 10)
    for dup in ["alpha", "  ALPHA  ", "Alpha"]:
        s, b, _ = api.call("POST", "/rooms", {"name": dup, "capacity": 5})
        assert s == 409
        assert code(b) == "room_exists"

    # Unknown fields are ignored
    s, b, _ = api.call("POST", "/rooms", {"name": "Beta", "capacity": 5, "unknown_field": "ignored"})
    assert s == 201
    assert "unknown_field" not in b


def test_get_room_by_id(api):
    r1 = api.room("Room 1", 10)
    api.room("Room 2", 20)

    s, b, _ = api.call("GET", f"/rooms/{r1}")
    assert s == 200
    assert b["id"] == r1
    assert b["name"] == "Room 1"
    assert b["capacity"] == 10

    s, b, _ = api.call("GET", "/rooms/r999")
    assert s == 404
    assert code(b) == "not_found"


def test_booking_validation_order_and_types(api):
    r = api.room("Room A", 10)

    # Missing fields
    s, b, _ = api.call("POST", "/bookings", {"room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Test"})
    assert s == 422
    assert code(b) == "validation_error"

    # Invalid timestamp format
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": "09:00", "end": ts("10:00"), "title": "Test", "attendees": 2
    })
    assert s == 422
    assert code(b) == "validation_error"

    # Non-quarter hour alignment
    for start, end in [("09:05", "10:00"), ("09:00", "10:10"), ("09:00:05", "10:00:00")]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts(start), "end": ts(end), "title": "Test", "attendees": 2
        })
        assert s == 422
        assert code(b) == "validation_error"

    # Duration <= 0 or > 8 hours
    for start, end in [("10:00", "09:00"), ("09:00", "09:00"), ("08:00", "16:01")]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts(start), "end": ts(end), "title": "Test", "attendees": 2
        })
        assert s == 422
        assert code(b) == "validation_error"

    # Title length (1 to 100 after trimming)
    for title in ["", "   ", "a" * 101]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": title, "attendees": 2
        })
        assert s == 422
        assert code(b) == "validation_error"

    # Attendees < 1
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Test", "attendees": 0
    })
    assert s == 422
    assert code(b) == "validation_error"

    # Check order: validation vs room existence
    # Unknown room with invalid timestamp should fail validation (422) before room check (404)
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": "r999", "start": "invalid", "end": ts("10:00"), "title": "Test", "attendees": 2
    })
    assert s == 422
    assert code(b) == "validation_error"

    # Unknown room with valid body should fail with 404 not_found
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": "r999", "start": ts("09:00"), "end": ts("10:00"), "title": "Test", "attendees": 2
    })
    assert s == 404
    assert code(b) == "not_found"

    # Room capacity check (over_capacity)
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Test", "attendees": 11
    })
    assert s == 422
    assert code(b) == "over_capacity"


def test_booking_id_monotonicity_and_no_reuse(api):
    r = api.room()
    _, b1, _ = api.book(r, "09:00", "10:00", title="B1")
    _, b2, _ = api.book(r, "10:15", "11:15", title="B2")
    assert b1["id"] == "b1"
    assert b2["id"] == "b2"

    api.call("DELETE", f"/bookings/{b1['id']}")

    _, b3, _ = api.book(r, "11:30", "12:30", title="B3")
    assert b3["id"] == "b3"  # ID not reused as b1


def test_idempotency(api):
    r = api.room()
    body = {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Sync",
        "attendees": 3
    }

    # Request with idempotency key (first call)
    key = "idem-key-123"
    s1, resp1, _ = api.call("POST", "/bookings", body, headers={"Idempotency-Key": key})
    assert s1 == 201

    # Subsequent request with same key and equal body (different key order / spacing)
    equal_body = {
        "attendees": 3,
        "title": "Sync",
        "end": ts("10:00"),
        "start": ts("09:00"),
        "room_id": r
    }
    s2, resp2, _ = api.call("POST", "/bookings", equal_body, headers={"Idempotency-Key": key})
    assert s2 == 201
    assert resp2 == resp1

    # Subsequent request with same key and different body -> 422 idempotency_mismatch
    diff_body = dict(body, title="Different Title")
    s3, resp3, _ = api.call("POST", "/bookings", diff_body, headers={"Idempotency-Key": key})
    assert s3 == 422
    assert code(resp3) == "idempotency_mismatch"

    # Failed requests are not remembered; key can be used again after failure
    key2 = "idem-key-fail"
    fail_body = dict(body, attendees=999)  # over capacity
    s_fail, _, _ = api.call("POST", "/bookings", fail_body, headers={"Idempotency-Key": key2})
    assert s_fail == 422

    # Now succeed with key2 using valid non-conflicting body (e.g. 11:00 to 12:00)
    succ_body = dict(body, start=ts("11:00"), end=ts("12:00"))
    s_succ, resp_succ, _ = api.call("POST", "/bookings", succ_body, headers={"Idempotency-Key": key2})
    assert s_succ == 201
    assert resp_succ["id"] != resp1["id"]  # created new booking


def test_delete_booking_edge_cases(api):
    r = api.room()
    _, b, _ = api.book(r, "09:00", "10:00")
    bid = b["id"]

    # Delete successfully
    s, _, _ = api.call("DELETE", f"/bookings/{bid}")
    assert s == 204

    # Delete again -> 404 not_found
    s, resp, _ = api.call("DELETE", f"/bookings/{bid}")
    assert s == 404
    assert code(resp) == "not_found"

    # Delete non-existent booking -> 404 not_found
    s, resp, _ = api.call("DELETE", "/bookings/b999")
    assert s == 404
    assert code(resp) == "not_found"


def test_get_booking_by_id(api):
    r = api.room()
    _, b, _ = api.book(r, "09:00", "10:00")
    bid = b["id"]

    s, got, _ = api.call("GET", f"/bookings/{bid}")
    assert s == 200
    assert got == b

    s, resp, _ = api.call("GET", "/bookings/b999")
    assert s == 404
    assert code(resp) == "not_found"


def test_list_bookings_filtering_and_pagination(api):
    r1 = api.room("Room 1", 10)
    r2 = api.room("Room 2", 10)

    api.book(r1, "11:00", "12:00", title="A")
    api.book(r1, "09:00", "10:00", title="B")
    api.book(r2, "09:30", "10:30", title="C")

    # Filter by room_id
    s, data, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert s == 200
    assert len(data["bookings"]) == 2
    assert [x["title"] for x in data["bookings"]] == ["B", "A"]  # sorted by start ascending

    # Unknown room_id gives empty list, not error
    s, data, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200
    assert data["bookings"] == []

    # Filter by from / to timestamps (half-open window [from, to), overlap check) with room_id filter
    s, data, _ = api.call("GET", f"/bookings?room_id={r1}&from={ts('10:00')}")
    assert s == 200
    assert {x["title"] for x in data["bookings"]} == {"A"}

    s, data, _ = api.call("GET", f"/bookings?room_id={r1}&to={ts('10:30')}")
    assert s == 200
    titles = {x["title"] for x in data["bookings"]}
    assert titles == {"B"}

    # Invalid from/to/limit/cursor -> 422 validation_error
    for param in ["from=invalid", "to=invalid", "limit=0", "limit=101", "limit=abc", "cursor=invalid"]:
        s, resp, _ = api.call("GET", f"/bookings?{param}")
        assert s == 422
        assert code(resp) == "validation_error"

    # Pagination with limit and cursor
    r3 = api.room("Room 3", 10)
    for i in range(5):
        api.book(r3, f"0{i}:00", f"0{i}:30", title=str(i))

    s, page1, _ = api.call("GET", f"/bookings?room_id={r3}&limit=2")
    assert s == 200
    assert len(page1["bookings"]) == 2
    assert page1["next_cursor"] is not None

    s, page2, _ = api.call("GET", f"/bookings?room_id={r3}&limit=2&cursor={page1['next_cursor']}")
    assert s == 200
    assert len(page2["bookings"]) == 2
    assert page2["next_cursor"] is not None

    s, page3, _ = api.call("GET", f"/bookings?room_id={r3}&limit=2&cursor={page2['next_cursor']}")
    assert s == 200
    assert len(page3["bookings"]) == 1
    assert page3["next_cursor"] is None


def test_availability_edge_cases(api):
    r = api.room()
    # Working day: 08:00:00Z to 18:00:00Z
    api.book(r, "09:00", "10:00")

    s, data, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert data["date"] == "2026-03-02"
    assert data["free"] == [
        {"start": ts("08:00"), "end": ts("09:00")},
        {"start": ts("10:00"), "end": ts("18:00")}
    ]

    # Adjacent bookings leave no gap
    api.book(r, "10:00", "11:00")
    s, data, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert data["free"] == [
        {"start": ts("08:00"), "end": ts("09:00")},
        {"start": ts("11:00"), "end": ts("18:00")}
    ]

    # Room check comes before date check!
    # Unknown room with invalid date -> 404 not_found (not 422 validation_error)
    s, resp, _ = api.call("GET", "/rooms/r999/availability?date=invalid-date")
    assert s == 404
    assert code(resp) == "not_found"

    # Known room with missing or invalid date -> 422 validation_error
    for q in ["", "date=", "date=bad-date", "date=2026-02-31"]:
        s, resp, _ = api.call("GET", f"/rooms/{r}/availability?{q}")
        assert s == 422
        assert code(resp) == "validation_error"


def test_fully_booked_availability(api):
    r = api.room()
    # Fully book 08:00 to 18:00 using two adjacent valid bookings (<= 8 hours each)
    api.book(r, "08:00", "14:00", title="Part 1")
    api.book(r, "14:00", "18:00", title="Part 2")
    s, data, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert data["free"] == []
