# bookings change request 1: cleaning buffers, rescheduling, totals

The API is live. Three changes are needed. Everything in `SPEC.md` that is not mentioned here stays exactly as it is.

## 1. Cleaning buffer per room

`POST /rooms` accepts an optional field `buffer_minutes`: an integer from 0 to 60, default 0. Anything else is `422` `validation_error`. Every room object returned by the API now includes `buffer_minutes`.

A room with a buffer needs that many minutes between two bookings:

- Creating (or rescheduling, see below) a booking gives `409` `conflict` if the gap between it and another booking of the same room is smaller than the room's buffer. With a 15 minute buffer and an existing booking 09:00 to 10:00, a new booking may start at 10:15 or end at 08:45, but not start at 10:00.
- Availability: every booking of the room counts as busy from `start - buffer` to `end + buffer`, then clipped to the working day as before.
- For all rooms, a free interval shorter than 15 minutes is not listed.

## 2. Rescheduling

New endpoint `PATCH /bookings/{id}`. The body is a JSON object with any subset of `start`, `end`, `title`, `attendees`. Fields that are not sent keep their value. `room_id` cannot be changed, and is ignored like any unknown field.

Checks, in this order:

1. Body is a JSON object, else `400` `bad_json`.
2. The booking exists, else `404` `not_found`.
3. The resulting booking passes the same validation as `POST /bookings` (`422` `validation_error`).
4. `attendees` fits the room (`422` `over_capacity`).
5. No conflict with any other booking of the room, including the buffer rule (`409` `conflict`). A booking never conflicts with itself, so moving it by 15 minutes onto its own old slot is fine.

Success: `200` with the updated booking. A failed PATCH changes nothing. The booking keeps its id and its position in creation order.

## 3. Total count in listings

`GET /bookings` adds a field `total`: the number of bookings that match the filters (`room_id`, `from`, `to`), regardless of `limit` and `cursor`. It is the same number on every page.
