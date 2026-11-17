# PLAN: stage 1 results and proposed full run

Status on 2026-11-18: stage 1 done. C3 has run in full. C1 and C4 have harnesses, a passing self-test (`uv run python -m sdlc.selftest`, 41 checks, no model calls) and pilots. Pilot spend: **$3.14 of the $5 hard cap** (list price, Gemini counted at list price though it ran on the free tier). Nothing is pushed; commits are local only.

Sources for every published claim, checked against the originals: `SOURCES.md`. Several claims as worded in the series handoff are wrong (see "Research claims" below).

## C3: passes the tests, hard to maintain? (full run, no model cost)

Report: `results/c3-report.md`; per-file metrics: `results/c3/metrics.jsonl`. Corpus: 177 shipped codebases from the dark factory main run (105 builds, 72 change rounds; every product is one Python file) plus the 3 reference implementations. Tools: ruff 0.17.3 and pylint 4.1.3 with maintainability rule sets only (no layout or naming rules), radon 6.0.1 for complexity and maintainability index, and a token-clone detector of my own (`sdlc/dup.py`, 50 tokens, 5 lines; jscpd was not approved).

1. **Agent code is longer, not smellier.** Against the reference, median agent code is 1.31 to 1.66 times as long, per spec and round, but has fewer ruff findings per 1,000 lines (0.59 to 0.84 of the reference) and similar or lower mean complexity (0.61 to 1.08). In absolute counts a brief-ticket build carries a median of 10 ruff and 6 pylint findings, the reference 11 and 5. The reference also holds the change-request code behind a flag, so the length gap for round 1 is if anything understated.
2. **Duplication is rare.** 13 of 105 round-1 builds have any clone of 50 tokens or more; 0.6% of code lines are duplicated on average, at most 4.2% in any spec, level and design group.
3. **No metric tracks correctness.** On the 72 brief-ticket builds (the only ones whose held-out pass rate varies), no metric correlates with the pass rate: |rho| at most 0.17, permutation p at least 0.15, ranks taken within each spec.
4. **No metric predicts how a later change goes** either, beyond one uncorrected p = 0.039 (duplication, rho -0.30) among 8 tests, which does not survive correction.
5. **After one change request** code grows by a median 34 to 35 lines, findings per 1,000 lines fall slightly (the added code is cleaner than the average) and the maintainability index drops 2.5 to 3.9 points.
6. With the C1 pilot added (16 codebases, `c1:c1-pilot2` in the report): Kiro-style builds are the longest (Haiku median 415 SLOC against 261 for no spec).

What this does and does not say about the papers: arXiv 2601.20109 found that 89% of SonarQube issues in merged agent bug fixes are code smells. Here too, static findings far outnumber functional failures, but the reference written with care has as many. The finding is "agents write more code, of about the same lint quality", not "agent code is less maintainable". Limits: one-file greenfield products of 150 to 550 lines, lint proxies for maintainability, a reference written by a model (below), no human-written baseline.

## Important caveat for C3 and C4: the baselines are not human

The dark factory lab's reference implementations and its public and held-out suites were written in a Claude Code session (every commit there is co-authored by Claude Fable 5.2). So C3 compares agent code with careful model-written code, and C4 compares quickly written agent tests with deliberately written model tests that serve as the lab's oracle. Neither is a human baseline. The posts must say so. A human baseline would need a different corpus (for example a small open-source project with its own human-written tests); I have not added one.

## C1 pilot: does a spec make the code better?

Report: `results/c1-c1-pilot2-report.md`; records: `results/c1/runs.jsonl` (tag `c1-pilot2`).

Setup: the brief ticket of each dark factory product, four conditions, same model, same tools and sandbox, same coding-phase prompt (the dark factory's), graded on the same held-out suites with the stated/implied/arbitrary split.

- `none`: the coder builds from the ticket.
- `plan`: an agent writes a free-form `plan.md` first, then a fresh coder builds from it. The handoff says "hand-written plan.md"; a human-written plan would carry the writer's knowledge of the full spec into the run, so I read it as "a plain plan, no tooling". Say if you meant something else.
- `speckit`: GitHub Spec Kit v1.2.6, its own command prompts, templates and scripts (`vendor/speckit`, MIT), in order `/speckit.specify`, `/speckit.plan`, `/speckit.tasks`, `/speckit.implement`. The specify CLI was not installed; `sdlc/speckit.py` renders the commands the way the CLI does. No constitution; `clarify`, `analyze` and `checklist` not run (no human to answer).
- `kiro`: Kiro cannot run headless without a paid Kiro API key, runs only Kiro's own models, and its `/spec` command is interactive only. So this is a **Kiro-style workflow, not Kiro**: requirements (user stories, EARS criteria), design, tasks, implement, with prompts I wrote from Kiro's published document structure (`sdlc/c1.py`).

Every phase starts with a fresh context; the run is unattended (questions and approval gates take the default).

**First pilot, discarded (tag `c1-pilot`, kept on disk, excluded from analysis):** I first gave every phase the dark factory's system prompt ("whatever is in the workspace is shipped"). In 2 of 8 Spec Kit runs the agent skipped the workflow: it wrote the product during `/speckit.specify` and said the spec scaffolding "doesn't change what ships". The fix: a neutral system prompt for document phases, the factory prompt for the coding phase in all four conditions, and a protocol check that records whether each phase left its document and whether product code appeared early. In the second pilot all 16 runs followed the protocol. This is worth a line in the post: whether a spec workflow is followed at all depends on framing.

**Second pilot (16 runs, $1.46):** one trial per cell, so only the direction is usable.

| model | condition | holdout | arbitrary class | spec docs | cost/run | time/run |
|---|---|---|---|---|---|---|
| Haiku 5.5 | none | 154/172 (90%) | 37/54 (69%) | 0 words | $0.006 | 37 s |
| Haiku 5.5 | plan | 158/172 (92%) | 40/54 (74%) | 1 file, 1,733 words | $0.016 | 101 s |
| Haiku 5.5 | speckit | 151/172 (88%) | 35/54 (65%) | 8 files, 8,599 words | $0.056 | 263 s |
| Haiku 5.5 | kiro-style | 158/172 (92%) | 41/54 (76%) | 3 files, 10,436 words | $0.104 | 423 s |
| Sonnet 5.5, splitr only | none / plan / speckit / kiro-style | 80% / 77% / 73% / 82% | | 0 / 688 / 4,155 / 4,838 words | $0.07 / $0.11 / $0.39 / $0.34 | |

- Stated behaviour stays near 100% in every condition; the misses stay in what the ticket leaves open (arbitrary class), as in the dark factory lab.
- Spec tooling produced 5 to 6 times as many words as the free-form plan, at 5 to 17 times the cost of no spec. Per spec, the spread between conditions is 2 to 4 held-out scenarios, inside run-to-run noise (in the dark factory main run, holdout rates across the 9 runs of one cell spread over 4 to 17 points).
- Times are indicative only: other lab sessions shared the machine and API limits.

## C4 pilot: are AI-written tests any good?

Report: `results/c4-c4-pilot-report.md`; records: `results/c4/runs.jsonl`, `results/c4/scores.jsonl`, `results/c4/realfaults.jsonl`; the suites: `suites/c4/`.

Setup: the writer sees the full spec, the reference code and the public examples, and writes `tests/test_agent.py`; it can run anything in the sandbox. Mutants: every mutant mutmut 3.8.1 generates for each reference (439, 916, 618). mutmut's own runner cannot attribute these black-box suites (they run the product as a subprocess; mutmut reports no test covers any mutant), so `sdlc/mutate.py` takes each mutant via `mutmut apply` and runs the suite against it, stopping at the first failure. Tests that fail on the reference are counted invalid and deselected.

| suite | mutation score, mean of 3 specs | tests (median) | writer cost |
|---|---|---|---|
| public examples | 61.7% | 8 | - |
| held-out suite | 77.4% | 44 | - |
| Claude Haiku 5.5 | 79.4% | 202 | $0.036 |
| GPT-6.1 Terra | 80.2% | 12 (large tests) | $0.069 |
| Gemini 3.5 Flash-Lite | 73.9% | 17 | free tier ($0.079 at list) |

- Haiku and GPT-6.1 Terra suites match or beat the held-out suites on mutants; Gemini's are slightly below. That agrees with arXiv 2608.15188 ("no worse than"). No invalid tests in 9 suites.
- **Mutation score rewards pinning the current code**, and these writers saw the code. So I added a check with real faults and no model cost: each suite runs against the dark factory's shipped codebases. Agent suites caught 21 to 24 of the 21 to 24 faulty brief-ticket builds per spec (one miss for Haiku on splitr, two for Gemini on sessionize). They also failed on full-spec builds that pass every held-out scenario: Haiku 1 of 33, Gemini 3 of 33, GPT-6.1 Terra 19 of 33 (all 11 on splitr). I read one GPT case: one failing test pins a choice the spec leaves open (which spelling to show when a date filter excludes a name's first row), another catches what looks like a real bug the held-out suite misses (a missing `date` column is not reported when date filters are given). So those failures are a mix of false alarms and real findings, and each needs classifying by hand.
- Gemini: the first attempt hit a thought-signature requirement (fixed in `sdlc/llm.py`), the second hit sustained HTTP 503 overload (fixed with a longer back-off); the third worked. Its runs took 4 to 26 minutes.

## Model choice

- **C1: Claude Haiku 5.5 as the main model, Sonnet 5.5 as a check.** The dark factory lab showed the unstated-intent gap does not move with model tier (Haiku and Opus within 2 points), and the Sonnet pilot on splitr was no better than Haiku. Haiku is 5 to 10 times cheaper per run, which buys 5 trials instead of 3 for the comparison that matters. Sonnet at 3 trials covers the obvious objection that Spec Kit is used with frontier models.
- **C4: three families**: Claude Haiku 5.5 (Anthropic), GPT-6.1 Terra (OpenAI), Gemini 3.5 Flash-Lite (Google), plus Claude Sonnet 5.5 because arXiv 2608.15188 studied Sonnet-written tests.

## Proposed full run (option M, recommended)

| block | runs | est. cost | est. time |
|---|---|---|---|
| C1, Haiku, 4 conditions × 3 specs × 5 trials | 60 | $2.70 | 3.5 h |
| C1, Sonnet, 4 conditions × 3 specs × 3 trials | 36 | $9 to $12 | 1.5 h |
| C1, fifth condition `speckit+clarify`: Spec Kit with `/speckit.clarify` answered by an owner model holding the full spec (as the dark factory's `ask` design), Haiku, 3 specs × 5 trials | 15 | $1.20 | 1.5 h |
| C4 writers: Haiku, Terra, Gemini × 3 specs × 5 trials | 45 | $1.60 paid + $1.20 Gemini at list | 5 h (Gemini rate limits) |
| C4 writers: Sonnet × 3 specs × 3 trials | 9 | $2.50 | 0.5 h |
| C4 black-box variant (writer sees spec and examples, not the code): Haiku and Terra × 3 specs × 3 trials | 18 | $1 | 0.5 h |
| C4 mutation and real-fault scoring, C3 re-run on C1 output | 0 | $0 | 3 h |
| **Total** | **183** | **about $20 to $23** | **about 15 h sequential** |

Proposed hard cap: **$30**, enforced in code over the main-run tags (`--cap 30 --cap-tags c1-main,c4-main`). Every block is resumable with `--resume`; errored runs are retried, not counted.

Also in stage 2, by hand: classify every agent-suite failure on a holdout-clean build as false alarm or real bug (reading the test against the spec), and re-check each failure story in the transcripts before it goes into a draft.

- **Option S** (about $9, cap $12): drop the Sonnet C1 block and the Sonnet C4 writers. Loses the frontier-model check on Spec Kit.
- **Option L** (about $35, cap $45): option M plus Sonnet at 5 trials in C1 and Opus 5.5 as a fourth C4 writer.

New code needed before the main run, all covered by the self-test: the `speckit+clarify` condition (an interactive loop between `/speckit.clarify` and the owner model) and the black-box C4 variant. About an hour of work, no model cost.

## Changed or cut, and why

- Kiro: a Kiro-style workflow with my prompts, not Kiro (headless needs a paid key; own models only; `/spec` is interactive).
- Spec Kit: rendered without the CLI; constitution, clarify, analyze and checklist not run in the four main conditions (clarify comes back as the fifth condition).
- `plan` is agent-written, not human-written (see above).
- mutmut generates the mutants; my runner executes them (mutmut's runner cannot see subprocess tests).
- jscpd not used (not approved); duplication comes from `sdlc/dup.py`, validated in the self-test.
- Pilot size: 47 model runs instead of 20 to 30, because the prompt bug and the two Gemini failures forced re-runs. Spend stayed within the cap.
- Wall time is not a headline number: other lab sessions ran at the same time.

## Research claims (details in `SOURCES.md`)

- 2601.20109 and 2609.26847: accurate as worded (smells are SonarQube smells; 1.62 is an odds ratio, 95% CI 1.10 to 2.39).
- "9.9% of generated methods deleted in review": not in 2605.06464, which cites it. It comes from arXiv 2602.17091 and covers only PRs that needed revisions; across all merged PRs it is 2.6%.
- Brenn Hill (SSRN / ESEM 2026): the "+5.0" and "p = 0.056" figures are not in the paper, and it does not study spec-driven tools: 97.5% of its "spec'd" PRs are links to an issue. Its within-author effects are rework +1.2 points and defects +1.4 points, which vanish after matching on complexity.
- Uvik: Kiro 41 and Spec Kit 40 of 50 are right; "90% against 60%" is 10 tickets per arm, with a stated noise band of ±10 points.
- 2608.15188: a "no worse than" result on one private Sonnet-written project, with Django and Pandas as the human baselines.
- Böckeler: accurate.

## C7 is not run

The verification-tax idea needs timing data from many developers working with and without AI; one person timing themselves is an anecdote, not a sample, and METR's own follow-up failed when developers refused to work without AI.
