# C3 report: maintainability of shipped agent code

304 agent codebases and 3 reference implementations. Per-file numbers: `results/c3/metrics.jsonl`. Tools: ruff 0.17.3, pylint 4.1.3, radon 6.0.1, duplication from `sdlc/dup.py` (50 normalized tokens, 5 lines). Rule sets: `sdlc/metrics.py`.

Caveat: the references were written by the dark factory lab session (a Claude model working against the full spec and the held-out suites), not by a human team. They are a careful baseline, not a human one.

## Source: c1:c1-main

### Round 1 builds by spec level, model and design (all three specs pooled)

Medians per codebase (duplication: mean, and how many files have any clone).

| spec_level | model | design | n | holdout | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % | files with clones |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| - | reference | reference | 3 | 100% | 186 | 59.1 | 21.6 | 7.9 | 27 | 3 | 21.1 | 0.0% | 0 |
| brief | claude-haiku-5-5 | kiro | 15 | 92% | 391 | 40.1 | 19.7 | 4.2 | 20 | 3 | 20.3 | 1.0% | 3 |
| brief | claude-haiku-5-5 | none | 15 | 86% | 231 | 34.6 | 24.9 | 5.8 | 20 | 3 | 27.0 | 0.4% | 1 |
| brief | claude-haiku-5-5 | plan | 15 | 92% | 285 | 42.9 | 24.6 | 4.8 | 23 | 3 | 23.8 | 1.2% | 4 |
| brief | claude-haiku-5-5 | speckit | 15 | 89% | 238 | 26.7 | 17.5 | 4.3 | 18 | 2 | 32.8 | 1.5% | 3 |
| brief | claude-haiku-5-5 | speckit-clarify | 15 | 91% | 238 | 25.9 | 17.7 | 4.2 | 20 | 2 | 32.5 | 2.1% | 4 |
| brief | claude-sonnet-5-5 | kiro | 9 | 86% | 255 | 39.3 | 22.0 | 5.3 | 18 | 3 | 15.7 | 1.2% | 3 |
| brief | claude-sonnet-5-5 | none | 9 | 84% | 189 | 47.6 | 36.2 | 11.5 | 24 | 2 | 21.3 | 0.0% | 0 |
| brief | claude-sonnet-5-5 | plan | 9 | 89% | 213 | 56.3 | 37.0 | 9.8 | 28 | 2 | 19.1 | 0.0% | 0 |
| brief | claude-sonnet-5-5 | speckit | 9 | 84% | 218 | 43.8 | 27.5 | 5.4 | 21 | 3 | 24.9 | 0.0% | 0 |

### Round 1 builds by spec, level and design (models pooled)

Medians per codebase (duplication: mean, and how many files have any clone).

| spec | spec_level | design | n | holdout | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % | files with clones |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bookings | - | reference | 1 | 100% | 231 | 82.2 | 21.6 | 7.0 | 21 | 6 | 13.8 | 0.0% | 0 |
| bookings | brief | kiro | 8 | 95% | 482.5 | 55.6 | 32.5 | 3.8 | 22 | 5.5 | 9.7 | 3.3% | 6 |
| bookings | brief | none | 8 | 92% | 323 | 58.6 | 34.5 | 4.9 | 22 | 5 | 14.3 | 0.7% | 1 |
| bookings | brief | plan | 8 | 95% | 360.5 | 75.6 | 37.9 | 5.1 | 25 | 5 | 12.3 | 2.2% | 4 |
| bookings | brief | speckit | 8 | 91% | 358.5 | 73.6 | 29.6 | 4.1 | 22.5 | 4 | 17.8 | 2.9% | 3 |
| bookings | brief | speckit-clarify | 5 | 92% | 399 | 34.1 | 19.5 | 3.8 | 20 | 6 | 18.9 | 6.4% | 4 |
| sessionize | - | reference | 1 | 100% | 144 | 48.6 | 20.8 | 8.3 | 38 | 1 | 29.4 | 0.0% | 0 |
| sessionize | brief | kiro | 8 | 89% | 303.5 | 37.5 | 17.4 | 4.4 | 15.5 | 2 | 25.5 | 0.0% | 0 |
| sessionize | brief | none | 8 | 86% | 189.5 | 25.5 | 22.4 | 6.7 | 17.5 | 2 | 30.7 | 0.0% | 0 |
| sessionize | brief | plan | 8 | 89% | 211.5 | 42.4 | 30.5 | 6.5 | 20 | 2 | 31.6 | 0.0% | 0 |
| sessionize | brief | speckit | 8 | 84% | 180.5 | 17.8 | 17.9 | 4.2 | 15.5 | 1 | 39.2 | 0.0% | 0 |
| sessionize | brief | speckit-clarify | 5 | 89% | 210 | 21.0 | 20.0 | 4.7 | 18 | 1 | 37.7 | 0.0% | 0 |
| splitr | - | reference | 1 | 100% | 186 | 59.1 | 43.0 | 7.9 | 27 | 3 | 21.1 | 0.0% | 0 |
| splitr | brief | kiro | 8 | 85% | 333.5 | 33.5 | 17.2 | 5.9 | 22 | 3 | 17.9 | 0.0% | 0 |
| splitr | brief | none | 8 | 80% | 221.5 | 35.7 | 23.7 | 7.3 | 23 | 2 | 24.8 | 0.0% | 0 |
| splitr | brief | plan | 8 | 85% | 269.5 | 38.9 | 26.6 | 7.5 | 24.5 | 2.5 | 21.2 | 0.0% | 0 |
| splitr | brief | speckit | 8 | 78% | 236 | 31.5 | 10.9 | 5.3 | 17 | 2 | 30.7 | 0.0% | 0 |
| splitr | brief | speckit-clarify | 5 | 86% | 238 | 16.4 | 12.3 | 4.4 | 22 | 2 | 32.5 | 0.0% | 0 |

### Agent code against the reference, per spec

Agent median (duplication: mean) divided by the reference value; 1.0 = same as the reference. MI: higher is better. The reference holds both the v1 and the v2 (change request) behaviour behind one flag, so it is compared with round-1 and round-2 agent code alike; for round 1 it is carrying code the agent did not have to write.

| spec | round | agent n | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % |
|---|---|---|---|---|---|---|---|---|---|---|
| bookings | 1 | 37 | 1.50 | 0.71 | 1.54 | 0.61 | 1.10 | 0.83 | 0.97 | 2.8% vs 0 |
| bookings | 2 | 0 | nan | nan | nan | nan | nan | nan | nan | - vs 0 |
| sessionize | 1 | 37 | 1.41 | 0.57 | 0.96 | 0.57 | 0.45 | 1.00 | 1.05 | 0.0% vs 0 |
| sessionize | 2 | 0 | nan | nan | nan | nan | nan | nan | nan | - vs 0 |
| splitr | 1 | 37 | 1.28 | 0.58 | 0.42 | 0.79 | 0.81 | 0.67 | 1.13 | 0.0% vs 0 |
| splitr | 2 | 0 | nan | nan | nan | nan | nan | nan | nan | - vs 0 |

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
| SLOC | +0.26 | 0.006 | -0.18 | +0.46 | +0.51 |
| ruff/KLOC | +0.08 | 0.418 | +0.25 | +0.10 | -0.11 |
| pylint/KLOC | +0.01 | 0.903 | +0.22 | -0.07 | -0.11 |
| CC mean | -0.11 | 0.257 | +0.35 | -0.32 | -0.35 |
| CC max | +0.06 | 0.527 | +0.46 | -0.21 | -0.07 |
| fns CC>10 | +0.08 | 0.396 | +0.06 | +0.14 | +0.08 |
| MI | -0.14 | 0.153 | -0.60 | +0.12 | +0.07 |
| dup % | +0.05 | 0.589 | -0.01 | - | - |

n = 111 codebases.

### Static findings against functional failures, per codebase (round 1)

Counts per codebase, medians. A finding is one ruff or pylint message from the maintainability rule sets; a failure is one held-out scenario that failed. They measure different things and are not interchangeable; the table only shows which one a reviewer would see more of.

| spec level | n | ruff findings | pylint findings | failed held-out scenarios | codebases with 0 failures |
|---|---|---|---|---|---|
| brief | 111 | 10.0 | 6.0 | 6.0 | 0 |
| reference | 3 | 11.0 | 5.0 | 0 | 3 |

### After one change request (round 2 against its own round-1 parent)

0 pairs. Median of (round 2 minus round 1) per codebase. Survival and change scores are in the dark factory report.

| spec level | model | n | Δ SLOC | Δ ruff/KLOC | Δ pylint/KLOC | Δ CC mean | Δ CC max | Δ fns CC>10 | Δ MI | Δ dup % |
|---|---|---|---|---|---|---|---|---|---|---|

### Most frequent findings in agent code (all rounds)

| ruff rule | count | pylint message | count |
|---|---|---|---|
| PLR2004 | 343 | raise-missing-from | 204 |
| B904 | 204 | unused-argument | 130 |
| C901 | 161 | too-many-locals | 105 |
| ARG001 | 114 | too-many-branches | 97 |
| PLR0912 | 97 | too-many-return-statements | 62 |
| BLE001 | 83 | broad-exception-caught | 62 |
| PLR0911 | 62 | too-few-public-methods | 34 |
| PLW2901 | 47 | cell-var-from-loop | 28 |
| TRY301 | 45 | too-many-statements | 21 |
| B905 | 33 | consider-using-max-builtin | 12 |
| B023 | 29 | too-many-arguments | 12 |
| SIM105 | 25 | too-many-positional-arguments | 12 |

## Source: c1:c1-pilot2

### Round 1 builds by spec level, model and design (all three specs pooled)

Medians per codebase (duplication: mean, and how many files have any clone).

| spec_level | model | design | n | holdout | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % | files with clones |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| - | reference | reference | 3 | 100% | 186 | 59.1 | 21.6 | 7.9 | 27 | 3 | 21.1 | 0.0% | 0 |
| brief | claude-haiku-5-5 | kiro | 3 | 92% | 395 | 43.0 | 30.4 | 4.1 | 24 | 4 | 16.5 | 0.0% | 0 |
| brief | claude-haiku-5-5 | none | 3 | 86% | 248 | 37.4 | 23.0 | 4.4 | 22 | 4 | 22.9 | 0.0% | 0 |
| brief | claude-haiku-5-5 | plan | 3 | 92% | 280 | 51.5 | 30.9 | 4.7 | 20 | 4 | 18.8 | 0.0% | 0 |
| brief | claude-haiku-5-5 | speckit | 3 | 84% | 272 | 22.9 | 15.3 | 3.8 | 19 | 4 | 25.4 | 0.0% | 0 |
| brief | claude-sonnet-5-5 | kiro | 1 | 82% | 263 | 45.6 | 19.0 | 6.8 | 19 | 4 | 16.0 | 0.0% | 0 |
| brief | claude-sonnet-5-5 | none | 1 | 80% | 194 | 56.7 | 41.2 | 11.6 | 51 | 2 | 24.4 | 0.0% | 0 |
| brief | claude-sonnet-5-5 | plan | 1 | 77% | 237 | 50.6 | 29.5 | 6.7 | 20 | 4 | 12.4 | 0.0% | 0 |
| brief | claude-sonnet-5-5 | speckit | 1 | 73% | 220 | 22.7 | 13.6 | 8.5 | 30 | 2 | 16.2 | 0.0% | 0 |

### Round 1 builds by spec, level and design (models pooled)

Medians per codebase (duplication: mean, and how many files have any clone).

| spec | spec_level | design | n | holdout | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % | files with clones |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bookings | - | reference | 1 | 100% | 231 | 82.2 | 21.6 | 7.0 | 21 | 6 | 13.8 | 0.0% | 0 |
| bookings | brief | kiro | 1 | 95% | 483 | 72.5 | 35.2 | 4.1 | 28 | 8 | 0.4 | 0.0% | 0 |
| bookings | brief | none | 1 | 93% | 348 | 37.4 | 23.0 | 3.4 | 22 | 5 | 15.5 | 0.0% | 0 |
| bookings | brief | plan | 1 | 95% | 388 | 72.2 | 38.7 | 4.1 | 22 | 7 | 11.1 | 0.0% | 0 |
| bookings | brief | speckit | 1 | 91% | 347 | 54.8 | 28.8 | 3.8 | 20 | 7 | 18.9 | 0.0% | 0 |
| sessionize | - | reference | 1 | 100% | 144 | 48.6 | 20.8 | 8.3 | 38 | 1 | 29.4 | 0.0% | 0 |
| sessionize | brief | kiro | 1 | 92% | 362 | 30.4 | 11.1 | 3.4 | 21 | 1 | 27.0 | 0.0% | 0 |
| sessionize | brief | none | 1 | 86% | 186 | 21.5 | 16.1 | 4.4 | 9 | 0 | 36.3 | 0.0% | 0 |
| sessionize | brief | plan | 1 | 92% | 194 | 51.5 | 30.9 | 4.7 | 17 | 1 | 35.6 | 0.0% | 0 |
| sessionize | brief | speckit | 1 | 84% | 262 | 22.9 | 15.3 | 2.9 | 16 | 1 | 39.4 | 0.0% | 0 |
| splitr | - | reference | 1 | 100% | 186 | 59.1 | 43.0 | 7.9 | 27 | 3 | 21.1 | 0.0% | 0 |
| splitr | brief | kiro | 2 | 84% | 329 | 44.3 | 24.7 | 5.9 | 21.5 | 4 | 16.3 | 0.0% | 0 |
| splitr | brief | none | 2 | 82% | 221 | 50.5 | 40.8 | 9.4 | 38.5 | 3 | 23.6 | 0.0% | 0 |
| splitr | brief | plan | 2 | 82% | 258.5 | 50.3 | 27.3 | 6.4 | 20 | 4 | 15.6 | 0.0% | 0 |
| splitr | brief | speckit | 2 | 78% | 246 | 20.6 | 10.5 | 7.1 | 24.5 | 3 | 20.8 | 0.0% | 0 |

### Agent code against the reference, per spec

Agent median (duplication: mean) divided by the reference value; 1.0 = same as the reference. MI: higher is better. The reference holds both the v1 and the v2 (change request) behaviour behind one flag, so it is compared with round-1 and round-2 agent code alike; for round 1 it is carrying code the agent did not have to write.

| spec | round | agent n | SLOC | ruff/KLOC | pylint/KLOC | CC mean | CC max | fns CC>10 | MI | dup % |
|---|---|---|---|---|---|---|---|---|---|---|
| bookings | 1 | 4 | 1.59 | 0.77 | 1.48 | 0.56 | 1.05 | 1.17 | 0.96 | 0.0% vs 0 |
| bookings | 2 | 0 | nan | nan | nan | nan | nan | nan | nan | - vs 0 |
| sessionize | 1 | 4 | 1.58 | 0.55 | 0.75 | 0.47 | 0.43 | 1.00 | 1.22 | 0.0% vs 0 |
| sessionize | 2 | 0 | nan | nan | nan | nan | nan | nan | nan | - vs 0 |
| splitr | 1 | 8 | 1.37 | 0.76 | 0.63 | 0.85 | 0.81 | 1.33 | 0.84 | 0.0% vs 0 |
| splitr | 2 | 0 | nan | nan | nan | nan | nan | nan | nan | - vs 0 |

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
| SLOC | +0.74 | 0.002 | +0.95 | +0.21 | +0.88 |
| ruff/KLOC | +0.30 | 0.263 | +0.74 | +0.74 | -0.17 |
| pylint/KLOC | +0.28 | 0.294 | +0.74 | +0.11 | +0.12 |
| CC mean | -0.01 | 0.955 | +0.74 | +0.63 | -0.70 |
| CC max | +0.30 | 0.264 | +0.83 | +0.74 | -0.32 |
| fns CC>10 | +0.41 | 0.118 | +0.50 | +0.27 | +0.64 |
| MI | -0.34 | 0.206 | -0.95 | -0.95 | +0.40 |
| dup % | +nan | nan | - | - | - |

n = 16 codebases.

### Static findings against functional failures, per codebase (round 1)

Counts per codebase, medians. A finding is one ruff or pylint message from the maintainability rule sets; a failure is one held-out scenario that failed. They measure different things and are not interchangeable; the table only shows which one a reviewer would see more of.

| spec level | n | ruff findings | pylint findings | failed held-out scenarios | codebases with 0 failures |
|---|---|---|---|---|---|
| brief | 16 | 11.5 | 7.0 | 6.0 | 0 |
| reference | 3 | 11.0 | 5.0 | 0 | 3 |

### After one change request (round 2 against its own round-1 parent)

0 pairs. Median of (round 2 minus round 1) per codebase. Survival and change scores are in the dark factory report.

| spec level | model | n | Δ SLOC | Δ ruff/KLOC | Δ pylint/KLOC | Δ CC mean | Δ CC max | Δ fns CC>10 | Δ MI | Δ dup % |
|---|---|---|---|---|---|---|---|---|---|---|

### Most frequent findings in agent code (all rounds)

| ruff rule | count | pylint message | count |
|---|---|---|---|
| PLR2004 | 60 | raise-missing-from | 28 |
| B904 | 28 | too-many-branches | 20 |
| C901 | 24 | too-many-locals | 15 |
| PLR0912 | 21 | too-many-return-statements | 11 |
| PLR0911 | 11 | broad-exception-caught | 8 |
| PLW2901 | 9 | too-few-public-methods | 5 |
| BLE001 | 9 | too-many-statements | 4 |
| B905 | 7 | cell-var-from-loop | 3 |
| TRY301 | 7 | unused-import | 3 |
| PLR0915 | 4 | attribute-defined-outside-init | 3 |
| B023 | 3 | unused-variable | 2 |
| F401 | 3 | global-statement | 2 |

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

