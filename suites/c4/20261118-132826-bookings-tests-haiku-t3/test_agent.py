"""Regression suite for the bookings API, written black-box from SPEC.md.

Every test starts a fresh server process (via the `api` fixture from helpers.py),
so tests are independent and state always begins empty.
"""
import http.client
import json
import urllib.parse

import pytest

from helpers import api, ts  # noqa: F401  (api is a pytest fixture)

DATE = "2026-03-02"
WORK_LO = ts("08:00")
WORK_HI = ts("18:00")


# ---------------------------------------------------------------- helpers

def room(api_, name="Atlas", capacity=8):
    return api_.room(name, capacity)


def make_booking(api_, room_id, start, end, title="sync", attendees=2, headers=None):
    s, b, _ = api_.book(room_id, start, end, title=title, attendees=attendees, headers=headers)
    assert s == 201, (s, b)
    return b


def bookings_list(api_, query=""):
    s, b, _ = api_.call("GET", "/bookings" + (("?" + query) if query else ""))
    assert s == 200, (s, b)
    return b


def walk_pages(api_, query_params, guard=200):
    """Follow next_cursor until null; return (all bookings, number of pages)."""
    out, pages = [], 0
    cursor = None
    while True:
        params = dict(query_params)
        if cursor is not None:
            params["cursor"] = cursor
        b = bookings_list(api_, urllib.parse.urlencode(params))
        pages += 1
        out.extend(b["bookings"])
        cursor = b["next_cursor"]
        if cursor is None:
            return out, pages
        assert pages < guard, "pagination does not terminate"


def free_pairs(body):
    """Availability 'free' list as (start, end) tuples of timestamp strings."""
    return [(f["start"], f["end"]) for f in body["free"]]


def availability(api_, room_id, date=DATE):
    s, b, _ = api_.call("GET", f"/rooms/{room_id}/availability?date={date}")
    assert s == 200, (s, b)
    return b


def assert_error(resp, status, err_code):
    s, b, h = resp
    assert s == status, (s, b)
    assert isinstance(b, dict) and set(b) == {"error"}, b
    assert isinstance(b["error"], dict) and set(b["error"]) >= {"code", "message"}
    assert isinstance(b["error"]["message"], str)
    assert b["error"]["code"] == err_code, b
    assert h["content-type"].startswith("application/json")


def valid_booking_body(room_id, **overrides):
    body = {"room_id": room_id, "start": ts("09:00"), "end": ts("10:00"),
            "title": "sync", "attendees": 2}
    body.update(overrides)
    return body


def post_booking(api_, body, headers=None):
    return api_.call("POST", "/bookings", body, headers=headers)


def q(value):
    return urllib.parse.quote(value, safe="")


# ---------------------------------------------------------------- transport / general

def test_state_starts_empty(api):
    assert api.call("GET", "/rooms")[1] == {"rooms": []}
    assert bookings_list(api) == {"bookings": [], "next_cursor": None}


def test_json_content_type_on_success_and_error_bodies(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert api.call("GET", "/rooms")[2]["content-type"].startswith("application/json")
    assert api.call("GET", f"/rooms/{r}")[2]["content-type"].startswith("application/json")
    assert api.call("GET", "/bookings")[2]["content-type"].startswith("application/json")
    assert api.call("GET", "/bookings/b1")[2]["content-type"].startswith("application/json")
    assert api.call("GET", "/nope")[2]["content-type"].startswith("application/json")
    assert api.call("POST", "/rooms", raw=b"{")[2]["content-type"].startswith("application/json")


def test_delete_returns_204_with_empty_body(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    conn.request("DELETE", "/bookings/b1")
    resp = conn.getresponse()
    assert resp.status == 204
    assert resp.read() == b""
    conn.close()


def test_unknown_path_is_404_not_found(api):
    assert_error(api.call("GET", "/nope"), 404, "not_found")
    assert_error(api.call("GET", "/"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r1/extra/more"), 404, "not_found")
    assert_error(api.call("DELETE", "/nope"), 404, "not_found")
    assert_error(api.call("POST", "/nope", {}), 404, "not_found")
    assert_error(api.call("PUT", "/nope"), 404, "not_found")


@pytest.mark.parametrize("method,path", [
    ("PUT", "/rooms"),
    ("DELETE", "/rooms"),
    ("PATCH", "/rooms"),
    ("POST", "/rooms/r1"),
    ("PUT", "/rooms/r1"),
    ("DELETE", "/rooms/r1"),
    ("POST", "/rooms/r1/availability?date=2026-03-02"),
    ("DELETE", "/rooms/r1/availability?date=2026-03-02"),
    ("PUT", "/bookings"),
    ("DELETE", "/bookings"),
    ("POST", "/bookings/b1"),
    ("PUT", "/bookings/b1"),
    ("PATCH", "/bookings/b1"),
])
def test_unsupported_method_is_405(api, method, path):
    body = {} if method in ("POST", "PUT", "PATCH") else None
    assert_error(api.call(method, path, body), 405, "method_not_allowed")


@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
def test_bad_json_is_400(api, path):
    assert_error(api.call("POST", path, raw=b"{not json"), 400, "bad_json")
    assert_error(api.call("POST", path, raw=b""), 400, "bad_json")


@pytest.mark.parametrize("raw", [b"[]", b'"text"', b"5", b"null", b"true", b"[1, 2]"])
@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
def test_valid_json_but_not_object_is_400(api, path, raw):
    assert_error(api.call("POST", path, raw=raw), 400, "bad_json")


def test_invalid_utf8_body_is_400(api):
    assert_error(api.call("POST", "/rooms", raw=b'{"name": "\xff\xfe", "capacity": 2}'), 400, "bad_json")


def test_unknown_fields_are_ignored_for_rooms(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8, "colour": "red", "x": [1]})
    assert s == 201 and b == {"id": "r1", "name": "Atlas", "capacity": 8}


def test_unknown_fields_are_ignored_for_bookings(api):
    r = room(api)
    s, b, _ = post_booking(api, valid_booking_body(r, notes="hi", extra={"a": 1}))
    assert s == 201
    assert set(b) == {"id", "room_id", "start", "end", "title", "attendees"}


# ---------------------------------------------------------------- rooms: create

def test_create_room_response_shape(api):
    s, b, h = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8})
    assert s == 201
    assert b == {"id": "r1", "name": "Atlas", "capacity": 8}
    assert h["content-type"].startswith("application/json")


def test_room_ids_are_sequential_in_creation_order(api):
    ids = [room(api, f"Room {i}") for i in range(3)]
    assert ids == ["r1", "r2", "r3"]


def test_room_name_is_stored_trimmed(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  Atlas  ", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    assert api.call("GET", "/rooms/r1")[1]["name"] == "Atlas"


@pytest.mark.parametrize("name", ["A", "x" * 50, "  " + "y" * 50 + "  ", "Ünïcødé", "a b"])
def test_room_name_valid_lengths(api, name):
    s, b, _ = api.call("POST", "/rooms", {"name": name, "capacity": 1})
    assert s == 201
    assert b["name"] == name.strip()


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 51, " " * 10 + "z" * 51])
def test_room_name_invalid(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 8}), 422, "validation_error")


@pytest.mark.parametrize("name", [None, 5, 1.5, ["Atlas"], {"n": 1}, True])
def test_room_name_wrong_type(api, name):
    assert_error(api.call("POST", "/rooms", {"name": name, "capacity": 8}), 422, "validation_error")


def test_room_missing_fields(api):
    assert_error(api.call("POST", "/rooms", {"capacity": 8}), 422, "validation_error")
    assert_error(api.call("POST", "/rooms", {"name": "Atlas"}), 422, "validation_error")
    assert_error(api.call("POST", "/rooms", {}), 422, "validation_error")


@pytest.mark.parametrize("cap", [1, 2, 8, 499, 500])
def test_room_capacity_bounds_accepted(api, cap):
    s, b, _ = api.call("POST", "/rooms", {"name": f"R{cap}", "capacity": cap})
    assert s == 201 and b["capacity"] == cap


@pytest.mark.parametrize("cap", [0, -1, 501, 10_000])
def test_room_capacity_out_of_range(api, cap):
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": cap}), 422, "validation_error")


@pytest.mark.parametrize("cap", ["8", 8.5, True, False, None, [8], "eight"])
def test_room_capacity_wrong_type(api, cap):
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": cap}), 422, "validation_error")


@pytest.mark.parametrize("other", ["atlas", "ATLAS", "  Atlas ", "aTlAs"])
def test_room_duplicate_name_ignores_case_and_surrounding_space(api, other):
    room(api, "Atlas")
    assert_error(api.call("POST", "/rooms", {"name": other, "capacity": 4}), 409, "room_exists")


def test_room_duplicate_check_is_on_trimmed_name(api):
    api.call("POST", "/rooms", {"name": "  Atlas  ", "capacity": 8})
    assert_error(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8}), 409, "room_exists")


def test_room_names_differing_inside_are_not_duplicates(api):
    room(api, "Atlas")
    assert api.call("POST", "/rooms", {"name": "Atlas 2", "capacity": 8})[0] == 201
    assert api.call("POST", "/rooms", {"name": "Atla s", "capacity": 8})[0] == 201


def test_failed_room_creation_does_not_consume_an_id(api):
    room(api, "Atlas")
    assert api.call("POST", "/rooms", {"name": "atlas", "capacity": 8})[0] == 409
    assert api.call("POST", "/rooms", {"name": "", "capacity": 8})[0] == 422
    s, b, _ = api.call("POST", "/rooms", {"name": "Borealis", "capacity": 8})
    assert s == 201 and b["id"] == "r2"


# ---------------------------------------------------------------- rooms: read

def test_list_rooms_in_creation_order_with_full_objects(api):
    room(api, "Zeta", 3)
    room(api, "Alpha", 9)
    s, b, h = api.call("GET", "/rooms")
    assert s == 200 and h["content-type"].startswith("application/json")
    assert b == {"rooms": [{"id": "r1", "name": "Zeta", "capacity": 3},
                           {"id": "r2", "name": "Alpha", "capacity": 9}]}


def test_get_room_by_id(api):
    room(api, "Atlas", 8)
    room(api, "Borealis", 4)
    s, b, _ = api.call("GET", "/rooms/r2")
    assert s == 200 and b == {"id": "r2", "name": "Borealis", "capacity": 4}


def test_get_unknown_room_is_404(api):
    room(api)
    assert_error(api.call("GET", "/rooms/r99"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/nope"), 404, "not_found")


# ---------------------------------------------------------------- bookings: create / validation

def test_create_booking_response_shape(api):
    r = room(api)
    s, b, h = post_booking(api, valid_booking_body(r, title="Planning", attendees=4,
                                                   start=ts("09:30"), end=ts("11:00")))
    assert s == 201
    assert b == {"id": "b1", "room_id": r, "start": ts("09:30"), "end": ts("11:00"),
                 "title": "Planning", "attendees": 4}
    assert h["content-type"].startswith("application/json")


def test_booking_ids_sequential_in_creation_order(api):
    r = room(api)
    ids = [make_booking(api, r, f"{h:02d}:00", f"{h:02d}:30")["id"] for h in (9, 10, 11)]
    assert ids == ["b1", "b2", "b3"]


def test_booking_id_not_reused_after_delete(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")  # b1
    make_booking(api, r, "10:00", "11:00")  # b2
    assert api.call("DELETE", "/bookings/b2")[0] == 204
    assert make_booking(api, r, "10:00", "11:00")["id"] == "b3"


def test_failed_booking_does_not_consume_an_id(api):
    r = room(api, capacity=2)
    make_booking(api, r, "09:00", "10:00")  # b1
    assert post_booking(api, valid_booking_body(r, start=ts("09:30")))[0] == 409
    assert post_booking(api, valid_booking_body(r, start=ts("11:00"), attendees=5))[0] == 422
    assert post_booking(api, valid_booking_body("r99"))[0] == 404
    assert make_booking(api, r, "12:00", "13:00")["id"] == "b2"


def test_booking_title_stored_trimmed(api):
    r = room(api)
    b = make_booking(api, r, "09:00", "10:00", title="   Team sync \t ")
    assert b["title"] == "Team sync"
    assert api.call("GET", "/bookings/b1")[1]["title"] == "Team sync"


@pytest.mark.parametrize("title", ["x", "x" * 100, "  " + "y" * 98 + "  "])
def test_booking_title_valid_lengths(api, title):
    r = room(api)
    s, b, _ = post_booking(api, valid_booking_body(r, title=title))
    assert s == 201 and b["title"] == title.strip()


@pytest.mark.parametrize("title", ["", "   ", "\n", "x" * 101, " " * 5 + "x" * 101])
def test_booking_title_invalid(api, title):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, title=title)), 422, "validation_error")


@pytest.mark.parametrize("title", [None, 7, ["a"], True])
def test_booking_title_wrong_type(api, title):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, title=title)), 422, "validation_error")


@pytest.mark.parametrize("attendees", [1, 2, 8])
def test_booking_attendees_valid(api, attendees):
    r = room(api, capacity=8)
    s, b, _ = post_booking(api, valid_booking_body(r, attendees=attendees))
    assert s == 201 and b["attendees"] == attendees


@pytest.mark.parametrize("attendees", [0, -1, -100])
def test_booking_attendees_below_one(api, attendees):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, attendees=attendees)), 422, "validation_error")


@pytest.mark.parametrize("attendees", ["2", 2.5, True, False, None, [2]])
def test_booking_attendees_wrong_type(api, attendees):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, attendees=attendees)), 422, "validation_error")


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field(api, field):
    r = room(api)
    body = valid_booking_body(r)
    del body[field]
    assert_error(post_booking(api, body), 422, "validation_error")


@pytest.mark.parametrize("room_id", [1, None, True, ["r1"]])
def test_booking_room_id_wrong_type(api, room_id):
    room(api)
    assert_error(post_booking(api, valid_booking_body(room_id)), 422, "validation_error")


@pytest.mark.parametrize("value", [
    "2026-03-02 09:00:00Z",          # space instead of T
    "2026-03-02T09:00:00",           # no zone designator
    "2026-03-02T09:00:00+00:00",     # offset form
    "2026-03-02T09:00Z",             # no seconds
    "2026-03-02T09:00:00.000Z",      # fractional seconds
    "2026-03-02t09:00:00Z",          # lower-case t
    "2026-03-02T09:00:00z",          # lower-case z
    "2026-3-2T09:00:00Z",            # unpadded
    "2026-02-30T09:00:00Z",          # not a real date
    "2026-02-29T09:00:00Z",          # 2026 is not a leap year
    "2026-03-02T24:00:00Z",          # hour out of range
    "2026-03-02T09:60:00Z",          # minute out of range
    "2026-03-02",                    # date only
    "09:00",                         # time only
    "",                              # empty
    " 2026-03-02T09:00:00Z",         # leading space
    "2026-03-02T09:00:00Z ",         # trailing space
    20260302,                        # not a string
    None,
])
def test_booking_invalid_timestamp_format(api, value):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, start=value)), 422, "validation_error")
    assert_error(post_booking(api, valid_booking_body(r, end=value)), 422, "validation_error")


def test_leap_day_2028_is_a_valid_timestamp(api):
    r = room(api)
    s, b, _ = post_booking(api, valid_booking_body(r, start="2028-02-29T09:00:00Z", end="2028-02-29T10:00:00Z"))
    assert s == 201 and b["start"] == "2028-02-29T09:00:00Z"


@pytest.mark.parametrize("minute", ["01", "07", "10", "14", "16", "29", "31", "44", "46", "59"])
def test_booking_start_must_be_quarter_hour_aligned(api, minute):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, start=f"2026-03-02T09:{minute}:00Z")),
                 422, "validation_error")


@pytest.mark.parametrize("minute", ["01", "10", "20", "59"])
def test_booking_end_must_be_quarter_hour_aligned(api, minute):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, end=f"2026-03-02T10:{minute}:00Z")),
                 422, "validation_error")


@pytest.mark.parametrize("minute", ["00", "15", "30", "45"])
def test_booking_quarter_hour_minutes_accepted(api, minute):
    r = room(api)
    s, _, _ = post_booking(api, valid_booking_body(r, start=f"2026-03-02T09:{minute}:00Z",
                                                   end=f"2026-03-02T10:{minute}:00Z"))
    assert s == 201


@pytest.mark.parametrize("sec", ["01", "30", "59"])
def test_booking_seconds_must_be_zero(api, sec):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, start=f"2026-03-02T09:00:{sec}Z")),
                 422, "validation_error")
    assert_error(post_booking(api, valid_booking_body(r, end=f"2026-03-02T10:00:{sec}Z")),
                 422, "validation_error")


def test_booking_end_equal_to_start_is_invalid(api):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:00"), end=ts("09:00"))),
                 422, "validation_error")


def test_booking_end_before_start_is_invalid(api):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, start=ts("10:00"), end=ts("09:00"))),
                 422, "validation_error")


def test_booking_exactly_eight_hours_is_allowed(api):
    r = room(api)
    s, _, _ = post_booking(api, valid_booking_body(r, start=ts("08:00"), end=ts("16:00")))
    assert s == 201


def test_booking_longer_than_eight_hours_is_invalid(api):
    r = room(api)
    assert_error(post_booking(api, valid_booking_body(r, start=ts("08:00"), end=ts("16:15"))),
                 422, "validation_error")
    assert_error(post_booking(api, valid_booking_body(r, start="2026-03-02T20:00:00Z",
                                                      end="2026-03-03T04:15:00Z")),
                 422, "validation_error")


def test_booking_across_midnight_eight_hours_allowed(api):
    r = room(api)
    s, _, _ = post_booking(api, valid_booking_body(r, start="2026-03-02T22:00:00Z",
                                                   end="2026-03-03T06:00:00Z"))
    assert s == 201


def test_booking_room_not_found(api):
    assert_error(post_booking(api, valid_booking_body("r1")), 404, "not_found")


def test_validation_is_checked_before_room_existence(api):
    assert_error(post_booking(api, valid_booking_body("r404", attendees=0)), 422, "validation_error")


def test_unknown_room_is_404_even_when_attendees_exceed_any_capacity(api):
    assert_error(post_booking(api, valid_booking_body("r9", attendees=1000)), 404, "not_found")


def test_over_capacity(api):
    r = room(api, capacity=2)
    assert_error(post_booking(api, valid_booking_body(r, attendees=3)), 422, "over_capacity")


def test_attendees_equal_to_capacity_is_allowed(api):
    r = room(api, capacity=2)
    assert post_booking(api, valid_booking_body(r, attendees=2))[0] == 201


def test_over_capacity_takes_precedence_over_conflict(api):
    r = room(api, capacity=2)
    make_booking(api, r, "09:00", "10:00", attendees=1)
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:30"), end=ts("10:30"), attendees=3)),
                 422, "over_capacity")


def test_validation_takes_precedence_over_conflict(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:30"), end=ts("10:30"), title="")),
                 422, "validation_error")


def test_unknown_room_is_404_even_if_it_would_conflict_elsewhere(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(post_booking(api, valid_booking_body("r9", start=ts("09:30"), end=ts("10:30"))),
                 404, "not_found")


# ---------------------------------------------------------------- bookings: overlap

def test_overlap_partial_start_conflicts(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("08:30"), end=ts("09:30"))), 409, "conflict")


def test_overlap_partial_end_conflicts(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:30"), end=ts("10:30"))), 409, "conflict")


def test_overlap_new_contains_existing_conflicts(api):
    r = room(api)
    make_booking(api, r, "10:00", "11:00")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:00"), end=ts("12:00"))), 409, "conflict")


def test_overlap_new_inside_existing_conflicts(api):
    r = room(api)
    make_booking(api, r, "09:00", "12:00")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("10:00"), end=ts("11:00"))), 409, "conflict")


def test_identical_interval_conflicts(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(post_booking(api, valid_booking_body(r)), 409, "conflict")


def test_one_quarter_hour_overlap_conflicts(api):
    r = room(api)
    make_booking(api, r, "09:00", "09:30")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:15"), end=ts("10:00"))), 409, "conflict")


def test_back_to_back_both_sides_allowed(api):
    r = room(api)
    make_booking(api, r, "10:00", "11:00")
    make_booking(api, r, "09:00", "10:00")  # ends exactly when the other starts
    make_booking(api, r, "11:00", "12:00")  # starts exactly when the other ends
    assert len(bookings_list(api)["bookings"]) == 3


def test_same_times_in_different_rooms_allowed(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    make_booking(api, r1, "09:00", "10:00")
    make_booking(api, r2, "09:00", "10:00")


def test_conflict_is_only_checked_within_same_room(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    make_booking(api, r1, "09:00", "10:00")
    assert post_booking(api, valid_booking_body(r2, start=ts("09:30"), end=ts("10:30")))[0] == 201


def test_deleted_booking_no_longer_conflicts(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert post_booking(api, valid_booking_body(r, start=ts("09:30"), end=ts("10:30")))[0] == 201


def test_conflict_with_one_of_many_bookings(api):
    r = room(api)
    make_booking(api, r, "08:00", "09:00")
    make_booking(api, r, "10:00", "11:00")
    make_booking(api, r, "13:00", "14:00")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("10:45"), end=ts("12:00"))), 409, "conflict")


# ---------------------------------------------------------------- bookings: read / delete

def test_get_booking(api):
    r = room(api)
    created = make_booking(api, r, "09:00", "10:00", title="Planning", attendees=3)
    s, b, h = api.call("GET", f"/bookings/{created['id']}")
    assert s == 200 and b == created
    assert h["content-type"].startswith("application/json")


def test_get_unknown_booking_is_404(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(api.call("GET", "/bookings/b2"), 404, "not_found")
    assert_error(api.call("GET", "/bookings/xyz"), 404, "not_found")


def test_delete_booking(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_unknown_booking_is_404(api):
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_delete_twice_second_is_404(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    assert_error(api.call("DELETE", "/bookings/b1"), 404, "not_found")


def test_delete_does_not_affect_other_bookings(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    make_booking(api, r, "11:00", "12:00")
    api.call("DELETE", "/bookings/b1")
    assert [b["id"] for b in bookings_list(api)["bookings"]] == ["b2"]


# ---------------------------------------------------------------- bookings: list

def test_list_empty(api):
    assert bookings_list(api) == {"bookings": [], "next_cursor": None}


def test_list_order_by_start_then_creation(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    make_booking(api, r1, "11:00", "12:00")          # b1
    make_booking(api, r2, "09:00", "10:00")          # b2
    make_booking(api, r1, "09:00", "10:00")          # b3 same start as b2, created later
    make_booking(api, r2, "08:00", "08:30")          # b4
    ids = [b["id"] for b in bookings_list(api)["bookings"]]
    assert ids == ["b4", "b2", "b3", "b1"]


def test_list_items_have_public_booking_shape(api):
    r = room(api)
    created = make_booking(api, r, "09:00", "10:00", title="X", attendees=1)
    assert bookings_list(api)["bookings"] == [created]


def test_list_filter_by_room(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    make_booking(api, r1, "09:00", "10:00")
    make_booking(api, r2, "09:00", "10:00")
    make_booking(api, r1, "10:00", "11:00")
    got = bookings_list(api, f"room_id={r1}")["bookings"]
    assert [b["id"] for b in got] == ["b1", "b3"]
    assert all(b["room_id"] == r1 for b in got)


def test_list_filter_by_unknown_room_is_empty_not_error(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert bookings_list(api, "room_id=r999") == {"bookings": [], "next_cursor": None}


def test_list_from_is_exclusive_of_booking_ending_exactly_at_from(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")  # b1
    make_booking(api, r, "10:00", "11:00")  # b2
    got = bookings_list(api, f"from={q(ts('10:00'))}")["bookings"]
    assert [b["id"] for b in got] == ["b2"]


def test_list_to_is_exclusive_of_booking_starting_exactly_at_to(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")  # b1
    make_booking(api, r, "10:00", "11:00")  # b2
    got = bookings_list(api, f"to={q(ts('10:00'))}")["bookings"]
    assert [b["id"] for b in got] == ["b1"]


def test_list_window_includes_partially_overlapping_bookings(api):
    r = room(api)
    make_booking(api, r, "08:00", "09:30")  # b1 overlaps start of window
    make_booking(api, r, "09:30", "10:00")  # b2 inside
    make_booking(api, r, "11:30", "12:00")  # b3 overlaps end of window
    make_booking(api, r, "12:00", "13:00")  # b4 outside
    make_booking(api, r, "07:00", "08:00")  # b5 outside
    query = f"from={q(ts('09:15'))}&to={q(ts('12:00'))}"
    assert [b["id"] for b in bookings_list(api, query)["bookings"]] == ["b1", "b2", "b3"]


def test_list_window_with_unaligned_bounds(api):
    r = room(api)
    make_booking(api, r, "09:00", "09:30")  # b1
    make_booking(api, r, "09:30", "10:00")  # b2
    got = bookings_list(api, f"from={q(ts('09:20'))}")["bookings"]
    assert [b["id"] for b in got] == ["b1", "b2"]
    got = bookings_list(api, f"to={q('2026-03-02T09:10:00Z')}")["bookings"]
    assert [b["id"] for b in got] == ["b1"]


def test_list_from_only_and_to_only(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")  # b1
    make_booking(api, r, "12:00", "13:00")  # b2
    assert [b["id"] for b in bookings_list(api, f"from={q(ts('11:00'))}")["bookings"]] == ["b2"]
    assert [b["id"] for b in bookings_list(api, f"to={q(ts('11:00'))}")["bookings"]] == ["b1"]


def test_list_window_filters_combined_with_room(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    make_booking(api, r1, "09:00", "10:00")
    make_booking(api, r2, "09:00", "10:00")
    query = f"room_id={r2}&from={q(ts('09:30'))}"
    assert [b["id"] for b in bookings_list(api, query)["bookings"]] == ["b2"]


@pytest.mark.parametrize("query", [
    "from=garbage",
    "to=garbage",
    "from=2026-03-02",
    "to=" + q("2026-03-02T09:00:00+00:00"),
    "from=" + q("2026-03-02T25:00:00Z"),
    "to=" + q("2026-02-30T09:00:00Z"),
    "from=" + q("2026-03-02T09:00:00.000Z"),
])
def test_list_invalid_window_is_422(api, query):
    room(api)
    assert_error(api.call("GET", f"/bookings?{query}"), 422, "validation_error")


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", ""])
def test_list_invalid_limit_is_422(api, limit):
    assert_error(api.call("GET", f"/bookings?limit={limit}"), 422, "validation_error")


@pytest.mark.parametrize("limit", ["1", "100"])
def test_list_limit_bounds_accepted(api, limit):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/bookings?limit={limit}")
    assert s == 200 and len(b["bookings"]) == 1


def test_list_default_limit_is_20_and_paginates(api):
    r = room(api, capacity=500)
    for i in range(21):  # back-to-back 15-minute bookings from 08:00
        start = 8 * 60 + 15 * i
        make_booking(api, r, f"{start // 60:02d}:{start % 60:02d}",
                     f"{(start + 15) // 60:02d}:{(start + 15) % 60:02d}")
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and len(b["bookings"]) == 20
    assert b["next_cursor"] is not None


def test_list_limit_one_returns_first_with_cursor(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    make_booking(api, r, "10:00", "11:00")
    make_booking(api, r, "11:00", "12:00")
    first = bookings_list(api, "limit=1")
    assert [b["id"] for b in first["bookings"]] == ["b1"] and first["next_cursor"] is not None


def test_next_cursor_null_when_exactly_fits(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    make_booking(api, r, "10:00", "11:00")
    b = bookings_list(api, "limit=2")
    assert len(b["bookings"]) == 2 and b["next_cursor"] is None


def test_walking_pages_returns_every_booking_exactly_once(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    expected = []
    for i in range(7):
        expected.append(make_booking(api, r1 if i % 2 == 0 else r2, f"{8 + i:02d}:00", f"{8 + i:02d}:30")["id"])
    # same start as the first booking, in another room: tie-break by creation order
    expected.append(make_booking(api, r2, "08:00", "08:30")["id"])
    got, pages = walk_pages(api, {"limit": 3})
    assert pages == 3
    assert sorted(b["id"] for b in got) == sorted(expected)
    assert len({b["id"] for b in got}) == len(got)
    keys = [(b["start"], int(b["id"][1:])) for b in got]
    assert keys == sorted(keys)


def test_pagination_respects_filters(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    for h in range(8, 14):
        make_booking(api, r1, f"{h:02d}:00", f"{h:02d}:30")
        make_booking(api, r2, f"{h:02d}:00", f"{h:02d}:30")
    got, _ = walk_pages(api, {"room_id": r2, "limit": 2})
    assert len(got) == 6 and all(b["room_id"] == r2 for b in got)
    got, _ = walk_pages(api, {"from": ts("10:00"), "to": ts("12:00"), "limit": 1})
    assert len(got) == 4  # two slots (10:00, 11:00) in each of the two rooms
    assert all(ts("10:00") < b["end"] and b["start"] < ts("12:00") for b in got)


def test_cursor_is_opaque_string_from_previous_response(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    make_booking(api, r, "10:00", "11:00")
    cur = bookings_list(api, "limit=1")["next_cursor"]
    assert isinstance(cur, str) and cur
    second = bookings_list(api, urllib.parse.urlencode({"limit": 1, "cursor": cur}))
    assert [b["id"] for b in second["bookings"]] == ["b2"]
    assert second["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["garbage", "1", "c", "2026-03-02T09:00:00Z", "cnotadate_1"])
def test_invalid_cursor_is_422(api, cursor):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert_error(api.call("GET", "/bookings?" + urllib.parse.urlencode({"cursor": cursor})),
                 422, "validation_error")


def test_list_response_has_bookings_and_next_cursor_keys(api):
    assert set(bookings_list(api)) == {"bookings", "next_cursor"}


# ---------------------------------------------------------------- idempotency

def test_idempotent_replay_returns_original_201_and_creates_nothing(api):
    r = room(api)
    h = {"Idempotency-Key": "k-1"}
    s1, b1, _ = post_booking(api, valid_booking_body(r), headers=h)
    s2, b2, hdr = post_booking(api, valid_booking_body(r), headers=h)
    assert s1 == 201 and s2 == 201
    assert b2 == b1 and b1["id"] == "b1"
    assert hdr["content-type"].startswith("application/json")
    assert len(bookings_list(api)["bookings"]) == 1
    # nothing was silently created, so the next real booking gets b2
    assert make_booking(api, r, "11:00", "12:00")["id"] == "b2"


def test_idempotent_replay_is_order_and_whitespace_insensitive(api):
    r = room(api)
    h = {"Idempotency-Key": "k-order"}
    first = json.dumps({"room_id": r, "start": ts("09:00"), "end": ts("10:00"),
                        "title": "sync", "attendees": 2})
    reordered = ('{\n  "attendees" : 2,\n  "title": "sync",\n'
                 f'  "end": "{ts("10:00")}", "start": "{ts("09:00")}",\n  "room_id": "{r}"\n}}')
    s1, b1, _ = api.call("POST", "/bookings", raw=first.encode(), headers=h)
    s2, b2, _ = api.call("POST", "/bookings", raw=reordered.encode(), headers=h)
    assert s1 == 201 and s2 == 201 and b1 == b2
    assert len(bookings_list(api)["bookings"]) == 1


def test_idempotent_replay_after_delete_returns_original_and_creates_nothing(api):
    r = room(api)
    h = {"Idempotency-Key": "k-del"}
    _, b1, _ = post_booking(api, valid_booking_body(r), headers=h)
    api.call("DELETE", f"/bookings/{b1['id']}")
    s, b2, _ = post_booking(api, valid_booking_body(r), headers=h)
    assert s == 201 and b2 == b1
    assert bookings_list(api)["bookings"] == []


def test_same_key_different_body_is_idempotency_mismatch(api):
    r = room(api)
    h = {"Idempotency-Key": "k-mm"}
    assert post_booking(api, valid_booking_body(r), headers=h)[0] == 201
    assert_error(post_booking(api, valid_booking_body(r, title="other"), headers=h),
                 422, "idempotency_mismatch")
    assert_error(post_booking(api, valid_booking_body(r, attendees=3), headers=h),
                 422, "idempotency_mismatch")
    assert_error(post_booking(api, valid_booking_body(r, start=ts("11:00"), end=ts("12:00")), headers=h),
                 422, "idempotency_mismatch")
    # mismatches create nothing and the original is untouched
    assert len(bookings_list(api)["bookings"]) == 1
    s, b, _ = post_booking(api, valid_booking_body(r), headers=h)
    assert s == 201 and b["id"] == "b1"


def test_idempotency_mismatch_when_room_differs(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    h = {"Idempotency-Key": "k-room"}
    assert post_booking(api, valid_booking_body(r1), headers=h)[0] == 201
    assert_error(post_booking(api, valid_booking_body(r2), headers=h), 422, "idempotency_mismatch")


def test_failed_request_with_key_is_not_remembered_conflict(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")  # b1
    h = {"Idempotency-Key": "k-fail"}
    assert_error(post_booking(api, valid_booking_body(r, start=ts("09:30"), end=ts("10:30")), headers=h),
                 409, "conflict")
    # the key is free again: a different body now succeeds
    s, b, _ = post_booking(api, valid_booking_body(r, start=ts("11:00"), end=ts("12:00")), headers=h)
    assert s == 201 and b["id"] == "b2"


def test_failed_request_with_key_is_not_remembered_validation(api):
    r = room(api)
    h = {"Idempotency-Key": "k-val"}
    assert_error(post_booking(api, valid_booking_body(r, attendees=0), headers=h), 422, "validation_error")
    s, b, _ = post_booking(api, valid_booking_body(r), headers=h)
    assert s == 201 and b["id"] == "b1"


def test_failed_request_with_key_is_not_remembered_over_capacity_and_404(api):
    r = room(api, capacity=2)
    h = {"Idempotency-Key": "k-cap"}
    assert_error(post_booking(api, valid_booking_body(r, attendees=5), headers=h), 422, "over_capacity")
    assert_error(post_booking(api, valid_booking_body("r9"), headers=h), 404, "not_found")
    s, _, _ = post_booking(api, valid_booking_body(r, attendees=2), headers=h)
    assert s == 201


def test_keys_are_independent(api):
    r = room(api)
    s1, b1, _ = post_booking(api, valid_booking_body(r), headers={"Idempotency-Key": "a"})
    s2, b2, _ = post_booking(api, valid_booking_body(r, start=ts("11:00"), end=ts("12:00")),
                             headers={"Idempotency-Key": "b"})
    assert s1 == 201 and s2 == 201 and b1["id"] != b2["id"]


def test_same_body_under_different_keys_second_is_conflict(api):
    r = room(api)
    assert post_booking(api, valid_booking_body(r), headers={"Idempotency-Key": "x"})[0] == 201
    assert_error(post_booking(api, valid_booking_body(r), headers={"Idempotency-Key": "y"}), 409, "conflict")


def test_keyed_booking_still_occupies_its_slot_for_unkeyed_requests(api):
    r = room(api)
    assert post_booking(api, valid_booking_body(r), headers={"Idempotency-Key": "z"})[0] == 201
    assert_error(post_booking(api, valid_booking_body(r)), 409, "conflict")


# ---------------------------------------------------------------- availability

def test_availability_empty_room_is_whole_working_day(api):
    r = room(api)
    assert availability(api, r) == {"date": DATE, "free": [{"start": WORK_LO, "end": WORK_HI}]}


def test_availability_response_headers(api):
    r = room(api)
    s, _, h = api.call("GET", f"/rooms/{r}/availability?date={DATE}")
    assert s == 200 and h["content-type"].startswith("application/json")


def test_availability_gaps_around_booking(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    assert free_pairs(availability(api, r)) == [(ts("08:00"), ts("09:00")), (ts("10:00"), ts("18:00"))]


def test_availability_gaps_ascending_with_several_bookings(api):
    r = room(api)
    make_booking(api, r, "14:00", "15:00")
    make_booking(api, r, "09:00", "10:00")
    make_booking(api, r, "11:15", "12:00")
    assert free_pairs(availability(api, r)) == [
        (ts("08:00"), ts("09:00")),
        (ts("10:00"), ts("11:15")),
        (ts("12:00"), ts("14:00")),
        (ts("15:00"), ts("18:00")),
    ]


def test_availability_adjacent_bookings_leave_no_gap(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    make_booking(api, r, "10:00", "11:00")
    assert free_pairs(availability(api, r)) == [(ts("08:00"), ts("09:00")), (ts("11:00"), ts("18:00"))]


def test_availability_booking_at_day_start_and_end(api):
    r = room(api)
    make_booking(api, r, "08:00", "09:00")
    make_booking(api, r, "17:00", "18:00")
    assert free_pairs(availability(api, r)) == [(ts("09:00"), ts("17:00"))]


def test_availability_fully_booked_day_is_empty_list(api):
    r = room(api)
    make_booking(api, r, "08:00", "12:00")
    make_booking(api, r, "12:00", "18:00")
    assert availability(api, r) == {"date": DATE, "free": []}


def test_availability_booking_starting_before_day_is_clipped(api):
    r = room(api)
    make_booking(api, r, "07:00", "09:00")
    assert free_pairs(availability(api, r)) == [(ts("09:00"), ts("18:00"))]


def test_availability_booking_ending_after_day_is_clipped(api):
    r = room(api)
    make_booking(api, r, "17:30", "18:30")
    assert free_pairs(availability(api, r)) == [(ts("08:00"), ts("17:30"))]


def test_availability_bookings_covering_whole_day_leave_nothing(api):
    r = room(api)
    make_booking(api, r, "07:00", "08:00")  # entirely before the day
    make_booking(api, r, "08:00", "16:00")
    make_booking(api, r, "16:00", "18:30")  # partly after the day
    assert free_pairs(availability(api, r)) == []


def test_availability_booking_entirely_outside_day_is_ignored(api):
    r = room(api)
    make_booking(api, r, "06:00", "07:00")
    make_booking(api, r, "18:00", "19:00")
    assert free_pairs(availability(api, r)) == [(WORK_LO, WORK_HI)]


def test_availability_ignores_other_rooms(api):
    r1 = room(api, "Atlas")
    r2 = room(api, "Borealis")
    make_booking(api, r2, "09:00", "10:00")
    assert free_pairs(availability(api, r1)) == [(WORK_LO, WORK_HI)]


def test_availability_ignores_bookings_on_other_dates(api):
    r = room(api)
    make_booking(api, r, "2026-03-01T09:00:00Z", "2026-03-01T10:00:00Z")
    make_booking(api, r, "2026-03-03T09:00:00Z", "2026-03-03T10:00:00Z")
    assert free_pairs(availability(api, r, DATE)) == [(WORK_LO, WORK_HI)]


def test_availability_reflects_delete(api):
    r = room(api)
    make_booking(api, r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert free_pairs(availability(api, r)) == [(WORK_LO, WORK_HI)]


def test_availability_quarter_hour_granularity(api):
    r = room(api)
    make_booking(api, r, "08:00", "08:15")
    make_booking(api, r, "08:30", "08:45")
    assert free_pairs(availability(api, r)) == [(ts("08:15"), ts("08:30")), (ts("08:45"), ts("18:00"))]


def test_availability_date_echoed_in_response(api):
    r = room(api)
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-12-31")
    assert s == 200 and b["date"] == "2026-12-31"
    assert b["free"] == [{"start": "2026-12-31T08:00:00Z", "end": "2026-12-31T18:00:00Z"}]


def test_availability_leap_day_valid(api):
    r = room(api)
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2028-02-29")
    assert s == 200 and b["date"] == "2028-02-29"


@pytest.mark.parametrize("date", [
    "2026-02-30", "2026-02-29", "2026-13-01", "2026-00-10", "2026-04-31",
    "2026/03/02", "03-02-2026", "2026-3-2", "2026-03-02T09:00:00Z", "20260302",
    "", "garbage", "2026-03-02 ", " 2026-03-02",
])
def test_availability_invalid_date_is_422(api, date):
    r = room(api)
    assert_error(api.call("GET", f"/rooms/{r}/availability?date={q(date)}"), 422, "validation_error")


def test_availability_missing_date_is_422(api):
    r = room(api)
    assert_error(api.call("GET", f"/rooms/{r}/availability"), 422, "validation_error")


def test_availability_unknown_room_is_404(api):
    assert_error(api.call("GET", f"/rooms/r9/availability?date={DATE}"), 404, "not_found")


def test_availability_room_check_precedes_date_check(api):
    assert_error(api.call("GET", "/rooms/r9/availability?date=garbage"), 404, "not_found")
    assert_error(api.call("GET", "/rooms/r9/availability"), 404, "not_found")
