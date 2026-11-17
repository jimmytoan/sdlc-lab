# Source verification: AI coding agents and spec-driven development claims

Checked 2026-11-17 against the original pages (arXiv abstract and HTML full text, the Dagstuhl/LIPIcs PDF, the Zenodo copy of the SSRN working paper, the Uvik page and martinfowler.com). Quotes are kept under 15 words. When a page could not be fetched, this file says so.

## Summary

| # | Claim (as received) | Status | Correct wording |
|---|---|---|---|
| 1 | arXiv 2601.20109: across 1,210 merged agent bug-fix PRs, maintainability problems outnumber functional bugs | **Accurate**, with a caveat about definitions | In 1,210 merged agent bug-fix PRs (Python, AIDev), SonarQube flagged 1,190 newly introduced issues: 1,059 code smells (89%), 83 security hotspots (7%), 48 bugs (4%) and 0 vulnerabilities. These are static-analysis findings, not confirmed defects, and there is no human baseline. |
| 2 | arXiv 2609.26847: merged agent PRs attract follow-up fixes at 1.62x the odds of human ones | **Accurate** | Merged agent PRs attract *verified* fixes (a later fix PR within 30 days that edits the same files, confirmed as a direct fix) at 1.62x the odds of human PRs from the same repos (Mantel-Haenszel OR, 95% CI 1.10-2.39, p = 0.015; raw 3.68% vs 2.34%). |
| 3 | arXiv 2605.06464: 9.9% of generated methods are deleted in review | **Wrong as attributed, and the number is misleading** | 2605.06464 only *cites* the figure. It comes from Watanabe et al., arXiv 2602.17091 (MSR '26), and applies only to PRs that needed revisions (397 PRs, 3,257 methods). Across all 1,664 merged Python agent PRs, 2.6% of added methods (323 of 12,343) were deleted before merge. |
| 4 | arXiv 2608.15188: Claude-written Python tests are not weaker than human-written ones under mutation testing | **Accurate** (a non-inferiority result, not a superiority one) | Under three fault-injection protocols plus a rubric, tests written by Claude Sonnet 4.6 for one private project were non-inferior to human tests from Django and Pandas, within one-sided 95% bootstrap bounds of a few percentage points. The confidence intervals overlap, so the data cannot reject "no difference". |
| 5 | SSRN preprint by Brenn Hill: 100,247 PRs, 119 repos; SDD linked to higher rework (+5.0 pts) and borderline higher defects (+1.4 pts, p = 0.056) | **Partly accurate. Numbers wrong** | Of 100,247 collected PRs, 88,052 were analysed (after 12,195 bot PRs were removed) across 119 repos. Within-author results: rework +1.2 pp (p = 0.001) and defects +1.4 pp (p = 0.003). After matching on complexity: rework +0.5 pp (p = 0.146) and defects -0.6 pp (p = 0.133). The treatment is "specification artifacts", 97.5% of which are issue or ticket links, not SDD tool use. |
| 6 | Uvik: 50 tickets; Kiro merged 41, Spec Kit 40; 90% with specs vs 60% without, feature tickets only | **Partly accurate** | The merge counts are correct. The 90% vs 60% figure is the *best* spec arm against the control on feature **and data-pipeline** tickets (10 tickets each), not on feature tickets only. Overall, the control merged 36 of 50 (72%). The benchmark is a vendor's single run with a stated ±10 pp noise band. |
| 7 | Böckeler (Thoughtworks) on Kiro, Spec Kit and Tessl: review overhead and a false sense of control | **Accurate** | martinfowler.com, 15 Oct 2025. She found spec-kit's markdown "very verbose and tedious to review". Under the heading "False sense of control?" she notes that agents often did not follow all the spec's instructions. |

**Surprises**

- Claim 3 is attributed to the wrong paper. The 9.9% figure is also conditional on PRs that needed revisions; the unconditional rate is 2.6%.
- Claim 5: neither version of Hill's paper that I read contains "+5.0" or "p = 0.056". The closest figures are in a robustness table of the SSRN working paper, for the top-20% complexity strata: rework +5.1 pp (p < 0.001, top 20% churn) and defects +2.4 pp (p = 0.057, top 20% entropy). The paper concludes that specs *proxy for task complexity*. It explicitly says it did *not* measure SDD tool adoption, because no repository commits SDD tool output.
- Claim 4: the "Claude" corpus in the main comparison is one private project (Extractor) written by Claude Sonnet 4.6. Django and Pandas are the *human* baselines. Some summaries say Claude wrote tests "in Django and Pandas", which is wrong.

---

## 1. arXiv 2601.20109: post-merge code quality in agent bug-fix PRs

- **Title:** Beyond Bug Fixes: An Empirical Investigation of Post-Merge Code Quality Issues in Agent-Generated Pull Requests
- **Authors:** Shamse Tasnim Cynthia, Al Muttakin, Banani Roy
- **Date:** v1, 27 Jan 2026 (only version)
- **URL:** https://arxiv.org/abs/2601.20109 (full text: https://arxiv.org/html/2601.20109)
- **Status:** Accurate. One caveat: "maintainability problems" and "functional bugs" correspond to SonarQube's *Code Smell* and *Bug* categories.
- **Numbers:** 1,210 merged agent bug-fix PRs from Python repositories in the AIDev dataset. The PRs by agent were Codex 949, Copilot 106, Devin 100, Cursor 40 and Claude Code 15. Together they introduced 1,190 SonarQube issues:
  - Code smells: 1,059 (89.0%). Of these, 50.1% are CRITICAL and 26.0% MAJOR.
  - Security hotspots: 83 (7.0%)
  - Bugs: 48 (4.0%). Of these, 85.4% are BLOCKER.
  - Vulnerabilities: 0
  - The most common rules were S1192 (duplicated string literals), S3776 (cognitive complexity), S1172 (unused parameters), and S930 (wrong argument count) for bugs.
- **Quotes:** "Code Smell issues dominate". Also "merge success does not reliably reflect post-merge code quality".
- **What was measured:** SonarQube (Community Edition) was run on each PR's base commit and on its merged commit, with the same configuration. An "issue" is a finding newly introduced by the PR. The authors treat issues as indicators of quality risk, not confirmed defects. Differences between agents in raw counts mostly disappear once counts are normalised by churn (issues per KLOC, Kruskal-Wallis); Cursor's median density is higher.
- **Limits stated by the authors:**
  - Only Python, only bug-fix PRs, only merged PRs from AIDev.
  - No human baseline.
  - Static signals only, with no runtime or long-term effects.
  - Severe imbalance across agents (Claude Code n = 15), so agent-level results are exploratory.
  - SonarQube severity labels may not reflect production risk.

## 2. arXiv 2609.26847: follow-up fixes on agent PRs

- **Title:** Who Finishes the Job? A Study of Follow-Up Fixes and Commit Authorship on AI Coding Agent Pull Requests
- **Authors:** Wannita Takerngsaksiri, Nhat Duong, Scott Barnett
- **Date:** v1 22 Sep 2026; v2 24 Sep 2026 (current)
- **URL:** https://arxiv.org/abs/2609.26847 (full text: https://arxiv.org/html/2609.26847)
- **Status:** Accurate.
- **Quote (abstract):** "agent PRs attract verified fixes at 1.62 times the odds of merged human PRs"
- **Numbers:**
  - Mantel-Haenszel OR 1.62 (95% CI 1.10-2.39, p = 0.015), stratified over 218 repositories that contain both cohorts. The MH risk ratio is 1.59.
  - Shared observation window ending 28 Jun 2025: agents 74 of 2,012 (3.68%) vs humans 95 of 4,063 (2.34%). Precision-adjusted: 3.34% vs 2.10%.
  - At the candidate-fix level, the OR is 1.41.
  - Overall sample: 6,774 merged agent PRs (Codex 2,834, Devin 1,813, Copilot 1,428, Cursor 569, Claude Code 130) and 5,044 human PRs, in repositories with 500 or more stars.
  - Other findings: about 69.6% of verified fixes to agent merges come from the same agent, and about 76.4% of verified fix PRs are agent-authored.
- **What was measured:** A *candidate fix* is a later PR that:
  - merged in the same repository within 30 days;
  - is tagged as a "fix" in AIDev;
  - co-edits at least one non-boilerplate file that the original PR changed.

  It counts as *verified* only when labelled "Direct fix", meaning it fixes the change the original PR introduced. An LLM judge (Claude Opus 4.8, temperature 0) labelled every pair. It was validated against two human annotators: human-human κ = 0.77 on the binary label, judge κ = 0.78, and direct-fix precision 54/60 = 90% (Wilson CI 80-95%).
- **Limits stated by the authors:**
  - The 30-day file-overlap linkage misses cross-file and slower fixes.
  - Agent identity comes from commit-author patterns, so unmarked agent PRs count as human.
  - Codex is excluded from the commit-level analysis because its commits carry no markers.
  - Results cover only five AIDev agents in repositories with 500 or more stars.
  - The judge was validated on only 60 pairs, and agreement on the three-way label is lower (κ 0.60).
  - The pooled comparison is controlled only by stratifying on repository; there is no other adjustment.

## 3. arXiv 2605.06464, where the 9.9% figure actually comes from

### The ID given in the claim

- **Title:** To What Extent Does Agent-generated Code Require Maintenance? An Empirical Study
- **Authors:** Shota Sawada, Tatsuya Shirai, Yutaro Kashiwa, Ken'ichi Yamaguchi, Hiroshi Iwata, Hajimu Iida
- **Date:** v1 7 May 2026; v2 9 May 2026
- **URL:** https://arxiv.org/abs/2605.06464
- **What it contains:** The 9.9% appears only once, in Related Work, as a citation of Watanabe et al. (2026): "9.9% of the generated methods are eventually deleted during review". The reference list mis-links this citation to a different 2025 Watanabe paper.
- **Its own findings:**
  - 508 AI-generated files and 508 human files from 100 repos, with 3,238 maintenance commits.
  - AI files are maintained less often; in the first month they get about half the commits of human files.
  - Feature extensions are the most common change to AI files, while fixes dominate for human files.
  - Humans made 83.21% of the commits to AI-generated files.
  - Stated limits: complexity and file importance are not controlled; the six-month window falls in early adoption; undeclared AI help may be in the "human" files; Codex is excluded.

### The original source of the 9.9%

- **Title:** What to Cut? Predicting Unnecessary Methods in Agentic Code Generation
- **Authors:** Kan Watanabe, Tatsuya Shirai, Yutaro Kashiwa, Hajimu Iida (NAIST)
- **Date:** arXiv v1 19 Feb 2026; MSR '26
- **URL:** https://arxiv.org/abs/2602.17091 (full text: https://arxiv.org/html/2602.17091)
- **Status of the claim:** Wrong paper ID. As worded, the number is also misleading.
- **Quote:** "9.9% of methods are deleted in PRs that require revisions"
- **Numbers:**
  - The 9.9% covers 397 PRs that needed revisions, containing 3,257 methods. It is made up of 6.0% method-level deletions and 3.9% deletions of whole files.
  - The unconditional rate is lower: of 12,343 methods added at PR creation across 1,664 merged Python PRs from 197 projects (AIDev), 323 (2.6%) were gone by merge.
  - 75.6% of the PRs had no revisions at all.
- **What was measured:**
  - A method is "added" if it is present at PR creation but absent at the base.
  - It is "deleted" if it was added but is absent at the merge revision.
  - Methods are matched on Python AST path, class and name. Renames and moves are detected with ActRef and count as survivals.
  - Only merged PRs are included.
- **Limits stated by the authors:**
  - Label noise from ActRef.
  - Only deletions during the review period are counted.
  - Deletion does not necessarily mean poor quality, since requirements and design can change.
  - Python only.

## 4. arXiv 2608.15188: Claude-written vs human-written Python tests

- **Title:** The Quality of Claude AI-authored Python Tests Is Not Weaker Than Human-authored Tests
- **Author:** Douglas J. Leith (sole author)
- **Date:** v1, 15 Aug 2026 (only version)
- **URL:** https://arxiv.org/abs/2608.15188 (full text: https://arxiv.org/html/2608.15188)
- **Status:** Accurate. The design is non-inferiority: it shows Claude's tests are "no worse than", not "better than", the human tests.
- **Quote:** "are no weaker than the two human-written corpora"

### Method

**Models and projects**

| Corpus | Author of the tests | Notes |
|---|---|---|
| Extractor (main AI corpus) | Claude Sonnet 4.6 | Private and previously unpublished: a reverse-engineering tool for protobuf descriptors in Apple binaries, written by the agent from scratch over several weeks. |
| Django | Humans | Baseline; repository linked, no version pinned. |
| Pandas | Humans | Baseline; repository linked, no version pinned. |
| Quay (extension) | Claude Opus 4.6 and Sonnet 4.6 | Secondary AI corpus. |
| AWX (extension) | Claude Opus 4.6/4.8 and a model the paper names "Fable 5" | Secondary AI corpus; the attribution is unverified. |

Tests from Opus 4.5 and earlier are excluded from the main comparison and analysed separately, because they looked weaker.

**Did the model see the source code?** Yes. The tests are real, not synthetic: each comes from the commit that introduced it, and the model wrote the production code and its tests together, in the same development context. For Extractor, "No special test-writing instructions were given", and only 22% of test-writing episodes followed a direct request for tests. For Quay, a maintainer said Claude Code was given a JIRA ticket and told to write unit and integration tests. For AWX, the human input is unclear.

**The three fault-injection protocols**

1. *Historical revert.* Revert the production change that the test's commit introduced. The test counts as EFFECTIVE if it then fails, SURVIVED if it still passes, and UNREACHED if it never runs the changed code (checked with coverage.py `--branch`). This is fault injection, not classic mutation testing.
2. *Per-commit AST mutation.* Mutate only the lines changed in the introducing commit, using **cosmic-ray v8.4.6** with PIT's curated operator set.
3. *Coverage-based mutation.* Mutate the production code the test is estimated to execute, again with cosmic-ray. Coverage is estimated with a custom tool, which excludes scaffolding outside the directly imported modules.

**How the mutation score is computed:** The kill rate is "the percentage of mutants that cause the test to fail", pooled over scored mutants. For example, Extractor's per-commit score is 370 of 695 = 53.2%. The paper does not say how equivalent, non-viable or timed-out mutants are handled. Production code with no mutable operators is excluded.

**Qualitative rubric:** Seven axes are scored by AI agents:
- intent fidelity
- assertion strength
- coupling to behaviour vs implementation
- anti-patterns
- proportionality of setup and fixtures
- whether the purpose is stated
- cohesion

Stage A is blind, using only source and commit text. Stage B adds mutation evidence. A Critical rating needs a majority of a three-agent panel. Humans manually verified the defects in every Critical-tagged test.

**Sample sizes:**
- Tests scored: Extractor 298 (48 commits), Django 350 (100 commits), Pandas 350 (116 commits). After exclusions, the rubric scored 298, 342 and 344.
- Quay: 214 rubric-scored tests. AWX: 102.
- Scored mutants, per-commit: 695, 273 and 291 (Extractor, Django, Pandas).
- Scored mutants, coverage-based: 2,791, 2,744 and 1,616. Quay 1,825, AWX 665.

**Statistics:** One-sided 95% non-inferiority bounds from 5,000 bootstrap resamples, and Wilson CIs. No margin is fixed in advance; the bound is reported for each comparison.

### Headline numbers (Extractor / Django / Pandas)

- Historical revert, share EFFECTIVE: 96.6% / 97.7% / 95.4%. Extractor is at most 3.3 pp worse than Django and at most 1.4 pp worse than Pandas.
- Per-commit kill rate: 53.2% / 48.0% / 45.0%. The bound against Pandas is positive (+2.4 pp).
- Coverage-based kill rate: 46.3% / 50.0% / 45.7%. Bounds are 5.9 pp against Django and 2.0 pp against Pandas.
- Whole package: Extractor 58.8% vs Django utils 58.3%.
- Critical rubric defects: 6 / 5 / 1.
- Quay and AWX coverage-based kill rates: 44.8% and 58.8%.

### Limits stated by the author

- Python only, and only Claude models.
- The kill rate has no validated absolute scale and correlates only weakly with real fault detection.
- Critical-defect counts are small.
- The CIs overlap, so the data cannot reject "no difference".
- Provenance of Quay and AWX is unclear.
- Django and Pandas may contain undisclosed AI-written tests. Excluding the disclosed ones changed scores by less than 0.1%.
- Django and Pandas may appear in training data.
- Extractor had one AI developer, one human director and no independent reviewer, which is a different setting from the human baselines.

## 5. Brenn Hill: specification artifacts and defects (SSRN working paper; ESEM 2026)

- **Versions found:**
  - *SSRN working paper:* "Does Spec-Driven Development Reduce Defects? An Empirical Test of Industry Claims Across 119 Open-Source Repositories". Brenn Hill (independent researcher), Working Paper, April 2026. The SSRN page could not be fetched (HTTP 403), and the SSRN ID 6515898 given by a secondary source is unverified. I read the copy on Zenodo, file "ssrn-sdd-null-result.pdf": https://zenodo.org/records/19432099 (5 Apr 2026).
  - *Peer-reviewed version:* "Specification Artifacts in Open-Source Pull Request Workflows Do Not Reduce Defects: An Empirical Test of Spec-Driven Development Claims". ESEM 2026, LIPIcs vol. 394, art. 84. DOI 10.4230/LIPIcs.ESEM.2026.84. https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.ESEM.2026.84 (Zenodo v4, 5 Oct 2026).
  - Zenodo also lists an earlier v1, "Specification-Driven Development and Software Quality: A Null Result from 89,599 Pull Requests" (4 Apr 2026). I did not read it.
- **Status:** Partly accurate. The headline numbers are wrong.

**Correct numbers**
- PRs: 100,247 collected, minus 12,195 bot PRs, leaves **88,052 analysed**, across 119 repos and 17 languages, from April 2025 to April 2026. 24,298 of these (27.6%) count as "spec'd".
- **Rework (H2):**

  | Estimate | Effect | p |
  |---|---|---|
  | Within-author, size controls | **+1.2 pp** | 0.001 |
  | Within-author + JIT controls | +0.9 pp | 0.024 |
  | Propensity-matched | +0.5 pp | 0.146 |
  | Raw comparison | OR 1.18 | |

- **Defects (H1):**

  | Estimate | Effect | p |
  |---|---|---|
  | Within-author, size controls | **+1.4 pp** | **0.003** (not 0.056) |
  | Within-author + JIT controls | +0.6 pp | 0.229 |
  | Propensity-matched (primary estimate) | **-0.6 pp** | 0.133 |
  | Raw comparison | OR 1.20 | |

- **Spec quality:** predicts neither defects (p = 0.164) nor rework (p = 0.860).
- **AI scope (H5):** the interaction is null (p = 0.997).
- **One subgroup:** in repositories with no AI tags, specs go with less rework (-3.3 pp, p = 0.014). This does not survive Bonferroni correction across six subgroup tests.
- **"+5.0" and "p = 0.056":** neither version I read contains them. The nearest figures are in the complexity-stratification table of the SSRN version: rework +5.1 pp in the top 20% churn stratum (p < 0.001), and defects +2.4 pp in the top 20% entropy stratum (p = 0.057). The source summary probably mixed these up. Status: NOT VERIFIED as stated.

**Quote (ESEM abstract):** "Specification artifacts proxy for task complexity, not quality improvement."

**What was measured**
- *"Spec'd"*: the PR title or body references an upfront specification. That means a GitHub issue with a linking keyword, a ticket key, a tracker or doc URL, or a filled requirements section of 20 words or more. **97.5%** of spec'd PRs qualify through issue or ticket references.
- *Rework*: a later PR by any author, within 30 days, that touches overlapping files and has a fix, revert, bug, hotfix, regression or "broke" title or body (13.4% of PRs). Very high-frequency files are excluded.
- *Defect*: the PR contains a commit that basic SZZ marks as bug-introducing (103 repos, 12.5% of PRs).
- *Design*: within-author fixed effects, propensity matching on just-in-time (JIT) risk features, and 12 robustness checks.
- *SDD tools*: **no repository in the sample commits SDD tool output**, so tool adoption is not tested.

**Limits stated by the author**
- A specification artifact is not SDD practice.
- Artifacts are not the same as the thinking behind them.
- Agentic workflows cannot be observed.
- SZZ misattributes 46-71% of bug-introducing changes, and the noise may be differential.
- Confounding by indication: developers spec their harder tasks.
- The quality rubric is applied by an LLM.
- AI tags have 24.2% recall.
- All repos are open source and purposively sampled.
- No correction is made across the five hypotheses, all analyses are post hoc, and the sample size was not fixed in advance.

## 6. Uvik Software: Spec-Driven Development Benchmark 2026

- **Title:** Spec-Driven Development Benchmark 2026: Spec Kit vs Kiro vs BMAD vs OpenSpec vs No Spec on 50 Production Python Tickets
- **Author:** Paul Francis (Uvik Software)
- **Date:** The run is dated 15 Oct 2026; the page was last updated 2 Nov 2026. No original publication date is stated.
- **URL:** https://uvik.net/blog/spec-driven-development-benchmark/
- **Status:** Partly accurate.
- **Quotes:**
  - "Kiro merged 41 of 50 tickets and GitHub Spec Kit merged 40"
  - "feature and data-pipeline tickets, where the best spec workflows reached a 90% merge rate"

**Numbers**

Merged out of 50 tickets:

| Arm | Merged | Rate |
|---|---|---|
| OpenSpec | 42 | 84% |
| Kiro | 41 | 82% |
| BMAD | 41 | 82% |
| Spec Kit | 40 | 80% |
| No-spec control | 36 | 72% |

Merge rates on the two ticket types behind the 90% vs 60% figure (10 tickets each):

| Ticket type | Spec Kit | Kiro | BMAD | OpenSpec | No spec |
|---|---|---|---|---|---|
| Feature | 80% | 80% | 90% | 80% | 60% |
| Data pipeline | 80% | 90% | 90% | 90% | 60% |

So the "90% with specs" is the *best* spec arm on each type. It is not "with specs" in general, and it is not features only.

Other results:
- Review defects per merged ticket: 0.46 pooled across spec arms vs 0.86 for the control.
- Spec-phase share of all tokens: 35.1%.
- Cost per merged ticket: Kiro $2.35 vs control $2.43.
- Merged code did not match its spec in 7.9% of merged spec-arm tickets.
- Bug-fix, refactor and test-writing tickets showed little benefit from specs.

**What was measured**
- 50 brownfield tickets from 5 private client repos: 10 each of bug fix, feature, refactor, data pipeline and test writing.
- claude-sonnet-5 in every arm. Four arms ran in Claude Code; Kiro ran in its own IDE.
- 45-minute time boxes for the spec phase and the build phase.
- "Merged" means a senior engineer, blind to the workflow, accepted the patch.

**Limits stated by the authors**
- ±10 pp noise band on merge rates; arms within it are not ranked.
- One team of five engineers.
- Python only, one model.
- Kiro's harness differs from the other arms.
- The time boxes may favour frameworks that plan more.
- Single tickets only, so the long-term value of a living spec was not measured.
- Uvik sells Python and AI staffing services (disclosed on the page).
- Client code is private, so the benchmark cannot be reproduced.

## 7. Birgitta Böckeler (Thoughtworks), martinfowler.com

- **Title:** Understanding Spec-Driven-Development: Kiro, spec-kit, and Tessl
- **Author:** Birgitta Böckeler (Thoughtworks)
- **Date:** 15 Oct 2025
- **URL:** https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html
- **Status:** Accurate.

**Quotes**
- On review overhead (about spec-kit): "very verbose and tedious to review"
- A section heading: "False sense of control?"
- On agents ignoring the spec: "I frequently saw the agent ultimately not follow all the instructions"

**What she says**
- *Kiro*: lightweight, with a Requirements → Design → Tasks flow. Overkill for a small bug, which became 4 user stories and 16 acceptance criteria.
- *spec-kit*: a Constitution → Specify → Plan → Tasks flow that produces many verbose markdown files. She would rather review code than those files. Because it creates a branch per spec, she reads it as spec-first.
- *Tessl*: in private beta. It aims at spec-anchored and spec-as-source development, maps one spec to one code file, and regenerated code from the same spec is non-deterministic.
- She describes three levels: spec-first, spec-anchored and spec-as-source.

**Limits she states**
- A hands-on trial in September 2025, while the tools were changing fast.
- Not tested at the depth of real use.
- Limited evaluation on existing (brownfield) codebases.
- No long-term usage reports; she has "a lot of open questions".
