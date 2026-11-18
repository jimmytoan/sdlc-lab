import pytest
from helpers import api, code, ts  # noqa: F401


def err(resp, status, c):
    s, b, h = resp
    assert s == status, (s, b)
    assert code(b) == c
    assert isinstance(b["error"]["message"], str)
    assert h["content-type"].startswith("application/json")


def bk(api, r, start, end, title="t", attendees=1, **kw):
    return api.book(r, start, end, title=title, attendees=attendees, **kw)


def body(r="r1", **over):
    d = {"room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "x", "attendees": 1}
    d.update(over)
    return d


# ---------- general ----------
def test_404_and_405(api):
    err(api.call("GET", "/nope"), 404, "not_found")
    err(api.call("GET", "/rooms/r1/other"), 404, "not_found")
    err(api.call("DELETE", "/rooms"), 405, "method_not_allowed")
    err(api.call("PUT", "/rooms"), 405, "method_not_allowed")
    err(api.call("POST", "/rooms/r1", {}), 405, "method_not_allowed")
    err(api.call("DELETE", "/rooms/r1"), 405, "method_not_allowed")
    err(api.call("POST", "/rooms/r1/availability", {}), 405, "method_not_allowed")
    err(api.call("PUT", "/bookings"), 405, "method_not_allowed")
    err(api.call("DELETE", "/bookings"), 405, "method_not_allowed")
    err(api.call("POST", "/bookings/b1", {}), 405, "method_not_allowed")
    err(api.call("PUT", "/bookings/b1", {}), 405, "method_not_allowed")
    err(api.call("PATCH", "/bookings/b1", {}), 405, "method_not_allowed")


@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
@pytest.mark.parametrize("raw", [b"{nope", b"", b"[]", b"[1]", b'"s"', b"5", b"null", b"true"])
def test_bad_json(api, path, raw):
    err(api.call("POST", path, raw=raw), 400, "bad_json")


def test_bad_json_before_other_checks(api):
    err(api.call("POST", "/bookings", raw=b"[]", headers={"Idempotency-Key": "k"}), 400, "bad_json")


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": 3, "zzz": 1})
    assert s == 201 and b == {"id": "r1", "name": "A", "capacity": 3}
    s, b, _ = api.call("POST", "/bookings", body(extra="x"))
    assert s == 201 and set(b) == {"id", "room_id", "start", "end", "title", "attendees"}


# ---------- rooms ----------
def test_room_create_shape_and_get(api):
    s, b, h = api.call("POST", "/rooms", {"name": "  Atlas  ", "capacity": 8})
    assert s == 201 and b == {"id": "r1", "name": "Atlas", "capacity": 8}
    assert h["content-type"].startswith("application/json")
    api.room("B", 2)
    s, b, _ = api.call("GET", "/rooms/r2")
    assert s == 200 and b == {"id": "r2", "name": "B", "capacity": 2}
    s, b, _ = api.call("GET", "/rooms/r1")
    assert b["name"] == "Atlas"
    err(api.call("GET", "/rooms/r9"), 404, "not_found")
    s, b, _ = api.call("GET", "/rooms")
    assert b == {"rooms": [{"id": "r1", "name": "Atlas", "capacity": 8}, {"id": "r2", "name": "B", "capacity": 2}]}


def test_rooms_empty(api):
    s, b, _ = api.call("GET", "/rooms")
    assert s == 200 and b == {"rooms": []}


@pytest.mark.parametrize("name", ["", "   ", "x" * 51, "  " + "x" * 51 + "  ", 5, None, True, ["a"], {}])
def test_room_bad_name(api, name):
    err(api.call("POST", "/rooms", {"name": name, "capacity": 5}), 422, "validation_error")


def test_room_name_missing(api):
    err(api.call("POST", "/rooms", {"capacity": 5}), 422, "validation_error")


def test_room_name_limits_ok(api):
    assert api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 5})[0] == 201
    s, b, _ = api.call("POST", "/rooms", {"name": " " + "y" * 50 + " ", "capacity": 5})
    assert s == 201 and b["name"] == "y" * 50
    assert api.call("POST", "/rooms", {"name": "z", "capacity": 5})[0] == 201


@pytest.mark.parametrize("cap", [0, -1, 501, 1000, True, False, "5", 5.5, 5.0, None, [], {}])
def test_room_bad_capacity(api, cap):
    err(api.call("POST", "/rooms", {"name": "A", "capacity": cap}), 422, "validation_error")


def test_room_capacity_missing(api):
    err(api.call("POST", "/rooms", {"name": "A"}), 422, "validation_error")


def test_room_capacity_bounds(api):
    assert api.call("POST", "/rooms", {"name": "A", "capacity": 1})[0] == 201
    assert api.call("POST", "/rooms", {"name": "B", "capacity": 500})[0] == 201


def test_room_duplicate_name(api):
    api.room("Atlas")
    err(api.call("POST", "/rooms", {"name": "atlas", "capacity": 3}), 409, "room_exists")
    err(api.call("POST", "/rooms", {"name": "  ATLAS ", "capacity": 3}), 409, "room_exists")
    # failed create doesn't consume an id
    assert api.room("Other") == "r2"
    assert len(api.call("GET", "/rooms")[1]["rooms"]) == 2


def test_room_validation_precedes_duplicate(api):
    api.room("Atlas")
    err(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 0}), 422, "validation_error")


def test_room_validation_failure_no_id_consumed(api):
    api.call("POST", "/rooms", {"name": "", "capacity": 1})
    assert api.room("A") == "r1"


# ---------- booking validation ----------
def test_booking_create_trim_title(api):
    r = api.room()
    s, b, h = bk(api, r, "09:00", "10:30", title="  Plan  ", attendees=3)
    assert s == 201 and h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": r, "start": ts("09:00"), "end": ts("10:30"), "title": "Plan", "attendees": 3}
    assert api.call("GET", "/bookings/b1")[1] == b
    assert bk(api, r, "10:30", "11:00")[1]["id"] == "b2"


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field(api, field):
    api.room()
    d = body()
    del d[field]
    err(api.call("POST", "/bookings", d), 422, "validation_error")


@pytest.mark.parametrize("over", [
    {"room_id": 1}, {"room_id": None}, {"title": 5}, {"title": None}, {"title": ""}, {"title": "   "},
    {"title": "x" * 101}, {"title": " " + "x" * 101}, {"attendees": 0}, {"attendees": -1},
    {"attendees": True}, {"attendees": "1"}, {"attendees": 1.5}, {"attendees": None},
    {"start": 5}, {"end": None}, {"start": True},
])
def test_booking_bad_types(api, over):
    api.room()
    err(api.call("POST", "/bookings", body(**over)), 422, "validation_error")


@pytest.mark.parametrize("start", [
    "2026-03-02T09:00:00", "2026-03-02 09:00:00Z", "2026-03-02T09:00Z", "2026-03-02T09:00:00z",
    "2026-03-02T09:00:00+00:00", "2026-03-02T09:00:00.000Z", "2026-3-2T09:00:00Z", "2026-03-02",
    "2026-02-30T09:00:00Z", "2026-13-02T09:00:00Z", "2026-03-02T25:00:00Z", "garbage", "",
    " 2026-03-02T09:00:00Z", "2026-03-02T09:00:00Z ", "2026-03-02T09:00:00Z\n",
    "2026-03-02T09:05:00Z", "2026-03-02T09:10:00Z", "2026-03-02T09:00:30Z", "2026-03-02T09:59:00Z",
    "2026-03-02T09:00:01Z",
])
def test_booking_bad_start(api, start):
    api.room()
    err(api.call("POST", "/bookings", body(start=start, end="2026-03-02T12:00:00Z")), 422, "validation_error")
    err(api.call("POST", "/bookings", body(start="2026-03-02T07:00:00Z", end=start)), 422, "validation_error")


@pytest.mark.parametrize("m", ["00", "15", "30", "45"])
def test_booking_aligned_minutes(api, m):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", body(r, start=f"2026-03-02T09:{m}:00Z", end=f"2026-03-02T10:{m}:00Z"))
    assert s == 201


def test_booking_end_not_after_start(api):
    api.room()
    err(api.call("POST", "/bookings", body(start=ts("10:00"), end=ts("10:00"))), 422, "validation_error")
    err(api.call("POST", "/bookings", body(start=ts("10:00"), end=ts("09:00"))), 422, "validation_error")


def test_booking_max_duration(api):
    r = api.room()
    err(api.call("POST", "/bookings", body(r, start=ts("08:00"), end=ts("16:15"))), 422, "validation_error")
    assert bk(api, r, "08:00", "16:00")[0] == 201


def test_booking_crossing_days(api):
    r = api.room()
    s, b, _ = api.call("POST", "/bookings", body(r, start="2026-03-02T20:00:00Z", end="2026-03-03T04:00:00Z"))
    assert s == 201 and b["end"] == "2026-03-03T04:00:00Z"
    err(api.call("POST", "/bookings", body(r, start="2026-03-04T20:00:00Z", end="2026-03-05T04:15:00Z")),
        422, "validation_error")


def test_booking_title_bounds(api):
    r = api.room()
    assert bk(api, r, "09:00", "10:00", title="x" * 100)[0] == 201
    s, b, _ = bk(api, r, "10:00", "11:00", title=" " + "y" * 100 + " ")
    assert s == 201 and b["title"] == "y" * 100


# ---------- check order ----------
def test_validation_before_room_lookup(api):
    err(api.call("POST", "/bookings", body("nope", attendees=0)), 422, "validation_error")


def test_unknown_room(api):
    api.room()
    err(api.call("POST", "/bookings", body("r9")), 404, "not_found")


def test_room_lookup_before_capacity(api):
    err(api.call("POST", "/bookings", body("r9", attendees=1000)), 404, "not_found")


def test_capacity(api):
    r = api.room(capacity=3)
    err(bk(api, r, "09:00", "10:00", attendees=4), 422, "over_capacity")
    assert bk(api, r, "09:00", "10:00", attendees=3)[0] == 201


def test_capacity_before_conflict(api):
    r = api.room(capacity=3)
    bk(api, r, "09:00", "10:00")
    err(bk(api, r, "09:00", "10:00", attendees=4), 422, "over_capacity")


def test_failed_booking_consumes_no_id(api):
    r = api.room(capacity=2)
    bk(api, r, "09:00", "10:00", attendees=5)
    bk(api, r, "09:00", "10:00", attendees=1)
    s, b, _ = bk(api, r, "09:30", "10:30")
    assert s == 409
    assert bk(api, r, "10:00", "11:00")[1]["id"] == "b2"


# ---------- conflicts ----------
@pytest.mark.parametrize("s,e", [("09:00", "10:00"), ("09:30", "10:30"), ("08:00", "09:15"), ("09:15", "09:45"),
                                 ("08:00", "11:00"), ("09:45", "10:30"), ("08:45", "09:15")])
def test_conflict_cases(api, s, e):
    r = api.room()
    assert bk(api, r, "09:00", "10:00")[0] == 201
    err(bk(api, r, s, e), 409, "conflict")


def test_adjacent_both_sides(api):
    r = api.room()
    assert bk(api, r, "09:00", "10:00")[0] == 201
    assert bk(api, r, "08:00", "09:00")[0] == 201
    assert bk(api, r, "10:00", "11:00")[0] == 201
    err(bk(api, r, "08:45", "09:15"), 409, "conflict")


def test_conflict_is_per_room(api):
    r1, r2 = api.room("A"), api.room("B")
    assert bk(api, r1, "09:00", "10:00")[0] == 201
    assert bk(api, r2, "09:00", "10:00")[0] == 201


def test_conflict_across_midnight(api):
    r = api.room()
    api.call("POST", "/bookings", body(r, start="2026-03-02T22:00:00Z", end="2026-03-03T02:00:00Z"))
    err(api.call("POST", "/bookings", body(r, start="2026-03-03T01:00:00Z", end="2026-03-03T03:00:00Z")),
        409, "conflict")
    assert api.call("POST", "/bookings", body(r, start="2026-03-03T02:00:00Z", end="2026-03-03T03:00:00Z"))[0] == 201


# ---------- get / delete ----------
def test_get_delete_bookings(api):
    r = api.room()
    bk(api, r, "09:00", "10:00")
    err(api.call("GET", "/bookings/b2"), 404, "not_found")
    err(api.call("DELETE", "/bookings/b2"), 404, "not_found")
    s, b, h = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    err(api.call("GET", "/bookings/b1"), 404, "not_found")
    err(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    assert api.call("GET", "/bookings")[1] == {"bookings": [], "next_cursor": None}


def test_ids_not_reused(api):
    r = api.room()
    bk(api, r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert bk(api, r, "09:00", "10:00")[1]["id"] == "b2"
    api.call("DELETE", "/bookings/b2")
    assert bk(api, r, "09:00", "10:00")[1]["id"] == "b3"


def test_delete_frees_only_that_slot(api):
    r = api.room()
    bk(api, r, "09:00", "10:00")
    bk(api, r, "10:00", "11:00")
    api.call("DELETE", "/bookings/b1")
    err(bk(api, r, "10:30", "11:30"), 409, "conflict")
    assert bk(api, r, "09:00", "10:00")[0] == 201


# ---------- idempotency ----------
def test_idem_replay(api):
    r = api.room()
    H = {"Idempotency-Key": "k1"}
    s1, b1, _ = api.call("POST", "/bookings", body(r), headers=H)
    assert s1 == 201
    s2, b2, _ = api.call("POST", "/bookings", body(r), headers=H)
    assert s2 == 201 and b2 == b1
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1
    # next booking gets next id: nothing was created by replay
    assert bk(api, r, "11:00", "12:00")[1]["id"] == "b2"


def test_idem_equal_json_different_formatting(api):
    r = api.room()
    H = {"Idempotency-Key": "k1"}
    raw1 = ('{"room_id":"%s","start":"%s","end":"%s","title":"x","attendees":1}' % (r, ts("09:00"), ts("10:00"))).encode()
    raw2 = ('{ "attendees": 1, "title": "x",\n "end": "%s", "start": "%s", "room_id": "%s" }'
            % (ts("10:00"), ts("09:00"), r)).encode()
    s1, b1, _ = api.call("POST", "/bookings", raw=raw1, headers=H)
    s2, b2, _ = api.call("POST", "/bookings", raw=raw2, headers=H)
    assert s1 == 201 and s2 == 201 and b1 == b2


@pytest.mark.parametrize("over", [{"title": "other"}, {"attendees": 2}, {"end": "2026-03-02T11:00:00Z"},
                                  {"start": "2026-03-02T08:00:00Z"}, {"room_id": "r2"}])
def test_idem_mismatch(api, over):
    api.room("A")
    api.room("B")
    H = {"Idempotency-Key": "k1"}
    assert api.call("POST", "/bookings", body("r1"), headers=H)[0] == 201
    err(api.call("POST", "/bookings", body("r1", **over), headers=H), 422, "idempotency_mismatch")
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1


def test_idem_extra_field_is_different_body(api):
    r = api.room()
    H = {"Idempotency-Key": "k1"}
    api.call("POST", "/bookings", body(r), headers=H)
    err(api.call("POST", "/bookings", body(r, extra=1), headers=H), 422, "idempotency_mismatch")


def test_idem_failed_not_remembered(api):
    r = api.room(capacity=2)
    H = {"Idempotency-Key": "k1"}
    err(api.call("POST", "/bookings", body(r, attendees=5), headers=H), 422, "over_capacity")
    s, b, _ = api.call("POST", "/bookings", body(r, attendees=1), headers=H)
    assert s == 201 and b["attendees"] == 1


def test_idem_failed_validation_and_conflict_not_remembered(api):
    r = api.room()
    H = {"Idempotency-Key": "k1"}
    err(api.call("POST", "/bookings", body(r, title=""), headers=H), 422, "validation_error")
    assert api.call("POST", "/bookings", body(r), headers=H)[0] == 201
    H2 = {"Idempotency-Key": "k2"}
    err(api.call("POST", "/bookings", body(r), headers=H2), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.call("POST", "/bookings", body(r), headers=H2)
    assert s == 201 and b["id"] == "b2"


def test_idem_keys_independent_and_no_key(api):
    r = api.room()
    assert api.call("POST", "/bookings", body(r), headers={"Idempotency-Key": "a"})[0] == 201
    err(api.call("POST", "/bookings", body(r), headers={"Idempotency-Key": "b"}), 409, "conflict")
    err(api.call("POST", "/bookings", body(r)), 409, "conflict")
    # different key, different slot
    s, b, _ = api.call("POST", "/bookings", body(r, start=ts("11:00"), end=ts("12:00")), headers={"Idempotency-Key": "b"})
    assert s == 201 and b["id"] == "b2"


# ---------- list ----------
def seed(api):
    r1, r2 = api.room("A"), api.room("B")
    bk(api, r1, "11:00", "12:00", title="a")   # b1
    bk(api, r1, "09:00", "10:00", title="b")   # b2
    bk(api, r2, "09:00", "10:00", title="c")   # b3
    bk(api, r1, "10:00", "11:00", title="d")   # b4
    return r1, r2


def ids(resp):
    s, b, _ = resp
    assert s == 200, b
    return [x["id"] for x in b["bookings"]]


def test_list_order_and_ties(api):
    seed(api)
    assert ids(api.call("GET", "/bookings")) == ["b2", "b3", "b4", "b1"]


def test_list_room_filter(api):
    seed(api)
    assert ids(api.call("GET", "/bookings?room_id=r1")) == ["b2", "b4", "b1"]
    assert ids(api.call("GET", "/bookings?room_id=r2")) == ["b3"]
    s, b, _ = api.call("GET", "/bookings?room_id=zzz")
    assert s == 200 and b == {"bookings": [], "next_cursor": None}


def test_list_from_to(api):
    seed(api)
    q = lambda s: ids(api.call("GET", "/bookings?" + s))
    # from: end > from
    assert q("from=2026-03-02T10:00:00Z") == ["b4", "b1"]
    assert q("from=2026-03-02T09:59:59Z") == ["b2", "b3", "b4", "b1"]
    assert q("from=2026-03-02T12:00:00Z") == []
    assert q("from=2026-03-02T11:59:59Z") == ["b1"]
    # to: start < to
    assert q("to=2026-03-02T10:00:00Z") == ["b2", "b3"]
    assert q("to=2026-03-02T09:00:00Z") == []
    assert q("to=2026-03-02T09:00:01Z") == ["b2", "b3"]
    assert q("from=2026-03-02T10:00:00Z&to=2026-03-02T11:00:00Z") == ["b4"]
    assert q("from=2026-03-02T09:30:00Z&to=2026-03-02T10:30:00Z") == ["b2", "b3", "b4"]
    assert q("from=2026-03-02T09:10:07Z&to=2026-03-02T09:20:33Z") == ["b2", "b3"]
    assert q("room_id=r1&from=2026-03-02T09:30:00Z&to=2026-03-02T10:30:00Z") == ["b2", "b4"]


@pytest.mark.parametrize("q", [
    "from=garbage", "to=garbage", "from=2026-03-02", "to=2026-03-02T09:00:00", "from=", "to=",
    "from=2026-02-30T09:00:00Z", "limit=0", "limit=101", "limit=-1", "limit=abc", "limit=", "limit=1.5",
    "limit=true", "cursor=garbage", "cursor=", "cursor=c", "cursor=c2026-03-02T09:00:00Z_",
])
def test_list_invalid_params(api, q):
    seed(api)
    err(api.call("GET", "/bookings?" + q), 422, "validation_error")


def test_list_limit_bounds(api):
    seed(api)
    assert len(ids(api.call("GET", "/bookings?limit=1"))) == 1
    assert len(ids(api.call("GET", "/bookings?limit=100"))) == 4


def test_pagination_walk(api):
    r = api.room()
    for h in range(8, 18):
        bk(api, r, f"{h:02d}:00", f"{h:02d}:30")
    r2 = api.room("B")
    for h in (8, 9, 12):  # same start times as some in r1: ties
        bk(api, r2, f"{h:02d}:00", f"{h:02d}:30")
    all_ids = ids(api.call("GET", "/bookings?limit=100"))
    assert len(all_ids) == 13
    assert all_ids[:3] == ["b1", "b11", "b2"]
    for lim in (1, 2, 3, 5, 13, 14):
        got, cur, pages = [], None, 0
        while True:
            q = f"/bookings?limit={lim}" + (f"&cursor={cur}" if cur else "")
            s, b, _ = api.call("GET", q)
            assert s == 200 and len(b["bookings"]) <= lim
            got += [x["id"] for x in b["bookings"]]
            pages += 1
            cur = b["next_cursor"]
            if cur is None:
                break
            assert isinstance(cur, str) and cur
            assert pages < 50
        assert got == all_ids, lim


def test_pagination_exact_boundary_null(api):
    r = api.room()
    for h in (8, 9, 10, 11):
        bk(api, r, f"{h:02d}:00", f"{h:02d}:30")
    s, b, _ = api.call("GET", "/bookings?limit=4")
    assert b["next_cursor"] is None and len(b["bookings"]) == 4
    s, b, _ = api.call("GET", "/bookings?limit=3")
    assert b["next_cursor"] is not None
    s, b2, _ = api.call("GET", "/bookings?limit=3&cursor=" + b["next_cursor"])
    assert [x["id"] for x in b2["bookings"]] == ["b4"] and b2["next_cursor"] is None


def test_pagination_with_filters(api):
    seed(api)
    s, b, _ = api.call("GET", "/bookings?room_id=r1&limit=2")
    assert [x["id"] for x in b["bookings"]] == ["b2", "b4"] and b["next_cursor"]
    s, b, _ = api.call("GET", "/bookings?room_id=r1&limit=2&cursor=" + b["next_cursor"])
    assert [x["id"] for x in b["bookings"]] == ["b1"] and b["next_cursor"] is None


def test_default_limit_20(api):
    r = api.room()
    n = 0
    for d in range(1, 4):
        for h in range(8, 16):
            api.call("POST", "/bookings", body(r, start=f"2026-03-{d:02d}T{h:02d}:00:00Z",
                                               end=f"2026-03-{d:02d}T{h:02d}:30:00Z"))
            n += 1
    s, b, _ = api.call("GET", "/bookings")
    assert len(b["bookings"]) == 20 and b["next_cursor"]
    s, b2, _ = api.call("GET", "/bookings?cursor=" + b["next_cursor"])
    assert len(b2["bookings"]) == 4 and b2["next_cursor"] is None


def test_pagination_survives_delete(api):
    r = api.room()
    for h in (8, 9, 10, 11):
        bk(api, r, f"{h:02d}:00", f"{h:02d}:30")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    api.call("DELETE", "/bookings/b2")
    s, b2, _ = api.call("GET", "/bookings?limit=2&cursor=" + b["next_cursor"])
    assert [x["id"] for x in b2["bookings"]] == ["b3", "b4"]


# ---------- availability ----------
def avail(api, r, date="2026-03-02"):
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date={date}")
    assert s == 200, b
    assert b["date"] == date
    return [(x["start"][11:19], x["end"][11:19]) for x in b["free"]]


def test_avail_empty_day(api):
    r = api.room()
    assert avail(api, r) == [("08:00:00", "18:00:00")]


def test_avail_gaps(api):
    r = api.room()
    bk(api, r, "09:00", "10:00")
    bk(api, r, "10:00", "11:00")
    bk(api, r, "13:15", "14:45")
    assert avail(api, r) == [("08:00:00", "09:00:00"), ("11:00:00", "13:15:00"), ("14:45:00", "18:00:00")]


def test_avail_full_day(api):
    r = api.room()
    bk(api, r, "08:00", "13:00")
    bk(api, r, "13:00", "18:00")
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert s == 200 and b == {"date": "2026-03-02", "free": []}


def test_avail_clipping(api):
    r = api.room()
    api.call("POST", "/bookings", body(r, start="2026-03-02T05:00:00Z", end="2026-03-02T09:00:00Z"))
    api.call("POST", "/bookings", body(r, start="2026-03-02T17:00:00Z", end="2026-03-02T20:00:00Z"))
    assert avail(api, r) == [("09:00:00", "17:00:00")]


def test_avail_outside_bookings_ignored(api):
    r = api.room()
    api.call("POST", "/bookings", body(r, start="2026-03-02T06:00:00Z", end="2026-03-02T08:00:00Z"))
    api.call("POST", "/bookings", body(r, start="2026-03-02T18:00:00Z", end="2026-03-02T19:00:00Z"))
    api.call("POST", "/bookings", body(r, start="2026-03-03T09:00:00Z", end="2026-03-03T10:00:00Z"))
    assert avail(api, r) == [("08:00:00", "18:00:00")]


def test_avail_spanning_booking(api):
    r = api.room()
    api.call("POST", "/bookings", body(r, start="2026-03-02T06:00:00Z", end="2026-03-02T14:00:00Z"))
    assert avail(api, r) == [("14:00:00", "18:00:00")]
    api.call("POST", "/bookings", body(r, start="2026-03-02T14:00:00Z", end="2026-03-02T22:00:00Z"))
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == []


def test_avail_other_room_and_delete(api):
    r1, r2 = api.room("A"), api.room("B")
    bk(api, r1, "09:00", "10:00")
    assert avail(api, r2) == [("08:00:00", "18:00:00")]
    api.call("DELETE", "/bookings/b1")
    assert avail(api, r1) == [("08:00:00", "18:00:00")]


def test_avail_full_timestamps(api):
    r = api.room()
    s, b, _ = api.call("GET", f"/rooms/{r}/availability?date=2026-03-02")
    assert b["free"] == [{"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T18:00:00Z"}]


@pytest.mark.parametrize("q", ["", "?date=", "?date=2026-02-30", "?date=2026-13-01", "?date=garbage",
                               "?date=2026-3-2", "?date=20260302", "?date=2026-03-02T00:00:00Z", "?other=2026-03-02"])
def test_avail_bad_date(api, q):
    r = api.room()
    err(api.call("GET", f"/rooms/{r}/availability{q}"), 422, "validation_error")


def test_avail_leap_day(api):
    r = api.room()
    assert avail(api, r, "2028-02-29") == [("08:00:00", "18:00:00")]
    err(api.call("GET", f"/rooms/{r}/availability?date=2026-02-29"), 422, "validation_error")


def test_avail_unknown_room_before_date(api):
    err(api.call("GET", "/rooms/r9/availability?date=2026-03-02"), 404, "not_found")
    err(api.call("GET", "/rooms/r9/availability"), 404, "not_found")
    err(api.call("GET", "/rooms/r9/availability?date=bad"), 404, "not_found")
