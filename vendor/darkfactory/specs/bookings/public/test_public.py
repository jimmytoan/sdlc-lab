"""Public acceptance examples for the bookings API. The coding agent may read and run these."""
from helpers import api, code, ts  # noqa: F401


def test_create_and_list_rooms(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201 and b["id"] == "r1" and b["name"] == "Atlas" and b["capacity"] == 8
    assert h["content-type"].startswith("application/json")
    api.room("Borealis", 4)
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and [r["id"] for r in b["rooms"]] == ["r1", "r2"]


def test_room_validation(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "", "capacity": 8})
    assert s == 422 and code(b) == "validation_error"


def test_create_booking(api):
    r = api.room()
    s, b, _ = api.book(r, "09:00", "10:00", title="Planning", attendees=4)
    assert s == 201
    assert b == {"id": "b1", "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Planning", "attendees": 4}
    s, got, _ = api.call("GET", "/bookings/b1")
    assert s == 200 and got == b


def test_overlap_conflict(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    s, b, _ = api.book(r, "09:30", "10:30")
    assert s == 409 and code(b) == "conflict"


def test_back_to_back_allowed(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert api.book(r, "10:00", "11:00")[0] == 201


def test_over_capacity(api):
    r = api.room(capacity=2)
    s, b, _ = api.book(r, "09:00", "10:00", attendees=3)
    assert s == 422 and code(b) == "over_capacity"


def test_delete_frees_slot(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    assert api.book(r, "09:00", "10:00")[0] == 201


def test_list_sorted_by_start(api):
    r = api.room()
    api.book(r, "11:00", "12:00")
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and [x["id"] for x in b["bookings"]] == ["b2", "b1"] and b["next_cursor"] is None


def test_availability(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200 and b["date"] == "2026-03-02"
    assert b["free"] == [{"start": ts("08:00"), "end": ts("09:00")}, {"start": ts("10:00"), "end": ts("18:00")}]


def test_unknown_path(api):
    s, b, _ = api.call("GET", "/nope")
    assert s == 404 and code(b) == "not_found"
