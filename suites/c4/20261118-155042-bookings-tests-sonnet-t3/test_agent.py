import json

import pytest

from helpers import api, code, ts  # noqa: F401


def err(resp, status, c):
    s, b, h = resp
    assert s == status, (s, b)
    assert code(b) == c
    assert isinstance(b["error"]["message"], str)
    assert h["content-type"].startswith("application/json")


def bk(room="r1", start="09:00", end="10:00", title="t", attendees=1):
    return {"room_id": room, "start": ts(start), "end": ts(end), "title": title, "attendees": attendees}


# ---------- general ----------

def test_not_found_paths(api):
    for p in ["/nope", "/rooms/", "/rooms/r1/x", "/bookings/b1/x", "/"]:
        err(api.call("GET", p), 404, "not_found")


def test_method_not_allowed(api):
    api.room()
    for m, p in [("PUT", "/rooms"), ("DELETE", "/rooms"), ("POST", "/rooms/r1"), ("DELETE", "/rooms/r1"),
                 ("POST", "/rooms/r1/availability"), ("PUT", "/bookings"), ("DELETE", "/bookings"),
                 ("POST", "/bookings/b1"), ("PUT", "/bookings/b1"), ("PATCH", "/bookings/b1")]:
        err(api.call(m, p, {} if m in ("POST", "PUT", "PATCH") else None), 405, "method_not_allowed")


@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
@pytest.mark.parametrize("raw", [b"{not json", b"", b"[]", b"[1,2]", b'"str"', b"5", b"null", b"true"])
def test_bad_json(api, path, raw):
    err(api.call("POST", path, raw=raw), 400, "bad_json")


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": 3, "extra": 1})
    assert s == 201 and b == {"id": "r1", "name": "A", "capacity": 3}
    d = bk()
    d["zzz"] = [1]
    s, b, _ = api.call("POST", "/bookings", d)
    assert s == 201 and set(b) == {"id", "room_id", "start", "end", "title", "attendees"}


# ---------- rooms ----------

def test_room_name_trim_and_stored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  Atlas  ", "capacity": 8})
    assert s == 201 and b["name"] == "Atlas"
    assert api.call("GET", "/rooms/r1")[1] == b


def test_room_name_length_limits(api):
    assert api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 1})[0] == 201
    err(api.call("POST", "/rooms", {"name": "y" * 51, "capacity": 1}), 422, "validation_error")
    assert api.call("POST", "/rooms", {"name": " " + "z" * 50 + " ", "capacity": 1})[0] == 201
    err(api.call("POST", "/rooms", {"name": "   ", "capacity": 1}), 422, "validation_error")
    assert api.call("POST", "/rooms", {"name": "q", "capacity": 1})[0] == 201


@pytest.mark.parametrize("cap", [0, -1, 501, 1.5, "5", True, False, None, [], 8.0])
def test_room_capacity_invalid(api, cap):
    err(api.call("POST", "/rooms", {"name": "A", "capacity": cap}), 422, "validation_error")


def test_room_capacity_bounds(api):
    assert api.call("POST", "/rooms", {"name": "A", "capacity": 1})[0] == 201
    assert api.call("POST", "/rooms", {"name": "B", "capacity": 500})[0] == 201


@pytest.mark.parametrize("body", [{}, {"name": "A"}, {"capacity": 3}, {"name": 5, "capacity": 3},
                                  {"name": None, "capacity": 3}, {"name": ["a"], "capacity": 3}])
def test_room_missing_or_bad_type(api, body):
    err(api.call("POST", "/rooms", body), 422, "validation_error")


def test_room_duplicate_name(api):
    api.room("Atlas")
    err(api.call("POST", "/rooms", {"name": "atlas", "capacity": 2}), 409, "room_exists")
    err(api.call("POST", "/rooms", {"name": "  ATLAS ", "capacity": 2}), 409, "room_exists")
    # failed creation does not consume an id
    assert api.room("Other") == "r2"


def test_room_validation_before_duplicate(api):
    api.room("Atlas")
    err(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 0}), 422, "validation_error")


def test_rooms_list_and_get(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}
    api.room("A", 1)
    api.room("B", 2)
    s, b, h = api.call("GET", "/rooms")
    assert b["rooms"] == [{"id": "r1", "name": "A", "capacity": 1}, {"id": "r2", "name": "B", "capacity": 2}]
    s, b, h = api.call("GET", "/rooms/r2")
    assert s == 200 and b == {"id": "r2", "name": "B", "capacity": 2}
    assert h["content-type"].startswith("application/json")
    err(api.call("GET", "/rooms/r3"), 404, "not_found")
    err(api.call("GET", "/rooms/xyz"), 404, "not_found")


# ---------- booking validation ----------

def test_booking_create_response(api):
    api.room()
    s, b, h = api.call("POST", "/bookings", bk(title="  Plan  ", attendees=3))
    assert s == 201 and h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": "r1", "start": ts("09:00"), "end": ts("10:00"),
                 "title": "Plan", "attendees": 3}
    assert api.call("GET", "/bookings/b1")[1] == b
    assert api.book("r1", "10:00", "11:00")[1]["id"] == "b2"


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field(api, field):
    api.room()
    d = bk()
    del d[field]
    err(api.call("POST", "/bookings", d), 422, "validation_error")


@pytest.mark.parametrize("field,val", [("room_id", 1), ("room_id", None), ("start", 5), ("end", None),
                                       ("title", 5), ("title", None), ("attendees", "2"),
                                       ("attendees", True), ("attendees", 1.5), ("attendees", None),
                                       ("attendees", 0), ("attendees", -3), ("title", ""),
                                       ("title", "   "), ("title", "x" * 101)])
def test_booking_bad_field(api, field, val):
    api.room()
    d = bk()
    d[field] = val
    err(api.call("POST", "/bookings", d), 422, "validation_error")


def test_booking_title_limits(api):
    api.room()
    s, b, _ = api.call("POST", "/bookings", bk(title="x" * 100))
    assert s == 201 and b["title"] == "x" * 100
    s, b, _ = api.call("POST", "/bookings", bk(start="10:00", end="11:00", title=" " + "y" * 100 + " "))
    assert s == 201 and b["title"] == "y" * 100


@pytest.mark.parametrize("bad", [
    "2026-03-02T09:00:00", "2026-03-02 09:00:00Z", "2026-03-02T09:00Z", "2026-03-02T09:00:00+00:00",
    "2026-03-02T09:00:00.000Z", "2026-03-02t09:00:00z", "2026-3-2T09:00:00Z", "", "garbage",
    "2026-02-30T09:00:00Z", "2026-13-02T09:00:00Z", "2026-03-02T25:00:00Z", "2026-03-02T09:60:00Z",
    " 2026-03-02T09:00:00Z", "2026-03-02T09:00:00Z\n", "2026-03-02T09:00:00Zx",
])
def test_booking_bad_timestamp(api, bad):
    api.room()
    d = bk()
    d["start"] = bad
    err(api.call("POST", "/bookings", d), 422, "validation_error")
    d = bk()
    d["end"] = bad
    err(api.call("POST", "/bookings", d), 422, "validation_error")


@pytest.mark.parametrize("t", ["09:01", "09:10", "09:20", "09:59"])
def test_booking_minutes_alignment(api, t):
    api.room()
    err(api.call("POST", "/bookings", bk(start=t, end="12:00")), 422, "validation_error")
    err(api.call("POST", "/bookings", bk(start="08:00", end=t.replace("09", "10"))), 422, "validation_error")


def test_booking_seconds_alignment(api):
    api.room()
    d = bk()
    d["start"] = "2026-03-02T09:00:30Z"
    err(api.call("POST", "/bookings", d), 422, "validation_error")
    d = bk()
    d["end"] = "2026-03-02T10:00:01Z"
    err(api.call("POST", "/bookings", d), 422, "validation_error")


@pytest.mark.parametrize("m", ["00", "15", "30", "45"])
def test_booking_quarters_ok(api, m):
    api.room()
    d = bk()
    d["start"] = f"2026-03-02T09:{m}:00Z"
    d["end"] = f"2026-03-02T11:{m}:00Z"
    assert api.call("POST", "/bookings", d)[0] == 201


def test_booking_end_before_or_equal_start(api):
    api.room()
    err(api.book("r1", "10:00", "10:00"), 422, "validation_error")
    err(api.book("r1", "10:00", "09:00"), 422, "validation_error")


def test_booking_duration_limit(api):
    api.room()
    assert api.book("r1", "09:00", "17:00")[0] == 201
    err(api.book("r1", "2026-03-03T09:00:00Z", "2026-03-03T17:15:00Z"), 422, "validation_error")
    # spans days
    err(api.book("r1", "2026-03-03T09:00:00Z", "2026-03-04T09:00:00Z"), 422, "validation_error")
    assert api.book("r1", "2026-03-05T20:00:00Z", "2026-03-06T04:00:00Z")[0] == 201


def test_booking_min_duration_15(api):
    api.room()
    assert api.book("r1", "09:00", "09:15")[0] == 201


def test_validation_before_room_lookup(api):
    err(api.call("POST", "/bookings", bk(room="nope", title="")), 422, "validation_error")
    err(api.call("POST", "/bookings", bk(room="nope", start="09:10")), 422, "validation_error")


def test_room_lookup_before_capacity(api):
    err(api.call("POST", "/bookings", bk(room="rX", attendees=9999)), 404, "not_found")


def test_unknown_room_404(api):
    api.room()
    err(api.call("POST", "/bookings", bk(room="r2")), 404, "not_found")


def test_capacity_checks(api):
    api.room(capacity=4)
    assert api.book("r1", "09:00", "10:00", attendees=4)[0] == 201
    err(api.book("r1", "11:00", "12:00", attendees=5), 422, "over_capacity")


def test_capacity_before_conflict(api):
    api.room(capacity=4)
    api.book("r1", "09:00", "10:00")
    err(api.book("r1", "09:00", "10:00", attendees=5), 422, "over_capacity")


# ---------- conflicts ----------

@pytest.mark.parametrize("s,e", [("09:00", "10:00"), ("09:30", "10:30"), ("08:30", "09:30"), ("09:15", "09:45"),
                                 ("08:00", "11:00"), ("09:45", "10:15"), ("08:45", "09:15")])
def test_conflicts(api, s, e):
    api.room()
    api.book("r1", "09:00", "10:00")
    err(api.book("r1", s, e), 409, "conflict")


def test_adjacent_ok_both_sides(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    assert api.book("r1", "08:00", "09:00")[0] == 201
    assert api.book("r1", "10:00", "11:00")[0] == 201


def test_conflict_is_per_room(api):
    api.room("A")
    api.room("B")
    api.book("r1", "09:00", "10:00")
    assert api.book("r2", "09:00", "10:00")[0] == 201


def test_conflict_failed_does_not_consume_id(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    api.book("r1", "09:00", "10:00")
    api.book("r1", "11:00", "12:00", attendees=999)
    assert api.book("r1", "11:00", "12:00")[1]["id"] == "b2"


def test_conflict_across_days(api):
    api.room()
    api.book("r1", "2026-03-02T22:00:00Z", "2026-03-03T02:00:00Z")
    err(api.book("r1", "2026-03-03T01:00:00Z", "2026-03-03T03:00:00Z"), 409, "conflict")
    assert api.book("r1", "2026-03-03T02:00:00Z", "2026-03-03T03:00:00Z")[0] == 201


# ---------- get / delete ----------

def test_get_booking_unknown(api):
    err(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_booking(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    s, b, h = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    err(api.call("GET", "/bookings/b1"), 404, "not_found")
    err(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    err(api.call("DELETE", "/bookings/b99"), 404, "not_found")
    assert api.call("GET", "/bookings")[1]["bookings"] == []


def test_ids_not_reused_after_delete(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book("r1", "09:00", "10:00")
    assert b["id"] == "b2"
    api.call("DELETE", "/bookings/b2")
    assert api.book("r1", "09:00", "10:00")[1]["id"] == "b3"


def test_delete_only_target(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    api.book("r1", "10:00", "11:00")
    api.call("DELETE", "/bookings/b1")
    assert api.call("GET", "/bookings/b2")[0] == 200
    assert api.book("r1", "10:00", "11:00")[0] == 409


# ---------- idempotency ----------

def test_idempotent_replay(api):
    api.room()
    h = {"Idempotency-Key": "k1"}
    s1, b1, _ = api.book("r1", "09:00", "10:00", headers=h)
    s2, b2, _ = api.book("r1", "09:00", "10:00", headers=h)
    assert s1 == s2 == 201 and b1 == b2
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1
    # next booking gets next id
    assert api.book("r1", "11:00", "12:00")[1]["id"] == "b2"


def test_idempotent_equal_json_different_formatting(api):
    api.room()
    h = {"Idempotency-Key": "k"}
    d = bk()
    raw1 = json.dumps(d).encode()
    raw2 = json.dumps(dict(reversed(list(d.items()))), indent=3).encode()
    s1, b1, _ = api.call("POST", "/bookings", raw=raw1, headers=h)
    s2, b2, _ = api.call("POST", "/bookings", raw=raw2, headers=h)
    assert s1 == s2 == 201 and b1 == b2
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1


def test_idempotent_mismatch(api):
    api.room()
    h = {"Idempotency-Key": "k"}
    api.book("r1", "09:00", "10:00", headers=h)
    err(api.book("r1", "11:00", "12:00", headers=h), 422, "idempotency_mismatch")
    err(api.book("r1", "09:00", "10:00", title="other", headers=h), 422, "idempotency_mismatch")
    err(api.book("r1", "09:00", "10:00", attendees=3, headers=h), 422, "idempotency_mismatch")
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1


def test_idempotent_mismatch_extra_field_differs(api):
    api.room()
    h = {"Idempotency-Key": "k"}
    api.call("POST", "/bookings", bk(), headers=h)
    d = bk()
    d["note"] = "x"
    err(api.call("POST", "/bookings", d, headers=h), 422, "idempotency_mismatch")


def test_idempotent_failed_not_remembered(api):
    h = {"Idempotency-Key": "k"}
    err(api.book("r1", "09:00", "10:00", headers=h), 404, "not_found")
    api.room()
    s, b, _ = api.book("r1", "09:00", "10:00", headers=h)
    assert s == 201
    # validation failure not remembered either
    h2 = {"Idempotency-Key": "k2"}
    err(api.book("r1", "09:00", "09:00", headers=h2), 422, "validation_error")
    s, b, _ = api.book("r1", "11:00", "12:00", headers=h2)
    assert s == 201
    # conflict not remembered
    h3 = {"Idempotency-Key": "k3"}
    err(api.book("r1", "09:00", "10:00", headers=h3), 409, "conflict")
    assert api.book("r1", "13:00", "14:00", headers=h3)[0] == 201


def test_idempotency_keys_independent(api):
    api.room()
    a = api.book("r1", "09:00", "10:00", headers={"Idempotency-Key": "a"})
    err(api.book("r1", "09:00", "10:00", headers={"Idempotency-Key": "b"}), 409, "conflict")
    assert a[0] == 201
    # no key: nothing remembered
    err(api.book("r1", "09:00", "10:00"), 409, "conflict")


# ---------- listing ----------

def seed(api):
    api.room("A", 10)
    api.room("B", 10)
    api.book("r1", "11:00", "12:00", title="b1")  # b1
    api.book("r2", "09:00", "10:00", title="b2")  # b2
    api.book("r1", "09:00", "10:00", title="b3")  # b3 same start as b2
    api.book("r1", "13:00", "14:00", title="b4")  # b4


def ids(resp):
    assert resp[0] == 200, resp
    return [x["id"] for x in resp[1]["bookings"]]


def test_list_empty(api):
    s, b, _ = api.call("GET", "/bookings")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_order_and_tiebreak(api):
    seed(api)
    assert ids(api.call("GET", "/bookings")) == ["b2", "b3", "b1", "b4"]


def test_list_room_filter(api):
    seed(api)
    assert ids(api.call("GET", "/bookings?room_id=r1")) == ["b3", "b1", "b4"]
    assert ids(api.call("GET", "/bookings?room_id=r2")) == ["b2"]
    s, b, _ = api.call("GET", "/bookings?room_id=nope")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_from_to(api):
    seed(api)
    q = lambda s: ids(api.call("GET", "/bookings?" + s))
    # from: end after from
    assert q("from=2026-03-02T10:00:00Z") == ["b1", "b4"]
    assert q("from=2026-03-02T09:59:59Z") == ["b2", "b3", "b1", "b4"]
    assert q("from=2026-03-02T12:00:00Z") == ["b4"]
    # to: start before to
    assert q("to=2026-03-02T11:00:00Z") == ["b2", "b3"]
    assert q("to=2026-03-02T11:00:01Z") == ["b2", "b3", "b1"]
    assert q("to=2026-03-02T09:00:00Z") == []
    assert q("from=2026-03-02T10:00:00Z&to=2026-03-02T13:00:00Z") == ["b1"]
    assert q("from=2026-03-02T09:30:00Z&to=2026-03-02T09:40:00Z") == ["b2", "b3"]
    assert q("from=2026-03-02T09:30:07Z&to=2026-03-02T09:30:08Z") == ["b2", "b3"]
    assert q("from=2026-03-02T14:00:00Z") == []
    assert q("room_id=r1&from=2026-03-02T10:00:00Z&to=2026-03-02T13:00:00Z") == ["b1"]
    assert q("room_id=r2&from=2026-03-02T10:00:00Z") == []


@pytest.mark.parametrize("qs", ["from=bad", "to=bad", "from=2026-03-02", "to=2026-03-02T09:00:00", "from=",
                                "limit=0", "limit=101", "limit=-1", "limit=abc", "limit=", "limit=1.5",
                                "limit=true", "cursor=garbage", "cursor=", "cursor=c2026-13-01T00:00:00Z_1",
                                "cursor=c2026-03-02T09:00:00Z_x"])
def test_list_invalid_params(api, qs):
    seed(api)
    err(api.call("GET", "/bookings?" + qs), 422, "validation_error")


def test_list_limit_bounds(api):
    seed(api)
    assert len(ids(api.call("GET", "/bookings?limit=1"))) == 1
    assert len(ids(api.call("GET", "/bookings?limit=100"))) == 4


def test_pagination_walk(api):
    seed(api)
    s, b, _ = api.call("GET", "/bookings?limit=3")
    assert [x["id"] for x in b["bookings"]] == ["b2", "b3", "b1"]
    assert isinstance(b["next_cursor"], str) and b["next_cursor"]
    s, b2, _ = api.call("GET", "/bookings?limit=3&cursor=" + b["next_cursor"])
    assert s == 200 and [x["id"] for x in b2["bookings"]] == ["b4"] and b2["next_cursor"] is None


def test_pagination_exact_boundary(api):
    seed(api)
    s, b, _ = api.call("GET", "/bookings?limit=4")
    assert len(b["bookings"]) == 4 and b["next_cursor"] is None


def test_pagination_one_at_a_time_with_filters(api):
    seed(api)
    seen, cur = [], None
    for _ in range(10):
        url = "/bookings?limit=1&room_id=r1"
        if cur:
            url += "&cursor=" + cur
        s, b, _ = api.call("GET", url)
        assert s == 200
        seen += [x["id"] for x in b["bookings"]]
        cur = b["next_cursor"]
        if cur is None:
            break
    assert seen == ["b3", "b1", "b4"]


def test_default_limit_20(api):
    api.room("A", 10)
    for i in range(25):
        d = {"room_id": "r1", "start": f"2026-03-{i + 1:02d}T09:00:00Z", "end": f"2026-03-{i + 1:02d}T10:00:00Z",
             "title": "t", "attendees": 1}
        assert api.call("POST", "/bookings", d)[0] == 201
    s, b, _ = api.call("GET", "/bookings")
    assert len(b["bookings"]) == 20 and b["next_cursor"]
    s, b2, _ = api.call("GET", "/bookings?cursor=" + b["next_cursor"])
    assert len(b2["bookings"]) == 5 and b2["next_cursor"] is None
    allids = [x["id"] for x in b["bookings"] + b2["bookings"]]
    assert allids == [f"b{i}" for i in range(1, 26)]


def test_pagination_with_same_start_across_pages(api):
    api.room("A", 10)
    api.room("B", 10)
    api.room("C", 10)
    for r in ("r3", "r1", "r2"):
        api.book(r, "09:00", "10:00")
    s, b, _ = api.call("GET", "/bookings?limit=1")
    assert [x["id"] for x in b["bookings"]] == ["b1"]
    s, b, _ = api.call("GET", "/bookings?limit=1&cursor=" + b["next_cursor"])
    assert [x["id"] for x in b["bookings"]] == ["b2"]
    s, b, _ = api.call("GET", "/bookings?limit=1&cursor=" + b["next_cursor"])
    assert [x["id"] for x in b["bookings"]] == ["b3"] and b["next_cursor"] is None


def test_list_after_delete(api):
    seed(api)
    api.call("DELETE", "/bookings/b2")
    assert ids(api.call("GET", "/bookings")) == ["b3", "b1", "b4"]


# ---------- availability ----------

def avail(api, rid="r1", date="2026-03-02"):
    return api.call("GET", f"/rooms/{rid}/availability?date={date}")


def fr(a, b, day="2026-03-02"):
    return {"start": f"{day}T{a}:00Z", "end": f"{day}T{b}:00Z"}


def test_availability_empty_day(api):
    api.room()
    s, b, h = avail(api)
    assert s == 200 and b == {"date": "2026-03-02", "free": [fr("08:00", "18:00")]}
    assert h["content-type"].startswith("application/json")


def test_availability_gaps(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    api.book("r1", "10:00", "11:00")
    api.book("r1", "12:00", "12:15")
    api.book("r1", "17:00", "18:00")
    assert avail(api)[1]["free"] == [fr("08:00", "09:00"), fr("11:00", "12:00"), fr("12:15", "17:00")]


def test_availability_clipping(api):
    api.room()
    api.book("r1", "2026-03-02T06:00:00Z", "2026-03-02T09:00:00Z")
    api.book("r1", "2026-03-02T17:00:00Z", "2026-03-02T20:00:00Z")
    assert avail(api)[1]["free"] == [fr("09:00", "17:00")]


def test_availability_full_day(api):
    api.room()
    api.book("r1", "2026-03-02T07:00:00Z", "2026-03-02T15:00:00Z")
    api.book("r1", "2026-03-02T15:00:00Z", "2026-03-02T19:00:00Z")
    assert avail(api)[1] == {"date": "2026-03-02", "free": []}


def test_availability_outside_bookings_ignored(api):
    api.room()
    api.book("r1", "2026-03-02T06:00:00Z", "2026-03-02T08:00:00Z")
    api.book("r1", "2026-03-02T18:00:00Z", "2026-03-02T19:00:00Z")
    api.book("r1", "2026-03-01T08:00:00Z", "2026-03-01T16:00:00Z")
    api.book("r1", "2026-03-03T08:00:00Z", "2026-03-03T16:00:00Z")
    assert avail(api)[1]["free"] == [fr("08:00", "18:00")]


def test_availability_overnight_booking(api):
    api.room()
    assert api.book("r1", "2026-03-02T02:00:00Z", "2026-03-02T09:30:00Z")[0] == 201
    assert api.book("r1", "2026-03-02T17:30:00Z", "2026-03-03T01:00:00Z")[0] == 201
    assert avail(api)[1]["free"] == [fr("09:30", "17:30")]
    assert avail(api, date="2026-03-03")[1]["free"] == [fr("08:00", "18:00", "2026-03-03")]


def test_availability_other_room_and_delete(api):
    api.room("A")
    api.room("B")
    api.book("r1", "09:00", "10:00")
    assert avail(api, "r2")[1]["free"] == [fr("08:00", "18:00")]
    api.call("DELETE", "/bookings/b1")
    assert avail(api)[1]["free"] == [fr("08:00", "18:00")]


def test_availability_errors(api):
    api.room()
    for d in ["", "2026-02-30", "2026-13-01", "20260302", "2026-3-2", "abc", "2026-03-02T00:00:00Z", "2026-03-02x",
              "2025-02-29"]:
        err(api.call("GET", f"/rooms/r1/availability?date={d}"), 422, "validation_error")
    err(api.call("GET", "/rooms/r1/availability"), 422, "validation_error")
    assert avail(api, date="2028-02-29")[0] == 200


def test_availability_unknown_room_first(api):
    err(api.call("GET", "/rooms/r9/availability?date=2026-03-02"), 404, "not_found")
    err(api.call("GET", "/rooms/r9/availability?date=bad"), 404, "not_found")
    err(api.call("GET", "/rooms/r9/availability"), 404, "not_found")
