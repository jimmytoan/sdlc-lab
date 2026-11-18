"""Thorough test suite for bookings API agent/implementation."""
import json
from helpers import api, code, ts


def test_error_response_structure_and_content_type(api):
    s, b, h = api.call("GET", "/nonexistent")
    assert s == 404
    assert h["content-type"].startswith("application/json")
    assert b == {"error": {"code": "not_found", "message": "not_found"}}


def test_unsupported_methods(api):
    r = api.room()
    s, b, _ = api.call("POST", f"/rooms/{r}")
    assert s == 405 and code(b) == "method_not_allowed"

    s, b, _ = api.call("DELETE", "/rooms")
    assert s == 405 and code(b) == "method_not_allowed"


def test_bad_json_and_types(api):
    # Invalid JSON
    s, b, _ = api.call("POST", "/rooms", raw=b"invalid json")
    assert s == 400 and code(b) == "bad_json"

    # Valid JSON but not an object (array)
    s, b, _ = api.call("POST", "/rooms", raw=b"[1, 2, 3]")
    assert s == 400 and code(b) == "bad_json"

    # JSON boolean where integer required
    s, b, _ = api.call("POST", "/rooms", {"name": "Test", "capacity": True})
    assert s == 422 and code(b) == "validation_error"


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 10, "unknown_field": "foo"})
    assert s == 201
    assert b["name"] == "Atlas"
    assert "unknown_field" not in b


def test_room_name_and_capacity_validation(api):
    # Name validation
    for bad_name in ("", "   ", "a" * 51, 123, None):
        s, b, _ = api.call("POST", "/rooms", {"name": bad_name, "capacity": 10})
        assert s == 422 and code(b) == "validation_error"

    # Capacity validation
    for bad_cap in (0, 501, -1, 10.5, "10", None):
        s, b, _ = api.call("POST", "/rooms", {"name": "Room", "capacity": bad_cap})
        assert s == 422 and code(b) == "validation_error"

    # Trimming and case-insensitive duplicate room names
    s, b, _ = api.call("POST", "/rooms", {"name": "  Meeting Room A  ", "capacity": 5})
    assert s == 201 and b["name"] == "Meeting Room A"

    s, b, _ = api.call("POST", "/rooms", {"name": "meeting room a", "capacity": 5})
    assert s == 409 and code(b) == "room_exists"


def test_get_room_by_id(api):
    r1 = api.room("Alpha", 10)
    s, b, _ = api.call("GET", f"/rooms/{r1}")
    assert s == 200 and b["id"] == r1 and b["name"] == "Alpha"

    s, b, _ = api.call("GET", "/rooms/r999")
    assert s == 404 and code(b) == "not_found"


def test_booking_timestamp_validation(api):
    r = api.room()
    # Invalid timestamp formats
    for bad_ts in ("2026-03-02 09:00:00Z", "2026-03-02T09:00:00", "2026/03/02T09:00:00Z", "bad"):
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": bad_ts, "end": ts("10:00"), "title": "Test", "attendees": 2
        })
        assert s == 422 and code(b) == "validation_error"


def test_booking_alignment_and_duration_validation(api):
    r = api.room()
    # Unaligned minutes (not 00, 15, 30, 45)
    s, b, _ = api.book(r, "09:10", "10:00")
    assert s == 422 and code(b) == "validation_error"

    # Non-zero seconds
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": "2026-03-02T09:00:01Z", "end": ts("10:00"), "title": "Test", "attendees": 2
    })
    assert s == 422 and code(b) == "validation_error"

    # End before or equal to start
    s, b, _ = api.book(r, "10:00", "09:00")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.book(r, "09:00", "09:00")
    assert s == 422 and code(b) == "validation_error"

    # Duration > 8 hours
    s, b, _ = api.book(r, "09:00", "17:15") # 8h 15m
    assert s == 422 and code(b) == "validation_error"


def test_booking_title_and_attendees_validation(api):
    r = api.room(capacity=10)
    # Title validation
    for bad_title in ("", "   ", "a" * 101, None, 123):
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": bad_title, "attendees": 2
        })
        assert s == 422 and code(b) == "validation_error"

    # Attendees validation
    for bad_att in (0, -1, "2", True, None):
        s, b, _ = api.call("POST", "/bookings", {
            "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Test", "attendees": bad_att
        })
        assert s == 422 and code(b) == "validation_error"


def test_booking_room_check_order(api):
    # Room check comes before over capacity and conflict (unknown room gives 404, not 422/409)
    s, b, _ = api.book("r999", "09:00", "10:00", attendees=999)
    assert s == 404 and code(b) == "not_found"


def test_booking_ids_not_reused(api):
    r = api.room()
    s1, b1, _ = api.book(r, "09:00", "10:00")
    b1_id = b1["id"]
    api.call("DELETE", f"/bookings/{b1_id}")

    s2, b2, _ = api.book(r, "10:00", "11:00")
    assert b2["id"] != b1_id
    assert b2["id"] == "b2"


def test_idempotency(api):
    r = api.room()
    body = {"room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Meeting", "attendees": 2}
    headers = {"Idempotency-Key": "key-123"}

    s1, resp1, _ = api.call("POST", "/bookings", body, headers=headers)
    assert s1 == 201

    # Same key, equal body (different dict key order in json representation or spacing)
    body_reordered = {"attendees": 2, "title": "Meeting", "room_id": r, "end": ts("10:00"), "start": ts("09:00")}
    s2, resp2, _ = api.call("POST", "/bookings", body_reordered, headers=headers)
    assert s2 == 201
    assert resp2 == resp1

    # Same key, different body -> idempotency_mismatch (422)
    body_diff = {**body, "title": "Different"}
    s3, resp3, _ = api.call("POST", "/bookings", body_diff, headers=headers)
    assert s3 == 422 and code(resp3) == "idempotency_mismatch"

    # Failed request is not remembered -> can reuse key with valid body if previous was failed?
    headers_fail = {"Idempotency-Key": "key-fail"}
    body_fail = {"room_id": r, "start": ts("12:00"), "end": ts("13:00"), "title": "Fail", "attendees": 999}
    s_fail, _, _ = api.call("POST", "/bookings", body_fail, headers=headers_fail) # over capacity / fail
    assert s_fail == 422

    body_success = {"room_id": r, "start": ts("12:00"), "end": ts("13:00"), "title": "Success", "attendees": 2}
    s_retry, resp_retry, _ = api.call("POST", "/bookings", body_success, headers=headers_fail)
    assert s_retry == 201


def test_get_and_delete_booking_not_found(api):
    s, b, _ = api.call("GET", "/bookings/b999")
    assert s == 404 and code(b) == "not_found"

    s, b, _ = api.call("DELETE", "/bookings/b999")
    assert s == 404 and code(b) == "not_found"


def test_list_bookings_filters_and_pagination(api):
    r1 = api.room("Room 1", 10)
    r2 = api.room("Room 2", 10)

    api.book(r1, "09:00", "10:00", title="B1")
    api.book(r1, "10:30", "11:30", title="B2")
    api.book(r2, "09:30", "10:30", title="B3")

    # Filter by room_id
    s, b, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert s == 200 and len(b["bookings"]) == 2
    assert [x["title"] for x in b["bookings"]] == ["B1", "B2"]

    # Unknown room_id -> empty list, not error
    s, b, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200 and b["bookings"] == [] and b["next_cursor"] is None

    # Filter by from / to
    s, b, _ = api.call("GET", f"/bookings?from={ts('10:00')}")
    assert s == 200 and len(b["bookings"]) == 2  # B2 (10:30-11:30) and B3 (09:30-10:30 overlaps from 10:00)

    # Invalid limit or from/to/cursor -> 422
    s, b, _ = api.call("GET", "/bookings?limit=0")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.call("GET", "/bookings?limit=101")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.call("GET", "/bookings?from=invalid")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.call("GET", "/bookings?cursor=invalid")
    assert s == 422 and code(b) == "validation_error"

    # Pagination with limit=1
    s, page1, _ = api.call("GET", f"/bookings?limit=1")
    assert s == 200 and len(page1["bookings"]) == 1 and page1["next_cursor"] is not None

    cursor = page1["next_cursor"]
    s, page2, _ = api.call("GET", f"/bookings?limit=1&cursor={cursor}")
    assert s == 200 and len(page2["bookings"]) == 1


def test_availability_edge_cases(api):
    r = api.room()
    # Book 09:00-10:00 and 11:00-12:00
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")

    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["date"] == "2026-03-02"
    assert b["free"] == [
        {"start": ts("08:00"), "end": ts("09:00")},
        {"start": ts("10:00"), "end": ts("11:00")},
        {"start": ts("12:00"), "end": ts("18:00")}
    ]

    # Invalid or missing date -> 422
    s, resp, _ = api.call("GET", f"/rooms/{r}/availability?date=bad-date")
    assert s == 422 and code(resp) == "validation_error"

    s, resp, _ = api.call("GET", f"/rooms/{r}/availability")
    assert s == 422 and code(resp) == "validation_error"

    # Room check comes before date check (unknown room + invalid date -> 404 not_found)
    s, resp, _ = api.call("GET", "/rooms/r999/availability?date=bad-date")
    assert s == 404 and code(resp) == "not_found"
