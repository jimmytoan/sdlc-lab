# C4 report for tag `c4-pilot`

15 test-writing runs (6 errored), 9 agent suites scored. Writer spend $0.57 at list price (Gemini ran on the free tier). Mutants: every mutmut 3.8.1 mutant of the v1 reference (`results/c4/mutants.json`). Records: `results/c4/runs.jsonl`, `results/c4/scores.jsonl`; agent suites: `suites/c4/`.

Caveat: the public and held-out suites were written by the dark factory lab session (a Claude model, working from the full spec, iterating against the reference), not by human developers. 'Killable' = killed by at least one suite in this table (baselines and every agent suite of every tag scored so far); the rest are survivors of every suite, which includes equivalent mutants and mutants in v2-only code (the reference carries the change request behind a flag).

## bookings

916 mutants, 718 killable.

| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |
|---|---|---|---|---|---|---|---|
| public | - | 10/10 | 416/916 | 45.4% | 57.9% | 0.622s | - |
| holdout | - | 91/91 | 689/916 | 75.2% | 96.0% | 5.418s | - |
| `20261117-213026-bookings-tests-haiku-t1` | claude-haiku-5-5 | 258/258 | 715/916 | 78.1% | 99.6% | 15.829s | $0.032 |
| `20261118-004122-bookings-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 19/19 | 652/916 | 71.2% | 90.8% | 1.187s | $0.044 |
| `20261117-215210-bookings-tests-gpt-61-terra-t1` | gpt-6.1-terra | 12/12 | 707/916 | 77.2% | 98.5% | 0.809s | $0.079 |

Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):

- `20261117-213026-bookings-tests-haiku-t1`: agent only 28, held-out only 2, both 687
- `20261118-004122-bookings-tests-gemini-35-flash-lite-t1`: agent only 7, held-out only 44, both 645
- `20261117-215210-bookings-tests-gpt-61-terra-t1`: agent only 21, held-out only 3, both 686

## sessionize

618 mutants, 521 killable.

| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |
|---|---|---|---|---|---|---|---|
| public | - | 8/8 | 481/618 | 77.8% | 92.3% | 0.185s | - |
| holdout | - | 37/37 | 507/618 | 82.0% | 97.3% | 0.751s | - |
| `20261117-214312-sessionize-tests-haiku-t1` | claude-haiku-5-5 | 202/202 | 514/618 | 83.2% | 98.7% | 4.135s | $0.030 |
| `20261118-010748-sessionize-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 13/13 | 489/618 | 79.1% | 93.9% | 0.328s | $0.092 |
| `20261117-215342-sessionize-tests-gpt-61-terra-t1` | gpt-6.1-terra | 10/10 | 520/618 | 84.1% | 99.8% | 0.232s | $0.061 |

Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):

- `20261117-214312-sessionize-tests-haiku-t1`: agent only 10, held-out only 3, both 504
- `20261118-010748-sessionize-tests-gemini-35-flash-lite-t1`: agent only 4, held-out only 22, both 485
- `20261117-215342-sessionize-tests-gpt-61-terra-t1`: agent only 14, held-out only 1, both 506

## splitr

439 mutants, 348 killable.

| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |
|---|---|---|---|---|---|---|---|
| public | - | 8/8 | 271/439 | 61.7% | 77.9% | 0.121s | - |
| holdout | - | 44/44 | 329/439 | 74.9% | 94.5% | 0.818s | - |
| `20261117-211929-splitr-tests-haiku-t1` | claude-haiku-5-5 | 175/175 | 338/439 | 77.0% | 97.1% | 3.399s | $0.045 |
| `20261118-003737-splitr-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 17/17 | 314/439 | 71.5% | 90.2% | 0.643s | $0.101 |
| `20261117-215035-splitr-tests-gpt-61-terra-t1` | gpt-6.1-terra | 92/92 | 348/439 | 79.3% | 100.0% | 1.812s | $0.066 |

Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):

- `20261117-211929-splitr-tests-haiku-t1`: agent only 10, held-out only 1, both 328
- `20261118-003737-splitr-tests-gemini-35-flash-lite-t1`: agent only 6, held-out only 21, both 308
- `20261117-215035-splitr-tests-gpt-61-terra-t1`: agent only 19, held-out only 0, both 329

## By model (specs pooled)

| model | suites | valid tests (median) | invalid tests (total) | mutation score (mean) | writer cost (mean) |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 3 | 202 | 0 | 79.4% | $0.036 |
| gemini-3.5-flash-lite | 3 | 17 | 0 | 73.9% | $0.079 |
| gpt-6.1-terra | 3 | 12 | 0 | 80.2% | $0.069 |
| public suite | 3 | 8 | 0 | 61.7% | - |
| holdout suite | 3 | 44 | 0 | 77.4% | - |

## Real faults: the suites against the dark factory's shipped codebases

Faulty = brief-ticket builds that fail at least one held-out scenario; holdout-clean = full-spec builds that pass all of them. A suite catches a faulty build if it fails on it. A failure on a holdout-clean build is either a false alarm (the test pins behaviour the spec leaves open) or a real bug the held-out suite misses; these need reading by hand before either word is used (pilot example: one is each). The held-out suite defines 'faulty', so it catches all of them by construction and is shown only as a check; the public examples pass on every build by construction.

| spec | suite | model | faulty caught | fails on holdout-clean builds |
|---|---|---|---|---|
| bookings | `20261117-213026-bookings-tests-haiku-t1` | claude-haiku-5-5 | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261117-215210-bookings-tests-gpt-61-terra-t1` | gpt-6.1-terra | 21/21 (100%) | 2/11 (18%) |
| bookings | `20261118-004122-bookings-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 21/21 (100%) | 0/11 (0%) |
| bookings | holdout | - | 21/21 (100%) | 0/11 (0%) |
| bookings | public | - | 0/21 (0%) | 0/11 (0%) |
| sessionize | `20261117-214312-sessionize-tests-haiku-t1` | claude-haiku-5-5 | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261117-215342-sessionize-tests-gpt-61-terra-t1` | gpt-6.1-terra | 24/24 (100%) | 6/11 (55%) |
| sessionize | `20261118-010748-sessionize-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 22/24 (92%) | 0/11 (0%) |
| sessionize | holdout | - | 24/24 (100%) | 0/11 (0%) |
| sessionize | public | - | 0/24 (0%) | 0/11 (0%) |
| splitr | `20261117-211929-splitr-tests-haiku-t1` | claude-haiku-5-5 | 21/22 (95%) | 0/11 (0%) |
| splitr | `20261117-215035-splitr-tests-gpt-61-terra-t1` | gpt-6.1-terra | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-003737-splitr-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 22/22 (100%) | 3/11 (27%) |
| splitr | holdout | - | 22/22 (100%) | 0/11 (0%) |
| splitr | public | - | 0/22 (0%) | 0/11 (0%) |

## Tampering and errors

- `20261117-215500-splitr-tests-gemini-35-flash-lite-t1` ERROR: BadRequestError: Error code: 400
- `20261117-215514-bookings-tests-gemini-35-flash-lite-t1` ERROR: BadRequestError: Error code: 400
- `20261117-215528-sessionize-tests-gemini-35-flash-lite-t1` ERROR: BadRequestError: Error code: 400
- `20261117-234133-splitr-tests-gemini-35-flash-lite-t1` ERROR: InternalServerError: Error code: 503
- `20261117-234402-bookings-tests-gemini-35-flash-lite-t1` ERROR: InternalServerError: Error code: 503
- `20261117-234733-sessionize-tests-gemini-35-flash-lite-t1` ERROR: InternalServerError: Error code: 503
