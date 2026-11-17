# DFL-HOLDOUT-CANARY-7c1e4b9a2f6d — held-out scenarios for bookings change request 1.
from helpers import api, code, ts  # noqa: F401


def test_buffer_default_zero_and_listed(api):
    api.room("A")
    api.room("B", buffer_minutes=30)
    rooms = api.call("GET", "/rooms")[1]["rooms"]
    assert [r["buffer_minutes"] for r in rooms] == [0, 30]
    s, b, _ = api.call("POST", "/rooms", {"name": "C", "capacity": 2})
    assert s == 201 and b["buffer_minutes"] == 0


def test_buffer_validation(api):
    for v in (-1, 61, "15", True, 2.5, None):
        s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": 2, "buffer_minutes": v})
        assert s == 422 and code(b) == "validation_error", v
    assert api.call("POST", "/rooms", {"name": "A", "capacity": 2, "buffer_minutes": 60})[0] == 201


def test_buffer_applies_before_and_after(api):
    r = api.room(buffer_minutes=30)
    assert api.book(r, "10:00", "11:00")[0] == 201
    for a, b_ in (("09:00", "09:45"), ("11:15", "12:00"), ("09:00", "10:00"), ("11:00", "12:00")):
        s, b, _ = api.book(r, a, b_)
        assert s == 409 and code(b) == "conflict", (a, b_)
    assert api.book(r, "09:00", "09:30")[0] == 201
    assert api.book(r, "11:30", "12:00")[0] == 201


def test_buffer_is_per_room(api):
    r1, r2 = api.room("A", buffer_minutes=30), api.room("B")
    assert api.book(r2, "09:00", "10:00")[0] == 201
    assert api.book(r2, "10:00", "11:00")[0] == 201
    assert api.book(r1, "09:00", "10:00")[0] == 201
    assert api.book(r1, "10:00", "11:00")[0] == 409


def test_availability_with_buffer(api):
    r = api.room(buffer_minutes=15)
    api.book(r, "10:00", "11:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:00"), "end": ts("09:45")}, {"start": ts("11:15"), "end": ts("18:00")}]


def test_availability_buffer_clipped_and_reaching_in(api):
    r = api.room(buffer_minutes=30)
    api.book(r, "07:00", "07:45")   # ends before the working day, buffer reaches to 08:15
    api.book(r, "17:00", "18:00")
    api.book(r, "18:15", "19:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:15"), "end": ts("16:30")}]


def test_availability_drops_short_gaps(api):
    r = api.room(buffer_minutes=10)
    api.book(r, "09:00", "10:00")
    api.book(r, "10:30", "11:00")   # gap 10:10-10:20 is 10 minutes -> not listed
    api.book(r, "11:45", "12:00")   # gap 11:10-11:35 is 25 minutes -> listed
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:00"), "end": ts("08:50")},
                         {"start": "2026-03-02T11:10:00Z", "end": "2026-03-02T11:35:00Z"},
                         {"start": "2026-03-02T12:10:00Z", "end": ts("18:00")}]


def test_patch_partial_fields(api):
    r = api.room()
    api.book(r, "09:00", "10:00", title="old", attendees=2)
    s, b, _ = api.call("PATCH", "/bookings/b1", {"title": "  new  "})
    assert s == 200 and b == {"id": "b1", "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "new", "attendees": 2}
    s, b, _ = api.call("PATCH", "/bookings/b1", {"end": ts("11:00")})
    assert s == 200 and b["start"] == ts("09:00") and b["end"] == ts("11:00")
    s, b, _ = api.call("PATCH", "/bookings/b1", {})
    assert s == 200 and b["title"] == "new" and b["end"] == ts("11:00")


def test_patch_onto_own_slot(api):
    r = api.room(buffer_minutes=30)
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("PATCH", "/bookings/b1", {"start": ts("09:15"), "end": ts("10:15")})
    assert s == 200 and b["start"] == ts("09:15")


def test_patch_conflict_and_buffer(api):
    r = api.room(buffer_minutes=15)
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")
    s, b, _ = api.call("PATCH", "/bookings/b2", {"start": ts("09:30"), "end": ts("10:30")})
    assert s == 409 and code(b) == "conflict"
    s, b, _ = api.call("PATCH", "/bookings/b2", {"start": ts("10:00")})
    assert s == 409 and code(b) == "conflict"
    assert api.call("GET", "/bookings/b2")[1]["start"] == ts("11:00")   # failed PATCH changed nothing
    assert api.call("PATCH", "/bookings/b2", {"start": ts("10:15")})[0] == 200


def test_patch_validation_and_capacity(api):
    r = api.room(capacity=2)
    api.book(r, "09:00", "10:00")
    for body in ({"start": ts("09:05")}, {"end": ts("08:00")}, {"title": " "}, {"attendees": 0}, {"attendees": "2"},
                 {"start": "2026-03-02 09:00"}, {"end": "2026-03-02T18:00:00Z"}):
        s, b, _ = api.call("PATCH", "/bookings/b1", body)
        assert s == 422 and code(b) == "validation_error", body
    s, b, _ = api.call("PATCH", "/bookings/b1", {"attendees": 3})
    assert s == 422 and code(b) == "over_capacity"
    got = api.call("GET", "/bookings/b1")[1]
    assert got["attendees"] == 2 and got["end"] == ts("10:00")


def test_patch_errors_order(api):
    r = api.room()
    s, b, _ = api.call("PATCH", "/bookings/b9", {"title": ""})
    assert s == 404 and code(b) == "not_found"
    s, b, _ = api.call("PATCH", "/bookings/b9", raw=b"nope")
    assert s == 400 and code(b) == "bad_json"
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("PATCH", "/bookings/b1", {"room_id": "r77", "color": "red"})
    assert s == 200 and b["room_id"] == r


def test_patch_frees_old_slot_and_keeps_order(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "12:00", "13:00")
    assert api.call("PATCH", "/bookings/b1", {"start": ts("12:00"), "end": ts("13:00")})[0] == 409
    assert api.call("PATCH", "/bookings/b1", {"start": ts("14:00"), "end": ts("15:00")})[0] == 200
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert [x["id"] for x in api.call("GET", "/bookings")[1]["bookings"]] == ["b3", "b2", "b1"]


def test_patch_then_delete_and_method_still_405_elsewhere(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("PATCH", "/bookings/b1", {"title": "t2"})[0] == 200
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert api.call("PATCH", "/bookings/b1", {"title": "t3"})[0] == 404
    s, b, _ = api.call("PATCH", "/rooms/r1", {"name": "x"})
    assert s == 405 and code(b) == "method_not_allowed"


def test_total_with_filters_and_pages(api):
    r1, r2 = api.room("A"), api.room("B")
    for h in (8, 9, 10, 11, 12):
        api.book(r1, f"{h:02d}:00", f"{h + 1:02d}:00")
    api.book(r2, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/bookings?room_id={r1}&limit=2")
    assert b["total"] == 5
    s, b2, _ = api.call("GET", f"/bookings?room_id={r1}&limit=2&cursor={b['next_cursor']}")
    assert b2["total"] == 5 and len(b2["bookings"]) == 2
    assert api.call("GET", "/bookings?from=2026-03-02T09:30:00Z&to=2026-03-02T10:30:00Z")[1]["total"] == 3
    assert api.call("GET", "/bookings?room_id=r99")[1]["total"] == 0
