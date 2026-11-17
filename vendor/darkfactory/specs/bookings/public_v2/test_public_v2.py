"""Public acceptance examples for bookings change request 1."""
from helpers import api, code, ts  # noqa: F401


def test_buffer_blocks_back_to_back(api):
    r = api.room(buffer_minutes=15)
    assert api.book(r, "09:00", "10:00")[0] == 201
    s, b, _ = api.book(r, "10:00", "11:00")
    assert s == 409 and code(b) == "conflict"
    assert api.book(r, "10:15", "11:00")[0] == 201


def test_room_shows_buffer(api):
    r = api.room(buffer_minutes=10)
    assert api.call("GET", f"/rooms/{r}")[1]["buffer_minutes"] == 10


def test_patch_moves_booking(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("PATCH", "/bookings/b1", {"start": ts("09:15"), "end": ts("10:15")})
    assert s == 200 and b["id"] == "b1" and b["start"] == ts("09:15") and b["end"] == ts("10:15")
    assert api.call("GET", "/bookings/b1")[1]["start"] == ts("09:15")


def test_list_total(api):
    r = api.room()
    for h in (9, 10, 11):
        api.book(r, f"{h:02d}:00", f"{h + 1:02d}:00")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    assert s == 200 and len(b["bookings"]) == 2 and b["total"] == 3
