# C4: why agent suites fail on holdout-clean builds

Every agent test that failed on a dark factory build that passes all held-out scenarios, read against the spec and sorted by hand. Rules and verdicts: `sdlc/fasort.py`; failing tests per build: `results/c4/falsealarms.jsonl`; assertion text: `results/c4/falsealarms-detail.jsonl`.

Verdicts: **false-alarm** = the test pins something the spec explicitly leaves free; **open** = the spec does not settle it and the test pins the reference's choice; **ref-deviates** = the reference departs from the spec text and the build follows the spec; **real-bug** = the build departs from the spec text and the held-out suite misses it; **flaky** = failed in the real-fault run, passed on a re-run.

72 agent suites x 11 holdout-clean builds per spec = 792 suite-build pairs. 236 of them fail. A failing pair counts once, under its most serious verdict (real-bug, then ref-deviates, open, false-alarm, flaky).

| verdict | failing suite-build pairs | share of failing pairs |
|---|---|---|
| false-alarm | 31 | 13% |
| open | 109 | 46% |
| ref-deviates | 50 | 21% |
| real-bug | 38 | 16% |
| flaky | 8 | 3% |

## By writer

| writer | false-alarm | open | ref-deviates | real-bug | flaky | total |
|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 0 | 9 | 0 | 12 | 5 | 26 |
| claude-haiku-5-5 (black-box) | 0 | 2 | 11 | 1 | 1 | 15 |
| claude-sonnet-5-5 | 0 | 27 | 0 | 2 | 1 | 30 |
| gemini-3.5-flash-lite | 31 | 0 | 0 | 0 | 0 | 31 |
| gpt-6.1-terra | 0 | 58 | 28 | 22 | 0 | 108 |
| gpt-6.1-terra (black-box) | 0 | 13 | 11 | 1 | 1 | 26 |

## Behaviours

| spec | behaviour | verdict | suites | suite-build pairs | spec wording |
|---|---|---|---|---|---|
| bookings | error message must equal the code | false-alarm | 2 | 22 | error body: "message": "<any human readable text>" |
| splitr | usage-error message must contain 'usage error' | false-alarm | 3 | 9 | usage error: stderr 'any message' |
| splitr | 'A:1:2' in participants (name 'A:1' with weight 2, or a bad weight) | open | 10 | 72 | 'name:weight, where weight is a positive integer'; the spec does not say a name cannot contain ':' |
| sessionize | ISO 8601 variants: '+0700' offset, lower-case 't'/'z', surrounding spaces | open | 11 | 38 | 'ISO 8601 date and time with seconds and a UTC offset'; the examples use '+07:00', the variants are not ruled in or out |
| sessionize | spaces around a purchase amount (' 1.20 ') accepted by the reference | open | 3 | 33 | amount is 'a string holding a non-negative decimal with at most two fraction digits'; trimming is stated for event_id and user only |
| splitr | header column names in another case ('Date', 'DATE') accepted | open | 3 | 27 | 'must contain the columns `date`, ...'; the spec does not say whether header names are case sensitive |
| splitr | spaces around a date or amount field (' 7.00 ') accepted by the reference | open | 10 | 14 | 'Names are trimmed'; the spec says nothing about trimming date or amount, whose formats are given exactly |
| bookings | trailing slash ('/rooms/') served as '/rooms' instead of 404 | open | 4 | 9 | 'A path that does not exist: 404'; the spec does not say whether '/rooms/' is the same path as '/rooms' |
| splitr | name spelling when --since/--until filters rows out | ref-deviates | 6 | 66 | 'shown with the spelling of their first appearance in the file'; the reference uses the first appearance among the considered rows |
| splitr | --help accepted (exit 0) instead of a usage error | real-bug | 3 | 18 | 'unknown option' is a usage error, exit 64; the spec defines no --help (argparse default) |
| sessionize | event type matched ignoring case or spaces ('VIEW', ' click ') | real-bug | 8 | 8 | 'type: one of view, click, purchase'; trimming is stated for event_id and user only |
| splitr | empty or comment-only file accepted as a valid empty ledger | real-bug | 6 | 6 | 'The first CSV row is the header and must contain the columns ...'; only 'a ledger with a valid header and no expenses is valid' |
| bookings | empty Idempotency-Key header treated as a key | real-bug | 4 | 4 | 'Idempotency-Key: <any non-empty string>' |
| bookings | list with from == to returns nothing instead of the bookings spanning that instant | real-bug | 3 | 3 | 'a booking is included if its end is after from and its start is before to' |

## Flaky

- `20261118-121800-bookings-tests-haiku-t1`: 2 build(s)
- `20261118-125734-bookings-tests-haiku-t2`: 2 build(s)
- `20261118-143846-bookings-tests-haiku-t5`: 1 build(s)
- `20261118-150113-bookings-tests-bb-haiku-t1`: 1 build(s)
- `20261118-153630-bookings-tests-bb-gpt-61-terra-t3`: 1 build(s)
- `20261118-154255-bookings-tests-sonnet-t1`: 1 build(s)

## Unclassified

- none
