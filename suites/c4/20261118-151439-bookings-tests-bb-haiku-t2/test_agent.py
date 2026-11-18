"""Black-box regression suite for the bookings API, written from SPEC.md.

Every test starts a fresh server (via the ``api`` fixture from helpers.py), so
state is empty at the start of each test.
"""
import json
import pytest

from helpers import api, code, ts  # noqa: F401  (api is a fixture)


def day_ts(hhmm, day="2026-03-02"):
    return f"{day}T{hhmm}:00Z"


def ids(items):
    return [x["id"] for x in items]


def create_booking_ok(api, room, start, end, title="sync", attendees=1, headers=None):
    s, b, _ = api.book(room, start, end, title=title, attendees=attendees, headers=headers)
    assert s == 201, (s, b)
    return b


def assert_error(resp, status, err_code):
    s, b, h = resp
    assert s == status, (s, b)
    assert isinstance(b, dict) and set(b) == {"error"}, b
    assert set(b["error"]) >= {"code", "message"}, b
    assert isinstance(b["error"]["message"], str)
    assert b["error"]["code"] == err_code, b
    assert h["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# General HTTP behaviour and errors
# ---------------------------------------------------------------------------

def test_fresh_server_has_no_rooms_or_bookings(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_success_bodies_are_json_content_type(api):
    s, b, h = api.call("POST", "/rooms", {"name": "A", "capacity": 1})
    assert s == 201 and h["content-type"].startswith("application/json")
    for path in ["/rooms", "/rooms/r1", "/bookings", "/rooms/r1/availability?date=2026-03-02"]:
        s, b, h = api.call("GET", path)
        assert s == 200, path
        assert h["content-type"].startswith("application/json"), path


def test_unknown_path_is_404_not_found(api):
    assert_error(api.call("GET", "/nope"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r1/nothing"), 404, "not_found")
    assert_error(api.call("GET", "/"), 404, "not_found")


def test_unknown_path_with_post_is_404(api):
    assert_error(api.call("POST", "/nope", {"a": 1}), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("PATCH", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/rooms/r1/availability?date=2026-03-02"),
])
def test_unsupported_method_is_405(api, method, path):
    api.room()  # make r1 exist so the path itself is real
    body = {"name": "x", "capacity": 1} if method in ("POST", "PUT", "PATCH") else None
    assert_error(api.call(method, path, body), 405, "method_not_allowed")


@pytest.mark.parametrize("raw", [
    "{not json",
    "",
    "[1, 2, 3]",
    '"just a string"',
    "42",
    "null",
    "true",
    "{'name': 'x'}",
])
def test_bad_json_or_non_object_body_is_400(api, raw):
    assert_error(api.call("POST", "/rooms", raw=raw.encode()), 400, "bad_json")


@pytest.mark.parametrize("raw", ["{bad", "[]", "7"])
def test_bad_json_on_booking_post_is_400(api, raw):
    api.room()
    assert_error(api.call("POST", "/bookings", raw=raw.encode()), 400, "bad_json")


def test_bad_json_on_unknown_path_is_404_not_400(api):
    # path resolution happens before body parsing
    assert_error(api.call("POST", "/nope", raw=b"{bad"), 404, "not_found")


def test_unknown_fields_are_ignored_in_room_body(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": 3, "colour": "red", "x": [1]})
    assert s == 201 and b == {"id": "r1", "name": "A", "capacity": 3}


def test_unknown_fields_are_ignored_in_booking_body(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": r, "start": day_ts("09:00"), "end": day_ts("10:00"),
        "title": "t", "attendees": 1, "extra": {"nested": True}})
    assert s == 201 and set(b) == {"id", "room_id", "start", "end", "title", "attendees"}


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def test_create_room_response_shape_and_ids(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}
    s, b, _ = api.call("POST", "/rooms", {"name": "Borealis", "capacity": 4})
    assert s == 201 and b["id"] == "r2"


def test_room_ids_not_reused_after_failed_create(api):
    api.room("A")
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": 4}), 409, "room_exists")
    assert_error(api.call("POST", "/rooms", {"name": "", "capacity": 4}), 422, "validation_error")
    s, b, _ = api.call("POST", "/rooms", {"name": "B", "capacity": 4})
    assert s == 201 and b["id"] == "r2"


def test_room_name_is_trimmed_on_storage(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "   Atlas  \t", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    s, got, _ = api.call("GET", "/rooms/r1")
    assert got["name"] == "Atlas"


@pytest.mark.parametrize("name", ["a", "x" * 50, "  " + "x" * 48 + "  "])
def test_room_name_length_boundaries_ok(api, name):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 1})
    assert s == 201, b
    assert b["name"] == name.strip()


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 51, "  " + "x" * 51 + "  "])
def test_room_name_length_out_of_range_rejected(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("name", [None, 5, ["Atlas"], {"n": 1}, True])
def test_room_name_wrong_type_rejected(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("capacity", [1, 500])
def test_room_capacity_boundaries_ok(api, capacity):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": capacity})
    assert s == 201 and b["capacity"] == capacity


@pytest.mark.parametrize("capacity", [0, -1, 501, 10**6, None, "8", "", True, False, [8], 8.5])
def test_room_capacity_invalid_rejected(api, capacity):
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": capacity}), 422, "validation_error")


@pytest.mark.parametrize("body", [
    {"capacity": 8},
    {"name": "A"},
    {},
])
def test_room_missing_fields_rejected(api, body):
    assert_error(api.call("POST", "/rooms", body), 422, "validation_error")


def test_room_duplicate_name_ignores_case_and_surrounding_space(api):
    api.room("Atlas")
    assert_error(api.call("POST", "/rooms", {"name": "ATLAS", "capacity": 2}), 409, "room_exists")
    assert_error(api.call("POST", "/rooms", {"name": "  atlas ", "capacity": 2}), 409, "room_exists")


def test_room_distinct_names_allowed(api):
    api.room("Atlas")
    s, _, _ = api.call("POST", "/rooms", {"name": "Atlas 2", "capacity": 2})
    assert s == 201


def test_list_rooms_in_creation_order(api):
    for n in ["Zed", "Alpha", "Mid"]:
        api.room(n)
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and [r["name"] for r in b["rooms"]] == ["Zed", "Alpha", "Mid"]
    assert ids(b["rooms"]) == ["r1", "r2", "r3"]


def test_get_room_by_id(api):
    r = api.room("Atlas", 8)
    s, b, _ = api.call("GET", f"/rooms/{r}")
    assert s == 200 and b == {"id": "r1", "name": "Atlas", "capacity": 8}


@pytest.mark.parametrize("path", ["/rooms/r99", "/rooms/x", "/rooms/R1"])
def test_get_unknown_room_404(api, path):
    api.room()
    assert_error(api.call("GET", path), 404, "not_found")


# ---------------------------------------------------------------------------
# Booking creation: validation
# ---------------------------------------------------------------------------

def booking_body(room_id, **over):
    body = {"room_id": room_id, "start": day_ts("09:00"), "end": day_ts("10:00"),
            "title": "Planning", "attendees": 2}
    body.update(over)
    return body


def post_booking(api, body, headers=None):
    return api.call("POST", "/bookings", body, headers=headers)


def test_create_booking_response_shape(api):
    r = api.room()
    s, b, h = post_booking(api, booking_body(r, title="Planning", attendees=4))
    assert s == 201 and h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": r, "start": day_ts("09:00"), "end": day_ts("10:00"),
                 "title": "Planning", "attendees": 4}


def test_booking_ids_sequential_and_get_returns_same(api):
    r = api.room()
    b1 = create_booking_ok(api, r, "09:00", "10:00")
    b2 = create_booking_ok(api, r, "10:00", "11:00")
    assert (b1["id"], b2["id"]) == ("b1", "b2")
    s, got, _ = api.call("GET", "/bookings/b2")
    assert s == 200 and got == b2


def test_booking_title_trimmed(api):
    r = api.room()
    b = create_booking_ok(api, r, "09:00", "10:00", title="  Planning  ")
    assert b["title"] == "Planning"
    s, got, _ = api.call("GET", "/bookings/b1")
    assert got["title"] == "Planning"


def test_booking_title_length_boundaries(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00", title="x" * 100)
    create_booking_ok(api, r, "10:00", "11:00", title="  " + "y" * 98 + "  ")


@pytest.mark.parametrize("title", ["", "   ", "x" * 101, "  " + "x" * 101 + "  ", None, 3])
def test_booking_title_invalid(api, title):
    r = api.room()
    assert_error(post_booking(api, booking_body(r, title=title)), 422, "validation_error")


@pytest.mark.parametrize("attendees", [0, -3, True, False, "2", 2.0, None, [2]])
def test_booking_attendees_invalid(api, attendees):
    r = api.room()
    assert_error(post_booking(api, booking_body(r, attendees=attendees)), 422, "validation_error")


def test_booking_attendees_equal_to_capacity_allowed(api):
    r = api.room(capacity=4)
    create_booking_ok(api, r, "09:00", "10:00", attendees=4)


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field_invalid(api, field):
    r = api.room()
    body = booking_body(r)
    del body[field]
    assert_error(post_booking(api, body), 422, "validation_error")


@pytest.mark.parametrize("room_id", [1, None, ["r1"], True])
def test_booking_room_id_wrong_type_is_validation_error(api, room_id):
    api.room()
    assert_error(post_booking(api, booking_body(room_id)), 422, "validation_error")


@pytest.mark.parametrize("value", [
    "2026-03-02 09:00:00Z",      # space instead of T
    "2026-03-02T09:00:00",       # no Z
    "2026-03-02T09:00Z",         # no seconds
    "2026-03-02T09:00:00+00:00", # offset form
    "2026-03-02T09:00:00.000Z",  # fractional seconds
    "2026-03-02t09:00:00Z",      # lower-case t
    "2026-3-2T09:00:00Z",        # not zero-padded
    "2026-02-30T09:00:00Z",      # impossible date
    "2026-03-02T24:00:00Z",      # impossible hour
    "2026-03-02T09:10:00Z",      # minutes not quarter-aligned
    "2026-03-02T09:00:30Z",      # seconds not zero
    "2026-03-02T09:07:00Z",
    "not a timestamp",
    "",
    20260302,
    None,
])
def test_booking_invalid_timestamp_forms_rejected(api, value):
    r = api.room()
    assert_error(post_booking(api, booking_body(r, start=value)), 422, "validation_error")
    assert_error(post_booking(api, booking_body(r, end=value)), 422, "validation_error")


@pytest.mark.parametrize("start,end", [
    ("09:00", "09:00"),   # equal
    ("10:00", "09:00"),   # end before start
    ("09:15", "09:00"),
])
def test_booking_end_must_be_after_start(api, start, end):
    r = api.room()
    assert_error(post_booking(api, booking_body(r, start=day_ts(start), end=day_ts(end))),
                 422, "validation_error")


def test_booking_exactly_eight_hours_allowed(api):
    r = api.room()
    create_booking_ok(api, r, "08:00", "16:00")


def test_booking_longer_than_eight_hours_rejected(api):
    r = api.room()
    assert_error(post_booking(api, booking_body(r, start=day_ts("08:00"), end=day_ts("16:15"))),
                 422, "validation_error")


def test_booking_spanning_midnight_longer_than_8h_rejected(api):
    r = api.room()
    assert_error(post_booking(api, booking_body(r, start=day_ts("22:00"), end="2026-03-03T07:00:00Z")),
                 422, "validation_error")


def test_booking_spanning_midnight_within_8h_allowed(api):
    r = api.room()
    s, b, _ = post_booking(api, booking_body(r, start=day_ts("22:00"), end="2026-03-03T06:00:00Z"))
    assert s == 201 and b["end"] == "2026-03-03T06:00:00Z"


def test_booking_all_quarter_hours_accepted(api):
    r = api.room()
    pairs = [("09:00", "09:15"), ("09:15", "09:30"), ("09:30", "09:45"), ("09:45", "10:00")]
    for start, end in pairs:
        s, b, _ = post_booking(api, booking_body(r, start=day_ts(start), end=day_ts(end)))
        assert s == 201, (start, b)


def test_validation_precedes_room_lookup(api):
    # unknown room AND invalid title -> validation wins
    assert_error(post_booking(api, booking_body("r99", title="")), 422, "validation_error")


def test_unknown_room_is_404_for_valid_body(api):
    assert_error(post_booking(api, booking_body("r99")), 404, "not_found")


def test_unknown_room_checked_before_conflict_and_capacity(api):
    assert_error(post_booking(api, booking_body("r99", attendees=50)), 404, "not_found")


def test_over_capacity(api):
    r = api.room(capacity=2)
    assert_error(post_booking(api, booking_body(r, attendees=3)), 422, "over_capacity")


def test_over_capacity_takes_precedence_over_conflict(api):
    r = api.room(capacity=2)
    create_booking_ok(api, r, "09:00", "10:00", attendees=1)
    assert_error(post_booking(api, booking_body(r, start=day_ts("09:00"), end=day_ts("10:00"),
                                                attendees=5)), 422, "over_capacity")


def test_validation_precedes_over_capacity(api):
    r = api.room(capacity=2)
    assert_error(post_booking(api, booking_body(r, attendees=0)), 422, "validation_error")


def test_failed_booking_does_not_consume_id(api):
    r = api.room(capacity=2)
    assert_error(post_booking(api, booking_body(r, attendees=9)), 422, "over_capacity")
    assert_error(post_booking(api, booking_body("r99")), 404, "not_found")
    b = create_booking_ok(api, r, "09:00", "10:00")
    assert b["id"] == "b1"


# ---------------------------------------------------------------------------
# Booking conflicts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start,end", [
    ("09:00", "10:00"),  # identical
    ("08:30", "09:30"),  # overlaps start
    ("09:30", "10:30"),  # overlaps end
    ("08:00", "11:00"),  # contains existing
    ("09:15", "09:45"),  # contained in existing
])
def test_overlapping_booking_conflicts(api, start, end):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    assert_error(post_booking(api, booking_body(r, start=day_ts(start), end=day_ts(end))),
                 409, "conflict")


def test_back_to_back_bookings_allowed_both_sides(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    create_booking_ok(api, r, "10:00", "11:00")   # starts exactly when another ends
    create_booking_ok(api, r, "08:00", "09:00")   # ends exactly when another starts


def test_conflict_checked_against_every_booking_not_just_neighbours(api):
    r = api.room()
    create_booking_ok(api, r, "08:00", "12:00")
    create_booking_ok(api, r, "12:00", "13:00")
    create_booking_ok(api, r, "13:00", "14:00")
    assert_error(post_booking(api, booking_body(r, start=day_ts("11:00"), end=day_ts("11:30"))),
                 409, "conflict")


def test_same_slot_in_different_rooms_allowed(api):
    r1 = api.room("A")
    r2 = api.room("B")
    create_booking_ok(api, r1, "09:00", "10:00")
    create_booking_ok(api, r2, "09:00", "10:00")


def test_different_days_do_not_conflict(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    s, _, _ = post_booking(api, booking_body(r, start=day_ts("09:00", "2026-03-03"),
                                             end=day_ts("10:00", "2026-03-03")))
    assert s == 201


def test_delete_frees_slot_and_ids_not_reused(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    b2 = create_booking_ok(api, r, "09:00", "10:00")
    assert b2["id"] == "b2"


def test_conflict_with_bookings_in_other_room_is_irrelevant_after_delete(api):
    r = api.room("A")
    other = api.room("B")
    create_booking_ok(api, other, "09:00", "10:00")
    create_booking_ok(api, r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert_error(post_booking(api, booking_body(r, start=day_ts("09:30"), end=day_ts("09:45"))),
                 409, "conflict")


# ---------------------------------------------------------------------------
# Get / delete bookings
# ---------------------------------------------------------------------------

def test_get_unknown_booking_404(api):
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/zzz"), 404, "not_found")


def test_delete_booking_204_empty_body(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None


def test_deleted_booking_not_found_and_second_delete_404(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_delete_unknown_booking_404(api):
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_deleted_booking_absent_from_list(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    create_booking_ok(api, r, "11:00", "12:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.call("GET", "/bookings")
    assert ids(b["bookings"]) == ["b2"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def count_bookings(api):
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200
    return len(b["bookings"])


def test_idempotent_replay_returns_original_response(api):
    r = api.room()
    h = {"Idempotency-Key": "k1"}
    s1, b1, _ = post_booking(api, booking_body(r), headers=h)
    assert s1 == 201
    s2, b2, _ = post_booking(api, booking_body(r), headers=h)
    assert s2 == 201 and b2 == b1
    assert count_bookings(api) == 1


def test_idempotent_replay_ignores_key_order_and_whitespace(api):
    r = api.room()
    h = {"Idempotency-Key": "k-order"}
    s1, b1, _ = api.call("POST", "/bookings", raw=json.dumps(booking_body(r)).encode(), headers=h)
    raw = ("{ \"attendees\" :2,\n\"title\":\"Planning\" ,\"end\":\"" + day_ts("10:00") +
           "\",\"start\":\"" + day_ts("09:00") + "\",\"room_id\":\"" + r + "\" }").encode()
    s2, b2, _ = api.call("POST", "/bookings", raw=raw, headers=h)
    assert s1 == 201 and s2 == 201 and b2 == b1
    assert count_bookings(api) == 1


def test_idempotent_replay_with_different_body_is_mismatch(api):
    r = api.room()
    h = {"Idempotency-Key": "k2"}
    assert post_booking(api, booking_body(r), headers=h)[0] == 201
    assert_error(post_booking(api, booking_body(r, title="Other"), headers=h), 422, "idempotency_mismatch")
    assert_error(post_booking(api, booking_body(r, attendees=3), headers=h), 422, "idempotency_mismatch")
    assert count_bookings(api) == 1


def test_idempotency_mismatch_creates_nothing(api):
    r = api.room()
    h = {"Idempotency-Key": "k3"}
    post_booking(api, booking_body(r), headers=h)
    assert_error(post_booking(api, booking_body(r, start=day_ts("13:00"), end=day_ts("14:00")), headers=h),
                 422, "idempotency_mismatch")
    assert api.call("GET", "/bookings/b2")[0] == 404


def test_different_keys_with_same_body_create_separate_bookings(api):
    r = api.room()
    assert post_booking(api, booking_body(r), headers={"Idempotency-Key": "a"})[0] == 201
    # same body under another key is a genuinely new request, so it conflicts with b1
    assert_error(post_booking(api, booking_body(r), headers={"Idempotency-Key": "b"}), 409, "conflict")


def test_failed_request_is_not_remembered(api):
    r = api.room(capacity=2)
    h = {"Idempotency-Key": "k4"}
    # first attempt fails validation (over capacity)
    assert_error(post_booking(api, booking_body(r, attendees=5), headers=h), 422, "over_capacity")
    # same key with a corrected body now succeeds
    s, b, _ = post_booking(api, booking_body(r, attendees=2), headers=h)
    assert s == 201 and b["id"] == "b1"


def test_failed_conflict_is_not_remembered(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    h = {"Idempotency-Key": "k5"}
    assert_error(post_booking(api, booking_body(r, start=day_ts("09:30"), end=day_ts("10:30")), headers=h),
                 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = post_booking(api, booking_body(r, start=day_ts("09:30"), end=day_ts("10:30")), headers=h)
    assert s == 201 and b["id"] == "b2"


def test_key_reuse_after_failed_room_lookup_succeeds_later(api):
    h = {"Idempotency-Key": "k6"}
    assert_error(post_booking(api, booking_body("r1"), headers=h), 404, "not_found")
    r = api.room()
    s, b, _ = post_booking(api, booking_body(r), headers=h)
    assert s == 201


def test_replay_of_success_after_delete_still_returns_original(api):
    r = api.room()
    h = {"Idempotency-Key": "k7"}
    s1, b1, _ = post_booking(api, booking_body(r), headers=h)
    assert api.call("DELETE", f"/bookings/{b1['id']}")[0] == 204
    s2, b2, _ = post_booking(api, booking_body(r), headers=h)
    assert s2 == 201 and b2 == b1
    assert count_bookings(api) == 0


def test_idempotency_key_does_not_affect_other_requests(api):
    r = api.room()
    post_booking(api, booking_body(r), headers={"Idempotency-Key": "kk"})
    s, b, _ = post_booking(api, booking_body(r, start=day_ts("11:00"), end=day_ts("12:00")))
    assert s == 201 and b["id"] == "b2"


# ---------------------------------------------------------------------------
# Listing bookings
# ---------------------------------------------------------------------------

def test_list_bookings_sorted_by_start_then_creation(api):
    a = api.room("A")
    b_room = api.room("B")
    create_booking_ok(api, a, "11:00", "12:00")       # b1
    create_booking_ok(api, b_room, "09:00", "10:00")  # b2
    create_booking_ok(api, a, "09:00", "10:00")       # b3, same start as b2
    s, body, _ = api.call("GET", "/bookings")
    assert s == 200
    assert ids(body["bookings"]) == ["b2", "b3", "b1"]
    assert body["next_cursor"] is None


def test_list_filter_by_room(api):
    a = api.room("A")
    b_room = api.room("B")
    create_booking_ok(api, a, "09:00", "10:00")
    create_booking_ok(api, b_room, "09:00", "10:00")
    create_booking_ok(api, a, "10:00", "11:00")
    s, body, _ = api.call("GET", f"/bookings?room_id={a}")
    assert s == 200 and ids(body["bookings"]) == ["b1", "b3"]
    assert all(x["room_id"] == a for x in body["bookings"])


def test_list_filter_unknown_room_is_empty_not_error(api):
    api.room()
    s, body, _ = api.call("GET", "/bookings?room_id=r99")
    assert s == 200 and body == {"bookings": [], "next_cursor": None}


def test_list_filter_window_half_open(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")  # b1
    create_booking_ok(api, r, "10:00", "11:00")  # b2
    create_booking_ok(api, r, "12:00", "13:00")  # b3
    # window [10:00, 12:00): b1 ends at 10:00 -> excluded; b2 in; b3 starts at 12:00 -> excluded
    s, body, _ = api.call("GET", f"/bookings?from={day_ts('10:00')}&to={day_ts('12:00')}")
    assert s == 200 and ids(body["bookings"]) == ["b2"]


def test_list_filter_window_non_aligned_times(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")  # b1
    create_booking_ok(api, r, "10:00", "11:00")  # b2
    # from 09:59:... not aligned; from=09:10 is inside b1 -> b1 included
    s, body, _ = api.call("GET", f"/bookings?from={day_ts('09:10')}&to={day_ts('10:10')}")
    assert s == 200 and ids(body["bookings"]) == ["b1", "b2"]


def test_list_filter_from_only(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")  # b1 ends at 10 -> excluded when from=10
    create_booking_ok(api, r, "10:00", "11:00")  # b2
    s, body, _ = api.call("GET", f"/bookings?from={day_ts('10:00')}")
    assert s == 200 and ids(body["bookings"]) == ["b2"]


def test_list_filter_to_only(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")  # b1 starts before 10 -> included
    create_booking_ok(api, r, "10:00", "11:00")  # b2 starts at 10 -> excluded
    s, body, _ = api.call("GET", f"/bookings?to={day_ts('10:00')}")
    assert s == 200 and ids(body["bookings"]) == ["b1"]


def test_list_filter_combined_with_room(api):
    a = api.room("A")
    b_room = api.room("B")
    create_booking_ok(api, a, "09:00", "10:00")
    create_booking_ok(api, b_room, "09:00", "10:00")
    s, body, _ = api.call("GET", f"/bookings?room_id={b_room}&from={day_ts('09:30')}")
    assert s == 200 and ids(body["bookings"]) == ["b2"]


@pytest.mark.parametrize("query", [
    "from=not-a-time",
    "to=2026-03-02",
    "from=2026-03-02T09:10:00",
    "to=2026-02-30T09:00:00Z",
    "limit=0",
    "limit=101",
    "limit=abc",
    "limit=-1",
    "limit=1.5",
    "cursor=%%%bogus%%%",
])
def test_list_invalid_query_parameters_422(api, query):
    api.room()
    assert_error(api.call("GET", f"/bookings?{query}"), 422, "validation_error")


def test_list_limit_boundaries_accepted(api):
    r = api.room()
    for i in range(3):
        create_booking_ok(api, r, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
    for lim in (1, 100):
        s, body, _ = api.call("GET", f"/bookings?limit={lim}")
        assert s == 200


def test_list_default_limit_is_20_and_paginates(api):
    r = api.room()
    # 25 bookings across the day, quarter-hour grid, all in one room
    starts = [f"{8 + i // 4:02d}:{(i % 4) * 15:02d}" for i in range(25)]
    for st in starts:
        end = _plus_15(st)
        create_booking_ok(api, r, st, end)
    s, first, _ = api.call("GET", "/bookings")
    assert s == 200 and len(first["bookings"]) == 20
    assert first["next_cursor"] is not None
    s, second, _ = api.call("GET", f"/bookings?cursor={first['next_cursor']}")
    assert s == 200 and len(second["bookings"]) == 5
    assert second["next_cursor"] is None
    assert ids(first["bookings"] + second["bookings"]) == [f"b{i}" for i in range(1, 26)]


def _plus_15(hhmm):
    h, m = map(int, hhmm.split(":"))
    m += 15
    if m == 60:
        h, m = h + 1, 0
    return f"{h:02d}:{m:02d}"


def test_paging_walks_every_booking_exactly_once(api):
    a = api.room("A")
    b_room = api.room("B")
    expected = []
    for i in range(7):
        room = a if i % 2 == 0 else b_room
        st = f"{8 + i:02d}:00"
        expected.append(create_booking_ok(api, room, st, f"{8 + i:02d}:30")["id"])
    seen = []
    cursor = None
    pages = 0
    while True:
        q = "/bookings?limit=2"
        if cursor:
            q += f"&cursor={cursor}"
        s, body, _ = api.call("GET", q)
        assert s == 200
        seen += ids(body["bookings"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 20, "paging does not terminate"
    assert seen == expected
    assert pages == 4


def test_paging_with_filters_respects_filter(api):
    a = api.room("A")
    b_room = api.room("B")
    for i in range(6):
        create_booking_ok(api, a, f"{8 + i:02d}:00", f"{8 + i:02d}:30")
        create_booking_ok(api, b_room, f"{8 + i:02d}:30", f"{8 + i:02d}:45")
    seen, cursor = [], None
    while True:
        q = f"/bookings?limit=4&room_id={a}" + (f"&cursor={cursor}" if cursor else "")
        s, body, _ = api.call("GET", q)
        assert s == 200
        seen += body["bookings"]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 6 and all(x["room_id"] == a for x in seen)
    assert len({x["id"] for x in seen}) == 6


def test_paging_ordering_stable_across_pages(api):
    r = api.room()
    created = [create_booking_ok(api, r, "09:00", "09:15")["id"]]
    created += [create_booking_ok(api, api.room(f"R{i}"), "09:00", "09:15")["id"] for i in range(4)]
    s, p1, _ = api.call("GET", "/bookings?limit=3")
    s, p2, _ = api.call("GET", f"/bookings?limit=3&cursor={p1['next_cursor']}")
    assert ids(p1["bookings"] + p2["bookings"]) == created


def test_empty_list_has_null_cursor(api):
    s, body, _ = api.call("GET", "/bookings")
    assert s == 200 and body["next_cursor"] is None and body["bookings"] == []


def test_list_entries_have_booking_shape(api):
    r = api.room()
    created = create_booking_ok(api, r, "09:00", "10:00", title="T", attendees=3)
    s, body, _ = api.call("GET", "/bookings")
    assert body["bookings"] == [created]


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def free(api, room, date="2026-03-02"):
    s, b, h = api.call("GET", f"/rooms/{room}/availability?date={date}")
    assert s == 200, (s, b)
    assert h["content-type"].startswith("application/json")
    assert b["date"] == date
    assert set(b) == {"date", "free"}
    return [(x["start"], x["end"]) for x in b["free"]]


def gap(a, b, day="2026-03-02"):
    return (f"{day}T{a}:00Z", f"{day}T{b}:00Z")


def test_availability_empty_room_is_whole_working_day(api):
    r = api.room()
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_gaps_around_booking(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    assert free(api, r) == [gap("08:00", "09:00"), gap("10:00", "18:00")]


def test_availability_multiple_gaps_ascending(api):
    r = api.room()
    create_booking_ok(api, r, "15:00", "16:00")
    create_booking_ok(api, r, "09:00", "10:00")
    create_booking_ok(api, r, "11:30", "12:00")
    assert free(api, r) == [gap("08:00", "09:00"), gap("10:00", "11:30"),
                            gap("12:00", "15:00"), gap("16:00", "18:00")]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = api.room()
    create_booking_ok(api, r, "08:00", "09:00")
    create_booking_ok(api, r, "09:00", "10:00")
    create_booking_ok(api, r, "10:00", "11:00")
    assert free(api, r) == [gap("11:00", "18:00")]


def test_availability_fully_booked_day_is_empty(api):
    r = api.room()
    create_booking_ok(api, r, "08:00", "13:00")
    create_booking_ok(api, r, "13:00", "18:00")
    assert free(api, r) == []


def test_availability_booking_clipped_at_start_of_working_day(api):
    r = api.room()
    s, _, _ = post_booking(api, booking_body(r, start=day_ts("07:00"), end=day_ts("09:00")))
    assert s == 201
    assert free(api, r) == [gap("09:00", "18:00")]


def test_availability_booking_clipped_at_end_of_working_day(api):
    r = api.room()
    s, _, _ = post_booking(api, booking_body(r, start=day_ts("17:00"), end=day_ts("19:00")))
    assert s == 201
    assert free(api, r) == [gap("08:00", "17:00")]


def test_availability_booking_entirely_outside_working_day_ignored(api):
    r = api.room()
    s, _, _ = post_booking(api, booking_body(r, start=day_ts("06:00"), end=day_ts("07:00")))
    assert s == 201
    s, _, _ = post_booking(api, booking_body(r, start=day_ts("18:00"), end=day_ts("19:00")))
    assert s == 201
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_booking_spanning_previous_midnight_outside_day_ignored(api):
    r = api.room()
    s, _, _ = post_booking(api, booking_body(r, start="2026-03-01T22:00:00Z", end=day_ts("06:00")))
    assert s == 201
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_booking_ending_after_midnight_before_workday_ignored(api):
    r = api.room()
    s, _, _ = post_booking(api, booking_body(r, start="2026-03-01T18:00:00Z", end=day_ts("02:00")))
    assert s == 201
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_booking_on_other_date_does_not_matter(api):
    r = api.room()
    s, _, _ = post_booking(api, booking_body(r, start=day_ts("09:00", "2026-03-03"),
                                             end=day_ts("10:00", "2026-03-03")))
    assert s == 201
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_other_rooms_bookings_do_not_matter(api):
    r = api.room("A")
    other = api.room("B")
    create_booking_ok(api, other, "09:00", "10:00")
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_reflects_delete(api):
    r = api.room()
    create_booking_ok(api, r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert free(api, r) == [gap("08:00", "18:00")]


def test_availability_missing_date_422(api):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability"), 422, "validation_error")


@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-13-01", "2026-00-10", "2026-3-2", "03-02-2026",
    "2026/03/02", "2026-03-02T00:00:00Z", "", "tomorrow", "2026-02-29",
])
def test_availability_invalid_date_422(api, date):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability?date={date}"), 422, "validation_error")


def test_availability_leap_day_valid(api):
    r = api.room()
    assert free(api, r, "2028-02-29") == [gap("08:00", "18:00", "2028-02-29")]


def test_availability_unknown_room_404(api):
    assert_error(api.call("GET", "/rooms/r99/availability?date=2026-03-02"), 404, "not_found")


def test_availability_room_check_precedes_date_check(api):
    assert_error(api.call("GET", "/rooms/r99/availability"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r99/availability?date=bad"), 404, "not_found")


def test_availability_booking_free_list_matches_real_bookings_after_many_ops(api):
    r = api.room()
    create_booking_ok(api, r, "08:00", "08:30")
    create_booking_ok(api, r, "09:00", "09:30")
    create_booking_ok(api, r, "17:30", "18:00")
    api.call("DELETE", "/bookings/b2")
    create_booking_ok(api, r, "09:15", "09:45")
    assert free(api, r) == [gap("08:30", "09:15"), gap("09:45", "17:30")]
