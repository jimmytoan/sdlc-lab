# bookings: meeting room booking API

Build a small HTTP JSON API for booking meeting rooms.

- Entry point: `python server.py --port <N>`. It listens on `127.0.0.1:<N>` until killed.
- Python 3.13, standard library only. No network access besides serving. State is kept in memory and starts empty.
- Request and response bodies are JSON. Every response with a body has `Content-Type: application/json`.

## Errors

Every error response has this body, with the status code given in each section:

```json
{"error": {"code": "<code>", "message": "<any human readable text>"}}
```

General rules:

- A path that does not exist: `404`, code `not_found`.
- A path that exists, called with a method it does not support: `405`, code `method_not_allowed`.
- A request body that is not valid JSON, or is valid JSON but not an object: `400`, code `bad_json`.
- Unknown fields in a request body are ignored.
- A JSON boolean is never accepted where an integer is required.

## Rooms

### `POST /rooms`

Body: `{"name": string, "capacity": integer}`.

- `name`: 1 to 50 characters after trimming. Stored trimmed.
- `capacity`: integer from 1 to 500.
- Invalid or missing field: `422`, code `validation_error`.
- A room with the same name already exists (compared ignoring case, after trimming): `409`, code `room_exists`.

Success: `201` with `{"id": "r1", "name": "...", "capacity": 8}`. Room ids are `r1`, `r2`, ... in creation order.

### `GET /rooms`

`200` with `{"rooms": [room, ...]}` in creation order.

### `GET /rooms/{id}`

`200` with the room, or `404` `not_found`.

## Bookings

Timestamps are strings of exactly the form `YYYY-MM-DDTHH:MM:SSZ` (UTC), for example `2026-03-02T09:30:00Z`. Any other form is invalid.

### `POST /bookings`

Body: `{"room_id": string, "start": timestamp, "end": timestamp, "title": string, "attendees": integer}`.

Checks, in this order. The first failing check decides the response.

1. Validation, `422` `validation_error`:
   - every field present and of the right type;
   - `start` and `end` are valid timestamps whose minutes are `00`, `15`, `30` or `45` and whose seconds are `00`;
   - `end` is after `start`, and the booking is at most 8 hours long;
   - `title` is 1 to 100 characters after trimming (stored trimmed);
   - `attendees` is an integer of at least 1.
2. The room exists, else `404` `not_found`.
3. `attendees` does not exceed the room capacity, else `422` with code `over_capacity`.
4. The booking does not overlap another booking of the same room, else `409` with code `conflict`. Bookings are half-open intervals `[start, end)`: a booking may start exactly when another ends.

Success: `201` with

```json
{"id": "b1", "room_id": "r1", "start": "...", "end": "...", "title": "...", "attendees": 4}
```

Booking ids are `b1`, `b2`, ... in creation order. Ids are never reused, also not after a delete.

#### Idempotency

A client may send the header `Idempotency-Key: <any non-empty string>` with `POST /bookings`.

- When a request with a key succeeds (`201`), the server remembers the key together with the request body.
- A later request with the same key and an equal body (equal as parsed JSON, so key order and whitespace do not matter) creates nothing and returns the original `201` response again.
- A later request with the same key and a different body: `422` with code `idempotency_mismatch`.
- Failed requests are not remembered. The key can be used again.

### `GET /bookings/{id}`

`200` with the booking, or `404` `not_found`.

### `DELETE /bookings/{id}`

`204` with an empty body. The slot is free again. Deleting an unknown or already deleted booking: `404` `not_found`.

### `GET /bookings`

Lists bookings. Query parameters, all optional:

- `room_id`: only bookings of this room. An unknown room gives an empty list, not an error.
- `from`, `to`: timestamps. Only bookings that overlap the half-open window `[from, to)` are returned: a booking is included if its `end` is after `from` and its `start` is before `to`. Either may be given alone. These do not need to be aligned to quarter hours.
- `limit`: integer from 1 to 100, default 20.
- `cursor`: an opaque string taken from a previous response.

An invalid `from`, `to`, `limit` or `cursor`: `422` `validation_error`.

Response: `200` with `{"bookings": [booking, ...], "next_cursor": string or null}`.

- Order: by `start` ascending, then by creation order.
- `next_cursor` is `null` when there are no more results. Otherwise, sending it back as `cursor` together with the same filters returns the next page. Walking all pages returns every matching booking exactly once.

## Availability

### `GET /rooms/{id}/availability?date=YYYY-MM-DD`

Returns the free time of a room within the working day, which runs from `08:00:00Z` to `18:00:00Z` on the given date.

`200` with

```json
{"date": "2026-03-02", "free": [{"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T09:00:00Z"}, ...]}
```

- `free` lists the gaps between bookings inside the working day, ascending. Bookings that are partly outside the working day are clipped to it. Adjacent bookings leave no gap between them. A fully booked day gives an empty list.
- Missing or invalid `date` (must be a real calendar date): `422` `validation_error`.
- Unknown room: `404` `not_found`. The room check comes before the date check.
