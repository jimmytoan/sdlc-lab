"""Thorough test suite for the bookings API guard against regressions."""
import pytest
from helpers import api, code, ts


def test_http_errors(api):
    # 404 not found
    s, b, _ = api.call("GET", "/nonexistent")
    assert s == 404 and code(b) == "not_found"

    s, b, _ = api.call("POST", "/rooms/r1/nonexistent", {"name": "X", "capacity": 5})
    assert s == 404 and code(b) == "not_found"

    # 405 method not allowed
    s, b, _ = api.call("PUT", "/rooms", {"name": "X", "capacity": 5})
    assert s == 405 and code(b) == "method_not_allowed"

    r = api.room()
    s, b, _ = api.call("POST", f"/rooms/{r}", {"name": "X", "capacity": 5})
    assert s == 405 and code(b) == "method_not_allowed"


def test_bad_json(api):
    # Invalid JSON string
    s, b, _ = api.call("POST", "/rooms", raw=b"not json")
    assert s == 400 and code(b) == "bad_json"

    # Valid JSON but not an object (array, scalar, etc.)
    for invalid_body in [[], "string", 123, True, None]:
        s, b, _ = api.call("POST", "/rooms", body=invalid_body)
        assert s == 400 and code(b) == "bad_json"


def test_unknown_fields_ignored(api):
    # Rooms
    s, b, _ = api.call("POST", "/rooms", {"name": "Omega", "capacity": 10, "unknown_field": "test"})
    assert s == 201
    assert b["name"] == "Omega" and b["capacity"] == 10
    assert "unknown_field" not in b

    # Bookings
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": b["id"],
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Sync",
        "attendees": 2,
        "extra_field": 123
    })
    assert s == 201
    assert "extra_field" not in b


def test_boolean_not_accepted_as_integer(api):
    # Room capacity
    s, b, _ = api.call("POST", "/rooms", {"name": "Test", "capacity": True})
    assert s == 422 and code(b) == "validation_error"

    # Booking attendees
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Sync",
        "attendees": True
    })
    assert s == 422 and code(b) == "validation_error"

    # List bookings limit
    s, b, _ = api.call("GET", "/bookings?limit=true")
    assert s == 422 and code(b) == "validation_error"


def test_room_name_validation(api):
    # Empty or whitespace only
    for name in ["", "   ", "\t\n"]:
        s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 10})
        assert s == 422 and code(b) == "validation_error"

    # Too long (> 50 chars)
    s, b, _ = api.call("POST", "/rooms", {"name": "a" * 51, "capacity": 10})
    assert s == 422 and code(b) == "validation_error"

    # Non-string name
    for invalid_name in [123, True, [], None]:
        s, b, _ = api.call("POST", "/rooms", {"name": invalid_name, "capacity": 10})
        assert s == 422 and code(b) == "validation_error"

    # Trimming
    s, b, _ = api.call("POST", "/rooms", {"name": "  Conference A  ", "capacity": 10})
    assert s == 201 and b["name"] == "Conference A"


def test_room_capacity_validation(api):
    for cap in [0, -1, 501, 5.5, "10", None, True]:
        s, b, _ = api.call("POST", "/rooms", {"name": "Room", "capacity": cap})
        assert s == 422 and code(b) == "validation_error"

    # Boundary valid capacities
    s, b1, _ = api.call("POST", "/rooms", {"name": "Small", "capacity": 1})
    assert s == 201 and b1["capacity"] == 1
    s, b500, _ = api.call("POST", "/rooms", {"name": "Large", "capacity": 500})
    assert s == 201 and b500["capacity"] == 500


def test_room_uniqueness_case_insensitive_trimmed(api):
    api.room("Alpha")
    for name in ["alpha", "ALPHA", "  alpha  ", "\tALPHA\n"]:
        s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 5})
        assert s == 409 and code(b) == "room_exists"


def test_get_room_by_id(api):
    r = api.room("Beta", 12)
    s, b, _ = api.call("GET", f"/rooms/{r}")
    assert s == 200 and b["id"] == r and b["name"] == "Beta" and b["capacity"] == 12

    s, b, _ = api.call("GET", "/rooms/r999")
    assert s == 404 and code(b) == "not_found"


def test_booking_timestamp_validation(api):
    r = api.room()
    # Invalid formats
    for start, end in [
        ("2026-03-02 09:00:00", "2026-03-02T10:00:00Z"),
        ("2026-03-02T09:00:00", "2026-03-02 10:00:00Z"),
        ("2026-03-02T09:00:00", "invalid-ts"),
    ]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": start, "end": end, "title": "Test", "attendees": 2
        })
        assert s == 422 and code(b) == "validation_error"

    # Minute alignment (must be 00, 15, 30, 45)
    for minute in ["09:10", "09:14", "09:16", "09:59"]:
        s, b, _ = api.book(r, minute, "10:00")
        assert s == 422 and code(b) == "validation_error", f"Minute {minute} should be invalid"

    # Seconds must be 00
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r,
        "start": "2026-03-02T09:00:01Z",
        "end": "2026-03-02T10:00:00Z",
        "title": "Test",
        "attendees": 2
    })
    assert s == 422 and code(b) == "validation_error"


def test_booking_duration_validation(api):
    r = api.room()
    # End <= start
    s, b, _ = api.book(r, "10:00", "10:00")
    assert s == 422 and code(b) == "validation_error"

    s, b, _ = api.book(r, "10:00", "09:00")
    assert s == 422 and code(b) == "validation_error"

    # Greater than 8 hours (> 8h, e.g. 09:00 to 17:15)
    s, b, _ = api.book(r, "09:00", "17:15")
    assert s == 422 and code(b) == "validation_error"

    # Exactly 8 hours is allowed (09:00 to 17:00)
    s, b, _ = api.book(r, "09:00", "17:00")
    assert s == 201


def test_booking_title_and_attendees_validation(api):
    r = api.room()
    # Title validation
    for title in ["", "   ", "\t", "a" * 101, 123, True, None]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": title, "attendees": 2
        })
        assert s == 422 and code(b) == "validation_error"

    # Attendees validation
    for attendees in [0, -1, 5.5, "2", None, True]:
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts("11:00"), "end": ts("12:00"), "title": "Sync", "attendees": attendees
        })
        assert s == 422 and code(b) == "validation_error"

    # Title trimming
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": ts("13:00"), "end": ts("14:00"), "title": "  Trimmed Title  ", "attendees": 2
    })
    assert s == 201 and b["title"] == "Trimmed Title"


def test_booking_check_order(api):
    # 1. Validation comes before room existence check
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": "r999", "start": "bad-ts", "end": ts("10:00"), "title": "Test", "attendees": 2
    })
    assert s == 422 and code(b) == "validation_error"

    # 2. Room existence comes before capacity / conflict check
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": "r999", "start": ts("09:00"), "end": ts("10:00"), "title": "Test", "attendees": 2
    })
    assert s == 404 and code(b) == "not_found"


def test_booking_ids_sequence_and_no_reuse(api):
    r = api.room()
    s1, b1, _ = api.book(r, "09:00", "10:00")
    s2, b2, _ = api.book(r, "10:15", "11:15")
    assert b1["id"] == "b1" and b2["id"] == "b2"

    api.call("DELETE", "/bookings/b1")
    s3, b3, _ = api.book(r, "11:30", "12:30")
    assert b3["id"] == "b3"  # b1 is never reused


def test_delete_booking_errors(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204

    # Deleting already deleted or unknown booking -> 404
    for bid in ["b1", "b999", "invalid"]:
        s, b, _ = api.call("DELETE", f"/bookings/{bid}")
        assert s == 404 and code(b) == "not_found"


def test_idempotency(api):
    r = api.room()
    body = {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Idem Test",
        "attendees": 2
    }
    headers = {"Idempotency-Key": "my-key-123"}

    # First request succeeds (201)
    s1, b1, _ = api.call("POST", "/bookings", body, headers=headers)
    assert s1 == 201

    # Second request with same key and equal body (different whitespace/key order parsed equal)
    body_reordered = {
        "attendees": 2,
        "title": "Idem Test",
        "end": ts("10:00"),
        "start": ts("09:00"),
        "room_id": r
    }
    s2, b2, _ = api.call("POST", "/bookings", body_reordered, headers=headers)
    assert s2 == 201
    assert b1 == b2

    # Third request with same key and different body -> 422 idempotency_mismatch
    body_diff = dict(body, title="Different Title")
    s3, b3, _ = api.call("POST", "/bookings", body_diff, headers=headers)
    assert s3 == 422 and code(b3) == "idempotency_mismatch"

    # Failed request is not remembered, key can be reused
    key_fail = "fail-key-999"
    fail_body = dict(body, start=ts("09:30"), end=ts("10:30"))  # overlaps b1
    s_fail, _, _ = api.call("POST", "/bookings", fail_body, headers={"Idempotency-Key": key_fail})
    assert s_fail == 409

    # Reuse key_fail with valid body should succeed (201)
    s_reuse, b_reuse, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": ts("14:00"), "end": ts("15:00"), "title": "Reuse", "attendees": 2
    }, headers={"Idempotency-Key": key_fail})
    assert s_reuse == 201


def test_list_bookings_filters_and_sorting(api):
    r1 = api.room("Room 1")
    r2 = api.room("Room 2")

    api.book(r1, "14:00", "15:00", title="C")
    api.book(r1, "09:00", "10:00", title="A")
    api.book(r1, "11:00", "12:00", title="B")
    api.book(r2, "10:00", "11:00", title="Other")

    # Filter by room_id
    s, b, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert s == 200 and [x["title"] for x in b["bookings"]] == ["A", "B", "C"]

    # Filter by unknown room_id gives empty list, not error
    s, b, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200 and b["bookings"] == [] and b["next_cursor"] is None

    # Filter by from / to (half-open window [from, to))
    s, b, _ = api.call("GET", f"/bookings?from={ts('10:00')}&to={ts('14:30')}")
    assert s == 200
    titles = {x["title"] for x in b["bookings"]}
    assert titles == {"B", "C", "Other"}

    # Invalid from/to/limit/cursor validation errors
    for param in ["from=bad", "to=bad", "limit=0", "limit=101", "limit=abc", "cursor=invalid"]:
        s, b, _ = api.call("GET", f"/bookings?{param}")
        assert s == 422 and code(b) == "validation_error"


def test_list_bookings_pagination(api):
    r = api.room()
    for h in range(8, 18):
        api.book(r, f"{h:02d}:00", f"{h:02d}:15", title=f"H{h}")

    # Limit = 3
    s, b, _ = api.call("GET", f"/bookings?room_id={r}&limit=3")
    assert s == 200
    page1 = b["bookings"]
    assert len(page1) == 3
    assert b["next_cursor"] is not None

    # Fetch next page using cursor
    s, b2, _ = api.call("GET", f"/bookings?room_id={r}&limit=3&cursor={b['next_cursor']}")
    assert s == 200
    page2 = b2["bookings"]
    assert len(page2) == 3

    all_titles = [x["title"] for x in page1 + page2]
    assert len(all_titles) == 6


def test_availability_calculation_and_edge_cases(api):
    r = api.room()
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


def test_availability_errors_and_check_order(api):
    r = api.room()
    for date_param in ["", "invalid-date", "2026-02-30", "2026-13-01"]:
        s, b, _ = api.call("GET", f"/rooms/{r}/availability?date={date_param}")
        assert s == 422 and code(b) == "validation_error"

    # Room check comes before date check: unknown room with invalid date -> 404 not_found
    s, b, _ = api.call("GET", "/rooms/r999/availability?date=invalid-date")
    assert s == 404 and code(b) == "not_found"
