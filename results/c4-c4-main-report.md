# C4 report for tag `c4-main`

72 test-writing runs (0 errored), 72 agent suites scored. Writer spend $5.55 at list price (Gemini ran on the free tier). Mutants: every mutmut 3.8.1 mutant of the v1 reference (`results/c4/mutants.json`). Records: `results/c4/runs.jsonl`, `results/c4/scores.jsonl`; agent suites: `suites/c4/`.

Caveat: the public and held-out suites were written by the dark factory lab session (a Claude model, working from the full spec, iterating against the reference), not by human developers. 'Killable' = killed by at least one suite in this table (baselines and every agent suite of every tag scored so far); the rest are survivors of every suite, which includes equivalent mutants and mutants in v2-only code (the reference carries the change request behind a flag).

## bookings

916 mutants, 762 killable.

| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |
|---|---|---|---|---|---|---|---|
| public | - | 10/10 | 416/916 | 45.4% | 54.6% | 0.622s | - |
| holdout | - | 91/91 | 689/916 | 75.2% | 90.4% | 5.418s | - |
| `20261118-121800-bookings-tests-haiku-t1` | claude-haiku-5-5 | 252/252 | 718/916 | 78.4% | 94.2% | 15.106s | $0.018 |
| `20261118-125734-bookings-tests-haiku-t2` | claude-haiku-5-5 | 267/267 | 711/916 | 77.6% | 93.3% | 16.0s | $0.029 |
| `20261118-132826-bookings-tests-haiku-t3` | claude-haiku-5-5 | 272/272 | 719/916 | 78.5% | 94.4% | 17.292s | $0.044 |
| `20261118-140413-bookings-tests-haiku-t4` | claude-haiku-5-5 | 224/224 | 713/916 | 77.8% | 93.6% | 14.346s | $0.041 |
| `20261118-143846-bookings-tests-haiku-t5` | claude-haiku-5-5 | 218/218 | 711/916 | 77.6% | 93.3% | 13.9s | $0.033 |
| `20261118-150113-bookings-tests-bb-haiku-t1` | claude-haiku-5-5 (black-box) | 219/219 | 715/916 | 78.1% | 93.8% | 13.085s | $0.028 |
| `20261118-151439-bookings-tests-bb-haiku-t2` | claude-haiku-5-5 (black-box) | 207/207 | 717/916 | 78.3% | 94.1% | 12.438s | $0.018 |
| `20261118-152938-bookings-tests-bb-haiku-t3` | claude-haiku-5-5 (black-box) | 198/198 | 714/916 | 78.0% | 93.7% | 11.937s | $0.018 |
| `20261118-154255-bookings-tests-sonnet-t1` | claude-sonnet-5-5 | 177/177 | 714/916 | 78.0% | 93.7% | 10.607s | $0.181 |
| `20261118-154648-bookings-tests-sonnet-t2` | claude-sonnet-5-5 | 126/126 | 713/916 | 77.8% | 93.6% | 7.523s | $0.167 |
| `20261118-155042-bookings-tests-sonnet-t3` | claude-sonnet-5-5 | 153/153 | 718/916 | 78.4% | 94.2% | 9.215s | $0.159 |
| `20261118-072915-bookings-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 17/17 | 676/916 | 73.8% | 88.7% | 1.121s | $0.075 |
| `20261118-073406-bookings-tests-gemini-35-flash-lite-t2` | gemini-3.5-flash-lite | 15/15 | 645/916 | 70.4% | 84.6% | 1.016s | $0.082 |
| `20261118-073910-bookings-tests-gemini-35-flash-lite-t3` | gemini-3.5-flash-lite | 13/13 | 668/916 | 72.9% | 87.7% | 0.87s | $0.112 |
| `20261118-074948-bookings-tests-gemini-35-flash-lite-t4` | gemini-3.5-flash-lite | 11/11 | 674/916 | 73.6% | 88.5% | 0.73s | $0.092 |
| `20261118-075626-bookings-tests-gemini-35-flash-lite-t5` | gemini-3.5-flash-lite | 18/18 | 627/916 | 68.5% | 82.3% | 1.198s | $0.051 |
| `20261118-122424-bookings-tests-gpt-61-terra-t1` | gpt-6.1-terra | 14/14 | 711/916 | 77.6% | 93.3% | 0.917s | $0.129 |
| `20261118-131309-bookings-tests-gpt-61-terra-t2` | gpt-6.1-terra | 16/16 | 710/916 | 77.5% | 93.2% | 1.109s | $0.074 |
| `20261118-134638-bookings-tests-gpt-61-terra-t3` | gpt-6.1-terra | 15/15 | 713/916 | 77.8% | 93.6% | 0.993s | $0.092 |
| `20261118-143019-bookings-tests-gpt-61-terra-t4` | gpt-6.1-terra | 133/133 | 715/916 | 78.1% | 93.8% | 8.305s | $0.076 |
| `20261118-145549-bookings-tests-gpt-61-terra-t5` | gpt-6.1-terra | 15/15 | 709/916 | 77.4% | 93.0% | 0.981s | $0.081 |
| `20261118-150832-bookings-tests-bb-gpt-61-terra-t1` | gpt-6.1-terra (black-box) | 15/15 | 708/916 | 77.3% | 92.9% | 0.968s | $0.117 |
| `20261118-152210-bookings-tests-bb-gpt-61-terra-t2` | gpt-6.1-terra (black-box) | 19/19 | 712/916 | 77.7% | 93.4% | 1.291s | $0.062 |
| `20261118-153630-bookings-tests-bb-gpt-61-terra-t3` | gpt-6.1-terra (black-box) | 137/137 | 717/916 | 78.3% | 94.1% | 8.064s | $0.120 |

Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):

- `20261118-121800-bookings-tests-haiku-t1`: agent only 32, held-out only 3, both 686
- `20261118-150113-bookings-tests-bb-haiku-t1`: agent only 28, held-out only 2, both 687
- `20261118-125734-bookings-tests-haiku-t2`: agent only 30, held-out only 8, both 681
- `20261118-151439-bookings-tests-bb-haiku-t2`: agent only 30, held-out only 2, both 687
- `20261118-132826-bookings-tests-haiku-t3`: agent only 32, held-out only 2, both 687
- `20261118-152938-bookings-tests-bb-haiku-t3`: agent only 27, held-out only 2, both 687
- `20261118-140413-bookings-tests-haiku-t4`: agent only 28, held-out only 4, both 685
- `20261118-143846-bookings-tests-haiku-t5`: agent only 24, held-out only 2, both 687
- `20261118-154255-bookings-tests-sonnet-t1`: agent only 27, held-out only 2, both 687
- `20261118-154648-bookings-tests-sonnet-t2`: agent only 26, held-out only 2, both 687
- `20261118-155042-bookings-tests-sonnet-t3`: agent only 31, held-out only 2, both 687
- `20261118-072915-bookings-tests-gemini-35-flash-lite-t1`: agent only 13, held-out only 26, both 663
- `20261118-073406-bookings-tests-gemini-35-flash-lite-t2`: agent only 11, held-out only 55, both 634
- `20261118-073910-bookings-tests-gemini-35-flash-lite-t3`: agent only 11, held-out only 32, both 657
- `20261118-074948-bookings-tests-gemini-35-flash-lite-t4`: agent only 11, held-out only 26, both 663
- `20261118-075626-bookings-tests-gemini-35-flash-lite-t5`: agent only 16, held-out only 78, both 611
- `20261118-122424-bookings-tests-gpt-61-terra-t1`: agent only 27, held-out only 5, both 684
- `20261118-150832-bookings-tests-bb-gpt-61-terra-t1`: agent only 24, held-out only 5, both 684
- `20261118-131309-bookings-tests-gpt-61-terra-t2`: agent only 25, held-out only 4, both 685
- `20261118-152210-bookings-tests-bb-gpt-61-terra-t2`: agent only 27, held-out only 4, both 685
- `20261118-134638-bookings-tests-gpt-61-terra-t3`: agent only 27, held-out only 3, both 686
- `20261118-153630-bookings-tests-bb-gpt-61-terra-t3`: agent only 30, held-out only 2, both 687
- `20261118-143019-bookings-tests-gpt-61-terra-t4`: agent only 30, held-out only 4, both 685
- `20261118-145549-bookings-tests-gpt-61-terra-t5`: agent only 27, held-out only 7, both 682

## sessionize

618 mutants, 522 killable.

| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |
|---|---|---|---|---|---|---|---|
| public | - | 8/8 | 481/618 | 77.8% | 92.1% | 0.185s | - |
| holdout | - | 37/37 | 507/618 | 82.0% | 97.1% | 0.751s | - |
| `20261118-122008-sessionize-tests-haiku-t1` | claude-haiku-5-5 | 207/207 | 506/618 | 81.9% | 96.9% | 4.017s | $0.017 |
| `20261118-130526-sessionize-tests-haiku-t2` | claude-haiku-5-5 | 157/157 | 514/618 | 83.2% | 98.5% | 3.111s | $0.023 |
| `20261118-133926-sessionize-tests-haiku-t3` | claude-haiku-5-5 | 174/174 | 516/618 | 83.5% | 98.9% | 3.257s | $0.026 |
| `20261118-142400-sessionize-tests-haiku-t4` | claude-haiku-5-5 | 175/175 | 516/618 | 83.5% | 98.9% | 3.267s | $0.026 |
| `20261118-144958-sessionize-tests-haiku-t5` | claude-haiku-5-5 | 151/151 | 513/618 | 83.0% | 98.3% | 2.862s | $0.027 |
| `20261118-150358-sessionize-tests-bb-haiku-t1` | claude-haiku-5-5 (black-box) | 212/212 | 512/618 | 82.8% | 98.1% | 4.165s | $0.026 |
| `20261118-151705-sessionize-tests-bb-haiku-t2` | claude-haiku-5-5 (black-box) | 178/178 | 514/618 | 83.2% | 98.5% | 3.576s | $0.033 |
| `20261118-153216-sessionize-tests-bb-haiku-t3` | claude-haiku-5-5 (black-box) | 161/161 | 515/618 | 83.3% | 98.7% | 3.023s | $0.022 |
| `20261118-154413-sessionize-tests-sonnet-t1` | claude-sonnet-5-5 | 59/59 | 517/618 | 83.7% | 99.0% | 3.308s | $0.168 |
| `20261118-154834-sessionize-tests-sonnet-t2` | claude-sonnet-5-5 | 91/91 | 511/618 | 82.7% | 97.9% | 1.721s | $0.121 |
| `20261118-155220-sessionize-tests-sonnet-t3` | claude-sonnet-5-5 | 156/156 | 513/618 | 83.0% | 98.3% | 2.862s | $0.258 |
| `20261118-073058-sessionize-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 16/16 | 500/618 | 80.9% | 95.8% | 0.33s | $0.073 |
| `20261118-073554-sessionize-tests-gemini-35-flash-lite-t2` | gemini-3.5-flash-lite | 18/18 | 496/618 | 80.3% | 95.0% | 0.344s | $0.055 |
| `20261118-074119-sessionize-tests-gemini-35-flash-lite-t3` | gemini-3.5-flash-lite | 14/14 | 486/618 | 78.6% | 93.1% | 0.274s | $0.071 |
| `20261118-075153-sessionize-tests-gemini-35-flash-lite-t4` | gemini-3.5-flash-lite | 13/13 | 497/618 | 80.4% | 95.2% | 0.354s | $0.075 |
| `20261118-075930-sessionize-tests-gemini-35-flash-lite-t5` | gemini-3.5-flash-lite | 11/11 | 491/618 | 79.5% | 94.1% | 0.24s | $0.051 |
| `20261118-122700-sessionize-tests-gpt-61-terra-t1` | gpt-6.1-terra | 17/17 | 518/618 | 83.8% | 99.2% | 0.362s | $0.062 |
| `20261118-131438-sessionize-tests-gpt-61-terra-t2` | gpt-6.1-terra | 16/16 | 520/618 | 84.1% | 99.6% | 0.359s | $0.099 |
| `20261118-134812-sessionize-tests-gpt-61-terra-t3` | gpt-6.1-terra | 14/14 | 521/618 | 84.3% | 99.8% | 0.317s | $0.104 |
| `20261118-143214-sessionize-tests-gpt-61-terra-t4` | gpt-6.1-terra | 14/14 | 521/618 | 84.3% | 99.8% | 0.298s | $0.077 |
| `20261118-145719-sessionize-tests-gpt-61-terra-t5` | gpt-6.1-terra | 19/19 | 520/618 | 84.1% | 99.6% | 0.401s | $0.064 |
| `20261118-151108-sessionize-tests-bb-gpt-61-terra-t1` | gpt-6.1-terra (black-box) | 14/14 | 519/618 | 84.0% | 99.4% | 0.314s | $0.076 |
| `20261118-152340-sessionize-tests-bb-gpt-61-terra-t2` | gpt-6.1-terra (black-box) | 13/13 | 518/618 | 83.8% | 99.2% | 0.292s | $0.095 |
| `20261118-153927-sessionize-tests-bb-gpt-61-terra-t3` | gpt-6.1-terra (black-box) | 16/16 | 518/618 | 83.8% | 99.2% | 0.331s | $0.087 |

Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):

- `20261118-122008-sessionize-tests-haiku-t1`: agent only 7, held-out only 8, both 499
- `20261118-150358-sessionize-tests-bb-haiku-t1`: agent only 7, held-out only 2, both 505
- `20261118-130526-sessionize-tests-haiku-t2`: agent only 11, held-out only 4, both 503
- `20261118-151705-sessionize-tests-bb-haiku-t2`: agent only 10, held-out only 3, both 504
- `20261118-133926-sessionize-tests-haiku-t3`: agent only 11, held-out only 2, both 505
- `20261118-153216-sessionize-tests-bb-haiku-t3`: agent only 9, held-out only 1, both 506
- `20261118-142400-sessionize-tests-haiku-t4`: agent only 13, held-out only 4, both 503
- `20261118-144958-sessionize-tests-haiku-t5`: agent only 10, held-out only 4, both 503
- `20261118-154413-sessionize-tests-sonnet-t1`: agent only 11, held-out only 1, both 506
- `20261118-154834-sessionize-tests-sonnet-t2`: agent only 4, held-out only 0, both 507
- `20261118-155220-sessionize-tests-sonnet-t3`: agent only 10, held-out only 4, both 503
- `20261118-073058-sessionize-tests-gemini-35-flash-lite-t1`: agent only 6, held-out only 13, both 494
- `20261118-073554-sessionize-tests-gemini-35-flash-lite-t2`: agent only 8, held-out only 19, both 488
- `20261118-074119-sessionize-tests-gemini-35-flash-lite-t3`: agent only 5, held-out only 26, both 481
- `20261118-075153-sessionize-tests-gemini-35-flash-lite-t4`: agent only 6, held-out only 16, both 491
- `20261118-075930-sessionize-tests-gemini-35-flash-lite-t5`: agent only 5, held-out only 21, both 486
- `20261118-122700-sessionize-tests-gpt-61-terra-t1`: agent only 12, held-out only 1, both 506
- `20261118-151108-sessionize-tests-bb-gpt-61-terra-t1`: agent only 13, held-out only 1, both 506
- `20261118-131438-sessionize-tests-gpt-61-terra-t2`: agent only 14, held-out only 1, both 506
- `20261118-152340-sessionize-tests-bb-gpt-61-terra-t2`: agent only 12, held-out only 1, both 506
- `20261118-134812-sessionize-tests-gpt-61-terra-t3`: agent only 14, held-out only 0, both 507
- `20261118-153927-sessionize-tests-bb-gpt-61-terra-t3`: agent only 12, held-out only 1, both 506
- `20261118-143214-sessionize-tests-gpt-61-terra-t4`: agent only 14, held-out only 0, both 507
- `20261118-145719-sessionize-tests-gpt-61-terra-t5`: agent only 14, held-out only 1, both 506

## splitr

439 mutants, 350 killable.

| suite | model | tests (valid/total) | killed | score | score on killable | pytest time on reference | writer cost |
|---|---|---|---|---|---|---|---|
| public | - | 8/8 | 271/439 | 61.7% | 77.4% | 0.121s | - |
| holdout | - | 44/44 | 329/439 | 74.9% | 94.0% | 0.818s | - |
| `20261118-121310-splitr-tests-haiku-t1` | claude-haiku-5-5 | 131/131 | 335/439 | 76.3% | 95.7% | 2.178s | $0.023 |
| `20261118-122818-splitr-tests-haiku-t2` | claude-haiku-5-5 | 201/201 | 336/439 | 76.5% | 96.0% | 5.269s | $0.051 |
| `20261118-131645-splitr-tests-haiku-t3` | claude-haiku-5-5 | 186/186 | 343/439 | 78.1% | 98.0% | 5.108s | $0.032 |
| `20261118-135024-splitr-tests-haiku-t4` | claude-haiku-5-5 | 168/168 | 342/439 | 77.9% | 97.7% | 3.805s | $0.062 |
| `20261118-143341-splitr-tests-haiku-t5` | claude-haiku-5-5 | 188/188 | 338/439 | 77.0% | 96.6% | 4.85s | $0.040 |
| `20261118-145844-splitr-tests-bb-haiku-t1` | claude-haiku-5-5 (black-box) | 166/166 | 337/439 | 76.8% | 96.3% | 3.816s | $0.023 |
| `20261118-151242-splitr-tests-bb-haiku-t2` | claude-haiku-5-5 (black-box) | 215/215 | 338/439 | 77.0% | 96.6% | 3.69s | $0.019 |
| `20261118-152558-splitr-tests-bb-haiku-t3` | claude-haiku-5-5 (black-box) | 160/160 | 338/439 | 77.0% | 96.6% | 4.0s | $0.035 |
| `20261118-154132-splitr-tests-sonnet-t1` | claude-sonnet-5-5 | 130/130 | 338/439 | 77.0% | 96.6% | 2.379s | $0.164 |
| `20261118-154525-splitr-tests-sonnet-t2` | claude-sonnet-5-5 | 146/146 | 340/439 | 77.5% | 97.1% | 2.475s | $0.155 |
| `20261118-154931-splitr-tests-sonnet-t3` | claude-sonnet-5-5 | 124/124 | 341/439 | 77.7% | 97.4% | 2.184s | $0.134 |
| `20261118-072648-splitr-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 21/21 | 313/439 | 71.3% | 89.4% | 0.874s | $0.093 |
| `20261118-073252-splitr-tests-gemini-35-flash-lite-t2` | gemini-3.5-flash-lite | 19/19 | 307/439 | 69.9% | 87.7% | 0.618s | $0.027 |
| `20261118-073735-splitr-tests-gemini-35-flash-lite-t3` | gemini-3.5-flash-lite | 18/18 | 300/439 | 68.3% | 85.7% | 0.654s | $0.045 |
| `20261118-074710-splitr-tests-gemini-35-flash-lite-t4` | gemini-3.5-flash-lite | 19/19 | 318/439 | 72.4% | 90.9% | 0.818s | $0.114 |
| `20261118-075354-splitr-tests-gemini-35-flash-lite-t5` | gemini-3.5-flash-lite | 14/14 | 284/439 | 64.7% | 81.1% | 0.284s | $0.063 |
| `20261118-122242-splitr-tests-gpt-61-terra-t1` | gpt-6.1-terra | 52/52 | 348/439 | 79.3% | 99.4% | 1.053s | $0.085 |
| `20261118-131037-splitr-tests-gpt-61-terra-t2` | gpt-6.1-terra | 98/98 | 346/439 | 78.8% | 98.9% | 2.248s | $0.123 |
| `20261118-134444-splitr-tests-gpt-61-terra-t3` | gpt-6.1-terra | 92/92 | 346/439 | 78.8% | 98.9% | 2.194s | $0.090 |
| `20261118-142733-splitr-tests-gpt-61-terra-t4` | gpt-6.1-terra | 55/55 | 349/439 | 79.5% | 99.7% | 1.115s | $0.151 |
| `20261118-145342-splitr-tests-gpt-61-terra-t5` | gpt-6.1-terra | 90/90 | 349/439 | 79.5% | 99.7% | 2.477s | $0.102 |
| `20261118-150643-splitr-tests-bb-gpt-61-terra-t1` | gpt-6.1-terra (black-box) | 42/42 | 337/439 | 76.8% | 96.3% | 1.582s | $0.085 |
| `20261118-152008-splitr-tests-bb-gpt-61-terra-t2` | gpt-6.1-terra (black-box) | 79/79 | 337/439 | 76.8% | 96.3% | 1.627s | $0.099 |
| `20261118-153428-splitr-tests-bb-gpt-61-terra-t3` | gpt-6.1-terra (black-box) | 75/75 | 336/439 | 76.5% | 96.0% | 2.285s | $0.096 |

Overlap with the held-out suite (mutants killed by the agent suite but not the held-out suite, and the reverse):

- `20261118-121310-splitr-tests-haiku-t1`: agent only 7, held-out only 1, both 328
- `20261118-145844-splitr-tests-bb-haiku-t1`: agent only 9, held-out only 1, both 328
- `20261118-122818-splitr-tests-haiku-t2`: agent only 9, held-out only 2, both 327
- `20261118-151242-splitr-tests-bb-haiku-t2`: agent only 10, held-out only 1, both 328
- `20261118-131645-splitr-tests-haiku-t3`: agent only 14, held-out only 0, both 329
- `20261118-152558-splitr-tests-bb-haiku-t3`: agent only 9, held-out only 0, both 329
- `20261118-135024-splitr-tests-haiku-t4`: agent only 13, held-out only 0, both 329
- `20261118-143341-splitr-tests-haiku-t5`: agent only 9, held-out only 0, both 329
- `20261118-154132-splitr-tests-sonnet-t1`: agent only 10, held-out only 1, both 328
- `20261118-154525-splitr-tests-sonnet-t2`: agent only 13, held-out only 2, both 327
- `20261118-154931-splitr-tests-sonnet-t3`: agent only 14, held-out only 2, both 327
- `20261118-072648-splitr-tests-gemini-35-flash-lite-t1`: agent only 0, held-out only 16, both 313
- `20261118-073252-splitr-tests-gemini-35-flash-lite-t2`: agent only 0, held-out only 22, both 307
- `20261118-073735-splitr-tests-gemini-35-flash-lite-t3`: agent only 0, held-out only 29, both 300
- `20261118-074710-splitr-tests-gemini-35-flash-lite-t4`: agent only 5, held-out only 16, both 313
- `20261118-075354-splitr-tests-gemini-35-flash-lite-t5`: agent only 1, held-out only 46, both 283
- `20261118-122242-splitr-tests-gpt-61-terra-t1`: agent only 19, held-out only 0, both 329
- `20261118-150643-splitr-tests-bb-gpt-61-terra-t1`: agent only 9, held-out only 1, both 328
- `20261118-131037-splitr-tests-gpt-61-terra-t2`: agent only 17, held-out only 0, both 329
- `20261118-152008-splitr-tests-bb-gpt-61-terra-t2`: agent only 10, held-out only 2, both 327
- `20261118-134444-splitr-tests-gpt-61-terra-t3`: agent only 18, held-out only 1, both 328
- `20261118-153428-splitr-tests-bb-gpt-61-terra-t3`: agent only 9, held-out only 2, both 327
- `20261118-142733-splitr-tests-gpt-61-terra-t4`: agent only 20, held-out only 0, both 329
- `20261118-145342-splitr-tests-gpt-61-terra-t5`: agent only 21, held-out only 1, both 328

## By model (specs pooled)

| model | suites | valid tests (median) | invalid tests (total) | mutation score (mean) | writer cost (mean) |
|---|---|---|---|---|---|
| claude-haiku-5-5 | 15 | 188 | 0 | 79.4% | $0.033 |
| claude-haiku-5-5 (black-box) | 9 | 198 | 0 | 79.4% | $0.025 |
| claude-sonnet-5-5 | 9 | 130 | 0 | 79.5% | $0.168 |
| gemini-3.5-flash-lite | 15 | 16 | 0 | 73.7% | $0.072 |
| gpt-6.1-terra | 15 | 17 | 0 | 80.3% | $0.094 |
| gpt-6.1-terra (black-box) | 9 | 19 | 0 | 79.4% | $0.093 |
| public suite | 3 | 8 | 0 | 61.7% | - |
| holdout suite | 3 | 44 | 0 | 77.4% | - |

## Real faults: the suites against the dark factory's shipped codebases

Faulty = brief-ticket builds that fail at least one held-out scenario; holdout-clean = full-spec builds that pass all of them. A suite catches a faulty build if it fails on it. A failure on a holdout-clean build is either a false alarm (the test pins behaviour the spec leaves open) or a real bug the held-out suite misses; these need reading by hand before either word is used: see `results/c4-falsealarms-report.md` for the sorting. The held-out suite defines 'faulty', so it catches all of them by construction and is shown only as a check; the public examples pass on every build by construction.

| spec | suite | model | faulty caught | fails on holdout-clean builds |
|---|---|---|---|---|
| bookings | `20261118-072915-bookings-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 19/21 (90%) | 0/11 (0%) |
| bookings | `20261118-073406-bookings-tests-gemini-35-flash-lite-t2` | gemini-3.5-flash-lite | 21/21 (100%) | 11/11 (100%) |
| bookings | `20261118-073910-bookings-tests-gemini-35-flash-lite-t3` | gemini-3.5-flash-lite | 19/21 (90%) | 0/11 (0%) |
| bookings | `20261118-074948-bookings-tests-gemini-35-flash-lite-t4` | gemini-3.5-flash-lite | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-075626-bookings-tests-gemini-35-flash-lite-t5` | gemini-3.5-flash-lite | 21/21 (100%) | 11/11 (100%) |
| bookings | `20261118-121800-bookings-tests-haiku-t1` | claude-haiku-5-5 | 21/21 (100%) | 3/11 (27%) |
| bookings | `20261118-122424-bookings-tests-gpt-61-terra-t1` | gpt-6.1-terra | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-125734-bookings-tests-haiku-t2` | claude-haiku-5-5 | 21/21 (100%) | 2/11 (18%) |
| bookings | `20261118-131309-bookings-tests-gpt-61-terra-t2` | gpt-6.1-terra | 21/21 (100%) | 2/11 (18%) |
| bookings | `20261118-132826-bookings-tests-haiku-t3` | claude-haiku-5-5 | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-134638-bookings-tests-gpt-61-terra-t3` | gpt-6.1-terra | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-140413-bookings-tests-haiku-t4` | claude-haiku-5-5 | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-143019-bookings-tests-gpt-61-terra-t4` | gpt-6.1-terra | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-143846-bookings-tests-haiku-t5` | claude-haiku-5-5 | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-145549-bookings-tests-gpt-61-terra-t5` | gpt-6.1-terra | 21/21 (100%) | 4/11 (36%) |
| bookings | `20261118-150113-bookings-tests-bb-haiku-t1` | claude-haiku-5-5 (black-box) | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-150832-bookings-tests-bb-gpt-61-terra-t1` | gpt-6.1-terra (black-box) | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-151439-bookings-tests-bb-haiku-t2` | claude-haiku-5-5 (black-box) | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-152210-bookings-tests-bb-gpt-61-terra-t2` | gpt-6.1-terra (black-box) | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-152938-bookings-tests-bb-haiku-t3` | claude-haiku-5-5 (black-box) | 21/21 (100%) | 0/11 (0%) |
| bookings | `20261118-153630-bookings-tests-bb-gpt-61-terra-t3` | gpt-6.1-terra (black-box) | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-154255-bookings-tests-sonnet-t1` | claude-sonnet-5-5 | 21/21 (100%) | 1/11 (9%) |
| bookings | `20261118-154648-bookings-tests-sonnet-t2` | claude-sonnet-5-5 | 21/21 (100%) | 3/11 (27%) |
| bookings | `20261118-155042-bookings-tests-sonnet-t3` | claude-sonnet-5-5 | 21/21 (100%) | 2/11 (18%) |
| bookings | holdout | - | 21/21 (100%) | 0/11 (0%) |
| bookings | public | - | 0/21 (0%) | 0/11 (0%) |
| sessionize | `20261118-073058-sessionize-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 22/24 (92%) | 0/11 (0%) |
| sessionize | `20261118-073554-sessionize-tests-gemini-35-flash-lite-t2` | gemini-3.5-flash-lite | 22/24 (92%) | 0/11 (0%) |
| sessionize | `20261118-074119-sessionize-tests-gemini-35-flash-lite-t3` | gemini-3.5-flash-lite | 23/24 (96%) | 0/11 (0%) |
| sessionize | `20261118-075153-sessionize-tests-gemini-35-flash-lite-t4` | gemini-3.5-flash-lite | 21/24 (88%) | 0/11 (0%) |
| sessionize | `20261118-075930-sessionize-tests-gemini-35-flash-lite-t5` | gemini-3.5-flash-lite | 23/24 (96%) | 0/11 (0%) |
| sessionize | `20261118-122008-sessionize-tests-haiku-t1` | claude-haiku-5-5 | 24/24 (100%) | 2/11 (18%) |
| sessionize | `20261118-122700-sessionize-tests-gpt-61-terra-t1` | gpt-6.1-terra | 24/24 (100%) | 11/11 (100%) |
| sessionize | `20261118-130526-sessionize-tests-haiku-t2` | claude-haiku-5-5 | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-131438-sessionize-tests-gpt-61-terra-t2` | gpt-6.1-terra | 24/24 (100%) | 6/11 (55%) |
| sessionize | `20261118-133926-sessionize-tests-haiku-t3` | claude-haiku-5-5 | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-134812-sessionize-tests-gpt-61-terra-t3` | gpt-6.1-terra | 24/24 (100%) | 11/11 (100%) |
| sessionize | `20261118-142400-sessionize-tests-haiku-t4` | claude-haiku-5-5 | 24/24 (100%) | 0/11 (0%) |
| sessionize | `20261118-143214-sessionize-tests-gpt-61-terra-t4` | gpt-6.1-terra | 24/24 (100%) | 6/11 (55%) |
| sessionize | `20261118-144958-sessionize-tests-haiku-t5` | claude-haiku-5-5 | 24/24 (100%) | 0/11 (0%) |
| sessionize | `20261118-145719-sessionize-tests-gpt-61-terra-t5` | gpt-6.1-terra | 24/24 (100%) | 11/11 (100%) |
| sessionize | `20261118-150358-sessionize-tests-bb-haiku-t1` | claude-haiku-5-5 (black-box) | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-151108-sessionize-tests-bb-gpt-61-terra-t1` | gpt-6.1-terra (black-box) | 24/24 (100%) | 0/11 (0%) |
| sessionize | `20261118-151705-sessionize-tests-bb-haiku-t2` | claude-haiku-5-5 (black-box) | 24/24 (100%) | 0/11 (0%) |
| sessionize | `20261118-152340-sessionize-tests-bb-gpt-61-terra-t2` | gpt-6.1-terra (black-box) | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-153216-sessionize-tests-bb-haiku-t3` | claude-haiku-5-5 (black-box) | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-153927-sessionize-tests-bb-gpt-61-terra-t3` | gpt-6.1-terra (black-box) | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-154413-sessionize-tests-sonnet-t1` | claude-sonnet-5-5 | 24/24 (100%) | 2/11 (18%) |
| sessionize | `20261118-154834-sessionize-tests-sonnet-t2` | claude-sonnet-5-5 | 24/24 (100%) | 1/11 (9%) |
| sessionize | `20261118-155220-sessionize-tests-sonnet-t3` | claude-sonnet-5-5 | 24/24 (100%) | 1/11 (9%) |
| sessionize | holdout | - | 24/24 (100%) | 0/11 (0%) |
| sessionize | public | - | 0/24 (0%) | 0/11 (0%) |
| splitr | `20261118-072648-splitr-tests-gemini-35-flash-lite-t1` | gemini-3.5-flash-lite | 22/22 (100%) | 3/11 (27%) |
| splitr | `20261118-073252-splitr-tests-gemini-35-flash-lite-t2` | gemini-3.5-flash-lite | 22/22 (100%) | 3/11 (27%) |
| splitr | `20261118-073735-splitr-tests-gemini-35-flash-lite-t3` | gemini-3.5-flash-lite | 20/22 (91%) | 0/11 (0%) |
| splitr | `20261118-074710-splitr-tests-gemini-35-flash-lite-t4` | gemini-3.5-flash-lite | 22/22 (100%) | 3/11 (27%) |
| splitr | `20261118-075354-splitr-tests-gemini-35-flash-lite-t5` | gemini-3.5-flash-lite | 19/22 (86%) | 0/11 (0%) |
| splitr | `20261118-121310-splitr-tests-haiku-t1` | claude-haiku-5-5 | 22/22 (100%) | 1/11 (9%) |
| splitr | `20261118-122242-splitr-tests-gpt-61-terra-t1` | gpt-6.1-terra | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-122818-splitr-tests-haiku-t2` | claude-haiku-5-5 | 21/22 (95%) | 0/11 (0%) |
| splitr | `20261118-131037-splitr-tests-gpt-61-terra-t2` | gpt-6.1-terra | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-131645-splitr-tests-haiku-t3` | claude-haiku-5-5 | 22/22 (100%) | 7/11 (64%) |
| splitr | `20261118-134444-splitr-tests-gpt-61-terra-t3` | gpt-6.1-terra | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-135024-splitr-tests-haiku-t4` | claude-haiku-5-5 | 21/22 (95%) | 7/11 (64%) |
| splitr | `20261118-142733-splitr-tests-gpt-61-terra-t4` | gpt-6.1-terra | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-143341-splitr-tests-haiku-t5` | claude-haiku-5-5 | 22/22 (100%) | 0/11 (0%) |
| splitr | `20261118-145342-splitr-tests-gpt-61-terra-t5` | gpt-6.1-terra | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-145844-splitr-tests-bb-haiku-t1` | claude-haiku-5-5 (black-box) | 21/22 (95%) | 11/11 (100%) |
| splitr | `20261118-150643-splitr-tests-bb-gpt-61-terra-t1` | gpt-6.1-terra (black-box) | 22/22 (100%) | 6/11 (55%) |
| splitr | `20261118-151242-splitr-tests-bb-haiku-t2` | claude-haiku-5-5 (black-box) | 21/22 (95%) | 1/11 (9%) |
| splitr | `20261118-152008-splitr-tests-bb-gpt-61-terra-t2` | gpt-6.1-terra (black-box) | 22/22 (100%) | 11/11 (100%) |
| splitr | `20261118-152558-splitr-tests-bb-haiku-t3` | claude-haiku-5-5 (black-box) | 20/22 (91%) | 0/11 (0%) |
| splitr | `20261118-153428-splitr-tests-bb-gpt-61-terra-t3` | gpt-6.1-terra (black-box) | 22/22 (100%) | 6/11 (55%) |
| splitr | `20261118-154132-splitr-tests-sonnet-t1` | claude-sonnet-5-5 | 22/22 (100%) | 10/11 (91%) |
| splitr | `20261118-154525-splitr-tests-sonnet-t2` | claude-sonnet-5-5 | 22/22 (100%) | 10/11 (91%) |
| splitr | `20261118-154931-splitr-tests-sonnet-t3` | claude-sonnet-5-5 | 22/22 (100%) | 0/11 (0%) |
| splitr | holdout | - | 22/22 (100%) | 0/11 (0%) |
| splitr | public | - | 0/22 (0%) | 0/11 (0%) |

## Black-box writers: commands that look like decompiling the product

- none

## Tampering and errors

- none
