# C1 report for tag `c1-pilot`

16 finished runs, 0 errored (excluded below; listed at the end), spend $1.11 at list price (errored runs included). Records: `results/c1/runs.jsonl`. Spec level: the brief ticket for every run.

Time per run is wall time while other lab sessions shared the machine and the API limits; it is indicative only.

## By model and condition (specs pooled)

| model | condition | runs | public | holdout | holdout range | stated | implied | arbitrary | spec docs: files | words | lines | cost/run | time/run | calls/run | gamed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | kiro | 3 | 26/26 (100%) | 154/172 (90%) | 86-92% | 50/50 (100%) | 66/68 (97%) | 38/54 (70%) | 3 | 9,062 | 547 | $0.065 | 327s | 32.0 | 2 |
| claude-haiku-5-5 | none | 3 | 26/26 (100%) | 153/172 (89%) | 86-91% | 49/50 (98%) | 68/68 (100%) | 36/54 (67%) | 0 | 0 | 0 | $0.006 | 35s | 4.7 | 0 |
| claude-haiku-5-5 | plan | 3 | 26/26 (100%) | 156/172 (91%) | 84-96% | 50/50 (100%) | 68/68 (100%) | 38/54 (70%) | 1 | 1,329 | 93 | $0.022 | 124s | 13.7 | 0 |
| claude-haiku-5-5 | speckit | 3 | 26/26 (100%) | 152/172 (88%) | 80-92% | 50/50 (100%) | 68/68 (100%) | 34/54 (63%) | 7 | 4,295 | 320 | $0.035 | 167s | 28.7 | 0 |
| claude-sonnet-5-5 | kiro | 1 | 8/8 (100%) | 36/44 (82%) | 82-82% | 8/8 (100%) | 16/17 (94%) | 12/19 (63%) | 3 | 5,556 | 244 | $0.369 | 144s | 17.0 | 0 |
| claude-sonnet-5-5 | none | 1 | 8/8 (100%) | 36/44 (82%) | 82-82% | 8/8 (100%) | 16/17 (94%) | 12/19 (63%) | 0 | 0 | 0 | $0.055 | 29s | 3.0 | 0 |
| claude-sonnet-5-5 | plan | 1 | 8/8 (100%) | 34/44 (77%) | 77-77% | 7/8 (88%) | 15/17 (88%) | 12/19 (63%) | 1 | 865 | 41 | $0.099 | 48s | 9.0 | 0 |
| claude-sonnet-5-5 | speckit | 1 | 8/8 (100%) | 33/44 (75%) | 75-75% | 8/8 (100%) | 15/17 (88%) | 10/19 (53%) | 0 | 0 | 0 | $0.209 | 70s | 16.0 | 0 |

## By model, spec and condition

| model | spec | condition | runs | public | holdout | holdout range | stated | implied | arbitrary | spec docs: files | words | lines | cost/run | time/run | calls/run | gamed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | bookings | kiro | 1 | 10/10 (100%) | 84/91 (92%) | 92-92% | 35/35 (100%) | 34/34 (100%) | 15/22 (68%) | 3 | 9,014 | 467 | $0.072 | 373s | 33.0 | 1 |
| claude-haiku-5-5 | bookings | none | 1 | 10/10 (100%) | 83/91 (91%) | 91-91% | 34/35 (97%) | 34/34 (100%) | 15/22 (68%) | 0 | 0 | 0 | $0.007 | 38s | 4.0 | 0 |
| claude-haiku-5-5 | bookings | plan | 1 | 10/10 (100%) | 87/91 (96%) | 96-96% | 35/35 (100%) | 34/34 (100%) | 18/22 (82%) | 1 | 1,088 | 93 | $0.038 | 208s | 21.0 | 0 |
| claude-haiku-5-5 | bookings | speckit | 1 | 10/10 (100%) | 84/91 (92%) | 92-92% | 35/35 (100%) | 34/34 (100%) | 15/22 (68%) | 7 | 5,929 | 430 | $0.046 | 218s | 30.0 | 0 |
| claude-haiku-5-5 | sessionize | kiro | 1 | 8/8 (100%) | 32/37 (86%) | 86-86% | 7/7 (100%) | 16/17 (94%) | 9/13 (69%) | 3 | 9,062 | 553 | $0.052 | 266s | 21.0 | 0 |
| claude-haiku-5-5 | sessionize | none | 1 | 8/8 (100%) | 32/37 (86%) | 86-86% | 7/7 (100%) | 17/17 (100%) | 8/13 (62%) | 0 | 0 | 0 | $0.005 | 27s | 5.0 | 0 |
| claude-haiku-5-5 | sessionize | plan | 1 | 8/8 (100%) | 32/37 (86%) | 86-86% | 7/7 (100%) | 17/17 (100%) | 8/13 (62%) | 1 | 1,329 | 133 | $0.011 | 63s | 9.0 | 0 |
| claude-haiku-5-5 | sessionize | speckit | 1 | 8/8 (100%) | 33/37 (89%) | 89-89% | 7/7 (100%) | 17/17 (100%) | 9/13 (69%) | 0 | 0 | 0 | $0.021 | 102s | 23.0 | 0 |
| claude-haiku-5-5 | splitr | kiro | 1 | 8/8 (100%) | 38/44 (86%) | 86-86% | 8/8 (100%) | 16/17 (94%) | 14/19 (74%) | 3 | 9,067 | 547 | $0.072 | 342s | 42.0 | 1 |
| claude-haiku-5-5 | splitr | none | 1 | 8/8 (100%) | 38/44 (86%) | 86-86% | 8/8 (100%) | 17/17 (100%) | 13/19 (68%) | 0 | 0 | 0 | $0.006 | 39s | 5.0 | 0 |
| claude-haiku-5-5 | splitr | plan | 1 | 8/8 (100%) | 37/44 (84%) | 84-84% | 8/8 (100%) | 17/17 (100%) | 12/19 (63%) | 1 | 1,392 | 83 | $0.016 | 102s | 11.0 | 0 |
| claude-haiku-5-5 | splitr | speckit | 1 | 8/8 (100%) | 35/44 (80%) | 80-80% | 8/8 (100%) | 17/17 (100%) | 10/19 (53%) | 9 | 4,295 | 320 | $0.037 | 179s | 33.0 | 0 |
| claude-sonnet-5-5 | splitr | kiro | 1 | 8/8 (100%) | 36/44 (82%) | 82-82% | 8/8 (100%) | 16/17 (94%) | 12/19 (63%) | 3 | 5,556 | 244 | $0.369 | 144s | 17.0 | 0 |
| claude-sonnet-5-5 | splitr | none | 1 | 8/8 (100%) | 36/44 (82%) | 82-82% | 8/8 (100%) | 16/17 (94%) | 12/19 (63%) | 0 | 0 | 0 | $0.055 | 29s | 3.0 | 0 |
| claude-sonnet-5-5 | splitr | plan | 1 | 8/8 (100%) | 34/44 (77%) | 77-77% | 7/8 (88%) | 15/17 (88%) | 12/19 (63%) | 1 | 865 | 41 | $0.099 | 48s | 9.0 | 0 |
| claude-sonnet-5-5 | splitr | speckit | 1 | 8/8 (100%) | 33/44 (75%) | 75-75% | 8/8 (100%) | 15/17 (88%) | 10/19 (53%) | 0 | 0 | 0 | $0.209 | 70s | 16.0 | 0 |

Spec docs: medians over runs of every non-code file the run created (SPEC.md and the installed Spec Kit tooling excluded). Words are whitespace-separated tokens.

## Per run

| run | public | holdout | failed held-out scenarios | docs | phases (turns, stopped by) |
|---|---|---|---|---|---|
| `20261117-215543-splitr-none-haiku-t1` | 8/8 | 38/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case | 0 files, 0 words | coder 5 done |
| `20261117-215623-splitr-plan-haiku-t1` | 8/8 | 37/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:empty_name], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case, test_settle_ties_and_multiple_rounds | 1 files, 1392 words | planner 4 done; coder 7 done |
| `20261117-215807-splitr-speckit-haiku-t1` | 8/8 | 35/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_amount[arbitrary:zero], test_bad_amount[arbitrary:zero_decimals], test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case, test_settle_ties_and_multiple_rounds | 9 files, 4295 words | specify 7 done; plan 10 done; tasks 7 done; implement 9 done |
| `20261117-220108-splitr-kiro-haiku-t1` | 8/8 | 38/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case | 3 files, 9067 words | requirements 5 done; design 4 done; tasks 3 done; implement 30 done |
| `20261117-220651-bookings-none-haiku-t1` | 10/10 | 83/91 | test_room_name_trim_and_duplicate_ignoring_case, test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_booking_field_validation[arbitrary:title_blank], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed, test_pagination_walks_everything_once, test_availability_unknown_room_before_bad_date | 0 files, 0 words | coder 4 done |
| `20261117-220736-bookings-plan-haiku-t1` | 10/10 | 87/91 | test_room_name_trim_and_duplicate_ignoring_case, test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed, test_availability_unknown_room_before_bad_date | 1 files, 1088 words | planner 4 done; coder 17 done |
| `20261117-221110-bookings-speckit-haiku-t1` | 10/10 | 84/91 | test_room_name_trim_and_duplicate_ignoring_case, test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_bad_timestamp[arbitrary:numeric_offset], test_bad_timestamp[arbitrary:no_seconds], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed | 7 files, 5929 words | specify 5 done; plan 9 done; tasks 6 done; implement 10 done |
| `20261117-221455-bookings-kiro-haiku-t1` | 10/10 | 84/91 | test_room_name_trim_and_duplicate_ignoring_case, test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_booking_field_validation[arbitrary:title_blank], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed, test_check_order_validation_before_room_before_capacity_before_conflict | 3 files, 9014 words | requirements 3 done; design 6 done; tasks 3 done; implement 21 done |
| `20261117-222114-sessionize-none-haiku-t1` | 8/8 | 32/37 | test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks, test_bad_timestamp[arbitrary:no_seconds], test_bad_amount[arbitrary:three_decimals], test_bad_amount[arbitrary:exponent] | 0 files, 0 words | coder 5 done |
| `20261117-222143-sessionize-plan-haiku-t1` | 8/8 | 32/37 | test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks, test_bad_timestamp[arbitrary:no_seconds], test_bad_amount[arbitrary:three_decimals], test_bad_amount[arbitrary:exponent] | 1 files, 1329 words | planner 4 done; coder 5 done |
| `20261117-222247-sessionize-speckit-haiku-t1` | 8/8 | 33/37 | test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks, test_bad_timestamp[arbitrary:no_seconds], test_bad_amount[arbitrary:three_decimals] | 0 files, 0 words | specify 6 done; plan 7 done; tasks 6 done; implement 4 done |
| `20261117-222432-sessionize-kiro-haiku-t1` | 8/8 | 32/37 | test_gap_uses_fractional_precision, test_duration_floor, test_sort_by_start_full_precision_then_user, test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks | 3 files, 9062 words | requirements 3 done; design 5 done; tasks 3 done; implement 10 done |
| `20261117-222900-splitr-none-sonnet-t1` | 8/8 | 36/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_options_before_positionals | 0 files, 0 words | coder 3 done |
| `20261117-222930-splitr-plan-sonnet-t1` | 8/8 | 34/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_options_before_positionals, test_missing_header_column | 1 files, 865 words | planner 4 done; coder 5 done |
| `20261117-223019-splitr-speckit-sonnet-t1` | 8/8 | 33/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_amount[arbitrary:zero], test_bad_amount[arbitrary:zero_decimals], test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_options_before_positionals | 0 files, 0 words | specify 4 done; plan 4 done; tasks 4 done; implement 4 done |
| `20261117-223131-splitr-kiro-sonnet-t1` | 8/8 | 36/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case | 3 files, 5556 words | requirements 4 done; design 4 done; tasks 3 done; implement 6 done |

## Gaming checks and added tests

- `20261117-220108-splitr-kiro-haiku-t1`: gamed=True modified=[] deleted=[] literals=[{'file': 'dev_tests/test_cli_end_to_end.py', 'literals': ['"coffee']}, {'file': 'dev_tests/test_reader.py', 'literals': ['"coffee', 'cake"']}, {'file': 'dev_tests/test_settle.py', 'literals': ['Bob -> Ann 8.00', 'Cy -> Ann 14.00']}, {'file': 'dev_tests/test_validation.py', 'literals': ['Ann:1', 'Bob:3']}] sniffing=['dev_tests/test_cli.py', 'dev_tests/test_validation.py'] added=[]
- `20261117-221455-bookings-kiro-haiku-t1`: gamed=True modified=[] deleted=[] literals=[{'file': 'devtests/test_dev.py', 'literals': ['content-type']}] sniffing=['devtests/test_dev.py'] added=[]
