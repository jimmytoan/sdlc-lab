# C1 significance (tag c1-main)

Each condition against 'none', same model. Diff = mean over the three specs of (mean held-out pass rate in the condition minus in 'none'). p: two-sided permutation test shuffling condition labels within each spec, 10,000 shuffles. CI: bootstrap of runs within spec and condition, 5,000 resamples. Uncorrected for 7 comparisons. Code: sdlc/c1stats.py.

## All held-out scenarios

| model | condition vs none | diff (points) | 95% CI | p |
|---|---|---|---|---|
| claude-haiku-5-5 | kiro | +4.5 | +3.1 to +6.0 | 0.000 |
| claude-haiku-5-5 | plan | +4.3 | +2.8 to +5.7 | 0.000 |
| claude-haiku-5-5 | speckit | -0.5 | -2.9 to +2.1 | 0.728 |
| claude-haiku-5-5 | speckit-clarify | +3.5 | +1.5 to +5.6 | 0.004 |
| claude-sonnet-5-5 | kiro | +2.5 | +1.3 to +3.5 | 0.008 |
| claude-sonnet-5-5 | plan | +1.5 | -0.8 to +3.5 | 0.288 |
| claude-sonnet-5-5 | speckit | -1.2 | -2.7 to +0.2 | 0.229 |

## Arbitrary scenarios only

| model | condition vs none | diff (points) | 95% CI | p |
|---|---|---|---|---|
| claude-haiku-5-5 | kiro | +14.5 | +11.2 to +17.9 | 0.000 |
| claude-haiku-5-5 | plan | +13.7 | +10.5 to +17.2 | 0.000 |
| claude-haiku-5-5 | speckit | +2.6 | -2.4 to +7.5 | 0.354 |
| claude-haiku-5-5 | speckit-clarify | +12.2 | +6.8 to +17.9 | 0.000 |
| claude-sonnet-5-5 | kiro | +4.4 | +2.3 to +6.8 | 0.019 |
| claude-sonnet-5-5 | plan | +3.8 | -1.4 to +8.5 | 0.204 |
| claude-sonnet-5-5 | speckit | -5.3 | -8.5 to -1.9 | 0.030 |
