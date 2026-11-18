"""Thorough pytest test suite for the bookings API spec (SPEC.md)."""
import json
from helpers import api, code, ts


def test_http_errors_and_methods(api):
    # Non-existent path -> 404 not_found
    s, b, h = api.call("GET", "/nonexistent")
    assert s == 404
    assert code(b) == "not_found"
    assert h["content-type"].startswith("application/json")

    # Unsupported method -> 405 method_not_allowed
    s, b, _ = api.call("PUT", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 405
    assert code(b) == "method_not_allowed"

    # Invalid JSON body (malformed) -> 400 bad_json
    s, b, _ = api.call("POST", "/rooms", raw=b"{invalid json")
    assert s == 400
    assert code(b) == "bad_json"

    # Valid JSON but not an object (e.g. array, number, string, boolean, null) -> 400 bad_json
    for non_obj in [[], 123, "string", True, None]:
        s, b, _ = api.call("POST", "/rooms", raw=json.dumps(non_obj).encode())
        assert s == 400
        assert code(b) == "bad_json"


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8, "unknown_field": "foo"})
    assert s == 201
    assert b["name"] == "Atlas"
    assert b["capacity"] == 8
    assert "unknown_field" not in b


def test_boolean_not_accepted_as_integer(api):
    # Capacity as boolean
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": True})
    assert s == 422
    assert code(b) == "validation_error"

    r = api.room(name="RoomBool")
    # Attendees as boolean
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r,
        "start": ts("09:00"),
        "end": ts("10:00"),
        "title": "Test",
        "attendees": True
    })
    assert s == 422
    assert code(b) == "validation_error"


def test_room_edge_cases(api):
    # Name trimming and length bounds
    s, b, _ = api.call("POST", "/rooms", {"name": "   ", "capacity": 8})
    assert s == 422 and code(b) == "validation_error"

    s, b, _ = api.call("POST", "/rooms", {"name": "x" * 51, "capacity": 8})
    assert s == 422 and code(b) == "validation_error"

    s, b, _ = api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 8})
    assert s == 201 and b["name"] == "x" * 50

    # Capacity bounds (1 to 500)
    s, b, _ = api.call("POST", "/rooms", {"name": "Cap0", "capacity": 0})
    assert s == 422 and code(b) == "validation_error"

    s, b, _ = api.call("POST", "/rooms", {"name": "Cap501", "capacity": 501})
    assert s == 422 and code(b) == "validation_error"

    # Duplicate room name case-insensitive and after trimming
    api.room(name="  Conference A  ", capacity=10)
    s, b, _ = api.call("POST", "/rooms", {"name": "conference a", "capacity": 5})
    assert s == 409 and code(b) == "room_exists"

    # Get single room and unknown room
    r_id = api.room(name="Meeting Room 1", capacity=20)
    s, b, _ = api.call("GET", f"/rooms/{r_id}")
    assert s == 200 and b["id"] == r_id and b["name"] == "Meeting Room 1"

    s, b, _ = api.call("GET", "/rooms/r999")
    assert s == 404 and code(b) == "not_found"


def test_booking_validation_checks(api):
    r = api.room(name="BookingTestRoom")

    # Invalid timestamp format
    s, b, _ = api.book(r, "2026-03-02 09:00:00", "10:00")
    assert s == 422 and code(b) == "validation_error"

    # Minute alignment (not 00, 15, 30, 45)
    s, b, _ = api.book(r, "09:01", "10:00")
    assert s == 422 and code(b) == "validation_error"

    # Seconds non-zero
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r,
        "start": "2026-03-02T09:00:05Z",
        "end": "2026-03-02T10:00:00Z",
        "title": "Test",
        "attendees": 2
    })
    assert s == 422 and code(b) == "validation_error"

    # End <= start
    s, b, _ = api.book(r, "10:00", "09:00")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.book(r, "09:00", "09:00")
    assert s == 422 and code(b) == "validation_error"

    # Duration > 8 hours
    s, b, _ = api.book(r, "08:00", "17:00") # 9 hours
    assert s == 422 and code(b) == "validation_error"

    # Exactly 8 hours allowed
    s, b, _ = api.book(r, "08:00", "16:00")
    assert s == 201

    # Title validation (1 to 100 chars, stored trimmed)
    r2 = api.room(name="Atlas2")
    s, b, _ = api.book(r2, "09:00", "10:00", title="   ")
    assert s == 422 and code(b) == "validation_error"

    s, b, _ = api.book(r2, "09:00", "10:00", title="  My Meeting  ")
    assert s == 201 and b["title"] == "My Meeting"

    # Attendees at least 1
    s, b, _ = api.book(r2, "11:00", "12:00", attendees=0)
    assert s == 422 and code(b) == "validation_error"


def test_booking_order_of_checks(api):
    # 1. Validation error before room check (room r999 does not exist, but validation fails first)
    s, b, _ = api.book("r999", "09:01", "10:00")
    assert s == 422 and code(b) == "validation_error"

    # 2. Room does not exist -> 404 not_found
    s, b, _ = api.book("r999", "09:00", "10:00")
    assert s == 404 and code(b) == "not_found"

    # 3. Over capacity -> 422 over_capacity
    r = api.room(name="CapRoom", capacity=2)
    s, b, _ = api.book(r, "09:00", "10:00", attendees=3)
    assert s == 422 and code(b) == "over_capacity"


def test_booking_ids_never_reused(api):
    r = api.room(name="IdRoom")
    b1_id = api.book(r, "09:00", "10:00")[1]["id"]
    api.call("DELETE", f"/bookings/{b1_id}")
    b2_id = api.book(r, "09:00", "10:00")[1]["id"]
    assert b1_id == "b1"
    assert b2_id == "b2"


def test_idempotency(api):
    r = api.room(name="IdemRoom")
    headers = {"Idempotency-Key": "key-123"}
    
    # First request succeeds
    s1, b1, _ = api.book(r, "09:00", "10:00", title="Meeting", attendees=2, headers=headers)
    assert s1 == 201

    # Second request with same key and equal body (different key order / whitespace in JSON payload)
    conn_body = json.dumps({"attendees": 2, "room_id": r, "end": ts("10:00"), "title": "Meeting", "start": ts("09:00")})
    s2, b2, _ = api.call("POST", "/bookings", raw=conn_body.encode(), headers=headers)
    assert s2 == 201
    assert b1 == b2

    # Third request with same key and different body -> 422 idempotency_mismatch
    s3, b3, _ = api.book(r, "10:00", "11:00", title="Different", headers=headers)
    assert s3 == 422 and code(b3) == "idempotency_mismatch"

    # Failed request is not remembered; key can be used again after failure
    headers_fail = {"Idempotency-Key": "fail-key"}
    # Conflict with existing 09:00-10:00 booking (attendees=2 <= room capacity 8)
    s_fail, b_fail, _ = api.book(r, "09:00", "10:00", attendees=2, headers=headers_fail)
    assert s_fail == 409

    # Reusing fail-key with valid body should succeed because failed requests are not remembered
    s_retry, b_retry, _ = api.book(r, "14:00", "15:00", headers=headers_fail)
    assert s_retry == 201


def test_get_and_delete_booking(api):
    r = api.room(name="DelRoom")
    b_id = api.book(r, "09:00", "10:00")[1]["id"]

    # GET existing
    s, b, _ = api.call("GET", f"/bookings/{b_id}")
    assert s == 200 and b["id"] == b_id

    # GET non-existent
    s, b, _ = api.call("GET", "/bookings/b999")
    assert s == 404 and code(b) == "not_found"

    # DELETE existing
    s, b, _ = api.call("DELETE", f"/bookings/{b_id}")
    assert s == 204 and b is None

    # DELETE already deleted
    s, b, _ = api.call("DELETE", f"/bookings/{b_id}")
    assert s == 404 and code(b) == "not_found"


def test_list_bookings_filters_and_pagination(api):
    r1 = api.room(name="Room List 1", capacity=10)
    r2 = api.room(name="Room List 2", capacity=10)

    api.book(r1, "09:00", "10:00", title="A")
    api.book(r1, "10:30", "11:30", title="B")
    api.book(r2, "09:30", "10:30", title="C")
    api.book(r1, "12:00", "13:00", title="D")

    # Filter by room_id
    s, b, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert s == 200
    assert [x["title"] for x in b["bookings"]] == ["A", "B", "D"]

    # Filter by unknown room_id gives empty list
    s, b, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200
    assert b["bookings"] == []

    # Filter by from / to (overlap half-open [from, to))
    s, b, _ = api.call("GET", f"/bookings?from={ts('09:45')}&to={ts('11:00')}")
    assert s == 200
    titles = {x["title"] for x in b["bookings"]}
    assert titles == {"A", "B", "C"}

    # Invalid from / to / limit / cursor
    for query in ["from=invalid", "to=invalid", "limit=0", "limit=101", "limit=abc", "cursor=invalid"]:
        s, b, _ = api.call(f"GET", f"/bookings?{query}")
        assert s == 422 and code(b) == "validation_error"

    # Pagination test with limit=2
    all_bookings = []
    cursor = None
    while True:
        url = "/bookings?limit=2" + (f"&cursor={cursor}" if cursor else "")
        s, b, _ = api.call("GET", url)
        assert s == 200
        all_bookings.extend(b["bookings"])
        cursor = b["next_cursor"]
        if cursor is None:
            break
    assert len(all_bookings) == 4
    assert [x["title"] for x in all_bookings] == ["A", "C", "B", "D"]


def test_availability_edge_cases(api):
    r = api.room(name="AvailRoom")

    # Empty bookings -> whole working day (08:00 to 18:00) free
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["date"] == "2026-03-02"
    assert b["free"] == [{"start": ts("08:00"), "end": ts("18:00")}]

    # Bookings, clipping outside working day, adjacent bookings
    api.book(r, "07:30", "09:00") # partly outside working day (07:30-08:00 clipped to 08:00)
    api.book(r, "09:00", "10:00") # adjacent to previous
    api.book(r, "17:00", "18:30") # partly outside end (18:00-18:30 clipped)

    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200
    assert b["free"] == [{"start": ts("10:00"), "end": ts("17:00")}]

    # Fully booked day -> empty list (working day 08:00 to 18:00 booked via 08:00-13:00 and 13:00-18:00)
    r_full = api.room(name="FullRoom")
    api.book(r_full, "08:00", "13:00")
    api.book(r_full, "13:00", "18:00")
    s, b, _ = api.call("GET", f"/rooms/{r_full}/availability?date=2026-03-02")
    assert s == 200
    assert b["free"] == []

    # Invalid date format or non-existent calendar date (e.g. 2026-02-30)
    for date_str in ["2026-02-30", "invalid", "03-02-2026"]:
        s, b, _ = api.call("GET", f"/rooms/{r}/availability?date={date_str}")
        assert s == 422 and code(b) == "validation_error"

    # Missing date -> 422 validation_error
    s, b, _ = api.call("GET", f"/rooms/{r}/availability")
    assert s == 422 and code(b) == "validation_error"

    # Room check comes before date check: unknown room with invalid date gives 404 not_found
    s, b, _ = api.call("GET", "/rooms/r999/availability?date=invalid")
    assert s == 404 and code(b) == "not_found"
