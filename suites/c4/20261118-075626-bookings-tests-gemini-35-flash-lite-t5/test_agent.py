"""Thorough test suite guarding the bookings API against regressions."""
import pytest
from helpers import api, code, ts


def test_error_response_structure_and_content_type(api):
    s, b, h = api.call("GET", "/nonexistent")
    assert s == 404
    assert h["content-type"].startswith("application/json")
    assert b == {"error": {"code": "not_found", "message": "not_found"}}


def test_method_not_allowed(api):
    s, b, h = api.call("DELETE", "/rooms")
    assert s == 405
    assert code(b) == "method_not_allowed"
    assert h["content-type"].startswith("application/json")


def test_bad_json_and_non_object(api):
    # Not valid JSON
    s, b, _ = api.call("POST", "/rooms", raw=b"invalid json")
    assert s == 400
    assert code(b) == "bad_json"

    # Valid JSON but not an object (array)
    s, b, _ = api.call("POST", "/rooms", raw=b"[1, 2, 3]")
    assert s == 400
    assert code(b) == "bad_json"

    # Primitive JSON (number, string, boolean, null)
    for raw_body in [b"123", b'"string"', b"true", b"null"]:
        s, b, _ = api.call("POST", "/rooms", raw=raw_body)
        assert s == 400
        assert code(b) == "bad_json"


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Conference A", "capacity": 10, "unknown_field": "foo"})
    assert s == 201
    assert "unknown_field" not in b
    assert b["name"] == "Conference A"
    assert b["capacity"] == 10


def test_boolean_not_accepted_for_integers(api):
    # capacity as boolean
    s, b, _ = api.call("POST", "/rooms", {"name": "Room X", "capacity": True})
    assert s == 422
    assert code(b) == "validation_error"

    r = api.room("Room Y", 10)
    # attendees as boolean
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Meeting",
        "attendees": True
    })
    assert s == 422
    assert code(b) == "validation_error"


def test_room_name_and_capacity_validation(api):
    # Empty name or whitespace
    for name in ["", "   "]:
        s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 10})
        assert s == 422
        assert code(b) == "validation_error"

    # Name too long (> 50 chars)
    s, b, _ = api.call("POST", "/rooms", {"name": "a" * 51, "capacity": 10})
    assert s == 422
    assert code(b) == "validation_error"

    # Capacity out of bounds (0, 501, negative, non-int)
    for cap in [0, 501, -5, 10.5, "10"]:
        s, b, _ = api.call("POST", "/rooms", {"name": "Room", "capacity": cap})
        assert s == 422
        assert code(b) == "validation_error"


def test_room_name_uniqueness(api):
    api.room("Atlas", 10)
    # Duplicate name case-insensitive and trimmed
    for name in ["Atlas", "  atlas  ", "ATLAS", "atlas"]:
        s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 5})
        assert s == 409
        assert code(b) == "room_exists"


def test_room_get_and_not_found(api):
    r_id = api.room("Meeting Room 1", 20)
    s, b, _ = api.call("GET", f"/rooms/{r_id}")
    assert s == 200
    assert b["id"] == r_id
    assert b["name"] == "Meeting Room 1"
    assert b["capacity"] == 20

    s, b, _ = api.call("GET", "/rooms/r999")
    assert s == 404
    assert code(b) == "not_found"


def test_booking_validation_checks_order(api):
    # 1. Validation error takes precedence over room not found
    s, b, _ = api.book("r999", "invalid_ts", "10:00", title="", attendees=0)
    assert s == 422
    assert code(b) == "validation_error"

    # If valid parameters but room not found -> 404 not_found
    s, b, _ = api.book("r999", "09:00", "10:00", title="Valid Title", attendees=2)
    assert s == 404
    assert code(b) == "not_found"


def test_booking_timestamp_and_alignment_validation(api):
    r = api.room()
    # Invalid timestamp string
    for start, end in [("2026-03-02 09:00:00Z", "10:00"), ("09:00", "2026-03-02 10:00:00"), ("bad", "10:00")]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r,
            "start": start,
            "end": end if "T" in end else ts(end),
            "title": "Meeting",
            "attendees": 2
        })
        assert s == 422
        assert code(b) == "validation_error"

    # Misaligned minutes (e.g. 09:05, 09:10, 09:20) or seconds non-zero
    for start in ["09:05", "09:10", "09:20"]:
        s, b, _ = api.book(r, start, "10:00")
        assert s == 422
        assert code(b) == "validation_error"


def test_booking_duration_validation(api):
    r = api.room()
    # End <= start
    s, b, _ = api.book(r, "10:00", "09:00")
    assert s == 422
    assert code(b) == "validation_error"

    s, b, _ = api.book(r, "10:00", "10:00")
    assert s == 422
    assert code(b) == "validation_error"

    # Duration > 8 hours (e.g. 08:00 to 16:15 is 8h 15m)
    s, b, _ = api.book(r, "08:00", "16:15")
    assert s == 422
    assert code(b) == "validation_error"

    # Exactly 8 hours is allowed (08:00 to 16:00)
    s, resp, _ = api.book(r, "08:00", "16:00")
    assert s == 201


def test_booking_title_and_attendees_validation(api):
    r = api.room()
    # Title empty or whitespace
    for title in ["", "   "]:
        s, b, _ = api.book(r, "09:00", "10:00", title=title)
        assert s == 422
        assert code(b) == "validation_error"

    # Title too long (> 100 chars)
    s, b, _ = api.book(r, "09:00", "10:00", title="a" * 101)
    assert s == 422
    assert code(b) == "validation_error"

    # Attendees < 1 or non-integer
    for att in [0, -1, 1.5]:
        s, b, _ = api.book(r, "09:00", "10:00", attendees=att)
        assert s == 422
        assert code(b) == "validation_error"


def test_booking_ids_sequence_and_non_reuse(api):
    r = api.room()
    _, b1, _ = api.book(r, "09:00", "10:00", title="One")
    _, b2, _ = api.book(r, "10:15", "11:15", title="Two")
    assert b1["id"] == "b1"
    assert b2["id"] == "b2"

    api.call("DELETE", "/bookings/b1")
    _, b3, _ = api.book(r, "11:30", "12:30", title="Three")
    assert b3["id"] == "b3"  # ID not reused!


def test_idempotency(api):
    r = api.room()
    payload = {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Idempotent Meeting",
        "attendees": 2
    }
    headers = {"Idempotency-Key": "key-123"}

    # First request succeeds (201)
    s1, b1, _ = api.call("POST", "/bookings", payload, headers=headers)
    assert s1 == 201

    # Second request with same key and equal body (even with rearranged keys / whitespace)
    payload_reordered = {
        "attendees": 2,
        "title": "Idempotent Meeting",
        "end": ts("10:00"),
        "start": ts("09:00"),
        "room_id": r
    }
    s2, b2, _ = api.call("POST", "/bookings", payload_reordered, headers=headers)
    assert s2 == 201
    assert b2 == b1

    # Third request with same key and DIFFERENT body -> 422 idempotency_mismatch
    payload_diff = dict(payload, title="Different Title")
    s3, b3, _ = api.call("POST", "/bookings", payload_diff, headers=headers)
    assert s3 == 422
    assert code(b3) == "idempotency_mismatch"

    # Failed requests are not remembered (original successful response still remembered)
    s4, b4, _ = api.call("POST", "/bookings", payload, headers=headers)
    assert s4 == 201
    assert b4 == b1


def test_booking_get_delete_not_found(api):
    r = api.room()
    _, b, _ = api.book(r, "09:00", "10:00")
    b_id = b["id"]

    # GET booking
    s, got, _ = api.call("GET", f"/bookings/{b_id}")
    assert s == 200
    assert got["id"] == b_id

    # GET unknown booking
    s, got, _ = api.call("GET", "/bookings/b999")
    assert s == 404
    assert code(got) == "not_found"

    # DELETE booking
    s, _, _ = api.call("DELETE", f"/bookings/{b_id}")
    assert s == 204

    # DELETE already deleted booking -> 404
    s, got, _ = api.call("DELETE", f"/bookings/{b_id}")
    assert s == 404
    assert code(got) == "not_found"


def test_list_bookings_filters_and_pagination(api):
    r1 = api.room("Room 1", 10)
    r2 = api.room("Room 2", 10)

    api.book(r1, "09:00", "10:00", title="A")
    api.book(r1, "11:00", "12:00", title="B")
    api.book(r2, "10:00", "11:00", title="C")

    # Filter by room_id
    s, b, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert s == 200
    assert [x["title"] for x in b["bookings"]] == ["A", "B"]

    # Filter by unknown room_id gives empty list
    s, b, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200
    assert b["bookings"] == []

    # Filter by from / to (half-open window [from, to))
    s, b, _ = api.call("GET", f"/bookings?from={ts('09:30')}&to={ts('11:30')}")
    assert s == 200
    assert [x["title"] for x in b["bookings"]] == ["A", "C", "B"]

    # Invalid from/to/limit/cursor -> 422 validation_error
    for param in ["from=invalid", "to=invalid", "limit=0", "limit=101", "limit=abc", "cursor=invalid"]:
        s, b, _ = api.call("GET", f"/bookings?{param}")
        assert s == 422
        assert code(b) == "validation_error"


def test_availability_checks_and_gaps(api):
    # Unknown room check comes before date check -> 404 not_found
    s, b, _ = api.call("GET", "/rooms/r999/availability?date=invalid-date")
    assert s == 404
    assert code(b) == "not_found"

    r = api.room()
    # Missing or invalid date -> 422 validation_error
    for q in ["", "date=2026-02-30", "date=not-a-date", "date=2026/03/02"]:
        s, b, _ = api.call("GET", f"/rooms/{r}/availability?{q}")
        assert s == 422
        assert code(b) == "validation_error"

    # Availability gaps calculation
    api.book(r, "09:00", "10:00")
    api.book(r, "14:00", "15:00")

    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["date"] == "2026-03-02"
    assert b["free"] == [
        {"start": ts("08:00"), "end": ts("09:00")},
        {"start": ts("10:00"), "end": ts("14:00")},
        {"start": ts("15:00"), "end": ts("18:00")}
    ]


def test_availability_clipping_and_adjacent(api):
    r = api.room()
    api.book(r, "07:00", "09:00")
    api.book(r, "17:00", "19:00")

    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["free"] == [
        {"start": ts("09:00"), "end": ts("17:00")}
    ]

    # Adjacent bookings leave no gap
    api.call("DELETE", "/bookings/b1")
    api.call("DELETE", "/bookings/b2")

    api.book(r, "09:00", "11:00")
    api.book(r, "11:00", "13:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["free"] == [
        {"start": ts("08:00"), "end": ts("09:00")},
        {"start": ts("13:00"), "end": ts("18:00")}
    ]
