"""Regression suite for the bookings API, written against SPEC.md.

Every test drives server.py as a black box over HTTP (see helpers.py).
"""
import pytest

from helpers import api, code, ts  # noqa: F401  (api is a pytest fixture)

DAY = "2026-03-02"


# ---------------------------------------------------------------- helpers

def raw_post(api, path, data, headers=None):
    """POST raw bytes (no JSON encoding) with a JSON content type."""
    return api.call("POST", path, raw=data, headers=headers)


def booking_ids(body):
    return [b["id"] for b in body["bookings"]]


def list_all(api, query=""):
    """Walk every page of GET /bookings and return all bookings in order."""
    out, cursor, pages = [], None, 0
    while True:
        q = [p for p in (query, f"cursor={cursor}" if cursor else "") if p]
        path = "/bookings" + ("?" + "&".join(q) if q else "")
        s, b, _ = api.call("GET", path)
        assert s == 200, b
        out.extend(b["bookings"])
        cursor = b["next_cursor"]
        pages += 1
        assert pages < 500, "pagination does not terminate"
        if cursor is None:
            return out


def free_of(api, room_id, date=DAY):
    s, b, _ = api.call("GET", f"/rooms/{room_id}/availability?date={date}")
    assert s == 200, b
    return [(f["start"], f["end"]) for f in b["free"]]


def iso(day_hhmm):
    return ts(day_hhmm)


VALID_BOOKING = {"room_id": "r1", "start": "2026-03-02T09:00:00Z", "end": "2026-03-02T10:00:00Z",
                 "title": "sync", "attendees": 2}


def booking_body(room_id, **over):
    body = dict(VALID_BOOKING, room_id=room_id)
    body.update(over)
    return body


# ---------------------------------------------------------------- startup / generic

def test_state_starts_empty(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


@pytest.mark.parametrize("method,path", [
    ("GET", "/nope"),
    ("POST", "/nope"),
    ("PUT", "/rooms/r1/nope"),
    ("GET", "/rooms/r1/availability/extra"),
    ("GET", "/bookings/b1/extra"),
    ("GET", "/"),
])
def test_unknown_path_is_404_not_found(api, method, path):
    s, b, h = api.call(method, path, body={} if method == "POST" else None)
    assert s == 404
    assert code(b) == "not_found"
    assert isinstance(b["error"]["message"], str)
    assert h["content-type"].startswith("application/json")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("POST", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("POST", "/rooms/r1/availability"),
    ("DELETE", "/rooms/r1/availability"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
])
def test_unsupported_method_is_405(api, method, path):
    # Create the room/booking so the paths named above genuinely exist.
    r = api.room()
    api.book(r, "09:00", "10:00")
    path = path.replace("r1", r)
    s, b, h = api.call(method, path, body={} if method in ("POST", "PUT", "PATCH") else None)
    assert s == 405, (method, path, s, b)
    assert code(b) == "method_not_allowed"
    assert h["content-type"].startswith("application/json")


@pytest.mark.parametrize("path,body_raw", [
    ("/rooms", b"{not json"),
    ("/rooms", b""),
    ("/rooms", b"[]"),
    ("/rooms", b'"Atlas"'),
    ("/rooms", b"42"),
    ("/rooms", b"null"),
    ("/bookings", b"{"),
    ("/bookings", b"[1,2]"),
    ("/bookings", b"true"),
    ("/bookings", b"\xff\xfe"),
])
def test_bad_json_is_400(api, path, body_raw):
    s, b, h = raw_post(api, path, body_raw)
    assert s == 400
    assert code(b) == "bad_json"
    assert h["content-type"].startswith("application/json")


def test_bad_json_checked_before_validation(api):
    # Not an object: bad_json wins over any validation of the (missing) fields.
    s, b, _ = raw_post(api, "/bookings", b"[]")
    assert s == 400 and code(b) == "bad_json"


def test_every_body_response_is_json_content_type(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    responses = [
        api.call("POST", "/rooms", {"name": "X", "capacity": 1}),
        api.call("POST", "/rooms", {"name": "X", "capacity": 1}),  # 409
        api.call("POST", "/rooms", {"name": "", "capacity": 1}),  # 422
        api.call("GET", "/rooms"),
        api.call("GET", f"/rooms/{r}"),
        api.call("GET", "/rooms/none"),
        api.call("GET", f"/rooms/{r}/availability?date={DAY}"),
        api.call("GET", "/bookings"),
        api.call("GET", "/bookings/b1"),
        api.call("GET", "/bookings/zz"),
        api.call("DELETE", "/bookings/zz"),
        api.call("GET", "/missing"),
        api.call("PUT", "/rooms"),
    ]
    for s, b, h in responses:
        if b is not None:
            assert h["content-type"].startswith("application/json"), (s, h)


def test_delete_204_has_no_body(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None


def test_error_bodies_have_code_and_message(api):
    s, b, _ = api.call("GET", "/rooms/nope")
    assert s == 404
    assert set(b.keys()) == {"error"}
    assert set(b["error"].keys()) >= {"code", "message"}
    assert isinstance(b["error"]["message"], str)


def test_unknown_fields_ignored_in_requests(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Zed", "capacity": 3, "colour": "red"})
    assert s == 201 and b == {"id": "r1", "name": "Zed", "capacity": 3}
    r = b["id"]
    s, b, _ = api.call("POST", "/bookings", dict(booking_body(r), extra={"x": 1}))
    assert s == 201
    assert set(b.keys()) == {"id", "room_id", "start", "end", "title", "attendees"}


# ---------------------------------------------------------------- rooms

def test_create_room_response_shape(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}
    assert h["content-type"].startswith("application/json")


def test_room_ids_sequential_in_creation_order(api):
    ids = [api.room(f"Room {i}", 1) for i in range(4)]
    assert ids == ["r1", "r2", "r3", "r4"]
    s, b, _ = api.call("GET", "/rooms")
    assert [r["id"] for r in b["rooms"]] == ids
    assert [r["name"] for r in b["rooms"]] == [f"Room {i}" for i in range(4)]


def test_get_room_by_id(api):
    r = api.room("Borealis", 4)
    s, b, _ = api.call("GET", f"/rooms/{r}")
    assert s == 200 and b == {"id": r, "name": "Borealis", "capacity": 4}


def test_get_unknown_room_404(api):
    s, b, _ = api.call("GET", "/rooms/r99")
    assert s == 404 and code(b) == "not_found"


@pytest.mark.parametrize("body", [
    {"capacity": 8},
    {"name": "Atlas"},
    {},
    {"name": None, "capacity": 8},
    {"name": 5, "capacity": 8},
    {"name": ["Atlas"], "capacity": 8},
    {"name": "Atlas", "capacity": None},
    {"name": "Atlas", "capacity": "8"},
    {"name": "Atlas", "capacity": 8.5},
    {"name": "Atlas", "capacity": True},
    {"name": "Atlas", "capacity": False},
    {"name": "Atlas", "capacity": 0},
    {"name": "Atlas", "capacity": -1},
    {"name": "Atlas", "capacity": 501},
    {"name": "", "capacity": 8},
    {"name": "   ", "capacity": 8},
    {"name": "x" * 51, "capacity": 8},
])
def test_room_validation_422(api, body):
    s, b, _ = api.call("POST", "/rooms", body)
    assert s == 422, (body, s, b)
    assert code(b) == "validation_error"
    assert api.call("GET", "/rooms")[1] == {"rooms": []}


@pytest.mark.parametrize("name,cap", [("x" * 50, 1), ("A", 1), ("  Trim  ", 500), ("é" * 50, 8),
                                      ("  " + "x" * 49 + "y  ", 3)])
def test_room_valid_boundaries(api, name, cap):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": cap})
    assert s == 201, (name, cap, b)
    assert b["capacity"] == cap
    assert b["name"] == name.strip()


def test_room_name_stored_trimmed(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  Atlas \t", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    s, b, _ = api.call("GET", "/rooms/r1")
    assert b["name"] == "Atlas"


def test_room_name_unique_ignoring_case_and_trim(api):
    api.room("Atlas", 8)
    for dup in ("atlas", "ATLAS", "  Atlas  ", "aTlAs"):
        s, b, _ = api.call("POST", "/rooms", {"name": dup, "capacity": 3})
        assert s == 409, dup
        assert code(b) == "room_exists"
    s, b, _ = api.call("GET", "/rooms")
    assert len(b["rooms"]) == 1


def test_failed_room_creation_does_not_consume_id(api):
    api.room("Atlas", 8)
    assert api.call("POST", "/rooms", {"name": "atlas", "capacity": 8})[0] == 409
    assert api.call("POST", "/rooms", {"name": "", "capacity": 8})[0] == 422
    s, b, _ = api.call("POST", "/rooms", {"name": "Borealis", "capacity": 8})
    assert s == 201 and b["id"] == "r2"


# ---------------------------------------------------------------- booking validation

def test_create_booking_response_shape(api):
    r = api.room()
    s, b, h = api.book(r, "09:00", "10:00", title="  Planning  ", attendees=4)
    assert s == 201
    assert b == {"id": "b1", "room_id": r, "start": iso("09:00"), "end": iso("10:00"),
                 "title": "Planning", "attendees": 4}
    assert h["content-type"].startswith("application/json")


def test_booking_ids_sequential(api):
    r = api.room()
    ids = [api.book(r, f"{h:02d}:00", f"{h:02d}:30")[1]["id"] for h in (8, 9, 10)]
    assert ids == ["b1", "b2", "b3"]


def test_get_booking_by_id(api):
    r = api.room()
    s, created, _ = api.book(r, "09:00", "10:00")
    s, got, _ = api.call("GET", f"/bookings/{created['id']}")
    assert s == 200 and got == created


def test_get_unknown_booking_404(api):
    s, b, _ = api.call("GET", "/bookings/b42")
    assert s == 404 and code(b) == "not_found"


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_missing_field_422(api, field):
    r = api.room()
    body = booking_body(r)
    del body[field]
    s, b, _ = api.call("POST", "/bookings", body)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("over", [
    {"room_id": 1},
    {"room_id": None},
    {"room_id": ["r1"]},
    {"title": 5},
    {"title": None},
    {"title": ["x"]},
    {"attendees": "2"},
    {"attendees": None},
    {"attendees": True},
    {"attendees": False},
    {"start": 1700000000},
    {"end": None},
])
def test_wrong_types_422(api, over):
    r = api.room()
    body = booking_body(r)
    body.update(over)
    s, b, _ = api.call("POST", "/bookings", body)
    assert s == 422, (over, b)
    assert code(b) == "validation_error"


@pytest.mark.parametrize("bad", [
    "2026-03-02 09:00:00Z",
    "2026-03-02T09:00:00",
    "2026-03-02T09:00:00+00:00",
    "2026-03-02T09:00:00.000Z",
    "2026-03-02T09:00Z",
    "2026-03-02T09:00:00z",
    "2026-03-02T9:00:00Z",
    "2026-3-02T09:00:00Z",
    "2026-02-30T09:00:00Z",
    "2026-02-29T09:00:00Z",  # 2026 is not a leap year
    "2026-03-02T24:00:00Z",
    "2026-03-02T09:60:00Z",
    "not a time",
    "",
])
def test_invalid_timestamp_422(api, bad):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, start=bad))
    assert s == 422 and code(b) == "validation_error", bad
    s, b, _ = api.call("POST", "/bookings", booking_body(r, end=bad))
    assert s == 422 and code(b) == "validation_error", bad


@pytest.mark.parametrize("t", [
    "2026-03-02T09:10:00Z",
    "2026-03-02T09:05:00Z",
    "2026-03-02T09:59:00Z",
    "2026-03-02T09:00:30Z",
    "2026-03-02T09:00:01Z",
    "2026-03-02T09:14:59Z",
])
def test_start_must_be_quarter_hour_aligned(api, t):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, start=t, end="2026-03-02T11:00:00Z"))
    assert s == 422 and code(b) == "validation_error", t


@pytest.mark.parametrize("t", [
    "2026-03-02T10:10:00Z",
    "2026-03-02T10:01:00Z",
    "2026-03-02T10:00:30Z",
])
def test_end_must_be_quarter_hour_aligned(api, t):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, end=t))
    assert s == 422 and code(b) == "validation_error", t


@pytest.mark.parametrize("start,end", [
    ("09:00", "09:00"),
    ("10:00", "09:00"),
    ("09:15", "09:00"),
    ("09:00", "08:45"),
])
def test_end_must_be_after_start(api, start, end):
    r = api.room()
    s, b, _ = api.book(r, start, end)
    assert s == 422 and code(b) == "validation_error"


def test_booking_exactly_eight_hours_allowed(api):
    r = api.room()
    s, b, _ = api.book(r, "08:00", "16:00")
    assert s == 201, b


def test_booking_longer_than_eight_hours_rejected(api):
    r = api.room()
    s, b, _ = api.book(r, "08:00", "16:15")
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.book(r, "00:00", "09:00")
    assert s == 422 and code(b) == "validation_error"


def test_booking_across_midnight_within_eight_hours(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(
        r, start="2026-03-02T22:00:00Z", end="2026-03-03T06:00:00Z"))
    assert s == 201, b


@pytest.mark.parametrize("title", ["", "   ", "\t \n", "x" * 101])
def test_title_length_422(api, title):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, title=title))
    assert s == 422 and code(b) == "validation_error"


def test_title_100_chars_ok_and_trimmed(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, title="  " + "t" * 100 + "  "))
    assert s == 201 and b["title"] == "t" * 100


def test_title_101_chars_after_trim_rejected(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, title=" " + "t" * 101 + " "))
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("attendees", [0, -1, -100])
def test_attendees_below_one_422(api, attendees):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", booking_body(r, attendees=attendees))
    assert s == 422 and code(b) == "validation_error"


def test_attendees_equal_capacity_allowed(api):
    r = api.room(capacity=5)
    s, b, _ = api.book(r, "09:00", "10:00", attendees=5)
    assert s == 201


def test_over_capacity(api):
    r = api.room(capacity=2)
    s, b, _ = api.book(r, "09:00", "10:00", attendees=3)
    assert s == 422 and code(b) == "over_capacity"


def test_over_capacity_creates_nothing(api):
    r = api.room(capacity=2)
    api.book(r, "09:00", "10:00", attendees=3)
    s, b, _ = api.call("GET", "/bookings")
    assert b["bookings"] == []


# ---------------------------------------------------------------- booking check order

def test_validation_precedes_room_lookup(api):
    s, b, _ = api.call("POST", "/bookings", booking_body("r404", attendees=0))
    assert s == 422 and code(b) == "validation_error"
    s, b, _ = api.call("POST", "/bookings", booking_body("r404", start="09:10"))
    assert s == 422 and code(b) == "validation_error"


def test_unknown_room_is_404(api):
    s, b, _ = api.call("POST", "/bookings", booking_body("r404"))
    assert s == 404 and code(b) == "not_found"


def test_room_lookup_precedes_capacity(api):
    s, b, _ = api.call("POST", "/bookings", booking_body("r404", attendees=400))
    assert s == 404 and code(b) == "not_found"


def test_over_capacity_precedes_conflict(api):
    r = api.room(capacity=2)
    assert api.book(r, "09:00", "10:00", attendees=1)[0] == 201
    s, b, _ = api.book(r, "09:00", "10:00", attendees=3)
    assert s == 422 and code(b) == "over_capacity"


def test_validation_precedes_conflict(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    s, b, _ = api.book(r, "09:00", "10:00", title="")
    assert s == 422 and code(b) == "validation_error"


def test_failed_booking_does_not_consume_id(api):
    r = api.room(capacity=2)
    api.book(r, "09:00", "10:00")
    assert api.book(r, "09:00", "10:00")[0] == 409
    assert api.book(r, "11:00", "12:00", attendees=9)[0] == 422
    assert api.book("r404", "11:00", "12:00")[0] == 404
    s, b, _ = api.book(r, "12:00", "13:00")
    assert s == 201 and b["id"] == "b2"


# ---------------------------------------------------------------- conflicts

def test_overlap_at_start_conflicts(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.book(r, "08:00", "09:15")
    assert s == 409 and code(b) == "conflict"


def test_overlap_at_end_conflicts(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.book(r, "09:45", "11:00")
    assert s == 409 and code(b) == "conflict"


def test_contained_booking_conflicts(api):
    r = api.room()
    api.book(r, "09:00", "12:00")
    s, b, _ = api.book(r, "10:00", "10:30")
    assert s == 409 and code(b) == "conflict"


def test_containing_booking_conflicts(api):
    r = api.room()
    api.book(r, "10:00", "10:30")
    s, b, _ = api.book(r, "09:00", "12:00")
    assert s == 409 and code(b) == "conflict"


def test_identical_slot_conflicts(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.book(r, "09:00", "10:00")
    assert s == 409 and code(b) == "conflict"


def test_back_to_back_allowed_both_sides(api):
    r = api.room()
    assert api.book(r, "10:00", "11:00")[0] == 201
    assert api.book(r, "09:00", "10:00")[0] == 201  # ends exactly when the other starts
    assert api.book(r, "11:00", "12:00")[0] == 201  # starts exactly when the other ends


def test_gap_between_bookings_is_free(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.book(r, "10:15", "10:30")[0] == 201


def test_same_slot_in_different_rooms_allowed(api):
    r1 = api.room("Atlas")
    r2 = api.room("Borealis")
    assert api.book(r1, "09:00", "10:00")[0] == 201
    assert api.book(r2, "09:00", "10:00")[0] == 201


def test_conflict_ignores_deleted_booking(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert api.book(r, "09:30", "10:30")[0] == 201


def test_conflict_checked_across_multiple_bookings(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    api.book(r, "11:00", "12:00")
    s, b, _ = api.book(r, "09:30", "11:30")
    assert s == 409 and code(b) == "conflict"


# ---------------------------------------------------------------- delete

def test_delete_then_get_is_404(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"


def test_delete_unknown_404(api):
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"


def test_delete_twice_second_is_404(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"


def test_delete_removes_from_list(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.call("GET", "/bookings")
    assert booking_ids(b) == ["b2"]


def test_ids_never_reused_after_delete(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:00", "10:00")
    assert s == 201 and b["id"] == "b2"
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 404


def test_ids_not_reused_after_deleting_latest(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:00")
    api.call("DELETE", "/bookings/b2")
    s, b, _ = api.book(r, "13:00", "14:00")
    assert b["id"] == "b3"


# ---------------------------------------------------------------- idempotency

def test_idempotent_replay_returns_original_and_creates_nothing(api):
    r = api.room()
    hdr = {"Idempotency-Key": "k-1"}
    s1, b1, _ = api.book(r, "09:00", "10:00", headers=hdr)
    assert s1 == 201
    s2, b2, _ = api.book(r, "09:00", "10:00", headers=hdr)
    assert s2 == 201 and b2 == b1
    s, lst, _ = api.call("GET", "/bookings")
    assert booking_ids(lst) == ["b1"]


def test_replay_does_not_consume_booking_id(api):
    r = api.room()
    hdr = {"Idempotency-Key": "k-1"}
    api.book(r, "09:00", "10:00", headers=hdr)
    api.book(r, "09:00", "10:00", headers=hdr)
    api.book(r, "09:00", "10:00", headers=hdr)
    s, b, _ = api.book(r, "11:00", "12:00")
    assert b["id"] == "b2"


def test_replay_ignores_json_key_order_and_whitespace(api):
    r = api.room()
    hdr = {"Idempotency-Key": "k-2"}
    s1, b1, _ = api.book(r, "09:00", "10:00", title="sync", attendees=2, headers=hdr)
    assert s1 == 201
    raw = (
        '{ "attendees" : 2,\n "title":"sync",  "end": "2026-03-02T10:00:00Z",'
        ' "start":"2026-03-02T09:00:00Z", "room_id" : "%s" }' % r
    ).encode()
    s2, b2, _ = raw_post(api, "/bookings", raw, headers=hdr)
    assert s2 == 201 and b2 == b1
    assert booking_ids(api.call("GET", "/bookings")[1]) == ["b1"]


def test_same_key_different_body_is_mismatch(api):
    r = api.room()
    hdr = {"Idempotency-Key": "k-3"}
    assert api.book(r, "09:00", "10:00", headers=hdr)[0] == 201
    s, b, _ = api.book(r, "09:00", "11:00", headers=hdr)
    assert s == 422 and code(b) == "idempotency_mismatch"
    s, b, _ = api.book(r, "09:00", "10:00", title="other", headers=hdr)
    assert s == 422 and code(b) == "idempotency_mismatch"
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1


def test_mismatch_when_body_differs_only_in_trimmed_title(api):
    # Equality is on the parsed request body, not on the stored (trimmed) form.
    r = api.room()
    hdr = {"Idempotency-Key": "k-3b"}
    assert api.book(r, "09:00", "10:00", title="sync", headers=hdr)[0] == 201
    s, b, _ = api.book(r, "09:00", "10:00", title=" sync ", headers=hdr)
    assert s == 422 and code(b) == "idempotency_mismatch"


def test_different_keys_same_body_create_two(api):
    r = api.room()
    s1, b1, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "a"})
    s2, b2, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "b"})
    # Second is a conflict on the same slot, not a replay of the first.
    assert s1 == 201 and s2 == 409 and code(b2) == "conflict"


def test_same_body_without_key_is_not_a_replay(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    s, b, _ = api.book(r, "09:00", "10:00")
    assert s == 409 and code(b) == "conflict"


def test_replay_after_delete_returns_original_without_recreating(api):
    r = api.room()
    hdr = {"Idempotency-Key": "k-del"}
    s1, b1, _ = api.book(r, "09:00", "10:00", headers=hdr)
    api.call("DELETE", f"/bookings/{b1['id']}")
    s2, b2, _ = api.book(r, "09:00", "10:00", headers=hdr)
    assert s2 == 201 and b2 == b1
    s, lst, _ = api.call("GET", "/bookings")
    assert lst["bookings"] == []


def test_failed_request_with_key_is_not_remembered(api):
    r = api.room(capacity=2)
    hdr = {"Idempotency-Key": "k-fail"}
    # 1. validation failure
    assert api.book(r, "09:00", "10:00", attendees=0, headers=hdr)[0] == 422
    # 2. then the same key with a valid body must succeed
    s, b, _ = api.book(r, "09:00", "10:00", headers=hdr)
    assert s == 201 and b["id"] == "b1"


def test_failed_over_capacity_with_key_not_remembered(api):
    r = api.room(capacity=2)
    hdr = {"Idempotency-Key": "k-cap"}
    assert api.book(r, "09:00", "10:00", attendees=3, headers=hdr)[0] == 422
    s, b, _ = api.book(r, "09:00", "10:00", attendees=2, headers=hdr)
    assert s == 201


def test_failed_conflict_with_key_not_remembered(api):
    r = api.room()
    api.book(r, "09:00", "10:00")  # b1, no key
    hdr = {"Idempotency-Key": "k-conflict"}
    assert api.book(r, "09:30", "10:30", headers=hdr)[0] == 409
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:30", "10:30", headers=hdr)
    assert s == 201 and b["id"] == "b2"


def test_failed_unknown_room_with_key_not_remembered(api):
    hdr = {"Idempotency-Key": "k-room"}
    s, b, _ = api.call("POST", "/bookings", booking_body("r1"), headers=hdr)
    assert s == 404
    r = api.room()
    assert r == "r1"
    s, b, _ = api.call("POST", "/bookings", booking_body("r1"), headers=hdr)
    assert s == 201


def test_key_with_failed_then_success_then_mismatch(api):
    r = api.room()
    hdr = {"Idempotency-Key": "seq"}
    assert api.book(r, "09:00", "10:00", attendees=0, headers=hdr)[0] == 422
    assert api.book(r, "09:00", "10:00", headers=hdr)[0] == 201
    s, b, _ = api.book(r, "12:00", "13:00", headers=hdr)
    assert s == 422 and code(b) == "idempotency_mismatch"


def test_empty_idempotency_key_is_not_a_key(api):
    # The spec requires a non-empty key; an empty header must not make requests replay.
    r = api.room()
    s1, _, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": ""})
    assert s1 == 201
    s2, b2, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": ""})
    assert s2 == 409 and code(b2) == "conflict"


def test_idempotency_key_is_case_sensitive_value(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "AbC"})[0] == 201
    s, b, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "abc"})
    assert s == 409 and code(b) == "conflict"


# ---------------------------------------------------------------- list bookings

def test_list_bookings_shape(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, h = api.call("GET", "/bookings")
    assert s == 200
    assert set(b.keys()) == {"bookings", "next_cursor"}
    assert b["next_cursor"] is None
    assert b["bookings"] == [{"id": "b1", "room_id": r, "start": iso("09:00"), "end": iso("10:00"),
                              "title": "sync", "attendees": 2}]


def test_list_sorted_by_start_then_creation(api):
    a = api.room("Atlas")
    bq = api.room("Borealis")
    api.book(a, "11:00", "12:00")          # b1
    api.book(bq, "09:00", "10:00")         # b2
    api.book(a, "09:00", "10:00")          # b3, same start as b2, created later
    api.book(bq, "10:00", "11:00")         # b4
    s, b, _ = api.call("GET", "/bookings")
    assert booking_ids(b) == ["b2", "b3", "b4", "b1"]


def test_list_filter_room_id(api):
    a = api.room("Atlas")
    bq = api.room("Borealis")
    api.book(a, "09:00", "10:00")
    api.book(bq, "09:00", "10:00")
    api.book(a, "11:00", "12:00")
    s, b, _ = api.call("GET", f"/bookings?room_id={a}")
    assert s == 200 and booking_ids(b) == ["b1", "b3"]
    assert all(x["room_id"] == a for x in b["bookings"])


def test_list_filter_unknown_room_is_empty_200(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_filter_window_half_open(api):
    r = api.room()
    api.book(r, "09:00", "10:00")  # b1
    api.book(r, "10:00", "11:00")  # b2
    api.book(r, "11:00", "12:00")  # b3

    def ids(q):
        s, b, _ = api.call("GET", "/bookings?" + q)
        assert s == 200, b
        return booking_ids(b)

    # window [10:00, 11:00): overlaps b2 only; b1 ends exactly at from, b3 starts exactly at to
    assert ids(f"from={iso('10:00')}&to={iso('11:00')}") == ["b2"]
    # from equal to a booking's end excludes it
    assert ids(f"from={iso('10:00')}") == ["b2", "b3"]
    # to equal to a booking's start excludes it
    assert ids(f"to={iso('10:00')}") == ["b1"]


def test_list_filter_window_partial_overlap_included(api):
    r = api.room()
    api.book(r, "09:00", "10:00")  # b1
    assert booking_ids(api.call("GET", "/bookings?from=2026-03-02T09:59:30Z")[1]) == ["b1"]
    assert booking_ids(api.call("GET", "/bookings?to=2026-03-02T09:00:30Z")[1]) == ["b1"]
    assert booking_ids(api.call("GET", "/bookings?from=2026-03-02T10:00:00Z")[1]) == []


def test_list_filter_window_unaligned_timestamps_allowed(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings?from=2026-03-02T09:30:17Z&to=2026-03-02T09:30:18Z")
    assert s == 200 and booking_ids(b) == ["b1"]


@pytest.mark.parametrize("q", [
    "from=2026-03-02",
    "from=2026-03-02T09:00:00",
    "from=2026-03-02%2009:00:00Z",
    "from=nonsense",
    "from=",
    "to=2026-02-30T00:00:00Z",
    "to=2026-03-02T25:00:00Z",
    "to=yesterday",
])
def test_list_invalid_window_422(api, q):
    s, b, _ = api.call("GET", "/bookings?" + q)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("q", ["limit=0", "limit=101", "limit=-1", "limit=abc", "limit=", "limit=2.5"])
def test_list_invalid_limit_422(api, q):
    s, b, _ = api.call("GET", "/bookings?" + q)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("cursor", ["garbage", "c", "c2026-03-02T09:00:00Z_x", "0"])
def test_list_invalid_cursor_422(api, cursor):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/bookings?cursor={cursor}")
    assert s == 422 and code(b) == "validation_error"


def test_list_limit_boundaries_accepted(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("GET", "/bookings?limit=1")[0] == 200
    assert api.call("GET", "/bookings?limit=100")[0] == 200


def test_list_default_page_size_20_with_cursor(api):
    for i in range(25):
        api.room(f"Room {i}", 2)
    for i in range(25):
        assert api.book(f"r{i + 1}", "09:00", "10:00")[0] == 201
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200
    assert len(b["bookings"]) == 20
    assert b["next_cursor"] is not None
    s, b2, _ = api.call("GET", "/bookings?cursor=" + b["next_cursor"])
    assert s == 200 and len(b2["bookings"]) == 5 and b2["next_cursor"] is None
    got = booking_ids(b) + booking_ids(b2)
    assert got == [f"b{i}" for i in range(1, 26)]


def test_list_walk_pages_returns_each_once(api):
    rooms = [api.room(f"Room {i}", 2) for i in range(6)]
    # Many bookings sharing the same start to stress the cursor tie-break.
    for rid in rooms:
        api.book(rid, "09:00", "10:00")
        api.book(rid, "10:00", "11:00")
    created = [f"b{i}" for i in range(1, 13)]
    seen = []
    s, b, _ = api.call("GET", "/bookings?limit=1")
    for _ in range(50):
        assert s == 200
        seen.extend(booking_ids(b))
        if b["next_cursor"] is None:
            break
        s, b, _ = api.call("GET", "/bookings?limit=1&cursor=" + b["next_cursor"])
    else:
        pytest.fail("pagination did not terminate")
    assert sorted(seen) == sorted(created)
    assert len(seen) == len(set(seen)) == 12
    # order: by start, then creation
    starts = [api.call("GET", f"/bookings/{i}")[1]["start"] for i in seen]
    assert starts == sorted(starts)


def test_list_walk_with_filters_and_small_pages(api):
    a = api.room("Atlas")
    bq = api.room("Borealis")
    for h in range(8, 16):
        api.book(a, f"{h:02d}:00", f"{h:02d}:30")
        api.book(bq, f"{h:02d}:00", f"{h:02d}:30")
    got = list_all(api, f"room_id={a}&limit=3")
    assert [x["room_id"] for x in got] == [a] * 8
    assert [x["id"] for x in got] == [f"b{2 * i + 1}" for i in range(8)]


def test_list_walk_with_window_filter(api):
    r = api.room()
    for h in range(8, 18):
        api.book(r, f"{h:02d}:00", f"{h:02d}:15")
    window = f"from={iso('10:00')}&to={iso('15:00')}"
    got = list_all(api, window + "&limit=2")
    assert [x["start"] for x in got] == [iso(f"{h:02d}:00") for h in range(10, 15)]


def test_list_cursor_past_end_gives_empty_last_page(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    s, b, _ = api.call("GET", "/bookings?limit=1")
    assert booking_ids(b) == ["b1"] and b["next_cursor"]
    s, b2, _ = api.call("GET", "/bookings?limit=1&cursor=" + b["next_cursor"])
    assert booking_ids(b2) == ["b2"] and b2["next_cursor"] is None


def test_list_respects_delete_between_pages(api):
    r = api.room()
    for h in range(8, 12):
        api.book(r, f"{h:02d}:00", f"{h:02d}:30")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    assert booking_ids(b) == ["b1", "b2"]
    api.call("DELETE", "/bookings/b3")
    s, b2, _ = api.call("GET", "/bookings?limit=2&cursor=" + b["next_cursor"])
    assert booking_ids(b2) == ["b4"] and b2["next_cursor"] is None


def test_list_empty_result_has_null_cursor(api):
    s, b, _ = api.call("GET", "/bookings?room_id=none")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


# ---------------------------------------------------------------- availability

def test_availability_empty_room_full_day(api):
    r = api.room()
    s, b, h = api.call("GET", f"/rooms/{r}/availability?date={DAY}")
    assert s == 200
    assert b == {"date": DAY, "free": [{"start": iso("08:00"), "end": iso("18:00")}]}
    assert h["content-type"].startswith("application/json")


def test_availability_gaps_between_bookings(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "11:00", "12:30")
    assert free_of(api, r) == [
        (iso("08:00"), iso("09:00")),
        (iso("10:00"), iso("11:00")),
        (iso("12:30"), iso("18:00")),
    ]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    assert free_of(api, r) == [(iso("08:00"), iso("09:00")), (iso("11:00"), iso("18:00"))]


def test_availability_booking_partly_before_day_clipped(api):
    r = api.room()
    api.book(r, "07:00", "09:00")
    assert free_of(api, r) == [(iso("09:00"), iso("18:00"))]


def test_availability_booking_partly_after_day_clipped(api):
    r = api.room()
    api.book(r, "17:30", "18:30")
    assert free_of(api, r) == [(iso("08:00"), iso("17:30"))]


def test_availability_booking_outside_day_ignored(api):
    r = api.room()
    api.book(r, "06:00", "07:00")
    api.book(r, "18:00", "19:00")
    assert free_of(api, r) == [(iso("08:00"), iso("18:00"))]


def test_availability_fully_booked_day_is_empty(api):
    r = api.room()
    api.book(r, "08:00", "16:00")
    api.book(r, "16:00", "18:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date={DAY}")
    assert s == 200 and b == {"date": DAY, "free": []}


def test_availability_booking_ending_at_day_start_leaves_day_free(api):
    r = api.room()
    api.book(r, "00:00", "08:00")  # ends exactly at the start of the working day
    assert free_of(api, r) == [(iso("08:00"), iso("18:00"))]


def test_availability_ignores_other_rooms(api):
    a = api.room("Atlas")
    bq = api.room("Borealis")
    api.book(bq, "09:00", "10:00")
    assert free_of(api, a) == [(iso("08:00"), iso("18:00"))]


def test_availability_is_per_date(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    other = "2026-03-03"
    assert free_of(api, r, date=other) == [("2026-03-03T08:00:00Z", "2026-03-03T18:00:00Z")]


def test_availability_booking_on_other_day_does_not_count(api):
    r = api.room()
    api.call("POST", "/bookings", booking_body(r, start="2026-03-03T09:00:00Z", end="2026-03-03T10:00:00Z"))
    assert free_of(api, r) == [(iso("08:00"), iso("18:00"))]


def test_availability_leap_day_valid(api):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2024-02-29")
    assert s == 200 and b["date"] == "2024-02-29"


def test_availability_free_intervals_ascending_and_disjoint(api):
    r = api.room()
    for h in (13, 9, 11, 15):
        api.book(r, f"{h:02d}:00", f"{h:02d}:30")
    free = free_of(api, r)
    assert free == sorted(free)
    for (s1, e1), (s2, e2) in zip(free, free[1:]):
        assert e1 < s2


@pytest.mark.parametrize("q", [
    "",
    "?date=",
    "?date=2026-02-30",
    "?date=2026-13-01",
    "?date=2026-02-29",
    "?date=2026-3-2",
    "?date=20260302",
    "?date=03/02/2026",
    "?date=2026-03-02T00:00:00Z",
    "?date=tomorrow",
    "?day=2026-03-02",
])
def test_availability_invalid_date_422(api, q):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability{q}")
    assert s == 422 and code(b) == "validation_error"


def test_availability_unknown_room_404(api):
    s, b, _ = api.call("GET", f"/rooms/r9/availability?date={DAY}")
    assert s == 404 and code(b) == "not_found"


def test_availability_room_check_precedes_date_check(api):
    s, b, _ = api.call("GET", "/rooms/r9/availability?date=bogus")
    assert s == 404 and code(b) == "not_found"
    s, b, _ = api.call("GET", "/rooms/r9/availability")
    assert s == 404 and code(b) == "not_found"


# ---------------------------------------------------------------- end-to-end

def test_end_to_end_flow(api):
    r = api.room("Atlas", 6)
    s, b1, _ = api.book(r, "09:00", "10:00", title=" Standup ", attendees=6, headers={"Idempotency-Key": "e2e"})
    assert s == 201 and b1["title"] == "Standup"
    assert api.book(r, "09:30", "10:30")[0] == 409
    assert api.book(r, "10:00", "11:00", attendees=7)[0] == 422
    s, again, _ = api.book(r, "09:00", "10:00", title=" Standup ", attendees=6, headers={"Idempotency-Key": "e2e"})
    assert again == b1
    s, b2, _ = api.book(r, "10:00", "11:00")
    assert s == 201 and b2["id"] == "b2"
    assert free_of(api, r) == [(iso("08:00"), iso("09:00")), (iso("11:00"), iso("18:00"))]
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert free_of(api, r) == [(iso("08:00"), iso("10:00")), (iso("11:00"), iso("18:00"))]
    s, lst, _ = api.call("GET", f"/bookings?room_id={r}")
    assert booking_ids(lst) == ["b2"]


def test_availability_short_quarter_hour_gap_is_reported(api):
    r = api.room()
    api.book(r, "09:00", "09:30")
    api.book(r, "09:45", "10:00")
    assert free_of(api, r) == [
        (iso("08:00"), iso("09:00")),
        (iso("09:30"), iso("09:45")),
        (iso("10:00"), iso("18:00")),
    ]
