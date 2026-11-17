# sessionize change request 1: configurable gap, logout, refunds

The job runs nightly. Three changes are needed. Everything in `SPEC.md` that is not mentioned here stays exactly as it is.

## 1. Configurable session gap

New optional argument `--gap-minutes <N>`: a positive integer, default 30. A new session starts when the gap to the user's previous event is more than `N` minutes. A value that is not a positive integer: exit code 64, a message on stderr, and no output files are written.

`daily.json` gets a new top-level field `gap_minutes` with the value used.

## 2. Logout ends a session

New event type `logout` (no `amount`). A logout event belongs to the session it falls into under the gap rule and counts in `events`. It then closes that session: the user's next event starts a new session, no matter how small the gap is.

## 3. Refunds

New event type `refund`, with an `amount` under the same rules as a purchase (`bad_amount` otherwise).

- `sessions.csv` gets a new column `refunds`, the number of refund events in the session, placed between `purchases` and `revenue`.
- `revenue` of a session is now purchases minus refunds. It can be negative, written like `-5.00`.
- In `daily.json`, `revenue` of a day is the sum of the session revenues (so it is net of refunds too).
- `top_user` is the user with the highest net revenue that day (ties: the user that sorts first), but only if that user's net revenue is greater than zero. Otherwise `top_user` is `null`.
