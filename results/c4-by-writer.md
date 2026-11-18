# C4 by writer (tag c4-main)

Pooled over specs. Faulty: brief-ticket dark factory builds that fail a held-out scenario. Clean: full-spec builds that pass all of them. Verdicts from `results/c4-falsealarms-report.md`; a clean-build failure that did not reproduce on a re-run is not in any verdict column. Code: `sdlc/c4writers.py`.

| writer | suites | mutants killed | faulty builds caught | suites catching every faulty build | clean-build failures | real bug | ref deviates | open | false alarm |
|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | 15 | 7831/9865 (79.4%) | 333/335 (99.4%) | 13/15 | 26/165 (16%) | 12 | 0 | 9 | 0 |
| claude-haiku-5-5 (black-box) | 9 | 4700/5919 (79.4%) | 197/201 (98.0%) | 6/9 | 15/99 (15%) | 1 | 11 | 2 | 0 |
| claude-sonnet-5-5 | 9 | 4705/5919 (79.5%) | 201/201 (100.0%) | 9/9 | 30/99 (30%) | 2 | 0 | 27 | 0 |
| gemini-3.5-flash-lite | 15 | 7282/9865 (73.8%) | 317/335 (94.6%) | 6/15 | 31/165 (19%) | 0 | 0 | 0 | 31 |
| gpt-6.1-terra | 15 | 7896/9865 (80.0%) | 335/335 (100.0%) | 15/15 | 108/165 (65%) | 22 | 28 | 58 | 0 |
| gpt-6.1-terra (black-box) | 9 | 4702/5919 (79.4%) | 201/201 (100.0%) | 9/9 | 26/99 (26%) | 1 | 11 | 13 | 0 |
