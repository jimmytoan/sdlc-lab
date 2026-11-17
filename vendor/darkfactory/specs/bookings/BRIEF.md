# bookings: meeting room booking API (ticket)

We need a small HTTP JSON API so our office app can book meeting rooms.

- `python server.py --port <N>`, listens on 127.0.0.1. Python 3.13, standard library only. Keeping state in memory is fine.
- JSON in, JSON out. Errors look like `{"error": {"code": "...", "message": "..."}}`.
- Error codes: `not_found` (404), `method_not_allowed` (405), `bad_json` (400), `validation_error` (422), `over_capacity` (422), `room_exists` (409), `conflict` (409), `idempotency_mismatch` (422).

## Rooms

- `POST /rooms` with `{"name", "capacity"}` creates a room and returns `201` with `{"id": "r1", "name", "capacity"}`. Ids are `r1`, `r2`, ... Names are unique and at most 50 characters, capacity is 1 to 500.
- `GET /rooms` returns `{"rooms": [...]}`. `GET /rooms/{id}` returns one room.

## Bookings

Timestamps are UTC, like `2026-03-02T09:30:00Z`.

- `POST /bookings` with `{"room_id", "start", "end", "title", "attendees"}` returns `201` with the booking: `{"id": "b1", "room_id", "start", "end", "title", "attendees"}`. Ids are `b1`, `b2`, ...
  - Bookings start and end on quarter hours and are at most 8 hours long.
  - More attendees than the room holds: `over_capacity`.
  - Two bookings in the same room must not overlap: `conflict`. A meeting may start when the previous one ends.
  - Clients can send an `Idempotency-Key` header so that a retry of the same request does not create a second booking; the retry gets the original response. Reusing a key for a different request is `idempotency_mismatch`.
- `GET /bookings/{id}`, and `DELETE /bookings/{id}` (returns `204`; `404` if it does not exist).
- `GET /bookings` lists bookings sorted by start time: `{"bookings": [...], "next_cursor": ...}`. Filters: `room_id`, and `from` / `to` to get the bookings that overlap a time window. Paging with `limit` (1 to 100, default 20) and `cursor`; `next_cursor` is `null` on the last page.

## Availability

`GET /rooms/{id}/availability?date=YYYY-MM-DD` returns the free slots of the room during the working day (08:00 to 18:00 UTC):

```json
{"date": "2026-03-02", "free": [{"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T09:00:00Z"}]}
```
