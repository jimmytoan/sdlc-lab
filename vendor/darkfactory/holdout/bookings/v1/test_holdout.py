# DFL-HOLDOUT-CANARY-7c1e4b9a2f6d — held-out scenarios for bookings v1. Never mounted into a coder sandbox.
import pytest
from helpers import api, code, ts  # noqa: F401


def post_booking(api, **over):
    body = {"room_id": "r1", "start": ts("09:00"), "end": ts("10:00"), "title": "x", "attendees": 1}
    body.update(over)
    body = {k: v for k, v in body.items() if v is not ...}
    return api.call("POST", "/bookings", body)


def test_room_name_trim_and_duplicate_ignoring_case(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  Atlas ", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    s, b, _ = api.call("POST", "/rooms", {"name": "ATLAS  ", "capacity": 3})
    assert s == 409 and code(b) == "room_exists"


@pytest.mark.parametrize("body", [pytest.param({"name": "A"}, id="implied:no_capacity"), pytest.param({"capacity": 3}, id="implied:no_name"), pytest.param({"name": "A", "capacity": 0}, id="stated:capacity_0"), pytest.param({"name": "A", "capacity": 501}, id="stated:capacity_501"), pytest.param({"name": "A", "capacity": "8"}, id="implied:capacity_string"), pytest.param({"name": "A", "capacity": True}, id="arbitrary:capacity_bool"), pytest.param({"name": "A", "capacity": 2.5}, id="implied:capacity_fraction"), pytest.param({"name": 7, "capacity": 2}, id="implied:name_number"), pytest.param({"name": "x" * 51, "capacity": 2}, id="stated:name_51_chars"), pytest.param({"name": "   ", "capacity": 2}, id="arbitrary:name_blank")])
def test_room_validation(api, body):
    s, b, _ = api.call("POST", "/rooms", body)
    assert s == 422 and code(b) == "validation_error"


def test_room_limits_accepted(api):
    assert api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 500})[0] == 201


def test_get_room_and_unknown_room(api):
    r = api.room("Atlas", 8)
    s, b, _ = api.call("GET", f"/rooms/{r}")
    assert s == 200 and b["name"] == "Atlas" and b["capacity"] == 8 and b["id"] == r
    s, b, _ = api.call("GET", "/rooms/r99")
    assert s == 404 and code(b) == "not_found"


@pytest.mark.parametrize("path,raw", [pytest.param("/rooms", b"{not json", id="stated:rooms_invalid"), pytest.param("/bookings", b"{not json", id="stated:bookings_invalid"), pytest.param("/rooms", b"[1,2]", id="arbitrary:rooms_array"), pytest.param("/bookings", b"[1,2]", id="arbitrary:bookings_array")])
def test_bad_json(api, path, raw):
    s, b, _ = api.call("POST", path, raw=raw)
    assert s == 400 and code(b) == "bad_json"


@pytest.mark.parametrize("method,path", [pytest.param("DELETE", "/rooms", id="stated:delete_rooms"), pytest.param("PUT", "/rooms/r1", id="stated:put_room"), pytest.param("POST", "/rooms/r1/availability", id="stated:post_availability"), pytest.param("PUT", "/bookings", id="stated:put_bookings"), pytest.param("POST", "/bookings/b1", id="stated:post_booking_id")])
def test_method_not_allowed(api, method, path):
    api.room()
    s, b, _ = api.call(method, path, {} if method in ("PUT", "POST") else None)
    assert s == 405 and code(b) == "method_not_allowed"


def test_error_content_type(api):
    s, b, h = api.call("GET", "/nope")
    assert s == 404 and h["content-type"].startswith("application/json")


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "Atlas", "capacity": 8, "color": "red"})
    assert s == 201


@pytest.mark.parametrize("start", [pytest.param("2026-03-02 09:00:00Z", id="arbitrary:space_separator"), pytest.param("2026-03-02T09:00:00", id="arbitrary:no_zone"), pytest.param("2026-03-02T09:00:00+00:00", id="arbitrary:numeric_offset"), pytest.param("2026-03-02T09:00Z", id="arbitrary:no_seconds"), pytest.param("2026-03-02T09:00:00.000Z", id="arbitrary:milliseconds"), pytest.param("2026-02-30T09:00:00Z", id="implied:feb_30"), pytest.param("2026-03-02T24:00:00Z", id="implied:hour_24"), pytest.param(1772442000, id="implied:number")])
def test_bad_timestamp(api, start):
    api.room()
    s, b, _ = post_booking(api, start=start)
    assert s == 422 and code(b) == "validation_error"


@pytest.mark.parametrize("a,b_", [pytest.param("09:05", "10:00", id="stated:start_off_quarter"), pytest.param("09:00", "09:50", id="stated:end_off_quarter"), pytest.param("10:00", "09:00", id="implied:end_before_start"), pytest.param("09:00", "09:00", id="implied:zero_length"), pytest.param("2026-03-02T09:00:30Z", "2026-03-02T10:00:00Z", id="stated:seconds_not_zero"), pytest.param("08:00", "16:15", id="stated:longer_than_8h")])
def test_bad_slot(api, a, b_):
    api.room()
    s, b, _ = post_booking(api, start=ts(a), end=ts(b_))
    assert s == 422 and code(b) == "validation_error"


def test_slot_limits_accepted(api):
    api.room()
    assert post_booking(api, start=ts("08:00"), end=ts("16:00"))[0] == 201
    assert post_booking(api, start=ts("16:00"), end=ts("16:15"))[0] == 201


@pytest.mark.parametrize("over", [pytest.param({"title": "  "}, id="arbitrary:title_blank"), pytest.param({"title": "x" * 101}, id="arbitrary:title_101_chars"), pytest.param({"title": 5}, id="implied:title_number"), pytest.param({"attendees": 0}, id="implied:attendees_0"), pytest.param({"attendees": "2"}, id="implied:attendees_string"), pytest.param({"attendees": True}, id="arbitrary:attendees_bool"), pytest.param({"room_id": 1}, id="implied:room_id_number"), pytest.param({"title": ...}, id="implied:no_title"), pytest.param({"end": ...}, id="implied:no_end"), pytest.param({"room_id": ...}, id="implied:no_room_id")])
def test_booking_field_validation(api, over):
    api.room()
    s, b, _ = post_booking(api, **over)
    assert s == 422 and code(b) == "validation_error"


def test_title_trimmed(api):
    api.room()
    s, b, _ = post_booking(api, title="  Weekly  ")
    assert s == 201 and b["title"] == "Weekly"


def test_check_order_validation_before_room_before_capacity_before_conflict(api):
    api.room(capacity=2)
    # invalid body and unknown room -> validation wins
    s, b, _ = post_booking(api, room_id="r99", title="")
    assert s == 422 and code(b) == "validation_error"
    # unknown room with otherwise valid body
    s, b, _ = post_booking(api, room_id="r99", attendees=99)
    assert s == 404 and code(b) == "not_found"
    # capacity is checked before conflict
    assert post_booking(api)[0] == 201
    s, b, _ = post_booking(api, attendees=3)
    assert s == 422 and code(b) == "over_capacity"


@pytest.mark.parametrize("a,b_", [pytest.param("09:00", "10:15", id="stated:overlaps_start"), pytest.param("10:45", "12:00", id="stated:overlaps_end"), pytest.param("10:15", "10:45", id="stated:inside"), pytest.param("09:00", "12:00", id="stated:around"), pytest.param("10:00", "11:00", id="stated:same")])
def test_overlap(api, a, b_):
    r = api.room()
    assert api.book(r, "10:00", "11:00")[0] == 201
    s, b, _ = api.book(r, a, b_)
    assert s == 409 and code(b) == "conflict"


def test_back_to_back_both_sides(api):
    r = api.room()
    assert api.book(r, "10:00", "11:00")[0] == 201
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert api.book(r, "11:00", "12:00")[0] == 201


def test_other_room_does_not_conflict(api):
    r1, r2 = api.room("A"), api.room("B")
    assert api.book(r1, "09:00", "10:00")[0] == 201
    assert api.book(r2, "09:00", "10:00")[0] == 201


def test_booking_across_midnight(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", {"room_id": r, "start": "2026-03-02T22:00:00Z", "end": "2026-03-03T02:00:00Z",
                                             "title": "night", "attendees": 1})
    assert s == 201
    s, b, _ = api.call("POST", "/bookings", {"room_id": r, "start": "2026-03-03T01:00:00Z", "end": "2026-03-03T03:00:00Z",
                                             "title": "night2", "attendees": 1})
    assert s == 409


def test_ids_not_reused_after_delete(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.book(r, "09:00", "10:00")
    assert s == 201 and b["id"] == "b2"
    s, b, _ = api.call("GET", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"


def test_delete_twice_and_unknown(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 404 and code(b) == "not_found"
    assert api.call("DELETE", "/bookings/b77")[0] == 404


def test_idempotent_replay_returns_original(api):
    r = api.room()
    h = {"Idempotency-Key": "k1"}
    s1, b1, _ = api.book(r, "09:00", "10:00", headers=h)
    s2, b2, _ = api.book(r, "09:00", "10:00", headers=h)
    assert (s1, s2) == (201, 201) and b1 == b2
    s, b, _ = api.call("GET", "/bookings")
    assert len(b["bookings"]) == 1


def test_idempotent_body_equality_ignores_key_order(api):
    r = api.room()
    h = {"Idempotency-Key": "k1", "Content-Type": "application/json"}
    raw1 = ('{"room_id":"%s","start":"%s","end":"%s","title":"t","attendees":1}' % (r, ts("09:00"), ts("10:00"))).encode()
    raw2 = ('{ "attendees": 1, "title": "t", "end": "%s", "start": "%s", "room_id": "%s" }' % (ts("10:00"), ts("09:00"), r)).encode()
    s1, b1, _ = api.call("POST", "/bookings", raw=raw1, headers=h)
    s2, b2, _ = api.call("POST", "/bookings", raw=raw2, headers=h)
    assert (s1, s2) == (201, 201) and b1["id"] == b2["id"] == "b1"


def test_idempotency_mismatch(api):
    r = api.room()
    h = {"Idempotency-Key": "k1"}
    assert api.book(r, "09:00", "10:00", headers=h)[0] == 201
    s, b, _ = api.book(r, "11:00", "12:00", headers=h)
    assert s == 422 and code(b) == "idempotency_mismatch"


def test_idempotency_failed_request_not_remembered(api):
    r = api.room()
    h = {"Idempotency-Key": "k1"}
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert api.book(r, "09:00", "10:00", headers=h)[0] == 409
    assert api.call("DELETE", "/bookings/b1")[0] == 204
    s, b, _ = api.book(r, "09:00", "10:00", headers=h)
    assert s == 201 and b["id"] == "b2"


def test_idempotent_replay_after_delete_still_returns_original(api):
    r = api.room()
    h = {"Idempotency-Key": "k1"}
    s1, b1, _ = api.book(r, "09:00", "10:00", headers=h)
    api.call("DELETE", "/bookings/b1")
    s2, b2, _ = api.book(r, "09:00", "10:00", headers=h)
    assert s2 == 201 and b2 == b1
    assert api.call("GET", "/bookings")[1]["bookings"] == []


def test_list_filter_by_room_and_unknown_room(api):
    r1, r2 = api.room("A"), api.room("B")
    api.book(r1, "09:00", "10:00")
    api.book(r2, "09:00", "10:00")
    s, b, _ = api.call("GET", f"/bookings?room_id={r2}")
    assert [x["id"] for x in b["bookings"]] == ["b2"]
    s, b, _ = api.call("GET", "/bookings?room_id=r99")
    assert s == 200 and b["bookings"] == [] and b["next_cursor"] is None


def test_list_order_same_start_by_creation(api):
    r1, r2, r3 = api.room("A"), api.room("B"), api.room("C")
    api.book(r3, "09:00", "10:00")
    api.book(r1, "09:00", "10:00")
    api.book(r2, "08:00", "09:00")
    s, b, _ = api.call("GET", "/bookings")
    assert [x["id"] for x in b["bookings"]] == ["b3", "b1", "b2"]


def test_list_window_half_open(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    api.book(r, "11:00", "12:00")
    q = "/bookings?from=2026-03-02T10:00:00Z&to=2026-03-02T11:00:00Z"
    assert [x["id"] for x in api.call("GET", q)[1]["bookings"]] == ["b2"]
    q = "/bookings?from=2026-03-02T10:59:59Z"
    assert [x["id"] for x in api.call("GET", q)[1]["bookings"]] == ["b2", "b3"]
    q = "/bookings?to=2026-03-02T10:00:01Z"
    assert [x["id"] for x in api.call("GET", q)[1]["bookings"]] == ["b1", "b2"]


@pytest.mark.parametrize("q", [pytest.param("limit=0", id="stated:limit_0"), pytest.param("limit=101", id="stated:limit_101"), pytest.param("limit=abc", id="implied:limit_text"), pytest.param("limit=2.5", id="implied:limit_fraction"), pytest.param("from=yesterday", id="implied:from_text"), pytest.param("to=2026-03-02", id="arbitrary:to_date_only"), pytest.param("cursor=%21%21garbage", id="implied:cursor_garbage")])
def test_list_invalid_param(api, q):
    s, b, _ = api.call("GET", "/bookings?" + q)
    assert s == 422 and code(b) == "validation_error"


def test_list_limit_100_accepted(api):
    assert api.call("GET", "/bookings?limit=100")[0] == 200


def test_pagination_walks_everything_once(api):
    r1, r2 = api.room("A"), api.room("B")
    expected = []
    for hour in (8, 9, 10, 11, 12):
        for r in (r2, r1):
            s, b, _ = api.book(r, f"{hour:02d}:00", f"{hour + 1:02d}:00")
            expected.append(b["id"])
    seen, cursor, pages = [], None, 0
    while True:
        q = "/bookings?limit=3" + (f"&cursor={cursor}" if cursor else "")
        s, b, _ = api.call("GET", q)
        assert s == 200 and len(b["bookings"]) <= 3
        seen += [x["id"] for x in b["bookings"]]
        pages += 1
        cursor = b["next_cursor"]
        if cursor is None:
            break
        assert isinstance(cursor, str) and pages < 10
    assert seen == expected and pages == 4


def test_pagination_exact_page_has_null_cursor(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:00")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    assert len(b["bookings"]) == 2 and b["next_cursor"] is None


def test_pagination_with_filter(api):
    r1, r2 = api.room("A"), api.room("B")
    for hour in (8, 9, 10):
        api.book(r1, f"{hour:02d}:00", f"{hour + 1:02d}:00")
        api.book(r2, f"{hour:02d}:00", f"{hour + 1:02d}:00")
    s, b, _ = api.call("GET", f"/bookings?room_id={r2}&limit=2")
    assert [x["id"] for x in b["bookings"]] == ["b2", "b4"] and b["next_cursor"]
    s, b, _ = api.call("GET", f"/bookings?room_id={r2}&limit=2&cursor={b['next_cursor']}")
    assert [x["id"] for x in b["bookings"]] == ["b6"] and b["next_cursor"] is None


def test_default_limit_is_20(api):
    rooms = [api.room(f"R{i}") for i in range(3)]
    for r in rooms:
        for hour in range(8, 16):
            api.book(r, f"{hour:02d}:00", f"{hour + 1:02d}:00")
    s, b, _ = api.call("GET", "/bookings")
    assert len(b["bookings"]) == 20 and b["next_cursor"] is not None


def test_availability_empty_day_and_full_day(api):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:00"), "end": ts("18:00")}]
    api.book(r, "08:00", "13:00")
    api.book(r, "13:00", "18:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200 and b["free"] == []


def test_availability_clipping_and_adjacent(api):
    r = api.room()
    api.book(r, "06:00", "08:30")
    api.book(r, "10:00", "11:00")
    api.book(r, "11:00", "11:30")
    api.book(r, "17:00", "20:00")
    api.book(r, "20:00", "21:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:30"), "end": ts("10:00")}, {"start": ts("11:30"), "end": ts("17:00")}]


def test_availability_other_days_and_rooms_ignored(api):
    r1, r2 = api.room("A"), api.room("B")
    api.book(r2, "09:00", "10:00")
    api.call("POST", "/bookings", {"room_id": r1, "start": "2026-03-03T09:00:00Z", "end": "2026-03-03T10:00:00Z",
                                   "title": "t", "attendees": 1})
    s, b, _ = api.call("GET", f"/rooms/{r1}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:00"), "end": ts("18:00")}]


def test_availability_overnight_booking_clipped(api):
    r = api.room()
    api.call("POST", "/bookings", {"room_id": r, "start": "2026-03-01T23:00:00Z", "end": "2026-03-02T07:00:00Z",
                                   "title": "t", "attendees": 1})
    api.call("POST", "/bookings", {"room_id": r, "start": "2026-03-02T16:00:00Z", "end": "2026-03-03T00:00:00Z",
                                   "title": "t", "attendees": 1})
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": ts("08:00"), "end": ts("16:00")}]


@pytest.mark.parametrize("q", [pytest.param("", id="implied:no_date"), pytest.param("?date=", id="implied:empty_date"), pytest.param("?date=2026-02-30", id="implied:feb_30"), pytest.param("?date=02-03-2026", id="implied:day_first"), pytest.param("?date=20260302", id="arbitrary:compact")])
def test_availability_bad_date(api, q):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability{q}")
    assert s == 422 and code(b) == "validation_error"


def test_availability_unknown_room_before_bad_date(api):
    s, b, _ = api.call("GET", "/rooms/r99/availability?date=bad")
    assert s == 404 and code(b) == "not_found"
