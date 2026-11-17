# C3 report: maintainability of shipped agent code

177 agent codebases and 3 reference implementations. Per-file numbers: `results/c3/metrics.jsonl`. Tools: ruff 0.17.3, pylint 4.1.3, radon 6.0.1, duplication from `sdlc/dup.py` (50 normalized tokens, 5 lines). Rule sets: `sdlc/metrics.py`.

Caveat: the references were written by the dark factory lab session (a Claude model working against the full spec and the held-out suites), not by a human team. They are a careful baseline, not a human one.

## Source: darkfactory

### Round 1 builds by spec level, model and design (all three specs pooled)

Medians per codebase (duplication: mean, and how many files have any clone).

| spec_level | model | design | n | holdout | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % | files with clones |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| - | reference | reference | 3 | 100% | 186 | 59.1 | 21.6 | 7.9 | 27 | 3 | 21.1 | 0.0% | 0 |
| brief | claude-haiku-5-5 | ask | 9 | 96% | 263 | 41.8 | 26.7 | 5.2 | 19 | 4 | 25.0 | 0.4% | 1 |
| brief | claude-haiku-5-5 | loop | 9 | 86% | 238 | 44.2 | 23.9 | 5.5 | 21 | 2 | 26.5 | 0.0% | 0 |
| brief | claude-haiku-5-5 | oneshot | 9 | 86% | 235 | 33.5 | 24.8 | 6.0 | 25 | 2 | 25.8 | 0.0% | 0 |
| brief | claude-haiku-5-5 | pce | 9 | 86% | 306 | 50.4 | 17.9 | 4.7 | 18 | 2 | 25.7 | 0.7% | 1 |
| brief | claude-opus-5-5 | ask | 9 | 98% | 239 | 46.0 | 25.3 | 7.4 | 28 | 3 | 20.4 | 0.9% | 2 |
| brief | claude-opus-5-5 | loop | 9 | 86% | 249 | 40.3 | 24.2 | 7.0 | 26 | 2 | 17.7 | 0.0% | 0 |
| brief | claude-opus-5-5 | oneshot | 9 | 86% | 262 | 52.8 | 26.2 | 8.7 | 25 | 3 | 15.0 | 0.7% | 1 |
| brief | claude-opus-5-5 | pce | 9 | 86% | 288 | 33.1 | 23.2 | 5.7 | 21 | 3 | 18.7 | 0.7% | 1 |
| full | claude-haiku-5-5 | loop | 9 | 100% | 275 | 46.9 | 28.8 | 5.6 | 22 | 2 | 24.9 | 1.2% | 3 |
| full | claude-haiku-5-5 | oneshot | 9 | 100% | 235 | 40.2 | 25.4 | 8.3 | 25 | 2 | 26.5 | 1.4% | 2 |
| full | claude-haiku-5-5 | pce | 9 | 100% | 289 | 45.8 | 21.5 | 4.8 | 22 | 2 | 26.8 | 0.9% | 1 |
| full | claude-opus-5-5 | loop | 3 | 100% | 252 | 51.6 | 27.8 | 7.6 | 28 | 3 | 17.6 | 0.0% | 0 |
| full | claude-opus-5-5 | oneshot | 3 | 100% | 265 | 60.4 | 37.2 | 8.1 | 29 | 2 | 18.9 | 1.2% | 1 |

### Round 1 builds by spec, level and design (models pooled)

Medians per codebase (duplication: mean, and how many files have any clone).

| spec | spec_level | design | n | holdout | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % | files with clones |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bookings | - | reference | 1 | 100% | 231 | 82.2 | 21.6 | 7.0 | 21 | 6 | 13.8 | 0.0% | 0 |
| bookings | brief | ask | 6 | 99% | 352.5 | 56.1 | 25.9 | 5.0 | 22.5 | 5 | 14.1 | 2.0% | 3 |
| bookings | brief | loop | 6 | 94% | 315.5 | 62.4 | 27.1 | 4.8 | 23.5 | 5 | 14.2 | 0.0% | 0 |
| bookings | brief | oneshot | 6 | 95% | 319 | 56.4 | 28.6 | 4.8 | 25 | 5.5 | 14.1 | 0.0% | 0 |
| bookings | brief | pce | 6 | 93% | 374.5 | 99.0 | 62.2 | 3.8 | 22.5 | 5.5 | 13.1 | 1.0% | 1 |
| bookings | full | loop | 4 | 100% | 370.5 | 61.2 | 25.3 | 3.9 | 22 | 7 | 11.8 | 2.8% | 3 |
| bookings | full | oneshot | 4 | 100% | 345 | 92.1 | 36.0 | 4.4 | 23 | 6.5 | 12.1 | 4.2% | 3 |
| bookings | full | pce | 3 | 100% | 398 | 100.5 | 70.3 | 3.7 | 23 | 6 | 11.2 | 2.8% | 1 |
| sessionize | - | reference | 1 | 100% | 144 | 48.6 | 20.8 | 8.3 | 38 | 1 | 29.4 | 0.0% | 0 |
| sessionize | brief | ask | 6 | 95% | 182.5 | 36.8 | 27.1 | 6.1 | 25.5 | 2 | 33.7 | 0.0% | 0 |
| sessionize | brief | loop | 6 | 86% | 175 | 29.0 | 23.2 | 6.2 | 19.5 | 2 | 30.9 | 0.0% | 0 |
| sessionize | brief | oneshot | 6 | 84% | 179 | 41.9 | 26.3 | 7.3 | 25 | 2 | 30.9 | 0.0% | 0 |
| sessionize | brief | pce | 6 | 85% | 199 | 41.2 | 16.1 | 4.8 | 16.5 | 1 | 32.2 | 0.0% | 0 |
| sessionize | full | loop | 4 | 100% | 195.5 | 43.8 | 35.8 | 5.8 | 24 | 1 | 34.6 | 0.0% | 0 |
| sessionize | full | oneshot | 4 | 100% | 199.5 | 35.3 | 23.9 | 8.3 | 23 | 2 | 35.2 | 0.0% | 0 |
| sessionize | full | pce | 3 | 100% | 251 | 47.8 | 15.9 | 4.8 | 13 | 2 | 35.3 | 0.0% | 0 |
| splitr | - | reference | 1 | 100% | 186 | 59.1 | 43.0 | 7.9 | 27 | 3 | 21.1 | 0.0% | 0 |
| splitr | brief | ask | 6 | 98% | 242.5 | 39.9 | 26.0 | 8.2 | 24.5 | 3 | 23.1 | 0.0% | 0 |
| splitr | brief | loop | 6 | 84% | 244.5 | 38.1 | 24.1 | 7.0 | 32.5 | 2 | 24.0 | 0.0% | 0 |
| splitr | brief | oneshot | 6 | 83% | 251 | 32.9 | 22.3 | 9.1 | 40.5 | 2 | 20.6 | 1.0% | 1 |
| splitr | brief | pce | 6 | 85% | 288 | 31.1 | 17.1 | 6.1 | 22.5 | 3 | 20.4 | 1.1% | 1 |
| splitr | full | loop | 4 | 100% | 272 | 45.2 | 26.6 | 7.8 | 40 | 2.5 | 22.6 | 0.0% | 0 |
| splitr | full | oneshot | 4 | 100% | 241 | 41.3 | 31.1 | 9.4 | 54.5 | 1 | 23.1 | 0.0% | 0 |
| splitr | full | pce | 3 | 100% | 289 | 31.1 | 17.3 | 5.9 | 27 | 2 | 28.7 | 0.0% | 0 |

### Agent code against the reference, per spec

Agent median (duplication: mean) divided by the reference value; 1.0 = same as the reference. MI: higher is better. The reference holds both the v1 and the v2 (change request) behaviour behind one flag, so it is compared with round-1 and round-2 agent code alike; for round 1 it is carrying code the agent did not have to write.

| spec | round | agent n | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % |
|---|---|---|---|---|---|---|---|---|---|---|
| bookings | 1 | 35 | 1.49 | 0.74 | 1.43 | 0.65 | 1.10 | 0.83 | 0.97 | 1.6% vs 0 |
| bookings | 2 | 24 | 1.66 | 0.67 | 1.20 | 0.61 | 1.10 | 1.00 | 0.68 | 2.4% vs 0 |
| sessionize | 1 | 35 | 1.31 | 0.83 | 1.19 | 0.73 | 0.55 | 2.00 | 1.12 | 0.0% vs 0 |
| sessionize | 2 | 24 | 1.51 | 0.84 | 1.05 | 0.74 | 0.63 | 2.00 | 1.07 | 0.0% vs 0 |
| splitr | 1 | 35 | 1.35 | 0.61 | 0.56 | 0.95 | 1.11 | 0.67 | 1.16 | 0.4% vs 0 |
| splitr | 2 | 24 | 1.52 | 0.59 | 0.51 | 1.08 | 1.43 | 1.00 | 1.08 | 0.2% vs 0 |

Reference values:

| spec | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % |
|---|---|---|---|---|---|---|---|---|
| bookings | 231 | 82.2 | 21.6 | 7.0 | 21 | 6 | 13.8 | 0.0% |
| sessionize | 144 | 48.6 | 20.8 | 8.3 | 38 | 1 | 29.4 | 0.0% |
| splitr | 186 | 59.1 | 43.0 | 7.9 | 27 | 3 | 21.1 | 0.0% |

### Do the metrics track holdout pass rate?

Brief-spec round-1 builds only: on the full spec every build passed every held-out scenario, so there is no variance to explain. Spearman rho; positive means more of the metric goes with a higher pass rate. Pooled rho uses ranks within each spec so spec size does not drive it; p is a two-sided permutation test (5,000 shuffles). Uncorrected for 8 metrics.

| metric | pooled rho (within-spec ranks) | p | bookings rho | sessionize rho | splitr rho |
|---|---|---|---|---|---|
| SLOC | -0.08 | 0.516 | -0.02 | -0.19 | -0.01 |
| ruff/KLOC | -0.10 | 0.412 | -0.13 | -0.22 | -0.01 |
| pylint/KLOC | +0.01 | 0.899 | -0.30 | +0.32 | +0.02 |
| CC mean | +0.12 | 0.353 | +0.34 | -0.11 | +0.14 |
| CC max | +0.09 | 0.458 | +0.25 | +0.10 | -0.15 |
| fns CC>10 | +0.04 | 0.744 | -0.18 | +0.14 | +0.21 |
| MI | +0.04 | 0.720 | -0.09 | +0.21 | +0.02 |
| dup % | +0.17 | 0.147 | +0.52 | - | +0.13 |

n = 72 codebases.

### Static findings against functional failures, per codebase (round 1)

Counts per codebase, medians. A finding is one ruff or pylint message from the maintainability rule sets; a failure is one held-out scenario that failed. They measure different things and are not interchangeable; the table only shows which one a reviewer would see more of.

| spec level | n | ruff findings | pylint findings | failed held-out scenarios | codebases with 0 failures |
|---|---|---|---|---|---|
| full | 33 | 12.0 | 8.0 | 0.0 | 33 |
| brief | 72 | 10.0 | 6.0 | 5.0 | 5 |
| reference | 3 | 11.0 | 5.0 | 0 | 3 |

### After one change request (round 2 against its own round-1 parent)

72 pairs. Median of (round 2 minus round 1) per codebase. Survival and change scores are in the dark factory report.

| spec level | model | n | Δ SLOC | Δ ruff/KLOC | Δ pylint/KLOC | Δ CC mean | Δ CC max | Δ fns CC>10 | Δ MI | Δ dup % |
|---|---|---|---|---|---|---|---|---|---|---|
| brief | claude-haiku-5-5 | 27 | 35 | -3.8 | -1.8 | 0.3 | 0 | 0 | -3.7 | 0.0% |
| brief | claude-opus-5-5 | 18 | 34.5 | -4.2 | -2.8 | 0.7 | 5.5 | 0 | -3.9 | 0.0% |
| full | claude-haiku-5-5 | 27 | 34 | -4.0 | -2.5 | 0.5 | 1 | 0 | -2.5 | 0.0% |

### Do the metrics track how well a later change goes?

Round-1 metrics of the parent codebase against the share of change scenarios passed in round 2 (brief-spec builds only; on the full spec almost everything passes). Survival of old behaviour was 100% in every pair in the dark factory run, so it cannot be correlated.

| metric | pooled rho (within-spec ranks) | p | bookings rho | sessionize rho | splitr rho |
|---|---|---|---|---|---|
| SLOC | -0.13 | 0.380 | -0.56 | -0.18 | +0.13 |
| ruff/KLOC | +0.03 | 0.854 | -0.17 | +0.03 | +0.16 |
| pylint/KLOC | -0.20 | 0.189 | -0.66 | -0.31 | +0.13 |
| CC mean | +0.21 | 0.156 | +0.51 | +0.09 | +0.16 |
| CC max | -0.15 | 0.333 | +0.30 | -0.23 | -0.41 |
| fns CC>10 | +0.08 | 0.616 | -0.41 | +0.08 | +0.45 |
| MI | -0.13 | 0.402 | +0.00 | -0.24 | -0.16 |
| dup % | -0.30 | 0.039 | - | - | -0.11 |

n = 45 codebases.

### Most frequent findings in agent code (all rounds)

| ruff rule | count | pylint message | count |
|---|---|---|---|
| PLR2004 | 603 | raise-missing-from | 337 |
| B904 | 337 | unused-argument | 221 |
| C901 | 325 | too-many-locals | 207 |
| PLR0912 | 193 | too-many-branches | 192 |
| ARG001 | 138 | too-many-return-statements | 87 |
| TRY301 | 113 | broad-exception-caught | 85 |
| BLE001 | 110 | too-many-statements | 82 |
| PLW2901 | 93 | cell-var-from-loop | 68 |
| PLR0911 | 87 | consider-using-max-builtin | 47 |
| ARG002 | 85 | too-few-public-methods | 33 |
| PLR0915 | 81 | unbalanced-tuple-unpacking | 26 |
| B023 | 67 | attribute-defined-outside-init | 17 |

