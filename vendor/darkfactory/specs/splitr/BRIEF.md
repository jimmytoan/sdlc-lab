# splitr: shared-expense settlement CLI (ticket)

We share costs on trips and need a small command line tool that tells us who owes whom.

- `python splitr.py <command> <ledger.csv> [options]`, Python 3.13, standard library only.
- Commands: `balances` and `settle`.

## Ledger

A CSV file with a header row: `date,payer,amount,participants,note`. Dates are `YYYY-MM-DD`, amounts look like `12.50`. `participants` is a list of names separated by `;`. A name can have a weight, `name:weight`, when someone should carry a bigger part. Lines starting with `#` are comments.

## What it should do

Each expense is split between its participants according to their weights, to the cent. The cents must always add up exactly. A person's balance is what they paid minus their shares.

`balances` prints one line per person, sorted by name: `<name> <amount>`, for example `Ann +22.00` or `Bob -8.00`.

`settle` prints the payments that clear everything: the person who owes the most pays the person who is owed the most, and so on until everyone is at zero. One line each: `Bob -> Ann 8.00`. If nobody owes anything, print `nothing to settle`.

Options:

- `--format text|json` (default text). JSON for balances: `{"balances": [{"name": "Ann", "cents": 2200}]}`. JSON for settle: `{"transfers": [{"from": "Bob", "to": "Ann", "cents": 800}]}`.
- `--since YYYY-MM-DD` and `--until YYYY-MM-DD` to look at a date range only (inclusive).

## Errors

- Bad rows: print `line <N>: <reason>` to stderr for each, nothing on stdout, exit code 2. Reasons: `bad-date`, `bad-payer`, `bad-amount`, `bad-participants`.
- Header without a required column: `header: missing <column>` on stderr, exit code 2.
- File cannot be read: `error: cannot read <path>` on stderr, exit code 3.
- Wrong usage (unknown command, bad option): exit code 64.
