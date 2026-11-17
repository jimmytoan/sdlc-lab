# C1 report for tag `c1-pilot2`

16 finished runs, 0 errored (excluded below; listed at the end), spend $1.46 at list price (errored runs included). Records: `results/c1/runs.jsonl`. Spec level: the brief ticket for every run.

Time per run is wall time while other lab sessions shared the machine and the API limits; it is indicative only.

## By model and condition (specs pooled)

| model | condition | runs | public | holdout | holdout range | stated | implied | arbitrary | spec docs: files | words | lines | cost/run | time/run | calls/run | gamed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | kiro | 3 | 26/26 (100%) | 158/172 (92%) | 86-95% | 50/50 (100%) | 67/68 (99%) | 41/54 (76%) | 3 | 10,436 | 517 | $0.104 | 423s | 30.7 | 0 |
| claude-haiku-5-5 | none | 3 | 26/26 (100%) | 154/172 (90%) | 84-93% | 50/50 (100%) | 67/68 (99%) | 37/54 (69%) | 0 | 0 | 0 | $0.006 | 37s | 5.7 | 0 |
| claude-haiku-5-5 | plan | 3 | 26/26 (100%) | 158/172 (92%) | 86-95% | 50/50 (100%) | 68/68 (100%) | 40/54 (74%) | 1 | 1,733 | 109 | $0.016 | 101s | 11.7 | 0 |
| claude-haiku-5-5 | speckit | 3 | 26/26 (100%) | 151/172 (88%) | 84-91% | 49/50 (98%) | 67/68 (99%) | 35/54 (65%) | 8 | 8,599 | 535 | $0.056 | 263s | 35.7 | 0 |
| claude-sonnet-5-5 | kiro | 1 | 8/8 (100%) | 36/44 (82%) | 82-82% | 7/8 (88%) | 16/17 (94%) | 13/19 (68%) | 3 | 4,838 | 247 | $0.337 | 140s | 15.0 | 0 |
| claude-sonnet-5-5 | none | 1 | 8/8 (100%) | 35/44 (80%) | 80-80% | 7/8 (88%) | 16/17 (94%) | 12/19 (63%) | 0 | 0 | 0 | $0.065 | 36s | 4.0 | 0 |
| claude-sonnet-5-5 | plan | 1 | 8/8 (100%) | 34/44 (77%) | 77-77% | 7/8 (88%) | 15/17 (88%) | 12/19 (63%) | 1 | 688 | 36 | $0.113 | 57s | 7.0 | 0 |
| claude-sonnet-5-5 | speckit | 1 | 8/8 (100%) | 32/44 (73%) | 73-73% | 7/8 (88%) | 15/17 (88%) | 10/19 (53%) | 8 | 4,155 | 293 | $0.391 | 141s | 20.0 | 0 |

## By model, spec and condition

| model | spec | condition | runs | public | holdout | holdout range | stated | implied | arbitrary | spec docs: files | words | lines | cost/run | time/run | calls/run | gamed |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-haiku-5-5 | bookings | kiro | 1 | 10/10 (100%) | 86/91 (95%) | 95-95% | 35/35 (100%) | 34/34 (100%) | 17/22 (77%) | 3 | 11,207 | 517 | $0.169 | 510s | 32.0 | 0 |
| claude-haiku-5-5 | bookings | none | 1 | 10/10 (100%) | 85/91 (93%) | 93-93% | 35/35 (100%) | 34/34 (100%) | 16/22 (73%) | 0 | 0 | 0 | $0.006 | 38s | 5.0 | 0 |
| claude-haiku-5-5 | bookings | plan | 1 | 10/10 (100%) | 86/91 (95%) | 95-95% | 35/35 (100%) | 34/34 (100%) | 17/22 (77%) | 1 | 1,733 | 110 | $0.021 | 123s | 13.0 | 0 |
| claude-haiku-5-5 | bookings | speckit | 1 | 10/10 (100%) | 83/91 (91%) | 91-91% | 35/35 (100%) | 34/34 (100%) | 14/22 (64%) | 8 | 8,115 | 535 | $0.057 | 260s | 35.0 | 0 |
| claude-haiku-5-5 | sessionize | kiro | 1 | 8/8 (100%) | 34/37 (92%) | 92-92% | 7/7 (100%) | 17/17 (100%) | 10/13 (77%) | 6 | 8,277 | 423 | $0.075 | 381s | 38.0 | 0 |
| claude-haiku-5-5 | sessionize | none | 1 | 8/8 (100%) | 32/37 (86%) | 86-86% | 7/7 (100%) | 16/17 (94%) | 9/13 (69%) | 0 | 0 | 0 | $0.005 | 31s | 6.0 | 0 |
| claude-haiku-5-5 | sessionize | plan | 1 | 8/8 (100%) | 34/37 (92%) | 92-92% | 7/7 (100%) | 17/17 (100%) | 10/13 (77%) | 1 | 1,776 | 109 | $0.012 | 72s | 10.0 | 0 |
| claude-haiku-5-5 | sessionize | speckit | 1 | 8/8 (100%) | 31/37 (84%) | 84-84% | 7/7 (100%) | 17/17 (100%) | 7/13 (54%) | 9 | 9,318 | 604 | $0.060 | 288s | 37.0 | 0 |
| claude-haiku-5-5 | splitr | kiro | 1 | 8/8 (100%) | 38/44 (86%) | 86-86% | 8/8 (100%) | 16/17 (94%) | 14/19 (74%) | 3 | 10,436 | 697 | $0.069 | 377s | 22.0 | 0 |
| claude-haiku-5-5 | splitr | none | 1 | 8/8 (100%) | 37/44 (84%) | 84-84% | 8/8 (100%) | 17/17 (100%) | 12/19 (63%) | 0 | 0 | 0 | $0.007 | 44s | 6.0 | 0 |
| claude-haiku-5-5 | splitr | plan | 1 | 8/8 (100%) | 38/44 (86%) | 86-86% | 8/8 (100%) | 17/17 (100%) | 13/19 (68%) | 1 | 1,657 | 93 | $0.017 | 108s | 12.0 | 0 |
| claude-haiku-5-5 | splitr | speckit | 1 | 8/8 (100%) | 37/44 (84%) | 84-84% | 7/8 (88%) | 16/17 (94%) | 14/19 (74%) | 8 | 8,599 | 527 | $0.052 | 242s | 35.0 | 0 |
| claude-sonnet-5-5 | splitr | kiro | 1 | 8/8 (100%) | 36/44 (82%) | 82-82% | 7/8 (88%) | 16/17 (94%) | 13/19 (68%) | 3 | 4,838 | 247 | $0.337 | 140s | 15.0 | 0 |
| claude-sonnet-5-5 | splitr | none | 1 | 8/8 (100%) | 35/44 (80%) | 80-80% | 7/8 (88%) | 16/17 (94%) | 12/19 (63%) | 0 | 0 | 0 | $0.065 | 36s | 4.0 | 0 |
| claude-sonnet-5-5 | splitr | plan | 1 | 8/8 (100%) | 34/44 (77%) | 77-77% | 7/8 (88%) | 15/17 (88%) | 12/19 (63%) | 1 | 688 | 36 | $0.113 | 57s | 7.0 | 0 |
| claude-sonnet-5-5 | splitr | speckit | 1 | 8/8 (100%) | 32/44 (73%) | 73-73% | 7/8 (88%) | 15/17 (88%) | 10/19 (53%) | 8 | 4,155 | 293 | $0.391 | 141s | 20.0 | 0 |

Spec docs: medians over runs of every non-code file the run created (SPEC.md and the installed Spec Kit tooling excluded). Words are whitespace-separated tokens.

## Per run

| run | public | holdout | failed held-out scenarios | docs | phases (turns, stopped by) |
|---|---|---|---|---|---|
| `20261117-234903-splitr-none-haiku-t1` | 8/8 | 37/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case, test_settle_ties_and_multiple_rounds | 0 files, 0 words | coder 6 done |
| `20261117-234949-splitr-plan-haiku-t1` | 8/8 | 38/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case | 1 files, 1657 words | planner 4 done; coder 8 done |
| `20261117-235139-splitr-speckit-haiku-t1` | 8/8 | 37/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case, test_missing_header_column | 8 files, 8599 words | specify 8 done; plan 8 done; tasks 8 done; implement 11 done |
| `20261117-235542-splitr-kiro-haiku-t1` | 8/8 | 38/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_leftover_two_cents_alphabetical_ignoring_case | 3 files, 10436 words | requirements 3 done; design 5 done; tasks 3 done; implement 11 done |
| `20261118-000201-bookings-none-haiku-t1` | 10/10 | 85/91 | test_room_name_trim_and_duplicate_ignoring_case, test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed, test_availability_unknown_room_before_bad_date | 0 files, 0 words | coder 5 done |
| `20261118-000245-bookings-plan-haiku-t1` | 10/10 | 86/91 | test_room_name_trim_and_duplicate_ignoring_case, test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed | 1 files, 1733 words | planner 4 done; coder 9 done |
| `20261118-000455-bookings-speckit-haiku-t1` | 10/10 | 83/91 | test_room_name_trim_and_duplicate_ignoring_case, test_room_validation[arbitrary:name_blank], test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_booking_field_validation[arbitrary:title_blank], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed, test_check_order_validation_before_room_before_capacity_before_conflict | 8 files, 8115 words | specify 10 done; plan 8 done; tasks 4 done; implement 13 done |
| `20261118-000922-bookings-kiro-haiku-t1` | 10/10 | 86/91 | test_room_name_trim_and_duplicate_ignoring_case, test_bad_json[arbitrary:rooms_array], test_bad_json[arbitrary:bookings_array], test_booking_field_validation[arbitrary:title_101_chars], test_title_trimmed | 3 files, 11207 words | requirements 3 done; design 4 done; tasks 4 done; implement 21 done |
| `20261118-001758-sessionize-none-haiku-t1` | 8/8 | 32/37 | test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks, test_bad_timestamp[arbitrary:no_seconds], test_bad_amount[implied:empty], test_bad_amount[arbitrary:exponent] | 0 files, 0 words | coder 6 done |
| `20261118-001830-sessionize-plan-haiku-t1` | 8/8 | 34/37 | test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks, test_bad_timestamp[arbitrary:no_seconds] | 1 files, 1776 words | planner 4 done; coder 6 done |
| `20261118-001944-sessionize-speckit-haiku-t1` | 8/8 | 31/37 | test_duration_floor, test_sort_by_start_full_precision_then_user, test_reject_reasons_and_order_of_checks, test_bad_timestamp[arbitrary:no_seconds], test_bad_amount[arbitrary:three_decimals], test_bad_amount[arbitrary:exponent] | 9 files, 9318 words | specify 7 done; plan 8 done; tasks 6 done; implement 16 done |
| `20261118-002433-sessionize-kiro-haiku-t1` | 8/8 | 34/37 | test_sort_by_start_full_precision_then_user, test_dedupe_keeps_earliest_timestamp, test_reject_reasons_and_order_of_checks | 6 files, 8277 words | requirements 3 done; design 4 done; tasks 3 done; implement 28 done |
| `20261118-003057-splitr-none-sonnet-t1` | 8/8 | 35/44 | test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_options_before_positionals, test_missing_header_column | 0 files, 0 words | coder 4 done |
| `20261118-003134-splitr-plan-sonnet-t1` | 8/8 | 34/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_options_before_positionals, test_missing_header_column | 1 files, 688 words | planner 4 done; coder 3 done |
| `20261118-003232-splitr-speckit-sonnet-t1` | 8/8 | 32/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:weight_fraction], test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_leftover_goes_to_largest_remainder_with_weights, test_leftover_remainder_tie_by_name, test_options_before_positionals, test_missing_header_column | 8 files, 4155 words | specify 4 done; plan 5 done; tasks 7 done; implement 4 done |
| `20261118-003455-splitr-kiro-sonnet-t1` | 8/8 | 36/44 | test_comments_and_blank_lines_ignored, test_multiple_bad_rows_in_order_one_reason_each, test_bad_participants[arbitrary:same_person_twice], test_names_case_insensitive_first_spelling, test_sort_ignores_case, test_zero_balance_prints_unsigned, test_leftover_two_cents_alphabetical_ignoring_case, test_missing_header_column | 3 files, 4838 words | requirements 4 done; design 3 done; tasks 3 done; implement 5 done |

## Protocol

- every finished run left each phase's document and wrote no product code before its coding phase

## Gaming checks and added tests

- none
