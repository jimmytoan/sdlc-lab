# splitr change request 1: repayments and payer-first rounding

The tool is in use. Two changes are needed. Everything in `SPEC.md` that is not mentioned here stays exactly as it is.

## 1. Repayments

The header may now contain an optional column `kind`. If the column is absent, every row is an expense, as before.

- `kind` is `expense`, `payment`, or empty (empty means `expense`). Any other value makes the row invalid with the reason `bad-kind`. This check runs after `bad-date` and before `bad-payer`.
- A `payment` row records that `payer` handed `amount` directly to one other person. Its `participants` field must be exactly one name, without a weight, and it must not be the payer. Otherwise the row is invalid with `bad-participants`.
- Effect on balances: the payer's balance goes up by `amount`, the recipient's balance goes down by `amount`. No splitting takes place.
- `--since` and `--until` apply to payment rows like any other row.

Example: Bob owes Ann 10.00. After the row `2026-03-05,Bob,10.00,Ann,,payment` (with a `kind` column last) both balances are `0.00`.

## 2. Payer-first rounding

Customers complained that the person who paid often also absorbs nothing of the rounding. New rule for leftover cents:

- If the payer is one of the participants of an expense and there is at least one leftover cent, the payer receives the first leftover cent.
- The remaining leftover cents are handed out as before (largest remainder first, ties by name ascending ignoring case), skipping the payer.
- If the payer is not a participant, nothing changes.

Example: Cy pays 10.00 for Ann, Bob and Cy. Shares are now Ann 3.33, Bob 3.33, Cy 3.34 (before this change Ann had 3.34).
