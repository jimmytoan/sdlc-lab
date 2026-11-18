"""Black-box regression suite for the bookings API, written from SPEC.md.

Every test starts a fresh server (via the `api` fixture in helpers.py), so state
is always empty at the start of a test.
"""
import datetime as dt
import json

import pytest

from helpers import api, code  # noqa: F401  (api is a pytest fixture)


# ---------------------------------------------------------------- helpers ---

def fmt(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


BASE = dt.datetime(2026, 3, 2, 0, 0, 0)


def at(hh, mm=0, day=0):
    """Timestamp on 2026-03-02 (+day offset) at hh:mm, as the spec's string form."""
    return fmt(BASE + dt.timedelta(days=day, hours=hh, minutes=mm))


def make_booking(api, room, start, end, title="sync", attendees=2, headers=None):
    """Creates a booking with full timestamps; returns (status, body, headers)."""
    s, b, h = api.call("POST", "/bookings", {
        "room_id": room, "start": start, "end": end,
        "title": title, "attendees": attendees}, headers=headers)
    return s, b, h


def list_bookings(api, query=""):
    s, b, _ = api.call("GET", "/bookings" + query)
    assert s == 200, (s, b)
    return b


def all_pages(api, query="", limit=None):
    """Walks every page and returns the concatenated booking list and the page count."""
    sep = "&" if "?" in query else "?"
    base_q = query
    if limit is not None:
        base_q = f"{query}{sep}limit={limit}"
        sep = "&"
    out, cursor, pages = [], None, 0
    while True:
        q = base_q
        if cursor is not None:
            q = f"{base_q}{sep if '?' in base_q else '?'}cursor={cursor}"
        b = list_bookings(api, q)
        pages += 1
        out.extend(b["bookings"])
        cursor = b["next_cursor"]
        if cursor is None:
            return out, pages
        assert pages < 1000, "pagination does not terminate"


def assert_error(resp, status, err_code):
    s, b, h = resp
    assert s == status, (s, b)
    assert isinstance(b, dict) and set(b) == {"error"}, b
    assert isinstance(b["error"], dict)
    assert b["error"].get("code") == err_code, b
    assert isinstance(b["error"].get("message"), str)
    assert h.get("content-type", "").startswith("application/json")


# ------------------------------------------------------------ general rules ---

def test_responses_with_body_are_json(api):
    s, b, h = api.call("POST", "/rooms", {"name": "A", "capacity": 1})
    assert s == 201
    assert h["content-type"].startswith("application/json")
    s, b, h = api.call("GET", "/rooms")
    assert h["content-type"].startswith("application/json")
    s, b, h = api.call("GET", "/nope")
    assert s == 404
    assert h["content-type"].startswith("application/json")


def test_unknown_path_is_404_not_found(api):
    assert_error(api.call("GET", "/nope"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r1/nothing/here"), 404, "not_found")
    assert_error(api.call("GET", "/"), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("PATCH", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("POST", "/rooms/r1/availability?date=2026-03-02"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
])
def test_wrong_method_is_405(api, method, path):
    api.room()  # r1 exists
    api.book("r1", "09:00", "10:00")  # b1 exists
    assert_error(api.call(method, path), 405, "method_not_allowed")


def test_invalid_json_body_is_400(api):
    for raw in [b"{not json", b"", b"null", b"[]", b"5", b'"text"', b"true"]:
        resp = api.call("POST", "/rooms", raw=raw, headers={"Content-Type": "application/json"})
        assert_error(resp, 400, "bad_json")


def test_invalid_json_body_is_400_for_bookings(api):
    api.room()
    for raw in [b"{", b"[1,2]", b"42"]:
        resp = api.call("POST", "/bookings", raw=raw, headers={"Content-Type": "application/json"})
        assert_error(resp, 400, "bad_json")


def test_unknown_fields_are_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": 3, "colour": "red"})
    assert s == 201 and b == {"id": "r1", "name": "A", "capacity": 3}
    s, b, _ = api.call("POST", "/bookings", {
        "room_id": "r1", "start": at(9), "end": at(10), "title": "t",
        "attendees": 1, "extra": {"x": 1}})
    assert s == 201 and set(b) == {"id", "room_id", "start", "end", "title", "attendees"}


# -------------------------------------------------------------------- rooms ---

def test_create_room_response_shape(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}


def test_room_ids_follow_creation_order(api):
    ids = [api.room(name=f"Room {i}") for i in range(5)]
    assert ids == ["r1", "r2", "r3", "r4", "r5"]


def test_room_name_is_trimmed(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "   Atlas \t", "capacity": 4})
    assert s == 201 and b["name"] == "Atlas"
    s, got, _ = api.call("GET", "/rooms/r1")
    assert got["name"] == "Atlas"


@pytest.mark.parametrize("name", ["A", "x" * 50, "  " + "y" * 50 + "  "])
def test_room_name_length_boundaries_accepted(api, name):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 1})
    assert s == 201, b
    assert b["name"] == name.strip()


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "z" * 51, " " + "z" * 51 + " "])
def test_room_name_length_out_of_range_rejected(api, name):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 1})
    assert_error((s, b, _), 422, "validation_error")


@pytest.mark.parametrize("name", [None, 5, ["Atlas"], {"n": 1}, True])
def test_room_name_wrong_type_rejected(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("capacity", [1, 500])
def test_room_capacity_boundaries_accepted(api, capacity):
    s, b, _ = api.call("POST", "/rooms", {"name": f"c{capacity}", "capacity": capacity})
    assert s == 201 and b["capacity"] == capacity


@pytest.mark.parametrize("capacity", [0, -1, 501, 10_000, None, "8", True, False, [8], 2.5])
def test_room_capacity_invalid_rejected(api, capacity):
    assert_error(api.call("POST", "/rooms", {"name": "A", "capacity": capacity}), 422, "validation_error")


@pytest.mark.parametrize("body", [
    {"capacity": 8},
    {"name": "A"},
    {},
])
def test_room_missing_fields_rejected(api, body):
    assert_error(api.call("POST", "/rooms", body), 422, "validation_error")


def test_room_duplicate_name_is_case_insensitive_and_trimmed(api):
    assert api.call("POST", "/rooms", {"name": "Atlas", "capacity": 1})[0] == 201
    assert_error(api.call("POST", "/rooms", {"name": "ATLAS", "capacity": 2}), 409, "room_exists")
    assert_error(api.call("POST", "/rooms", {"name": "  atlas  ", "capacity": 2}), 409, "room_exists")
    # a different name is fine
    assert api.call("POST", "/rooms", {"name": "Atlas 2", "capacity": 2})[0] == 201


def test_rejected_room_does_not_consume_an_id(api):
    assert api.call("POST", "/rooms", {"name": "", "capacity": 1})[0] == 422
    assert api.call("POST", "/rooms", {"name": "A", "capacity": 1})[1]["id"] == "r1"
    assert api.call("POST", "/rooms", {"name": "a", "capacity": 1})[0] == 409
    assert api.call("POST", "/rooms", {"name": "B", "capacity": 1})[1]["id"] == "r2"


def test_list_rooms_empty_then_creation_order(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}
    api.room("Zeta", 1)
    api.room("Alpha", 2)
    s, b, _ = api.call("GET", "/rooms")
    assert [r["name"] for r in b["rooms"]] == ["Zeta", "Alpha"]
    assert b["rooms"][0] == {"id": "r1", "name": "Zeta", "capacity": 1}


def test_get_room(api):
    api.room("Atlas", 8)
    s, b, _ = api.call("GET", "/rooms/r1")
    assert s == 200 and b == {"id": "r1", "name": "Atlas", "capacity": 8}


def test_get_unknown_room_is_404(api):
    api.room()
    assert_error(api.call("GET", "/rooms/r99"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/nope"), 404, "not_found")


# ----------------------------------------------------------- timestamps ---

@pytest.mark.parametrize("ts_value", [
    "2026-03-02T09:30:00Z",
    "2026-03-02T00:00:00Z",
    "2026-03-02T23:45:00Z",
    "2028-02-29T08:15:00Z",  # leap day
])
def test_valid_timestamp_forms_accepted(api, ts_value):
    api.room()
    end = fmt(dt.datetime.strptime(ts_value, "%Y-%m-%dT%H:%M:%SZ") + dt.timedelta(minutes=15))
    s, b, _ = make_booking(api, "r1", ts_value, end)
    assert s == 201, b
    assert b["start"] == ts_value and b["end"] == end


@pytest.mark.parametrize("bad", [
    "2026-03-02T09:30:00",          # no Z
    "2026-03-02T09:30:00+00:00",    # offset
    "2026-03-02T09:30:00.000Z",     # fractional seconds
    "2026-03-02 09:30:00Z",         # space instead of T
    "2026-03-02t09:30:00Z",         # lower-case t
    "2026-3-2T09:30:00Z",           # not zero padded
    "2026-03-02",                   # date only
    "09:30",                        # time only
    "2026-02-30T09:00:00Z",         # not a real date
    "2027-02-29T09:00:00Z",         # not a leap year
    "2026-03-02T24:00:00Z",         # invalid hour
    "2026-03-02T09:60:00Z",         # invalid minute
    "not a timestamp",
    "",
])
def test_invalid_timestamp_forms_rejected(api, bad):
    api.room()
    s, b, _ = make_booking(api, "r1", bad, at(12))
    assert_error((s, b, _), 422, "validation_error")
    s, b, _ = make_booking(api, "r1", at(9), bad)
    assert_error((s, b, _), 422, "validation_error")


@pytest.mark.parametrize("hh,mm", [(9, 1), (9, 10), (9, 14), (9, 29), (9, 59), (9, 46)])
def test_start_minutes_must_be_quarter_hours(api, hh, mm):
    api.room()
    s, b, _ = make_booking(api, "r1", at(hh, mm), at(hh + 1))
    assert_error((s, b, _), 422, "validation_error")
    s, b, _ = make_booking(api, "r1", at(hh), at(hh + 1, mm))
    assert_error((s, b, _), 422, "validation_error")


@pytest.mark.parametrize("mm", [0, 15, 30, 45])
def test_quarter_hour_minutes_accepted(api, mm):
    api.room()
    s, b, _ = make_booking(api, "r1", at(9, mm), at(9, mm + 15))
    assert s == 201, b


def test_seconds_must_be_zero(api):
    api.room()
    assert_error(make_booking(api, "r1", "2026-03-02T09:00:01Z", at(10)), 422, "validation_error")
    assert_error(make_booking(api, "r1", at(9), "2026-03-02T10:00:30Z"), 422, "validation_error")


# -------------------------------------------------------------- bookings ---

def test_create_booking_response_shape(api):
    r = api.room()
    s, b, h = make_booking(api, r, at(9, 30), at(10, 15), title="Planning", attendees=4)
    assert s == 201
    assert h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": "r1", "start": at(9, 30), "end": at(10, 15),
                 "title": "Planning", "attendees": 4}


def test_booking_ids_follow_creation_order(api):
    r = api.room()
    ids = [make_booking(api, r, at(i), at(i + 1))[1]["id"] for i in range(4)]
    assert ids == ["b1", "b2", "b3", "b4"]


def test_booking_title_trimmed_and_length_bounds(api):
    r = api.room()
    s, b, _ = make_booking(api, r, at(9), at(10), title="  Sync  ")
    assert s == 201 and b["title"] == "Sync"
    s, b, _ = make_booking(api, r, at(10), at(11), title="t" * 100)
    assert s == 201 and b["title"] == "t" * 100
    s, b, _ = make_booking(api, r, at(11), at(12), title="  " + "u" * 100 + "  ")
    assert s == 201 and b["title"] == "u" * 100


@pytest.mark.parametrize("title", ["", "   ", "v" * 101, " " + "v" * 101 + " ", None, 7])
def test_booking_title_invalid_rejected(api, title):
    r = api.room()
    assert_error(make_booking(api, r, at(9), at(10), title=title), 422, "validation_error")


@pytest.mark.parametrize("attendees", [1, 8])
def test_attendees_valid_values(api, attendees):
    r = api.room(capacity=8)
    s, b, _ = make_booking(api, r, at(9), at(10), attendees=attendees)
    assert s == 201 and b["attendees"] == attendees


@pytest.mark.parametrize("attendees", [0, -3, True, False, "4", None, 2.5])
def test_attendees_invalid_rejected(api, attendees):
    r = api.room(capacity=8)
    assert_error(make_booking(api, r, at(9), at(10), attendees=attendees), 422, "validation_error")


@pytest.mark.parametrize("missing", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field_rejected(api, missing):
    api.room()
    body = {"room_id": "r1", "start": at(9), "end": at(10), "title": "t", "attendees": 1}
    del body[missing]
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


@pytest.mark.parametrize("field,value", [
    ("room_id", 1), ("room_id", None), ("start", 900), ("end", None), ("title", ["t"]),
])
def test_booking_wrong_type_rejected(api, field, value):
    api.room()
    body = {"room_id": "r1", "start": at(9), "end": at(10), "title": "t", "attendees": 1}
    body[field] = value
    assert_error(api.call("POST", "/bookings", body), 422, "validation_error")


def test_end_must_be_after_start(api):
    r = api.room()
    assert_error(make_booking(api, r, at(10), at(9)), 422, "validation_error")
    assert_error(make_booking(api, r, at(10), at(10)), 422, "validation_error")


def test_booking_duration_limit_is_eight_hours(api):
    r = api.room(capacity=8)
    assert make_booking(api, r, at(8), at(16))[0] == 201  # exactly 8h
    assert_error(make_booking(api, r, at(9), at(17, 15)), 422, "validation_error")  # 8h15


def test_booking_may_span_midnight(api):
    r = api.room()
    s, b, _ = make_booking(api, r, at(23), at(0, 0, day=1))
    assert s == 201 and b["end"] == at(0, 0, day=1)


def test_booking_outside_working_hours_is_allowed(api):
    r = api.room()
    assert make_booking(api, r, at(3), at(4))[0] == 201


def test_booking_room_id_must_exist(api):
    api.room()
    assert_error(make_booking(api, "r9", at(9), at(10)), 404, "not_found")


def test_validation_precedes_room_lookup(api):
    # Unknown room AND invalid body: validation wins, so 422.
    assert_error(make_booking(api, "r9", at(9), at(10), attendees=0), 422, "validation_error")
    assert_error(make_booking(api, "r9", at(9, 7), at(10)), 422, "validation_error")


def test_room_lookup_precedes_capacity_and_conflict(api):
    r = api.room(capacity=1)
    assert_error(make_booking(api, "r9", at(9), at(10), attendees=5), 404, "not_found")


def test_over_capacity(api):
    r = api.room(capacity=2)
    assert_error(make_booking(api, r, at(9), at(10), attendees=3), 422, "over_capacity")
    assert make_booking(api, r, at(9), at(10), attendees=2)[0] == 201


def test_over_capacity_precedes_conflict(api):
    r = api.room(capacity=2)
    assert make_booking(api, r, at(9), at(10), attendees=2)[0] == 201
    # overlaps AND over capacity -> over_capacity wins
    assert_error(make_booking(api, r, at(9), at(10), attendees=3), 422, "over_capacity")


@pytest.mark.parametrize("start,end", [
    (at(9, 30), at(10, 30)),   # overlaps the end
    (at(8, 30), at(9, 30)),    # overlaps the start
    (at(8), at(11)),           # contains existing
    (at(9, 15), at(9, 45)),    # contained in existing
    (at(9), at(10)),           # identical
])
def test_overlapping_booking_conflicts(api, start, end):
    r = api.room()
    assert make_booking(api, r, at(9), at(10))[0] == 201
    assert_error(make_booking(api, r, start, end), 409, "conflict")


def test_back_to_back_bookings_do_not_conflict(api):
    r = api.room()
    assert make_booking(api, r, at(9), at(10))[0] == 201
    assert make_booking(api, r, at(10), at(11))[0] == 201  # starts when other ends
    assert make_booking(api, r, at(8), at(9))[0] == 201    # ends when other starts


def test_same_slot_in_other_room_is_fine(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    assert make_booking(api, r1, at(9), at(10))[0] == 201
    assert make_booking(api, r2, at(9), at(10))[0] == 201


def test_conflict_check_ignores_rejected_bookings(api):
    r = api.room(capacity=2)
    assert make_booking(api, r, at(9), at(10), attendees=5)[0] == 422
    assert make_booking(api, r, at(9), at(10), attendees=2)[0] == 201


def test_get_booking(api):
    r = api.room()
    s, created, _ = make_booking(api, r, at(9), at(10), title="X", attendees=1)
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 200 and b == created


def test_get_unknown_booking_is_404(api):
    api.room()
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/xyz"), 404, "not_found")


def test_delete_booking(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    s, b, h = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    assert api.call("GET", "/bookings/b1")[0] == 404


def test_delete_twice_is_404(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_delete_unknown_is_404(api):
    api.room()
    assert_error(api.call("DELETE", "/bookings/b42"), 404, "not_found")


def test_deleted_slot_is_free_again(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    api.call("DELETE", "/bookings/b1")
    assert make_booking(api, r, at(9), at(10))[0] == 201


def test_ids_are_never_reused_after_delete(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))   # b1
    make_booking(api, r, at(11), at(12))  # b2
    api.call("DELETE", "/bookings/b2")
    s, b, _ = make_booking(api, r, at(13), at(14))
    assert b["id"] == "b3"
    api.call("DELETE", "/bookings/b3")
    s, b, _ = make_booking(api, r, at(15), at(16))
    assert b["id"] == "b4"
    assert api.call("GET", "/bookings/b2")[0] == 404


def test_rejected_booking_does_not_consume_an_id(api):
    r = api.room(capacity=1)
    assert make_booking(api, r, at(9), at(10), attendees=5)[0] == 422
    assert make_booking(api, "r9", at(9), at(10))[0] == 404
    assert make_booking(api, r, at(9), at(10), attendees=1)[1]["id"] == "b1"


def test_booking_is_bound_to_its_room(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    make_booking(api, r2, at(9), at(10))
    s, b, _ = make_booking(api, r1, at(9), at(10))
    assert s == 201 and b["room_id"] == r1


# ----------------------------------------------------------- idempotency ---

def test_idempotent_replay_returns_original_and_creates_nothing(api):
    r = api.room()
    h = {"Idempotency-Key": "k-1"}
    s1, b1, _ = make_booking(api, r, at(9), at(10), title="Once", headers=h)
    assert s1 == 201
    s2, b2, _ = make_booking(api, r, at(9), at(10), title="Once", headers=h)
    assert s2 == 201 and b2 == b1
    assert len(list_bookings(api)["bookings"]) == 1
    # next new booking still gets the next id, i.e. the replay created nothing
    assert make_booking(api, r, at(11), at(12))[1]["id"] == "b2"


def test_idempotent_replay_ignores_json_key_order_and_whitespace(api):
    r = api.room()
    h = {"Idempotency-Key": "k-2", "Content-Type": "application/json"}
    raw1 = json.dumps({"room_id": r, "start": at(9), "end": at(10), "title": "t", "attendees": 2}).encode()
    raw2 = (b'{ "attendees" : 2,\n "title":"t" , "end":"' + at(10).encode() +
            b'", "start": "' + at(9).encode() + b'", "room_id": "' + r.encode() + b'" }')
    s1, b1, _ = api.call("POST", "/bookings", raw=raw1, headers=h)
    s2, b2, _ = api.call("POST", "/bookings", raw=raw2, headers=h)
    assert s1 == 201 and s2 == 201 and b1 == b2
    assert len(list_bookings(api)["bookings"]) == 1


def test_idempotent_replay_survives_unknown_field_difference_only_if_body_equal(api):
    r = api.room()
    h = {"Idempotency-Key": "k-3"}
    assert make_booking(api, r, at(9), at(10), headers=h)[0] == 201
    # an extra unknown field changes the parsed body -> mismatch
    s, b, _ = api.call("POST", "/bookings", {"room_id": r, "start": at(9), "end": at(10),
                                             "title": "sync", "attendees": 2, "note": "x"},
                       headers=h)
    assert_error((s, b, _), 422, "idempotency_mismatch")


def test_idempotency_key_with_different_body_is_mismatch(api):
    r = api.room()
    h = {"Idempotency-Key": "k-4"}
    s1, b1, _ = make_booking(api, r, at(9), at(10), title="A", headers=h)
    assert s1 == 201
    assert_error(make_booking(api, r, at(9), at(10), title="B", headers=h), 422, "idempotency_mismatch")
    assert_error(make_booking(api, r, at(11), at(12), title="A", headers=h), 422, "idempotency_mismatch")
    assert_error(make_booking(api, r, at(9), at(10), title="A", attendees=1, headers=h),
                 422, "idempotency_mismatch")
    # mismatches create nothing
    assert len(list_bookings(api)["bookings"]) == 1


def test_idempotency_key_is_per_key(api):
    r = api.room()
    assert make_booking(api, r, at(9), at(10), headers={"Idempotency-Key": "a"})[0] == 201
    # a different key with the same body is a new request -> conflict with existing booking
    assert_error(make_booking(api, r, at(9), at(10), headers={"Idempotency-Key": "b"}), 409, "conflict")


def test_failed_request_with_key_is_not_remembered(api):
    r = api.room(capacity=1)
    h = {"Idempotency-Key": "k-5"}
    # fails validation
    assert_error(make_booking(api, r, at(9), at(10), attendees=0, headers=h), 422, "validation_error")
    # the same key is now usable for a valid request
    assert make_booking(api, r, at(9), at(10), attendees=1, headers=h)[0] == 201


def test_failed_conflict_request_with_key_is_not_remembered(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))  # b1
    h = {"Idempotency-Key": "k-6"}
    assert_error(make_booking(api, r, at(9, 30), at(10, 30), headers=h), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = make_booking(api, r, at(9, 30), at(10, 30), headers=h)
    assert s == 201 and b["start"] == at(9, 30)


def test_failed_not_found_with_key_is_not_remembered(api):
    h = {"Idempotency-Key": "k-7"}
    assert_error(make_booking(api, "r1", at(9), at(10), headers=h), 404, "not_found")
    api.room()
    assert make_booking(api, "r1", at(9), at(10), headers=h)[0] == 201


def test_replay_after_delete_does_not_recreate(api):
    r = api.room()
    h = {"Idempotency-Key": "k-8"}
    s1, b1, _ = make_booking(api, r, at(9), at(10), headers=h)
    api.call("DELETE", "/bookings/b1")
    s2, b2, _ = make_booking(api, r, at(9), at(10), headers=h)
    assert s2 == 201 and b2 == b1
    assert api.call("GET", "/bookings/b1")[0] == 404
    assert list_bookings(api)["bookings"] == []


def test_idempotency_key_only_applies_to_booking_post(api):
    r = api.room()
    s, b, _ = api.call("POST", "/rooms", {"name": "Other", "capacity": 1},
                       headers={"Idempotency-Key": "room-key"})
    assert s == 201 and b["id"] == "r2"
    s, b, _ = api.call("POST", "/rooms", {"name": "Else", "capacity": 3},
                       headers={"Idempotency-Key": "room-key"})
    assert s == 201 and b["id"] == "r3"


# ---------------------------------------------------------- list bookings ---

def test_list_bookings_empty(api):
    assert list_bookings(api) == {"bookings": [], "next_cursor": None}


def test_list_bookings_sorted_by_start_then_creation(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    make_booking(api, r1, at(11), at(12))   # b1
    make_booking(api, r2, at(9), at(10))    # b2
    make_booking(api, r1, at(9), at(10))    # b3 same start as b2, created later
    make_booking(api, r2, at(8), at(9))     # b4
    b = list_bookings(api)
    assert [x["id"] for x in b["bookings"]] == ["b4", "b2", "b3", "b1"]
    assert b["next_cursor"] is None


def test_list_bookings_filter_by_room(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    make_booking(api, r1, at(9), at(10))
    make_booking(api, r2, at(9), at(10))
    make_booking(api, r1, at(11), at(12))
    b = list_bookings(api, f"?room_id={r1}")
    assert [x["id"] for x in b["bookings"]] == ["b1", "b3"]
    assert all(x["room_id"] == r1 for x in b["bookings"])


def test_list_bookings_unknown_room_gives_empty_list(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    assert list_bookings(api, "?room_id=r999") == {"bookings": [], "next_cursor": None}


def test_list_bookings_window_overlap_semantics(api):
    r = api.room()
    r2 = api.room("Other", 8)
    make_booking(api, r, at(8), at(9))    # b1 ends exactly at window start -> excluded
    make_booking(api, r, at(9), at(10))   # b2 inside
    make_booking(api, r2, at(7), at(12))  # b3 spans window (other room, so no conflict)
    make_booking(api, r, at(11), at(12))  # b4 starts exactly at window end -> excluded
    make_booking(api, r, at(12), at(13))  # b5 after
    b = list_bookings(api, f"?from={at(9)}&to={at(11)}")
    assert [x["id"] for x in b["bookings"]] == ["b3", "b2"]  # sorted by start


def test_list_bookings_from_only(api):
    r = api.room()
    make_booking(api, r, at(8), at(9))    # excluded: ends at from
    make_booking(api, r, at(9), at(10))   # included
    make_booking(api, r, at(20), at(21))  # included
    b = list_bookings(api, f"?from={at(9)}")
    assert [x["id"] for x in b["bookings"]] == ["b2", "b3"]


def test_list_bookings_to_only(api):
    r = api.room()
    make_booking(api, r, at(8), at(9))    # included
    make_booking(api, r, at(9), at(10))   # included: starts before to
    make_booking(api, r, at(10), at(11))  # excluded: starts at to
    b = list_bookings(api, f"?to={at(10)}")
    assert [x["id"] for x in b["bookings"]] == ["b1", "b2"]


def test_list_bookings_window_not_aligned_to_quarter_hours(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))   # b1
    make_booking(api, r, at(11), at(12))  # b2
    b = list_bookings(api, f"?from={at(9, 20)}&to={at(11, 10)}")
    assert [x["id"] for x in b["bookings"]] == ["b1", "b2"]
    b = list_bookings(api, f"?from={at(10, 10)}&to={at(10, 50)}")
    assert b["bookings"] == []


@pytest.mark.parametrize("query", [
    "?from=2026-03-02",
    "?from=garbage",
    "?to=2026-03-02T09:00:00",
    "?from=2026-02-30T00:00:00Z",
    "?to=2026-03-02T25:00:00Z",
    "?limit=0",
    "?limit=101",
    "?limit=-1",
    "?limit=abc",
    "?limit=2.5",
    "?cursor=not-a-cursor-at-all-zzz",
])
def test_list_bookings_invalid_query_is_422(api, query):
    api.room()
    make_booking(api, "r1", at(9), at(10))
    assert_error(api.call("GET", "/bookings" + query), 422, "validation_error")


def test_list_bookings_limit_boundaries(api):
    r = api.room()
    for i in range(3):
        make_booking(api, r, at(i), at(i + 1))
    assert len(list_bookings(api, "?limit=1")["bookings"]) == 1
    assert len(list_bookings(api, "?limit=100")["bookings"]) == 3


def test_list_bookings_default_limit_is_20(api):
    r = api.room()
    for i in range(25):
        make_booking(api, r, at(i), at(i + 1))
    page1 = list_bookings(api)
    assert len(page1["bookings"]) == 20
    assert page1["next_cursor"] is not None
    page2 = list_bookings(api, f"?cursor={page1['next_cursor']}")
    assert len(page2["bookings"]) == 5
    assert page2["next_cursor"] is None


def test_pagination_returns_every_booking_exactly_once(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    created = []
    for i in range(12):
        room = r1 if i % 2 == 0 else r2
        start = at(i // 2) if i % 2 == 0 else at(i // 2, 30)
        created.append(make_booking(api, room, start, fmt(dt.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ") + dt.timedelta(minutes=30)))[1]["id"])
    for limit in (1, 2, 5, 7, 100):
        got, pages = all_pages(api, limit=limit)
        assert sorted(x["id"] for x in got) == sorted(created)
        assert len({x["id"] for x in got}) == len(got) == len(created)
        assert pages == -(-len(created) // limit)


def test_pagination_order_matches_unpaged_order(api):
    r = api.room()
    for i in range(7):
        make_booking(api, r, at(7 - i), at(8 - i))  # created in reverse time order
    full = list_bookings(api, "?limit=100")["bookings"]
    paged, _ = all_pages(api, limit=2)
    assert [x["id"] for x in paged] == [x["id"] for x in full]
    assert [x["start"] for x in full] == sorted(x["start"] for x in full)


def test_pagination_respects_filters(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    for i in range(6):
        make_booking(api, r1, at(i), at(i + 1))
        make_booking(api, r2, at(i, 30), at(i + 1, 30))
    got, pages = all_pages(api, query=f"?room_id={r1}", limit=4)
    assert len(got) == 6 and all(x["room_id"] == r1 for x in got)
    assert pages == 2
    got, _ = all_pages(api, query=f"?from={at(2)}&to={at(4)}", limit=1)
    assert got
    assert all(x["end"] > at(2) and x["start"] < at(4) for x in got)
    assert len(got) == len(list_bookings(api, f"?from={at(2)}&to={at(4)}&limit=100")["bookings"])


def test_pagination_next_cursor_null_on_last_page(api):
    r = api.room()
    for i in range(3):
        make_booking(api, r, at(i), at(i + 1))
    b = list_bookings(api, "?limit=2")
    assert len(b["bookings"]) == 2 and b["next_cursor"] is not None
    b2 = list_bookings(api, f"?limit=2&cursor={b['next_cursor']}")
    assert len(b2["bookings"]) == 1 and b2["next_cursor"] is None


def test_exact_page_size_has_no_next_cursor_on_last_page_walk(api):
    r = api.room()
    for i in range(4):
        make_booking(api, r, at(i), at(i + 1))
    got, pages = all_pages(api, limit=2)
    assert len(got) == 4 and pages == 2


def test_list_bookings_reflects_deletes(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    make_booking(api, r, at(11), at(12))
    api.call("DELETE", "/bookings/b1")
    assert [x["id"] for x in list_bookings(api)["bookings"]] == ["b2"]


# ---------------------------------------------------------- availability ---

def free_of(api, room, date="2026-03-02"):
    s, b, _ = api.call("GET", f"/rooms/{room}/availability?date={date}")
    assert s == 200, (s, b)
    assert b["date"] == date
    assert set(b) == {"date", "free"}
    return b["free"]


def window(a, b, day="2026-03-02"):
    return {"start": f"{day}T{a}:00Z", "end": f"{day}T{b}:00Z"}


def test_availability_empty_room_is_whole_working_day(api):
    r = api.room()
    assert free_of(api, r) == [window("08:00", "18:00")]


def test_availability_gaps_between_bookings(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    make_booking(api, r, at(12), at(13, 30))
    assert free_of(api, r) == [window("08:00", "09:00"), window("10:00", "12:00"), window("13:30", "18:00")]


def test_availability_example_from_spec(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert free_of(api, r) == [window("08:00", "09:00"), window("10:00", "18:00")]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    make_booking(api, r, at(10), at(11))
    make_booking(api, r, at(11, 15), at(12))  # gap of 15 min (11:00-11:15)
    assert free_of(api, r) == [window("08:00", "09:00"), window("11:00", "11:15"), window("12:00", "18:00")]


def test_availability_fully_booked_day_is_empty(api):
    r = api.room(capacity=8)
    make_booking(api, r, at(8), at(12))
    make_booking(api, r, at(12), at(18))
    assert free_of(api, r) == []


def test_availability_clips_bookings_partly_outside_working_day(api):
    r = api.room()
    make_booking(api, r, at(7), at(9))    # clipped to 08:00-09:00
    make_booking(api, r, at(17), at(19))  # clipped to 17:00-18:00
    assert free_of(api, r) == [window("09:00", "17:00")]


def test_availability_ignores_bookings_fully_outside_working_day(api):
    r = api.room()
    make_booking(api, r, at(5), at(7))
    make_booking(api, r, at(18), at(20))
    assert free_of(api, r) == [window("08:00", "18:00")]


def test_availability_booking_covering_whole_day_clipped(api):
    r = api.room()
    make_booking(api, r, at(6), at(14))   # 8h, clipped to 08:00-14:00
    make_booking(api, r, at(14), at(22))  # 8h, clipped to 14:00-18:00
    assert free_of(api, r) == []


def test_availability_ignores_other_days_and_rooms(api):
    r1 = api.room("A", 8)
    r2 = api.room("B", 8)
    make_booking(api, r1, at(9, day=1), at(10, day=1))  # 2026-03-03
    make_booking(api, r2, at(9), at(10))                # other room, same day
    assert free_of(api, r1) == [window("08:00", "18:00")]
    assert free_of(api, r2) == [window("08:00", "09:00"), window("10:00", "18:00")]
    assert free_of(api, r1, "2026-03-03") == [window("08:00", "09:00", "2026-03-03"),
                                              window("10:00", "18:00", "2026-03-03")]


def test_availability_is_a_snapshot_of_current_bookings(api):
    r = api.room()
    make_booking(api, r, at(9), at(10))
    assert len(free_of(api, r)) == 2
    api.call("DELETE", "/bookings/b1")
    assert free_of(api, r) == [window("08:00", "18:00")]


@pytest.mark.parametrize("date", ["2026-02-30", "2026-02-29", "2026-13-01", "2026-00-10",
                                  "2026-3-2", "2026/03/02", "02-03-2026", "garbage", "",
                                  "2026-03-02T00:00:00Z", "2026-04-31"])
def test_availability_invalid_date_is_422(api, date):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability?date={date}"), 422, "validation_error")


def test_availability_missing_date_is_422(api):
    r = api.room()
    assert_error(api.call("GET", f"/rooms/{r}/availability"), 422, "validation_error")


def test_availability_leap_day_is_valid(api):
    r = api.room()
    assert free_of(api, r, "2028-02-29") == [window("08:00", "18:00", "2028-02-29")]


def test_availability_unknown_room_is_404(api):
    api.room()
    assert_error(api.call("GET", "/rooms/r9/availability?date=2026-03-02"), 404, "not_found")


def test_availability_room_check_precedes_date_check(api):
    assert_error(api.call("GET", "/rooms/r9/availability?date=bogus"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r9/availability"), 404, "not_found")
