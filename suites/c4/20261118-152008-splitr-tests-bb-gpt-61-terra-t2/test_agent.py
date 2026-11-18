import csv
import io
import json
import random

import pytest

from helpers import run, lines


HEADER = "date,payer,amount,participants,note\n"


def invoke(ledger, *args, raw=None):
    return run(ledger, *args, raw=raw)


def report(ledger, command, *opts):
    code, out, err = invoke(ledger, command, "{f}", *opts)
    assert code == 0, (out, err)
    assert err == ""
    return out


def test_csv_bom_comments_header_reordering_and_first_spelling():
    ledger = ('\ufeff  # before header\r\n\r\n'
              'note,participants,amount,payer,date\r\n'
              '"meal, with cake",  bOb ; ALICE ,10.01,  Alice  ,2024-02-29\r\n'
              ' # comment\r\n'
              'taxi,alice;BOB,2,bOB,2024-03-01\r\n')
    assert lines(report(ledger, 'balances')) == ['Alice +4.00', 'bOb -4.00']
    assert lines(report(ledger, 'settle')) == ['bOb -> Alice 4.00']


def test_payer_before_participants_and_zero_balance_people():
    ledger = HEADER + ('2025-01-01, zED ,0.01,zed; ALICE;bob,\n'
                       '2025-01-02,ALICE,1,alice,\n')
    assert lines(report(ledger, 'balances')) == ['ALICE -0.01', 'bob 0.00', 'zED +0.01']
    assert lines(report(ledger, 'settle')) == ['ALICE -> zED 0.01']


@pytest.mark.parametrize('amount,participants,expected', [
    ('0.01', 'Zoe;amy;Bob', ['amy -0.01', 'Bob 0.00', 'Payer +0.01', 'Zoe 0.00']),
    ('0.02', 'Zoe;amy;Bob', ['amy -0.01', 'Bob -0.01', 'Payer +0.02', 'Zoe 0.00']),
    ('0.05', 'A:1;B:2;C:3', ['A -0.01', 'B -0.02', 'C -0.02', 'Payer +0.05']),
    ('0.02', 'A:1;B:3;C:2', ['A 0.00', 'B -0.01', 'C -0.01', 'Payer +0.02']),
    ('12.5', 'A:3;B:1', ['A -9.38', 'B -3.12', 'Payer +12.50']),
    ('1', 'Payer:2;B:1', ['B -0.33', 'Payer +0.33']),
])
def test_splitting_cents_and_weights(amount, participants, expected):
    ledger = HEADER + f'2025-01-01,Payer,{amount},{participants},\n'
    assert lines(report(ledger, 'balances')) == expected


def test_settlement_reselects_extremes_and_resolves_ties():
    # Equal-sized debtors and creditors are selected alphabetically.
    ledger = HEADER + ('2025-01-01,CredB,7,debB,\n'
                       '2025-01-01,CredA,7,debA,\n'
                       '2025-01-01,CredC,4,debC,\n')
    assert lines(report(ledger, 'settle')) == [
        'debA -> CredA 7.00', 'debB -> CredB 7.00', 'debC -> CredC 4.00']
    ledger = HEADER + ('2025-01-01,A,10,X,\n'
                       '2025-01-01,B,8,Y,\n'
                       '2025-01-01,C,7,Z,\n'
                       '2025-01-01,A,2,Y,\n')
    assert lines(report(ledger, 'settle')) == [
        'X -> A 10.00', 'Y -> B 8.00', 'Z -> C 7.00', 'Y -> A 2.00']


def test_empty_and_zero_outputs_text_and_json():
    for ledger in [HEADER, HEADER + '2024-01-01,A,1,A,\n']:
        expected = [] if ledger == HEADER else [{'name': 'A', 'cents': 0}]
        assert json.loads(report(ledger, 'balances', '--format', 'json')) == {'balances': expected}
        assert json.loads(report(ledger, 'settle', '--format', 'json')) == {'transfers': []}
        assert report(ledger, 'settle') == 'nothing to settle\n'
    assert report(HEADER, 'balances') == ''
    assert report(HEADER + '2024-01-01,A,1,A,\n', 'balances') == 'A 0.00\n'


def test_json_transfers_and_option_positions():
    ledger = HEADER + '2025-01-01,Ann,3,Bob;Cy,\n'
    code, out, err = invoke(ledger, '--format', 'json', 'settle', '--since', '2025-01-01', '{f}', '--until', '2025-01-01')
    assert (code, err) == (0, '')
    assert json.loads(out) == {'transfers': [
        {'from': 'Bob', 'to': 'Ann', 'cents': 150},
        {'from': 'Cy', 'to': 'Ann', 'cents': 150}]}


FILTER_LEDGER = HEADER + ('2024-01-01,Old,1,Other,\n'
                          '2024-02-01,Ann,2,Bob,\n'
                          '2024-02-02,Bob,1,Ann,\n'
                          '2024-03-01,Future,3,Else,\n')


@pytest.mark.parametrize('opts,expected', [
    (['--since', '2024-02-01', '--until', '2024-02-02'], ['Ann +1.00', 'Bob -1.00']),
    (['--since', '2024-02-02'], ['Ann -1.00', 'Bob +1.00', 'Else -3.00', 'Future +3.00']),
    (['--until', '2024-01-01'], ['Old +1.00', 'Other -1.00']),
    (['--since', '2025-01-01'], []),
])
def test_date_filters(opts, expected):
    assert lines(report(FILTER_LEDGER, 'balances', *opts)) == expected


def test_filtered_out_invalid_rows_are_still_reported():
    ledger = HEADER + ('2020-01-01,X,no,X,\n'
                       '2024-02-01,Ann,2,Bob,\n'
                       '2030-01-01,Y,1,Y:0,\n')
    for command in ('balances', 'settle'):
        code, out, err = invoke(ledger, command, '{f}', '--since', '2024-01-01', '--until', '2024-12-31')
        assert (code, out, lines(err)) == (2, '', ['line 2: bad-amount', 'line 4: bad-participants'])


@pytest.mark.parametrize('bad_date', ['2023-02-29', '2024-02-30', '2024-13-01', '2024-00-01', '2024-01-00', '2024-04-31', 'not-a-date', '24-01-01', '2024/01/01', '2024-1-01', '2024-01-1', ''])
def test_bad_row_dates(bad_date):
    code, out, err = invoke(HEADER + f'{bad_date},A,1,A,\n', 'balances', '{f}')
    assert (code, out, lines(err)) == (2, '', ['line 2: bad-date'])


@pytest.mark.parametrize('amount', ['0', '0.00', '0.000', '-1', '+1', '1.234', '1e2', 'NaN', 'inf', '.5', '1.', 'abc', '"1,000"', ''])
def test_bad_amounts(amount):
    code, out, err = invoke(HEADER + f'2024-01-01,A,{amount},A,\n', 'balances', '{f}')
    assert (code, out, lines(err)) == (2, '', ['line 2: bad-amount'])


@pytest.mark.parametrize('participants', ['', '  ', ';A', 'A;', 'A;;B', 'A; a ', 'A:0', 'A:-1', 'A:+1', 'A:1.5', 'A:x', 'A:', ':2', 'A:1; a:2'])
def test_bad_participants(participants):
    code, out, err = invoke(HEADER + f'2024-01-01,A,1,{participants},\n', 'settle', '{f}')
    assert (code, out, lines(err)) == (2, '', ['line 2: bad-participants'])


def test_all_row_errors_order_precedence_and_physical_lines():
    ledger = ('# initial comment\n\n' + HEADER +
              'invalid, ,wrong,,\n'
              '2024-01-01,  ,wrong,,\n'
              '# ignored\n'
              '   \n'
              '2024-01-01,A,wrong,,\n'
              '2024-01-01,A,1,A; a,\n'
              '2024-01-01,B,1,B,\n')
    for command in ['balances', 'settle']:
        code, out, err = invoke(ledger, command, '{f}')
        assert (code, out, err) == (2, '',
            'line 4: bad-date\nline 5: bad-payer\nline 8: bad-amount\nline 9: bad-participants\n')


@pytest.mark.parametrize('columns,missing', [
    ('payer,amount,participants,note', 'date'),
    ('date,amount,participants,note', 'payer'),
    ('date,payer,participants,note', 'amount'),
    ('date,payer,amount,note', 'participants'),
    ('date,payer,amount,participants', 'note'),
    ('note', 'date'),
    ('date,note', 'payer'),
])
def test_missing_header_precedes_row_errors(columns, missing):
    code, out, err = invoke(columns + '\ninvalid,row\n', 'balances', '{f}')
    assert (code, out, lines(err)) == (2, '', [f'header: missing {missing}'])


def test_unreadable_file():
    code, out, err = invoke('', 'balances', '{f}/does-not-exist')
    assert (code, out, lines(err)) == (3, '', ['error: cannot read {f}/does-not-exist'])


@pytest.mark.parametrize('args', [
    (), ('balances',), ('{f}',), ('bad-command', '{f}'),
    ('balances', '{f}', '--unknown'),
    ('balances', '{f}', '--format', 'xml'),
    ('balances', '{f}', '--format'),
    ('balances', '{f}', '--since', '2024-02-30'),
    ('balances', '{f}', '--until', '2024/01/01'),
    ('balances', '{f}', '--since'),
    ('settle', '{f}', '--until'),
])
def test_usage_errors(args):
    code, out, err = invoke(HEADER, *args)
    assert code == 64 and out == '' and err


def reference(rows, since=None, until=None):
    names = {}
    balances = {}
    for date, payer, cents, participants in rows:
        if (since and date < since) or (until and date > until):
            continue
        def person(name):
            key = name.strip().lower()
            names.setdefault(key, name.strip())
            balances.setdefault(key, 0)
            return key
        payer_key = person(payer)
        balances[payer_key] += cents
        weighted = [(person(name), weight) for name, weight in participants]
        total = sum(weight for _, weight in weighted)
        shares = {key: cents * weight // total for key, weight in weighted}
        remaining = cents - sum(shares.values())
        order = sorted(weighted, key=lambda x: (-(cents * x[1] % total), x[0]))
        for key, _ in order[:remaining]:
            shares[key] += 1
        for key, share in shares.items():
            balances[key] -= share
    result = [{'name': names[k], 'cents': balances[k]} for k in sorted(balances)]
    transfers = []
    while any(balances.values()):
        debtor = min((k for k in balances if balances[k] < 0), key=lambda k: (balances[k], k))
        creditor = min((k for k in balances if balances[k] > 0), key=lambda k: (-balances[k], k))
        amount = min(-balances[debtor], balances[creditor])
        transfers.append({'from': names[debtor], 'to': names[creditor], 'cents': amount})
        balances[debtor] += amount
        balances[creditor] -= amount
    return result, transfers


def test_mixed_ledger_against_independent_integer_reference():
    rng = random.Random(14409)
    people = ['Anna', 'Bert', 'Celia', 'Dora', 'Eli', 'Finn', 'Gus', 'Hana']
    rows = []
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\n')
    writer.writerow(['date', 'payer', 'amount', 'participants', 'note'])
    for _ in range(80):
        date = f'2024-{rng.randrange(1, 5):02d}-{rng.randrange(1, 29):02d}'
        payer = rng.choice(people)
        payer = payer.upper() if rng.randrange(3) == 0 else payer
        cents = rng.randrange(1, 20000)
        participants = [(n.lower() if rng.randrange(2) else n, rng.randrange(1, 8))
                        for n in rng.sample(people, rng.randrange(1, len(people) + 1))]
        rows.append((date, payer, cents, participants))
        writer.writerow([date, payer, f'{cents // 100}.{cents % 100:02d}',
                         ';'.join(f'{n}:{w}' for n, w in participants), 'food, drink'])
    ledger = buffer.getvalue()
    for since, until in [(None, None), ('2024-02-01', '2024-03-12'), ('2024-03-01', None), (None, '2024-01-15')]:
        opts = ['--format', 'json']
        if since:
            opts += ['--since', since]
        if until:
            opts += ['--until', until]
        balances, transfers = reference(rows, since, until)
        assert sum(x['cents'] for x in balances) == 0
        assert json.loads(report(ledger, 'balances', *opts)) == {'balances': balances}
        assert json.loads(report(ledger, 'settle', *opts)) == {'transfers': transfers}
        text_opts = opts[2:]
        def money(cents):
            return f'{abs(cents) // 100}.{abs(cents) % 100:02d}'
        balance_text = ''.join(
            f"{item['name']} {'+' if item['cents'] > 0 else '-' if item['cents'] < 0 else ''}{money(item['cents'])}\n"
            for item in balances)
        transfer_text = ''.join(
            f"{item['from']} -> {item['to']} {money(item['cents'])}\n"
            for item in transfers) or 'nothing to settle\n'
        assert report(ledger, 'balances', *text_opts) == balance_text
        assert report(ledger, 'settle', *text_opts) == transfer_text


def test_extra_header_columns_large_amount_and_quoted_note():
    ledger = ('extra,note,payer,date,participants,amount\n'
              'ignored,"a note, with a ""quote""",Alice,2000-02-29,Alice;Bob,999999999999999999999.99\n')
    assert report(ledger, 'balances') == (
        'Alice +499999999999999999999.99\nBob -499999999999999999999.99\n')
    assert report(ledger, 'settle') == 'Bob -> Alice 499999999999999999999.99\n'


def test_first_appearance_and_case_insensitive_identity_across_rows():
    ledger = HEADER + ('2024-01-01,  tOM  ,1, Bob ; tom,\n'
                       '2024-01-02,BOB,2, TOM ;carol; CARL,\n'
                       '2024-01-03,carol,1,bOb,\n')
    assert json.loads(report(ledger, 'balances', '--format', 'json')) == {
        'balances': [
            {'name': 'Bob', 'cents': 50},
            {'name': 'CARL', 'cents': -67},
            {'name': 'carol', 'cents': 33},
            {'name': 'tOM', 'cents': -16},
        ]}
