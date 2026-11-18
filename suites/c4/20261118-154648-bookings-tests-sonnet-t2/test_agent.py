import pytest
from helpers import api, code, ts  # noqa: F401


def err(resp, status, c):
    s, b, h = resp
    assert s == status, (s, b)
    assert code(b) == c
    assert isinstance(b["error"]["message"], str)
    assert h["content-type"].startswith("application/json")


def body_for(room, start="09:00", end="10:00", title="t", attendees=1):
    return {"room_id": room, "start": ts(start), "end": ts(end), "title": title, "attendees": attendees}


def post_b(api, body, headers=None):
    return api.call("POST", "/bookings", body, headers=headers)


# ---------------- general ----------------

def test_not_found_paths(api):
    for p in ["/nope", "/rooms/", "/bookings/b1/x", "/rooms/r1/foo", "/"]:
        err(api.call("GET", p), 404, "not_found")


def test_method_not_allowed(api):
    api.room()
    api.book("r1", "09:00", "10:00")
    for m, p in [("PUT", "/rooms"), ("DELETE", "/rooms"), ("POST", "/rooms/r1"), ("DELETE", "/rooms/r1"),
                 ("POST", "/rooms/r1/availability"), ("PUT", "/bookings"), ("DELETE", "/bookings"),
                 ("POST", "/bookings/b1"), ("PUT", "/bookings/b1"), ("PATCH", "/bookings/b1")]:
        err(api.call(m, p, {} if m in ("POST", "PUT", "PATCH") else None), 405, "method_not_allowed")


@pytest.mark.parametrize("path", ["/rooms", "/bookings"])
@pytest.mark.parametrize("raw", [b"{nope", b"", b"[]", b"123", b'"s"', b"null", b"[1,2]"])
def test_bad_json(api, path, raw):
    err(api.call("POST", path, raw=raw), 400, "bad_json")


def test_unknown_fields_ignored(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "A", "capacity": 3, "extra": 1})
    assert s == 201 and b == {"id": "r1", "name": "A", "capacity": 3}
    d = body_for("r1")
    d["zzz"] = [1]
    assert post_b(api, d)[0] == 201


# ---------------- rooms ----------------

def test_room_basic_and_ids(api):
    for i in range(1, 4):
        s, b, h = api.call("POST", "/rooms", {"name": f"R{i}", "capacity": i})
        assert s == 201 and b == {"id": f"r{i}", "name": f"R{i}", "capacity": i}
        assert h["content-type"].startswith("application/json")
    s, b, _ = api.call("GET", "/rooms")
    assert [r["name"] for r in b["rooms"]] == ["R1", "R2", "R3"]
    s, b, _ = api.call("GET", "/rooms/r2")
    assert s == 200 and b == {"id": "r2", "name": "R2", "capacity": 2}


def test_rooms_empty(api):
    assert api.call("GET", "/rooms")[:2] == (200, {"rooms": []})


def test_room_get_unknown(api):
    err(api.call("GET", "/rooms/r9"), 404, "not_found")


def test_room_name_trim_and_limits(api):
    s, b, _ = api.call("POST", "/rooms", {"name": "  Foo  ", "capacity": 1})
    assert s == 201 and b["name"] == "Foo"
    s, b, _ = api.call("POST", "/rooms", {"name": "x" * 50, "capacity": 1})
    assert s == 201
    s, b, _ = api.call("POST", "/rooms", {"name": " " + "y" * 50 + " ", "capacity": 1})
    assert s == 201 and b["name"] == "y" * 50
    err(api.call("POST", "/rooms", {"name": "z" * 51, "capacity": 1}), 422, "validation_error")
    err(api.call("POST", "/rooms", {"name": "   ", "capacity": 1}), 422, "validation_error")


@pytest.mark.parametrize("cap", [0, -1, 501, True, False, "5", 5.5, None, [5]])
def test_room_bad_capacity(api, cap):
    err(api.call("POST", "/rooms", {"name": "A", "capacity": cap}), 422, "validation_error")


@pytest.mark.parametrize("cap", [1, 500])
def test_room_capacity_bounds(api, cap):
    assert api.call("POST", "/rooms", {"name": "A", "capacity": cap})[0] == 201


@pytest.mark.parametrize("name", [None, 5, True, ["a"], {"a": 1}])
def test_room_bad_name_type(api, name):
    err(api.call("POST", "/rooms", {"name": name, "capacity": 4}), 422, "validation_error")


def test_room_missing_fields(api):
    err(api.call("POST", "/rooms", {"capacity": 4}), 422, "validation_error")
    err(api.call("POST", "/rooms", {"name": "A"}), 422, "validation_error")
    err(api.call("POST", "/rooms", {}), 422, "validation_error")


def test_room_duplicate_name(api):
    api.room("Atlas")
    err(api.call("POST", "/rooms", {"name": "atlas", "capacity": 2}), 409, "room_exists")
    err(api.call("POST", "/rooms", {"name": "  ATLAS ", "capacity": 2}), 409, "room_exists")
    # failed attempts don't consume ids
    assert api.room("Other") == "r2"


def test_room_validation_before_duplicate(api):
    api.room("Atlas")
    err(api.call("POST", "/rooms", {"name": "Atlas", "capacity": 0}), 422, "validation_error")


# ---------------- bookings: validation ----------------

def test_booking_success_shape_and_trim(api):
    r = api.room()
    s, b, h = post_b(api, body_for(r, title="  Hello  ", attendees=3))
    assert s == 201 and h["content-type"].startswith("application/json")
    assert b == {"id": "b1", "room_id": r, "start": ts("09:00"), "end": ts("10:00"), "title": "Hello", "attendees": 3}
    s, b2, _ = api.call("GET", "/bookings/b1")
    assert b2 == b


@pytest.mark.parametrize("field", ["room_id", "start", "end", "title", "attendees"])
def test_booking_missing_field(api, field):
    r = api.room()
    d = body_for(r)
    del d[field]
    err(post_b(api, d), 422, "validation_error")


@pytest.mark.parametrize("field,val", [
    ("room_id", 1), ("room_id", None), ("start", 5), ("end", None), ("title", 5), ("title", None),
    ("attendees", "2"), ("attendees", True), ("attendees", 1.5), ("attendees", None),
])
def test_booking_wrong_types(api, field, val):
    r = api.room()
    d = body_for(r)
    d[field] = val
    err(post_b(api, d), 422, "validation_error")


@pytest.mark.parametrize("bad", [
    "2026-03-02T09:00:00", "2026-03-02 09:00:00Z", "2026-03-02T09:00Z", "2026-03-02T09:00:00+00:00",
    "2026-03-02T09:00:00.000Z", "2026-3-2T09:00:00Z", "2026-03-02t09:00:00z", "garbage", "",
    "2026-02-30T09:00:00Z", "2026-03-02T25:00:00Z", "2026-03-02T09:60:00Z", " 2026-03-02T09:00:00Z",
    "2026-03-02T09:00:00Z ",
])
def test_booking_bad_timestamp(api, bad):
    r = api.room()
    d = body_for(r)
    d["start"] = bad
    err(post_b(api, d), 422, "validation_error")
    d = body_for(r)
    d["end"] = bad
    err(post_b(api, d), 422, "validation_error")


@pytest.mark.parametrize("t", ["09:05", "09:10", "09:59", "09:01"])
def test_booking_minute_alignment(api, t):
    r = api.room()
    err(post_b(api, body_for(r, t, "11:00")), 422, "validation_error")
    err(post_b(api, body_for(r, "08:00", t.replace("09", "10"))), 422, "validation_error")


def test_booking_seconds_alignment(api):
    r = api.room()
    d = body_for(r)
    d["start"] = "2026-03-02T09:00:30Z"
    err(post_b(api, d), 422, "validation_error")
    d = body_for(r)
    d["end"] = "2026-03-02T10:00:01Z"
    err(post_b(api, d), 422, "validation_error")


@pytest.mark.parametrize("m", ["00", "15", "30", "45"])
def test_booking_aligned_minutes_ok(api, m):
    r = api.room()
    assert api.book(r, f"09:{m}", f"11:{m}")[0] == 201


def test_booking_end_not_after_start(api):
    r = api.room()
    err(api.book(r, "10:00", "10:00"), 422, "validation_error")
    err(api.book(r, "11:00", "10:00"), 422, "validation_error")


def test_booking_max_duration(api):
    r = api.room()
    assert api.book(r, "08:00", "16:00")[0] == 201
    err(api.book(r, "09:00", "17:15"), 422, "validation_error")
    r2 = api.room("B")
    err(api.book(r2, "08:00", "16:15"), 422, "validation_error")
    # across days
    err(api.book(r2, "2026-03-02T20:00:00Z", "2026-03-03T06:00:00Z"), 422, "validation_error")
    assert api.book(r2, "2026-03-02T20:00:00Z", "2026-03-03T04:00:00Z")[0] == 201


def test_booking_title_rules(api):
    r = api.room()
    err(api.book(r, "09:00", "10:00", title=""), 422, "validation_error")
    err(api.book(r, "09:00", "10:00", title="   "), 422, "validation_error")
    err(api.book(r, "09:00", "10:00", title="x" * 101), 422, "validation_error")
    s, b, _ = api.book(r, "09:00", "10:00", title="x" * 100)
    assert s == 201
    s, b, _ = api.book(r, "10:00", "11:00", title=" " + "y" * 100 + " ")
    assert s == 201 and b["title"] == "y" * 100


@pytest.mark.parametrize("n", [0, -3])
def test_booking_attendees_min(api, n):
    r = api.room()
    err(api.book(r, "09:00", "10:00", attendees=n), 422, "validation_error")


def test_booking_check_order(api):
    r = api.room(capacity=2)
    # validation before room existence
    err(post_b(api, body_for("r99", "09:05", "10:00")), 422, "validation_error")
    err(post_b(api, body_for("r99", attendees=0)), 422, "validation_error")
    # room existence before capacity
    err(post_b(api, body_for("r99", attendees=50)), 404, "not_found")
    # capacity before conflict
    assert api.book(r, "09:00", "10:00")[0] == 201
    err(api.book(r, "09:00", "10:00", attendees=3), 422, "over_capacity")
    err(api.book(r, "09:00", "10:00", attendees=2), 409, "conflict")


def test_capacity_boundary(api):
    r = api.room(capacity=5)
    assert api.book(r, "09:00", "10:00", attendees=5)[0] == 201
    err(api.book(r, "10:00", "11:00", attendees=6), 422, "over_capacity")


def test_failed_booking_does_not_consume_id(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "09:00", "10:00")  # conflict
    api.book(r, "09:05", "10:00")  # invalid
    s, b, _ = api.book(r, "10:00", "11:00")
    assert b["id"] == "b2"


# ---------------- conflicts ----------------

def test_conflict_variants(api):
    r = api.room()
    api.book(r, "10:00", "12:00")
    for a, b in [("09:00", "10:15"), ("11:45", "13:00"), ("10:30", "11:00"), ("09:00", "13:00"),
                 ("10:00", "12:00"), ("10:00", "10:15"), ("11:45", "12:00")]:
        err(api.book(r, a, b), 409, "conflict")
    assert api.book(r, "09:00", "10:00")[0] == 201
    assert api.book(r, "12:00", "13:00")[0] == 201


def test_conflict_only_same_room(api):
    r1 = api.room("A")
    r2 = api.room("B")
    assert api.book(r1, "09:00", "10:00")[0] == 201
    assert api.book(r2, "09:00", "10:00")[0] == 201


def test_conflict_across_days(api):
    r = api.room()
    api.book(r, "2026-03-02T22:00:00Z", "2026-03-03T02:00:00Z")
    err(api.book(r, "2026-03-03T01:00:00Z", "2026-03-03T03:00:00Z"), 409, "conflict")
    assert api.book(r, "2026-03-03T02:00:00Z", "2026-03-03T03:00:00Z")[0] == 201


def test_get_booking_unknown(api):
    err(api.call("GET", "/bookings/b1"), 404, "not_found")


def test_delete_booking(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    s, b, _ = api.call("DELETE", "/bookings/b1")
    assert s == 204 and b is None
    err(api.call("GET", "/bookings/b1"), 404, "not_found")
    err(api.call("DELETE", "/bookings/b1"), 404, "not_found")
    err(api.call("DELETE", "/bookings/b77"), 404, "not_found")
    assert api.call("GET", "/bookings")[1]["bookings"] == []


def test_ids_not_reused_after_delete(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    s, b, _ = api.book(r, "09:00", "10:00")
    assert b["id"] == "b2"
    api.call("DELETE", "/bookings/b2")
    assert api.book(r, "09:00", "10:00")[1]["id"] == "b3"


# ---------------- idempotency ----------------

def test_idempotent_replay(api):
    r = api.room()
    d = body_for(r, title="  Plan ")
    h = {"Idempotency-Key": "k1"}
    s1, b1, _ = post_b(api, d, h)
    assert s1 == 201
    s2, b2, hh = post_b(api, d, h)
    assert s2 == 201 and b2 == b1 and hh["content-type"].startswith("application/json")
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1
    # next booking gets next id
    assert api.book(r, "11:00", "12:00")[1]["id"] == "b2"


def test_idempotent_key_order_whitespace(api):
    r = api.room()
    h = {"Idempotency-Key": "k"}
    d = body_for(r)
    s1, b1, _ = post_b(api, d, h)
    raw = ('{ "attendees": 1,\n "title": "t", "end": "%s", "start": "%s", "room_id": "%s" }'
           % (d["end"], d["start"], r)).encode()
    s2, b2, _ = api.call("POST", "/bookings", raw=raw, headers=h)
    assert s2 == 201 and b2 == b1
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1


def test_idempotent_mismatch(api):
    r = api.room()
    h = {"Idempotency-Key": "k"}
    post_b(api, body_for(r), h)
    err(post_b(api, body_for(r, "11:00", "12:00"), h), 422, "idempotency_mismatch")
    err(post_b(api, body_for(r, title="other"), h), 422, "idempotency_mismatch")
    extra = body_for(r)
    extra["x"] = 1
    err(post_b(api, extra, h), 422, "idempotency_mismatch")
    assert len(api.call("GET", "/bookings")[1]["bookings"]) == 1


def test_idempotent_failures_not_remembered(api):
    r = api.room(capacity=2)
    h = {"Idempotency-Key": "k"}
    err(post_b(api, body_for(r, attendees=5), h), 422, "over_capacity")
    s, b, _ = post_b(api, body_for(r, "11:00", "12:00"), h)  # different body, allowed now
    assert s == 201 and b["id"] == "b1"
    # conflict failure not remembered either
    h2 = {"Idempotency-Key": "k2"}
    err(post_b(api, body_for(r, "11:00", "12:00"), h2), 409, "conflict")
    api.call("DELETE", "/bookings/b1")
    assert post_b(api, body_for(r, "11:00", "12:00"), h2)[0] == 201
    # invalid failure
    h3 = {"Idempotency-Key": "k3"}
    err(post_b(api, body_for(r, "09:05", "10:00"), h3), 422, "validation_error")
    assert post_b(api, body_for(r, "09:00", "10:00"), h3)[0] == 201


def test_idempotent_distinct_keys_and_no_key(api):
    r = api.room()
    d = body_for(r)
    assert post_b(api, d, {"Idempotency-Key": "a"})[0] == 201
    err(post_b(api, d, {"Idempotency-Key": "b"}), 409, "conflict")
    err(post_b(api, d), 409, "conflict")


# ---------------- listing ----------------

def seed(api):
    r1 = api.room("A")
    r2 = api.room("B")
    api.book(r1, "11:00", "12:00", title="b1")
    api.book(r2, "09:00", "10:00", title="b2")
    api.book(r1, "09:00", "10:00", title="b3")
    api.book(r1, "13:00", "14:00", title="b4")
    return r1, r2


def ids(resp):
    s, b, _ = resp
    assert s == 200, b
    return [x["id"] for x in b["bookings"]]


def test_list_order_start_then_creation(api):
    seed(api)
    assert ids(api.call("GET", "/bookings")) == ["b2", "b3", "b1", "b4"]


def test_list_empty(api):
    assert api.call("GET", "/bookings")[:2] == (200, {"bookings": [], "next_cursor": None})


def test_list_room_filter(api):
    seed(api)
    assert ids(api.call("GET", "/bookings?room_id=r1")) == ["b3", "b1", "b4"]
    assert ids(api.call("GET", "/bookings?room_id=r2")) == ["b2"]
    assert ids(api.call("GET", "/bookings?room_id=r99")) == []


def test_list_from_to(api):
    seed(api)
    q = lambda s: ids(api.call("GET", "/bookings?" + s))
    assert q("from=" + ts("10:00")) == ["b1", "b4"]  # end==from excluded
    assert q("from=2026-03-02T09:59:59Z") == ["b2", "b3", "b1", "b4"]
    assert q("to=" + ts("11:00")) == ["b2", "b3"]  # start==to excluded
    assert q("to=2026-03-02T11:00:01Z") == ["b2", "b3", "b1"]
    assert q("from=" + ts("10:00") + "&to=" + ts("13:00")) == ["b1"]
    assert q("from=2026-03-02T09:30:00Z&to=2026-03-02T09:31:00Z") == ["b2", "b3"]
    assert q("from=2026-03-02T09:30:00Z&to=2026-03-02T09:30:00Z") == ["b2", "b3"]
    assert q("from=2026-03-02T09:00:00Z&room_id=r2") == ["b2"]
    assert q("from=2026-03-03T00:00:00Z") == []


def test_list_invalid_params(api):
    seed(api)
    for qs in ["from=x", "to=2026-03-02", "from=2026-03-02T09:00:00", "to=", "from=",
               "limit=0", "limit=101", "limit=abc", "limit=-1", "limit=", "limit=1.5",
               "cursor=abc", "cursor=", "cursor=c2026-13-01T00:00:00Z_1", "cursor=c2026-03-02T09:00:00Z_x"]:
        err(api.call("GET", "/bookings?" + qs), 422, "validation_error")


def test_list_limit_bounds(api):
    r = api.room()
    for i in range(25):
        h = 8 + i // 4
        api.call("POST", "/bookings", {"room_id": r, "start": f"2026-03-02T{h:02d}:{(i % 4) * 15:02d}:00Z",
                                       "end": f"2026-03-02T{h + (i % 4 == 3):02d}:{((i % 4) * 15 + 15) % 60:02d}:00Z",
                                       "title": "t", "attendees": 1})
    s, b, _ = api.call("GET", "/bookings")
    assert len(b["bookings"]) == 20 and b["next_cursor"]  # default 20
    s, b, _ = api.call("GET", "/bookings?limit=100")
    assert len(b["bookings"]) == 25 and b["next_cursor"] is None
    s, b, _ = api.call("GET", "/bookings?limit=25")
    assert b["next_cursor"] is None
    s, b, _ = api.call("GET", "/bookings?limit=24")
    assert len(b["bookings"]) == 24 and b["next_cursor"]
    s, b, _ = api.call("GET", "/bookings?limit=1")
    assert len(b["bookings"]) == 1 and b["next_cursor"]


def walk(api, qs, limit):
    out, cur = [], None
    for _ in range(200):
        q = f"limit={limit}" + (("&" + qs) if qs else "") + (f"&cursor={cur}" if cur else "")
        s, b, _ = api.call("GET", "/bookings?" + q)
        assert s == 200, b
        assert len(b["bookings"]) <= limit
        out += [x["id"] for x in b["bookings"]]
        cur = b["next_cursor"]
        if cur is None:
            return out
    raise AssertionError("no end")


def test_pagination_walk(api):
    r1 = api.room("A")
    r2 = api.room("B")
    # many with equal starts across rooms, plus distinct ones
    n = 0
    for i in range(6):
        for r in (r1, r2):
            api.book(r, f"{9 + i}:00" if i > 0 else "09:00", f"{10 + i}:00")
            n += 1
    full = ids(api.call("GET", "/bookings"))
    assert len(full) == 12
    for limit in (1, 2, 3, 5, 11, 12, 13):
        assert walk(api, "", limit) == full
    assert walk(api, "room_id=r1", 2) == ids(api.call("GET", "/bookings?room_id=r1"))
    assert walk(api, "from=2026-03-02T11:00:00Z&to=2026-03-02T14:00:00Z", 2) == \
        ids(api.call("GET", "/bookings?from=2026-03-02T11:00:00Z&to=2026-03-02T14:00:00Z"))


def test_pagination_with_equal_start_order(api):
    rooms = [api.room(f"R{i}") for i in range(5)]
    for r in rooms:
        api.book(r, "09:00", "10:00")
    assert walk(api, "", 2) == ["b1", "b2", "b3", "b4", "b5"]


def test_pagination_after_delete(api):
    r = api.room()
    for i in range(5):
        api.book(r, f"{9 + i}:00", f"{10 + i}:00")
    s, b, _ = api.call("GET", "/bookings?limit=2")
    assert [x["id"] for x in b["bookings"]] == ["b1", "b2"]
    api.call("DELETE", "/bookings/b2")
    s, b2, _ = api.call("GET", "/bookings?limit=2&cursor=" + b["next_cursor"])
    assert [x["id"] for x in b2["bookings"]] == ["b3", "b4"]


# ---------------- availability ----------------

def avail(api, r, d="2026-03-02"):
    return api.call("GET", f"/rooms/{r}/availability?date={d}")


def fr(a, b, d="2026-03-02"):
    return {"start": f"{d}T{a}:00Z", "end": f"{d}T{b}:00Z"}


def test_availability_empty_day(api):
    r = api.room()
    s, b, h = avail(api, r)
    assert s == 200 and b == {"date": "2026-03-02", "free": [fr("08:00", "18:00")]}
    assert h["content-type"].startswith("application/json")


def test_availability_gaps(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.book(r, "10:00", "11:30")  # adjacent
    api.book(r, "13:00", "14:00")
    api.book(r, "17:00", "18:00")
    s, b, _ = avail(api, r)
    assert b["free"] == [fr("08:00", "09:00"), fr("11:30", "13:00"), fr("14:00", "17:00")]


def test_availability_clipping(api):
    r = api.room()
    api.book(r, "2026-03-02T06:00:00Z", "2026-03-02T09:00:00Z")
    api.book(r, "2026-03-02T17:00:00Z", "2026-03-02T20:00:00Z")
    s, b, _ = avail(api, r)
    assert b["free"] == [fr("09:00", "17:00")]


def test_availability_full_day(api):
    r = api.room()
    api.book(r, "2026-03-02T07:00:00Z", "2026-03-02T15:00:00Z")
    api.book(r, "2026-03-02T15:00:00Z", "2026-03-02T19:00:00Z")
    assert avail(api, r)[1] == {"date": "2026-03-02", "free": []}


def test_availability_other_days_and_rooms_ignored(api):
    r = api.room("A")
    r2 = api.room("B")
    api.book(r2, "09:00", "10:00")
    api.book(r, "2026-03-01T20:00:00Z", "2026-03-02T08:00:00Z")  # ends exactly at open
    api.book(r, "2026-03-02T18:00:00Z", "2026-03-02T19:00:00Z")  # starts at close
    api.book(r, "2026-03-03T09:00:00Z", "2026-03-03T10:00:00Z")
    assert avail(api, r)[1]["free"] == [fr("08:00", "18:00")]
    assert avail(api, r, "2026-03-03")[1]["free"] == [fr("08:00", "09:00", "2026-03-03"), fr("10:00", "18:00", "2026-03-03")]


def test_availability_quarter_gap(api):
    r = api.room()
    api.book(r, "08:00", "09:00")
    api.book(r, "09:15", "17:15")
    assert avail(api, r)[1]["free"] == [fr("09:00", "09:15"), fr("17:15", "18:00")]


def test_availability_after_delete(api):
    r = api.room()
    api.book(r, "09:00", "10:00")
    api.call("DELETE", "/bookings/b1")
    assert avail(api, r)[1]["free"] == [fr("08:00", "18:00")]


@pytest.mark.parametrize("q", ["", "?date=", "?date=2026-02-30", "?date=2026-13-01", "?date=20260302",
                               "?date=2026-3-2", "?date=garbage", "?date=2026-03-02T00:00:00Z", "?foo=1",
                               "?date=2026-03-02x"])
def test_availability_bad_date(api, q):
    r = api.room()
    err(api.call("GET", f"/rooms/{r}/availability{q}"), 422, "validation_error")


def test_availability_leap_date(api):
    r = api.room()
    assert avail(api, r, "2028-02-29")[0] == 200
    err(avail(api, r, "2026-02-29"), 422, "validation_error")


def test_availability_unknown_room_precedence(api):
    err(api.call("GET", "/rooms/r9/availability?date=2026-03-02"), 404, "not_found")
    err(api.call("GET", "/rooms/r9/availability?date=bad"), 404, "not_found")
    err(api.call("GET", "/rooms/r9/availability"), 404, "not_found")
