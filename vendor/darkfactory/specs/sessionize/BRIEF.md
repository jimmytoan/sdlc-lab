# sessionize: clickstream sessions batch job (ticket)

We need a nightly job that turns raw web events into user sessions and a daily summary for the dashboard.

- `python job.py --in <INPUT_DIR> --out <OUTPUT_DIR>`, Python 3.13, standard library only.
- Reads `<INPUT_DIR>/events.jsonl`, writes `sessions.csv`, `daily.json` and `rejects.jsonl` to `<OUTPUT_DIR>`.
- Exit code 0 on success. If the input file is missing: exit code 2 and a message on stderr.

## Input

One JSON event per line:

```json
{"event_id": "e1", "user": "Ann", "ts": "2026-03-01T09:00:00+07:00", "type": "purchase", "amount": "19.99"}
```

`type` is `view`, `click` or `purchase`. Purchases have an `amount` (a decimal as a string). `ts` is ISO 8601 with a UTC offset. User names are not case sensitive; use lower case in the output. Events can arrive more than once (same `event_id`) and out of order; count each event once.

Bad lines go to `rejects.jsonl` as `{"line": <line number>, "reason": "<reason>"}` with reason `bad_json`, `missing_field`, `bad_ts`, `bad_type` or `bad_amount`. The job carries on with the rest.

## Sessions

A session is a run of events of one user with no more than 30 minutes of inactivity in between. Everything is in UTC.

`sessions.csv`:

```
session_id,user,start,end,duration_s,events,purchases,revenue
ann-1,ann,2026-03-01T02:00:00Z,2026-03-01T02:10:30Z,630,3,1,19.99
```

`session_id` is the user plus a running number per user. Rows are sorted by start time.

## Daily summary

`daily.json`:

```json
{
  "days": [{"date": "2026-03-01", "sessions": 2, "users": 2, "revenue": "24.99", "top_user": "ann"}],
  "duplicates_dropped": 1,
  "rejected": 3
}
```

One entry per day that has sessions. `top_user` is the user with the most revenue that day, `null` if there was no revenue.
