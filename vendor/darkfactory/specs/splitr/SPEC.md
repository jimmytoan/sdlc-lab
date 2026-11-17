# splitr: shared-expense settlement CLI

Build a command line tool that reads a ledger of shared expenses and reports who owes whom.

- Entry point: `python splitr.py <command> <ledger.csv> [options]`
- Python 3.13, standard library only. No network.
- Commands: `balances`, `settle`.

## Ledger file

A UTF-8 CSV file. It may start with a byte order mark (BOM). The first CSV row is the header and must contain the columns `date`, `payer`, `amount`, `participants`, `note`. Columns may come in any order. Fields may be quoted in the usual CSV way (a quoted `note` may contain commas). Fields never contain line breaks. Lines end with `\n` or `\r\n`.

A line that is empty, contains only whitespace, or whose first non-space character is `#` is ignored. Ignored lines still count when numbering lines.

Each remaining row is one expense:

| Column | Rule |
| --- | --- |
| `date` | `YYYY-MM-DD`, a real calendar date. |
| `payer` | A person's name, not empty after trimming. |
| `amount` | A positive decimal number with at most two fraction digits: `12`, `12.5`, `12.50`. No sign, no thousands separator, no exponent. Zero is not allowed. |
| `participants` | One or more names separated by `;`. A name may carry a weight as `name:weight`, where weight is a positive integer. The default weight is 1. Listing the same person twice in one row is not allowed. The payer does not have to be a participant. |
| `note` | Free text, may be empty. |

Names are trimmed. Two names are the same person if they are equal ignoring case. In output, a person is shown with the spelling of their first appearance in the file (reading rows top to bottom, `payer` before `participants`, participants left to right).

## Splitting rule

All arithmetic is in whole cents. For an expense of `C` cents with weights `w_i` (sum `W`):

1. Each participant's base share is `floor(C * w_i / W)`.
2. The leftover cents (`C` minus the sum of base shares) are handed out one cent each, to the participants with the largest remainder `(C * w_i) mod W` first. Ties are broken by name, ascending, ignoring case.

A person's balance is what they paid minus the sum of their shares. Balances always sum to zero.

## Commands

### `balances`

Prints one line per person who appears in the considered rows (as payer or participant), sorted by name ascending ignoring case:

```
<name> <amount>
```

`<amount>` has a sign and two decimals: `+12.50`, `-3.00`. A zero balance prints as `0.00` with no sign.

### `settle`

Prints the transfers that clear all balances, produced by this exact procedure:

1. The debtor is the person with the most negative balance. The creditor is the person with the most positive balance. Ties are broken by name, ascending, ignoring case.
2. The debtor pays the creditor the smaller of the two absolute balances. Print `<debtor> -> <creditor> <amount>` with `<amount>` unsigned with two decimals, for example `Bob -> Ann 12.50`.
3. Repeat until every balance is zero.

If there is nothing to transfer, print exactly `nothing to settle`.

## Options

- `--format text|json`. Default `text`. With `json`, stdout is one JSON document:
  - `balances`: `{"balances": [{"name": "Ann", "cents": 1250}, ...]}` in the same order as the text output, `cents` a signed integer.
  - `settle`: `{"transfers": [{"from": "Bob", "to": "Ann", "cents": 1250}, ...]}`. With nothing to settle the list is empty.
- `--since YYYY-MM-DD` and `--until YYYY-MM-DD`. Only rows whose date is within the inclusive range are considered. Either may be given alone. Validation (below) still applies to every row of the file, including rows outside the range. People who only appear in rows outside the range are not printed.

Options may come before or after the positional arguments.

## Errors and exit codes

| Situation | stdout | stderr | Exit code |
| --- | --- | --- | --- |
| Success | the report | empty | 0 |
| One or more invalid rows | empty | one line per invalid row | 2 |
| Header is missing a required column | empty | `header: missing <column>` | 2 |
| Ledger file cannot be read | empty | `error: cannot read <path>` | 3 |
| Usage error: unknown command, missing argument, unknown option, bad `--format` value, bad `--since`/`--until` date | empty | any message | 64 |

Invalid rows: for each invalid row print `line <N>: <reason>` to stderr, in ascending line order. `<N>` is the 1-based physical line number in the file (the header is normally line 1; ignored lines count). Report one reason per row: the first check that fails, in this order.

1. `bad-date`
2. `bad-payer` (empty after trimming)
3. `bad-amount`
4. `bad-participants` (empty, an empty name, a weight that is not a positive integer, or a repeated person)

A missing header column is reported for the first missing column in the order `date`, `payer`, `amount`, `participants`, `note`, and nothing else is reported.

A ledger with a valid header and no expenses is valid: `balances` prints nothing and `settle` prints `nothing to settle`.
