"""Regression suite for the bookings API, written against SPEC.md.

Every test drives server.py as a black box over HTTP (via the `api` fixture in helpers.py),
so each test gets a fresh server with empty in-memory state.
"""
import pytest

from helpers import api, code, ts  # noqa: F401  (api is a fixture)

DAY = "2026-03-02"
ERROR_CODES = {"not_found", "method_not_allowed", "bad_json", "validation_error",
               "room_exists", "over_capacity", "conflict", "idempotency_mismatch"}
BOOKING_FIELDS = {"id", "room_id", "start", "end", "title", "attendees"}
ROOM_FIELDS = {"id", "name", "capacity"}


# ---------------------------------------------------------------- helpers

def at(day, hhmm):
    """Full timestamp on the given day of March 2026, e.g. at(3, '09:00') -> '2026-03-03T09:00:00Z'."""
    return f"2026-03-{day:02d}T{hhmm}:00Z"


def assert_error(resp, status, err_code):
    s, body, headers = resp
    assert s == status, (s, body)
    assert body is not None and set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message"}, body
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["code"] == err_code, body
    assert headers["content-type"].startswith("application/json")


def post_booking_raw(api, raw, headers=None):
    return api.call("POST", "/bookings", raw=raw, headers=headers)


def valid_booking(room_id, start=None, end=None, title="Planning", attendees=2):
    return {"room_id": room_id,
            "start": start if start is not None else at(2, "09:00"),
            "end": end if end is not None else at(2, "10:00"),
            "title": title, "attendees": attendees}


def create_booking_ok(api, room_id, start, end, **kw):
    s, b, _ = api.call("POST", "/bookings", valid_booking(room_id, start, end, **kw))
    assert s == 201, (s, b)
    return b


def list_all(api, query=""):
    """Walk every page of GET /bookings and return all items in the order received."""
    out, cursor = [], None
    while True:
        q = [p for p in (query, f"cursor={cursor}" if cursor else "") if p]
        path = "/bookings" + ("?" + "&".join(q) if q else "")
        s, b, _ = api.call("GET", path)
        assert s == 200, b
        out.extend(b["bookings"])
        cursor = b["next_cursor"]
        if cursor is None:
            return out


# ---------------------------------------------------------------- general / errors

def test_initial_state_is_empty(api):
    assert api.call("GET", "/rooms")[1] == {"rooms": []}
    assert api.call("GET", "/bookings")[1] == {"bookings": [], "next_cursor": None}


def test_unknown_path_is_404_not_found(api):
    assert_error(api.call("GET", "/nope"), 404, "not_found")
    assert_error(api.call("GET", "/"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r1/unknown"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/b1/extra"), 404, "not_found")
    assert_error(api.call("POST", "/nope", {"a": 1}), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("POST", "/rooms/r1"),
    ("DELETE", "/rooms"),
    ("PUT", "/rooms"),
    ("POST", "/rooms/r1/availability?date=2026-03-02"),
    ("DELETE", "/bookings"),
    ("PUT", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
])
def test_unsupported_method_is_405(api, method, path):
    body = {"name": "x", "capacity": 1} if method in ("POST", "PUT") else None
    assert_error(api.call(method, path, body), 405, "method_not_allowed")


def test_405_on_existing_resource_not_404(api):
    r = api.room()
    assert_error(api.call("DELETE", f"/rooms/{r}"), 405, "method_not_allowed")


@pytest.mark.parametrize("raw", [
    b"not json",
    b"{",
    b"",
    b"[]",
    b"[1, 2]",
    b'"a string"',
    b"42",
    b"null",
    b"true",
    b"\xff\xfe\x00",
])
def test_bad_json_on_rooms(api, raw):
    assert_error(api.call("POST", "/rooms", raw=raw), 400, "bad_json")


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"null", b'"x"', b""])
def test_bad_json_on_bookings(api, raw):
    assert_error(post_booking_raw(api, raw), 400, "bad_json")


def test_bad_json_creates_nothing(api):
    post_booking_raw(api, b"{oops")
    api.call("POST", "/rooms", raw=b"{oops")
    assert api.call("GET", "/rooms")[1] == {"rooms": []}
    assert api.call("GET", "/bookings")[1]["bookings"] == []


def test_every_body_response_is_json(api):
    r = api.room()
    responses = [
        api.call("POST", "/rooms", {"name": "X", "capacity": 1}),
        api.call("POST", "/rooms", {"name": "X", "capacity": 1}),  # 409
        api.call("POST", "/rooms", {"name": "", "capacity": 1}),  # 422
        api.call("POST", "/rooms", raw=b"{"),  # 400
        api.call("GET", "/rooms"),
        api.call("GET", f"/rooms/{r}"),
        api.call("GET", "/rooms/zz"),
        api.call("GET", "/missing"),
        api.call("DELETE", "/rooms"),
        api.call("GET", "/bookings"),
        api.book(r, "09:00", "10:00"),
        api.call("GET", "/bookings/b1"),
        api.call("GET", "/bookings/bX"),
        api.call("GET", f"/rooms/{r}/availability?date={DAY}"),
    ]
    for s, body, headers in responses:
        assert headers["content-type"].startswith("application/json"), (s, headers)
        assert body is not None


def test_unknown_fields_are_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 4, "colour": "red", "x": None})
    assert s == 201 and set(b) == ROOM_FIELDS
    s, b, _ = api.call("POST", "/bookings", {**valid_booking(b["id"]), "extra": [1, 2]})
    assert s == 201 and set(b) == BOOKING_FIELDS


def test_bool_is_not_an_integer(api):
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": True}), 422, "validation_error")
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": False}), 422, "validation_error")
    r = api.room(capacity=8)
    assert_error(api.call("POST", "/bookings", valid_booking(r, attendees=True)), 422, "validation_error")


# ---------------------------------------------------------------- rooms

def test_create_room_response_shape(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}
    assert h["content-type"].startswith("application/json")


def test_room_ids_in_creation_order(api):
    ids = [api.room(name=f"Room {i}") for i in range(5)]
    assert ids == ["r1", "r2", "r3", "r4", "r5"]


def test_failed_room_creation_does_not_consume_id(api):
    api.call("POST", "/rooms", {"name": "", "capacity": 1})
    api.call("POST", "/rooms", {"name": "A", "capacity": 0})
    api.room(name="A")
    api.call("POST", "/rooms", {"name": "a", "capacity": 3})  # 409
    assert api.room(name="B") == "r2"


def test_room_name_is_trimmed_and_stored_trimmed(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "   Atlas  \t", "capacity": 3})
    assert s == 201 and b["name"] == "Atlas"
    assert api.call("GET", "/rooms/r1")[1]["name"] == "Atlas"


@pytest.mark.parametrize("name", ["", "   ", "\t \n", None, 5, ["Atlas"], {"n": 1}, True])
def test_room_name_invalid(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 4}), 422, "validation_error")


def test_room_name_length_boundaries(api):
    assert api.call("POST", "/rooms", {"name": "a" * 50, "capacity": 1})[0] == 201
    assert_error(api.call("POST", "/rooms", {"name": "b" * 51, "capacity": 1}), 422, "validation_error")
    # Length is counted after trimming.
    s, b, _ = api.call("POST", "/rooms", {"name": "  " + "c" * 50 + "  ", "capacity": 1})
    assert s == 201 and b["name"] == "c" * 50
    # ...and in characters, not bytes.
    assert api.call("POST", "/rooms", {"name": "é" * 50, "capacity": 1})[0] == 201
    assert_error(api.call("POST", "/rooms", {"name": "é" * 51, "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("capacity", [0, -1, 501, 10_000, "8", None, 2.5, [8], True])
def test_room_capacity_invalid(api, capacity):
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": capacity}), 422, "validation_error")


@pytest.mark.parametrize("capacity", [1, 500])
def test_room_capacity_bounds_accepted(api, capacity):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": capacity})
    assert s == 201 and b["capacity"] == capacity


@pytest.mark.parametrize("body", [{"capacity": 4}, {"name": "A"}, {}])
def test_room_missing_fields(api, body):
    assert_error(api.call("POST", "/rooms", body), 422, "validation_error")


@pytest.mark.parametrize("second", ["atlas", "ATLAS", "  Atlas  ", "aTlAs"])
def test_room_name_duplicate_ignores_case_and_trim(api, second):
    api.room(name="Atlas")
    assert_error(api.call("POST", "/rooms", {"name": second, "capacity": 9}), 409, "room_exists")


def test_room_duplicate_not_consumed_on_conflict(api):
    api.room(name="Atlas")
    api.call("POST", "/rooms", {"name": "ATLAS", "capacity": 9})
    assert api.room(name="Other") == "r2"


def test_list_rooms_in_creation_order(api):
    api.room(name="Zulu")
    api.room(name="Alpha")
    s, b, h = api.call("GET", "/rooms")
    assert s == 200 and h["content-type"].startswith("application/json")
    assert [r["name"] for r in b["rooms"]] == ["Zulu", "Alpha"]
    assert all(set(r) == ROOM_FIELDS for r in b["rooms"])


def test_get_room(api):
    r = api.room(name="Atlas", capacity=12)
    s, b, _ = api.call("GET", f"/rooms/{r}")
    assert s == 200 and b == {"id": r, "name": "Atlas", "capacity": 12}


def test_get_unknown_room_404(api):
    assert_error(api.call("GET", "/rooms/r99"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/nope"), 404, "not_found")


# ---------------------------------------------------------------- bookings: validation

def test_create_booking_response_shape(api):
    r = api.room()
    s, b, h = api.call("POST", "/bookings", valid_booking(r, at(2, "09:00"), at(2, "10:30"), title="Sync", attendees=3))
    assert s == 201
    assert b == {"id": "b1", "room_id": r, "start": at(2, "09:00"), "end": at(2, "10:30"),
                 "title": "Sync", "attendees": 3}
    assert h["content-type"].startswith("application/json")
    assert api.call("GET", "/bookings/b1")[1] == b


def test_booking_ids_in_creation_order_and_never_reused(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[1]["id"] == "b1"
    assert api.book(r, "11:00", "12:00")[1]["id"] == "b2"
    assert api.call("DELETE", "/bookings/b2")[0] == 204
    assert api.book(r, "11:00", "12:00")[1]["id"] == "b3"
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert api.book(r, "09:00", "10:00")[1]["id"] == "b4"


def test_failed_booking_does_not_consume_id(api):
    r = api.room(capacity=2)
    api.book(r, "09:00", "10:00")                       # b1
    assert api.book(r, "09:30", "10:30")[0] == 409       # conflict
    assert api.book(r, "11:00", "12:00", attendees=5)[0] == 422  # over capacity
    assert api.book(r, "11:00", "12:00", title="   ")[0] == 422
    assert api.book(r, "12:00", "13:00")[1]["id"] == "b2"


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field_422(api, field):
    r = api.room()
    body = valid_booking(r)
    del body[field]
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("field,value", [
    ("room_id", 1), ("room_id", None), ("room_id", ["r1"]),
    ("title", 7), ("title", None), ("title", ["x"]),
    ("attendees", "2"), ("attendees", None), ("attendees", 2.5), ("attendees", [2]), ("attendees", True),
    ("start", 1234), ("start", None), ("end", ["x"]), ("end", True),
])
def test_booking_wrong_types_422(api, field, value):
    r = api.room()
    body = valid_booking(r)
    body[field] = value
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("stamp", [
    "2026-03-02 09:00:00Z",          # space instead of T
    "2026-03-02T09:00:00",           # no zone
    "2026-03-02T09:00:00+00:00",     # offset form
    "2026-03-02T09:00:00.000Z",      # fractional seconds
    "2026-03-02T09:00Z",             # no seconds
    "2026-03-02T09:00:00z",          # lower-case z
    "2026-3-2T09:00:00Z",            # not zero-padded
    "2026-02-30T09:00:00Z",          # not a real date
    "2026-13-01T09:00:00Z",          # not a real month
    "2026-03-02T24:00:00Z",          # not a real hour
    "2026-03-02T09:00:00Z ",         # trailing junk
    " 2026-03-02T09:00:00Z",         # leading junk
    "09:00",
    "",
])
def test_booking_malformed_timestamp_422(api, stamp):
    r = api.room()
    assert_error(api.call("POST", "/bookings", valid_booking(r, start=stamp)), 422, "validation_error")
    assert_error(api.call("POST", "/bookings", valid_booking(r, end=stamp)), 422, "validation_error")


@pytest.mark.parametrize("stamp", ["2026-03-02T09:10:00Z", "2026-03-02T09:05:00Z", "2026-03-02T09:59:00Z",
                                   "2026-03-02T09:00:30Z", "2026-03-02T09:00:01Z"])
def test_booking_unaligned_timestamp_422(api, stamp):
    r = api.room()
    assert_error(api.call("POST", "/bookings", valid_booking(r, start=stamp, end=at(2, "12:00"))),
                 422, "validation_error")
    assert_error(api.call("POST", "/bookings", valid_booking(r, start=at(2, "09:00"), end=stamp)),
                 422, "validation_error")


@pytest.mark.parametrize("start,end", [
    ("09:00", "09:15"), ("09:15", "09:30"), ("09:30", "09:45"), ("09:45", "10:00"),
])
def test_quarter_hour_bookings_accepted(api, start, end):
    r = api.room()
    s, b, _ = api.book(r, start, end)
    assert s == 201


@pytest.mark.parametrize("start,end", [("10:00", "10:00"), ("10:00", "09:00"), ("10:15", "10:00")])
def test_end_must_be_after_start(api, start, end):
    r = api.room()
    assert_error(api.call("POST", "/bookings", valid_booking(r, at(2, start), at(2, end))),
                 422, "validation_error")


def test_booking_max_eight_hours(api):
    r = api.room()
    assert api.book(r, "08:00", "16:00")[0] == 201
    assert_error(api.call("POST", "/bookings", valid_booking(r, at(3, "08:00"), at(3, "16:15"))),
                 422, "validation_error")
    assert_error(api.call("POST", "/bookings", valid_booking(r, at(4, "08:00"), at(5, "08:15"))),
                 422, "validation_error")


def test_booking_across_midnight_within_limit(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", valid_booking(r, at(2, "22:00"), at(3, "06:00")))
    assert s == 201


@pytest.mark.parametrize("title", ["", " ", "\t\n", None, 12, ["t"]])
def test_title_invalid(api, title):
    r = api.room()
    assert_error(api.call("POST", "/bookings", valid_booking(r, title=title)), 422, "validation_error")


def test_title_trimmed_and_length_bounded(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", valid_booking(r, title="  Planning  "))
    assert s == 201 and b["title"] == "Planning"
    s, b, _ = api.call("POST", "/bookings", valid_booking(r, at(3, "09:00"), at(3, "10:00"), title="t" * 100))
    assert s == 201 and b["title"] == "t" * 100
    assert_error(api.call("POST", "/bookings", valid_booking(r, at(4, "09:00"), at(4, "10:00"), title="t" * 101)),
                 422, "validation_error")
    s, b, _ = api.call("POST", "/bookings",
                       valid_booking(r, at(5, "09:00"), at(5, "10:00"), title="  " + "t" * 100 + "  "))
    assert s == 201 and b["title"] == "t" * 100


@pytest.mark.parametrize("attendees", [0, -1, -100])
def test_attendees_must_be_at_least_one(api, attendees):
    r = api.room()
    assert_error(api.call("POST", "/bookings", valid_booking(r, attendees=attendees)), 422, "validation_error")


def test_attendees_equal_to_capacity_accepted(api):
    r = api.room(capacity=5)
    s, b, _ = api.book(r, "09:00", "10:00", attendees=5)
    assert s == 201 and b["attendees"] == 5


def test_validation_precedes_room_lookup(api):
    # Invalid body against an unknown room: validation error wins.
    assert_error(api.call("POST", "/bookings", valid_booking("r404", attendees=0)), 422, "validation_error")


def test_unknown_room_404(api):
    assert_error(api.call("POST", "/bookings", valid_booking("r404")), 404, "not_found")


def test_over_capacity(api):
    r = api.room(capacity=2)
    assert_error(api.book(r, "09:00", "10:00", attendees=3), 422, "over_capacity")


def test_over_capacity_precedes_conflict(api):
    r = api.room(capacity=2)
    api.book(r, "09:00", "10:00", attendees=1)
    assert_error(api.book(r, "09:00", "10:00", attendees=3), 422, "over_capacity")


def test_validation_precedes_conflict(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert_error(api.call("POST", "/bookings", valid_booking(r, at(2, "09:00"), at(2, "10:00"), title="")),
                 422, "validation_error")


def test_room_lookup_precedes_over_capacity_and_conflict(api):
    # Unknown room is 404 regardless of the other rules (here attendees are valid).
    assert_error(api.call("POST", "/bookings", valid_booking("r404", attendees=1000)), 404, "not_found")


# ---------------------------------------------------------------- bookings: conflicts

@pytest.mark.parametrize("second", [
    ("09:30", "10:30"),   # overlaps end
    ("08:30", "09:30"),   # overlaps start
    ("09:15", "09:45"),   # contained in existing
    ("08:00", "11:00"),   # contains existing
    ("09:00", "10:00"),   # identical
])
def test_overlap_is_conflict(api, second):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert_error(api.book(r, *second), 409, "conflict")


def test_back_to_back_is_allowed_both_sides(api):
    r = api.room()
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert api.book(r, "10:00", "11:00")[0] == 201   # starts exactly when previous ends
    assert api.book(r, "08:00", "09:00")[0] == 201   # ends exactly when previous starts
    assert len(list_all(api, f"room_id={r}")) == 3


def test_conflict_only_within_same_room(api):
    r1 = api.room(name="A")
    r2 = api.room(name="B")
    assert api.book(r1, "09:00", "10:00")[0] == 201
    assert api.book(r2, "09:00", "10:00")[0] == 201
    assert_error(api.book(r2, "09:30", "09:45"), 409, "conflict")


def test_cancelled_booking_does_not_conflict(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert api.book(r, "09:00", "10:00")[0] == 201


def test_conflict_error_leaves_existing_booking_intact(api):
    r = api.room()
    api.book(r, "09:00", "10:00", title="Original")
    api.book(r, "09:30", "10:30", title="Intruder")
    assert api.call("GET", "/bookings/b1")[1]["title"] == "Original"
    assert len(list_all(api, f"room_id={r}")) == 1


# ---------------------------------------------------------------- idempotency

def test_idempotent_replay_returns_original_and_creates_nothing(api):
    r = api.room()
    body = valid_booking(r, title="Once")
    s1, b1, _ = api.call("POST", "/bookings", body, headers={"Idempotency-Key": "k-1"})
    s2, b2, _ = api.call("POST", "/bookings", body, headers={"Idempotency-Key": "k-1"})
    assert s1 == 201 and s2 == 201
    assert b1 == b2 == {"id": "b1", "room_id": r, "start": at(2, "09:00"), "end": at(2, "10:00"),
                        "title": "Once", "attendees": 2}
    assert len(list_all(api)) == 1
    # The next genuinely new booking still gets b2.
    assert api.book(r, "11:00", "12:00")[1]["id"] == "b2"


def test_idempotent_replay_ignores_json_key_order_and_whitespace(api):
    r = api.room()
    first = '{"room_id": "%s", "start": "%s", "end": "%s", "title": "T", "attendees": 2}' % (
        r, at(2, "09:00"), at(2, "10:00"))
    reordered = '{ "attendees":2,"title":"T","end":"%s","start":"%s","room_id":"%s" }' % (
        at(2, "10:00"), at(2, "09:00"), r)
    h = {"Idempotency-Key": "k-order", "Content-Type": "application/json"}
    s1, b1, _ = post_booking_raw(api, first.encode(), headers=h)
    s2, b2, _ = post_booking_raw(api, reordered.encode(), headers=h)
    assert s1 == 201 and s2 == 201 and b1 == b2
    assert len(list_all(api)) == 1


def test_same_key_different_body_is_mismatch(api):
    r = api.room()
    s, b, _ = api.book(r, "09:00", "10:00", title="A", headers={"Idempotency-Key": "k-2"})
    assert s == 201
    assert_error(api.book(r, "09:00", "10:00", title="B", headers={"Idempotency-Key": "k-2"}),
                 422, "idempotency_mismatch")
    assert_error(api.book(r, "11:00", "12:00", title="A", headers={"Idempotency-Key": "k-2"}),
                 422, "idempotency_mismatch")
    assert len(list_all(api)) == 1


def test_idempotency_mismatch_creates_nothing_and_key_still_maps_to_original(api):
    r = api.room()
    api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k-3"})
    assert_error(api.book(r, "12:00", "13:00", headers={"Idempotency-Key": "k-3"}), 422, "idempotency_mismatch")
    s, b, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k-3"})
    assert s == 201 and b["id"] == "b1"
    assert len(list_all(api)) == 1


def test_idempotent_replay_after_delete_does_not_recreate(api):
    r = api.room()
    api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k-4"})
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k-4"})
    assert s == 201 and b["id"] == "b1"
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert list_all(api) == []


def test_keys_are_independent(api):
    r = api.room()
    s1, b1, _ = api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "a"})
    s2, b2, _ = api.book(r, "11:00", "12:00", headers={"Idempotency-Key": "b"})
    assert s1 == s2 == 201 and b1["id"] == "b1" and b2["id"] == "b2"


def test_failed_request_with_key_is_not_remembered_validation(api):
    r = api.room()
    assert_error(api.book(r, "09:00", "10:00", attendees=0, headers={"Idempotency-Key": "k-5"}),
                 422, "validation_error")
    s, b, _ = api.book(r, "09:00", "10:00", attendees=2, headers={"Idempotency-Key": "k-5"})
    assert s == 201 and b["id"] == "b1"


def test_failed_request_with_key_is_not_remembered_conflict(api):
    r = api.room()
    api.book(r, "09:00", "10:00")  # b1, no key
    assert_error(api.book(r, "09:30", "10:30", headers={"Idempotency-Key": "k-6"}), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:30", "10:30", headers={"Idempotency-Key": "k-6"})
    assert s == 201 and b["id"] == "b2"


def test_failed_request_with_key_is_not_remembered_unknown_room(api):
    r = api.room()
    assert_error(api.call("POST", "/bookings", valid_booking("r404"), headers={"Idempotency-Key": "k-7"}),
                 404, "not_found")
    s, b, _ = api.call("POST", "/bookings", valid_booking(r), headers={"Idempotency-Key": "k-7"})
    assert s == 201


def test_failed_request_with_key_is_not_remembered_over_capacity(api):
    r = api.room(capacity=1)
    assert_error(api.book(r, "09:00", "10:00", attendees=2, headers={"Idempotency-Key": "k-8"}),
                 422, "over_capacity")
    assert api.book(r, "09:00", "10:00", attendees=1, headers={"Idempotency-Key": "k-8"})[0] == 201


def test_failed_bad_json_with_key_is_not_remembered(api):
    r = api.room()
    assert_error(post_booking_raw(api, b"{", headers={"Idempotency-Key": "k-9"}), 400, "bad_json")
    assert api.book(r, "09:00", "10:00", headers={"Idempotency-Key": "k-9"})[0] == 201


def test_no_key_means_no_deduplication(api):
    r = api.room()
    body = valid_booking(r)
    assert api.call("POST", "/bookings", body)[0] == 201
    assert_error(api.call("POST", "/bookings", body), 409, "conflict")


# ---------------------------------------------------------------- get / delete

def test_get_booking(api):
    r = api.room()
    created = api.book(r, "09:00", "10:00", title="Hi")[1]
    s, b, h = api.call("GET", f"/bookings/{created['id']}")
    assert s == 200 and b == created and set(b) == BOOKING_FIELDS
    assert h["content-type"].startswith("application/json")


def test_get_unknown_booking_404(api):
    assert_error(api.call("GET", "/bookings/b99"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/xyz"), 404, "not_found")


def test_delete_booking_204_empty_body(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_unknown_or_twice_404(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert_error(api.call("DELETE", "/bookings/b99"), 404, "not_found")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


# ---------------------------------------------------------------- list bookings

def test_list_sorted_by_start_then_creation_order(api):
    r1 = api.room(name="A")
    r2 = api.room(name="B")
    api.book(r1, "11:00", "12:00")          # b1
    api.book(r2, "09:00", "10:00")          # b2, same start as b3, created earlier
    api.book(r1, "09:00", "09:30")          # b3
    api.book(r2, "08:00", "08:30")          # b4
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and b["next_cursor"] is None
    assert [x["id"] for x in b["bookings"]] == ["b4", "b2", "b3", "b1"]


def test_list_items_have_exact_booking_shape(api):
    r = api.room()
    api.book(r, "09:00", "10:00", title="Shape")
    s, b, _ = api.call("GET", "/bookings")
    assert all(set(x) == BOOKING_FIELDS for x in b["bookings"])
    assert b["bookings"][0]["title"] == "Shape"


def test_list_filter_by_room(api):
    r1 = api.room(name="A")
    r2 = api.room(name="B")
    api.book(r1, "09:00", "10:00")
    api.book(r2, "09:00", "10:00")
    api.book(r1, "11:00", "12:00")
    s, b, _ = api.call("GET", f"/bookings?room_id={r1}")
    assert s == 200 and [x["id"] for x in b["bookings"]] == ["b1", "b3"]
    assert all(x["room_id"] == r1 for x in b["bookings"])


def test_list_filter_unknown_room_is_empty_not_error(api):
    api.book(api.room(), "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings?room_id=r999")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_window_half_open(api):
    r = api.room()
    other = api.room(name="Other")
    api.book(r, "08:00", "09:00")  # b1 ends exactly at window start -> excluded
    api.book(r, "09:00", "10:00")  # b2 inside
    api.book(r, "10:00", "11:00")  # b3 inside
    api.book(r, "11:00", "12:00")  # b4 starts exactly at window end -> excluded
    api.book(other, "07:00", "13:00")  # b5 spans the whole window -> included
    s, b, _ = api.call("GET", f"/bookings?from={ts('09:00')}&to={ts('11:00')}")
    assert s == 200 and sorted(x["id"] for x in b["bookings"]) == ["b2", "b3", "b5"]


def test_list_window_from_only_and_to_only(api):
    r = api.room()
    api.book(r, "08:00", "09:00")  # b1
    api.book(r, "10:00", "11:00")  # b2
    s, b, _ = api.call("GET", f"/bookings?from={ts('09:00')}")
    assert [x["id"] for x in b["bookings"]] == ["b2"]
    s, b, _ = api.call("GET", f"/bookings?to={ts('10:00')}")
    assert [x["id"] for x in b["bookings"]] == ["b1"]


def test_list_window_not_aligned_to_quarter_hours(api):
    r = api.room()
    api.book(r, "09:00", "09:30")  # b1
    api.book(r, "09:30", "10:00")  # b2
    api.book(r, "10:00", "10:30")  # b3
    s, b, _ = api.call("GET", f"/bookings?from={ts('09:20')}&to={ts('10:10')}")
    assert s == 200 and [x["id"] for x in b["bookings"]] == ["b1", "b2", "b3"]
    s, b, _ = api.call("GET", f"/bookings?from={ts('09:10')}&to={ts('09:20')}")
    assert s == 200 and [x["id"] for x in b["bookings"]] == ["b1"]


@pytest.mark.parametrize("query", [
    "from=2026-03-02",
    "from=garbage",
    "from=2026-03-02T09:00:00",
    "to=2026-02-30T09:00:00Z",
    "to=yesterday",
    "from=2026-03-02T09%3A00%3A00Z%20",
])
def test_list_invalid_window_422(api, query):
    api.book(api.room(), "09:00", "10:00")
    assert_error(api.call("GET", f"/bookings?{query}"), 422, "validation_error")


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", "1e2"])
def test_list_invalid_limit_422(api, limit):
    assert_error(api.call("GET", f"/bookings?limit={limit}"), 422, "validation_error")


@pytest.mark.parametrize("cursor", ["garbage", "0", "c", "nope_1"])
def test_list_invalid_cursor_422(api, cursor):
    api.book(api.room(), "09:00", "10:00")
    assert_error(api.call("GET", f"/bookings?cursor={cursor}"), 422, "validation_error")


def test_list_limit_bounds_accepted(api):
    r = api.room()
    for i in range(3):
        api.book(r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    assert len(api.call("GET", "/bookings?limit=1")[1]["bookings"]) == 1
    assert len(api.call("GET", "/bookings?limit=100")[1]["bookings"]) == 3


def test_list_default_page_size_is_20_with_cursor(api):
    r = api.room()
    for d in range(1, 26):
        assert api.call("POST", "/bookings", valid_booking(r, at(d, "09:00"), at(d, "10:00")))[0] == 201
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and len(b["bookings"]) == 20
    assert b["next_cursor"] is not None


def test_list_no_next_cursor_when_exactly_one_page(api):
    r = api.room()
    for d in range(1, 21):
        assert api.call("POST", "/bookings", valid_booking(r, at(d, "09:00"), at(d, "10:00")))[0] == 201
    s, b, _ = api.call("GET", "/bookings")
    assert len(b["bookings"]) == 20 and b["next_cursor"] is None


def test_list_next_cursor_null_on_last_partial_page(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    assert len(b["bookings"]) == 2 and b["next_cursor"] is None


def test_pagination_walk_returns_each_booking_exactly_once_in_order(api):
    r1 = api.room(name="A")
    r2 = api.room(name="B")
    created = []
    # Create in reverse chronological order so creation order != start order.
    for d in range(25, 0, -1):
        room = r1 if d % 2 else r2
        created.append(api.call("POST", "/bookings", valid_booking(room, at(d, "09:00"), at(d, "10:00")))[1]["id"])
    created_starts = {bid: at(d, "09:00") for bid, d in zip(created, range(25, 0, -1))}
    expected = sorted(created, key=lambda bid: created_starts[bid])

    seen, cursor, pages = [], None, 0
    while True:
        path = "/bookings?limit=7" + (f"&cursor={cursor}" if cursor else "")
        s, b, _ = api.call("GET", path)
        assert s == 200
        assert len(b["bookings"]) <= 7
        seen.extend(x["id"] for x in b["bookings"])
        pages += 1
        cursor = b["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "pagination does not terminate"
    assert pages == 4
    assert seen == expected


def test_pagination_walk_with_filter_uses_same_filters(api):
    r1 = api.room(name="A")
    r2 = api.room(name="B")
    for d in range(1, 16):
        api.call("POST", "/bookings", valid_booking(r1, at(d, "09:00"), at(d, "10:00")))
        api.call("POST", "/bookings", valid_booking(r2, at(d, "11:00"), at(d, "12:00")))
    got = list_all(api, f"room_id={r1}&limit=4")
    assert len(got) == 15 and all(x["room_id"] == r1 for x in got)
    assert len({x["id"] for x in got}) == 15
    got = list_all(api, f"room_id={r2}&from={at(5, '00:00')}&to={at(9, '00:00')}&limit=3")
    assert [x["start"] for x in got] == [at(d, "11:00") for d in range(5, 9)]


def test_pagination_ties_on_start_are_not_skipped(api):
    rooms = [api.room(name=f"R{i}") for i in range(6)]
    for r in rooms:
        api.book(r, "09:00", "10:00")
    ids = [x["id"] for x in list_all(api, "limit=2")]
    assert ids == [f"b{i}" for i in range(1, 7)]


def test_list_empty_result_has_null_cursor(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/bookings?from={ts('12:00')}")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


# ---------------------------------------------------------------- availability

def free(api, room, date=DAY):
    s, b, h = api.call("GET", f"/rooms/{room}/availability?date={date}")
    assert s == 200, b
    assert h["content-type"].startswith("application/json")
    assert set(b) == {"date", "free"} and b["date"] == date
    return b["free"]


def span(a, b, day=DAY):
    return {"start": f"{day}T{a}:00Z", "end": f"{day}T{b}:00Z"}


def test_availability_empty_day_is_whole_working_day(api):
    r = api.room()
    assert free(api, r) == [span("08:00", "18:00")]


def test_availability_gaps_between_bookings(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "12:00", "13:30")
    assert free(api, r) == [span("08:00", "09:00"), span("10:00", "12:00"), span("13:30", "18:00")]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    assert free(api, r) == [span("08:00", "09:00"), span("11:00", "18:00")]


def test_availability_fully_booked_day_is_empty_list(api):
    r = api.room()
    api.book(r, "08:00", "13:00")
    api.book(r, "13:00", "18:00")
    assert free(api, r) == []


def test_availability_clips_bookings_partly_outside_working_day(api):
    r = api.room()
    api.book(r, "07:00", "09:00")
    api.book(r, "17:00", "19:00")
    assert free(api, r) == [span("09:00", "17:00")]


def test_availability_booking_spanning_whole_day_leaves_nothing(api):
    r = api.room()
    api.book(r, "08:00", "16:00")
    api.book(r, "16:00", "18:00")
    assert free(api, r) == []


def test_availability_ignores_bookings_outside_working_day(api):
    r = api.room()
    api.book(r, "06:00", "07:00")
    assert free(api, r) == [span("08:00", "18:00")]


def test_availability_booking_touching_day_edges(api):
    r = api.room()
    api.book(r, "08:00", "09:00")
    api.book(r, "17:00", "18:00")
    assert free(api, r) == [span("09:00", "17:00")]


def test_availability_only_this_room_and_this_date(api):
    r1 = api.room(name="A")
    r2 = api.room(name="B")
    api.book(r2, "09:00", "10:00")
    api.call("POST", "/bookings", valid_booking(r1, at(3, "09:00"), at(3, "10:00")))
    assert free(api, r1) == [span("08:00", "18:00")]
    assert free(api, r2) == [span("08:00", "09:00"), span("10:00", "18:00")]
    assert free(api, r1, date="2026-03-03") == [span("08:00", "09:00", "2026-03-03"),
                                                span("10:00", "18:00", "2026-03-03")]


def test_availability_free_list_ascending(api):
    r = api.room()
    api.book(r, "15:00", "16:00")
    api.book(r, "09:00", "10:00")
    api.book(r, "12:00", "12:15")
    starts = [f["start"] for f in free(api, r)]
    assert starts == sorted(starts) and len(starts) == 4


def test_availability_reflects_deletion(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert free(api, r) == [span("08:00", "18:00")]


@pytest.mark.parametrize("query", [
    "", "?", "?date=", "?date=2026-02-30", "?date=2026-13-01", "?date=2026-00-10",
    "?date=2026/03/02", "?date=03-02-2026", "?date=2026-3-2", "?date=yesterday",
    "?date=2026-03-02T00:00:00Z", "?day=2026-03-02",
])
def test_availability_invalid_or_missing_date_422(api, query):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability{query}"), 422, "validation_error")


def test_availability_unknown_room_404(api):
    assert_error(api.call("GET", f"/rooms/r404/availability?date={DAY}"), 404, "not_found")


def test_availability_room_check_before_date_check(api):
    assert_error(api.call("GET", "/rooms/r404/availability?date=nonsense"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r404/availability"), 404, "not_found")


def test_availability_leap_day_valid(api):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2028-02-29")
    assert s == 200 and b["date"] == "2028-02-29"
    assert_error(api.call("GET", f"/rooms/{r}/availability?date=2027-02-29"), 422, "validation_error")


def test_availability_booking_at_day_start(api):
    r = api.room()
    api.book(r, "08:00", "08:15")
    assert free(api, r) == [span("08:15", "18:00")]


# ---------------------------------------------------------------- end-to-end scenario

def test_full_scenario(api):
    atlas = api.room(name="Atlas", capacity=6)
    borealis = api.room(name="Borealis", capacity=20)
    assert api.book(atlas, "09:00", "10:00", title="  Standup ", attendees=6)[1] == {
        "id": "b1", "room_id": atlas, "start": at(2, "09:00"), "end": at(2, "10:00"),
        "title": "Standup", "attendees": 6}
    assert_error(api.book(atlas, "09:30", "10:30", attendees=2), 409, "conflict")
    assert_error(api.book(atlas, "11:00", "12:00", attendees=7), 422, "over_capacity")
    assert api.book(borealis, "09:30", "10:30", attendees=12)[1]["id"] == "b2"
    assert api.book(atlas, "10:00", "11:00", headers={"Idempotency-Key": "lunch"})[1]["id"] == "b3"
    assert api.book(atlas, "10:00", "11:00", headers={"Idempotency-Key": "lunch"})[1]["id"] == "b3"
    assert_error(api.book(atlas, "10:00", "11:00", title="Other", headers={"Idempotency-Key": "lunch"}),
                 422, "idempotency_mismatch")
    listed = list_all(api)
    assert [x["id"] for x in listed] == ["b1", "b2", "b3"]
    assert [x["id"] for x in list_all(api, f"room_id={atlas}")] == ["b1", "b3"]
    assert free(api, atlas) == [span("08:00", "09:00"), span("11:00", "18:00")]
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert free(api, atlas) == [span("08:00", "10:00"), span("11:00", "18:00")]
    assert api.book(atlas, "09:00", "10:00")[1]["id"] == "b4"
