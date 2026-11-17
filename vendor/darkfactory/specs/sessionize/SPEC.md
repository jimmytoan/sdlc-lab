# sessionize: clickstream sessions batch job

Build a batch job that turns a file of raw web events into user sessions and a daily summary.

- Entry point: `python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>`
- Python 3.13, standard library only. No network.
- Reads `<INPUT_DIR>/events.jsonl`. Writes `sessions.csv`, `daily.json` and `rejects.jsonl` into `<OUTPUT_DIR>` (created if it does not exist). All three files are always written, also when they have no rows.
- Exit code 0 on success, also when some lines were rejected. If `events.jsonl` does not exist: exit code 2 and a message on stderr.

## Input

`events.jsonl` is UTF-8 with one JSON object per line. Lines that are empty or only whitespace are skipped (they still count when numbering lines). An event looks like this:

```json
{"event_id": "e1", "user": "Ann", "ts": "2026-03-01T09:00:00+07:00", "type": "purchase", "amount": "19.99"}
```

| Field | Rule |
| --- | --- |
| `event_id` | string, not empty after trimming |
| `user` | string, not empty after trimming. Users are compared after trimming and lower-casing; the lower-cased form is used in all output. |
| `ts` | string. ISO 8601 date and time with seconds and a UTC offset: `2026-03-01T09:00:00Z`, `2026-03-01T09:00:00+07:00`, `2026-03-01T09:00:00.250-05:30`. Fractional seconds are optional. A timestamp without an offset is invalid. |
| `type` | one of `view`, `click`, `purchase` |
| `amount` | required when `type` is `purchase`: a string holding a non-negative decimal with at most two fraction digits (`"5"`, `"5.5"`, `"19.99"`). Ignored for other types. |

## Rejected lines

A line that breaks a rule is rejected and takes no further part. Report the first failing check, in this order:

1. `bad_json`: the line is not valid JSON or not a JSON object.
2. `missing_field`: `event_id`, `user`, `ts` or `type` is absent, not a string, or empty after trimming.
3. `bad_ts`: `ts` cannot be parsed under the rule above.
4. `bad_type`: `type` is not one of the allowed values.
5. `bad_amount`: a `purchase` whose `amount` is absent, not a string, or not a valid amount.

`rejects.jsonl` has one JSON object per rejected line, in input order: `{"line": 7, "reason": "bad_ts"}`. `line` is the 1-based physical line number.

## Duplicates

Events are delivered at least once. Among valid events with the same `event_id` (exact string match), keep only the one with the earliest timestamp. If timestamps are equal, keep the one that comes first in the file. Dropped duplicates are not rejects.

## Sessions

All times are converted to UTC. For each user, order the kept events by timestamp (ties: file order). A new session starts with the user's first event, and whenever the gap to the user's previous event is more than 30 minutes. A gap of exactly 30 minutes stays in the same session.

`sessions.csv` has a header line and one line per session:

```
session_id,user,start,end,duration_s,events,purchases,revenue
ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:10:30Z,630,3,1,19.99
```

- `session_id`: `<user>-<n>`, where `n` counts the user's sessions from 1 in time order.
- `start`, `end`: timestamps of the first and last event of the session, in UTC as `YYYY-MM-DDTHH:MM:SSZ`. Fractional seconds are cut off in the output, but ordering and gaps use the full precision.
- `duration_s`: whole seconds from first to last event, rounded down.
- `events`: number of events. `purchases`: number of purchase events.
- `revenue`: sum of the purchase amounts, exact decimal arithmetic, always with two decimals (`0.00`).
- Rows are ordered by `start` (full precision) ascending, then by `user` ascending.

## Daily summary

`daily.json` is one JSON object:

```json
{
  "days": [
    {"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "24.99", "top_user": "ann"}
  ],
  "duplicates_dropped": 1,
  "rejected": 3
}
```

- A session belongs to the UTC date of its `start`, also when it runs past midnight.
- `days` lists every date with at least one session, ascending. `sessions` is the number of sessions, `users` the number of distinct users with a session that day, `revenue` the sum of the revenue of those sessions as a string with two decimals.
- `top_user` is the user with the highest revenue that day (ties: the user that sorts first). It is `null` when the day's revenue is zero.
- `duplicates_dropped` is the number of events dropped as duplicates. `rejected` is the number of rejected lines.
