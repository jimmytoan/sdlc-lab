"""Black-box regression tests for splitr."""
import csv
import datetime as dt
import io
import json
import random
from fractions import Fraction

import pytest
from helpers import run

HEADER = 'date,payer,amount,participants,note\n'


def success(ledger, *args, raw=None):
    code, out, err = run(ledger, *args, raw=raw)
    assert (code, err) == (0, '')
    return out


def failure(ledger, expected, *args, code=2, raw=None):
    assert run(ledger, *args, raw=raw) == (code, '', expected)


def as_json(ledger, *args):
    return json.loads(success(ledger, *args, '--format', 'json'))


def test_csv_bom_reordered_comments_and_first_spelling():
    ledger = ('\ufeff  # before header\r\n\r\n'
              'note,participants,amount,date,payer,extra\r\n'
              '"pizza, with ""olives""",  bOb:2 ; ALICE : 1 ,0.05,2024-02-29,  ZoE  ,ignored\r\n'
              '   # ignored line\r\n'
              ',zOE;BOB,0.02,2024-03-01, alice ,x\r\n')
    assert success(ledger, 'balances', '{f}') == 'ALICE 0.00\nbOb -0.04\nZoE +0.04\n'
    assert as_json(ledger, 'balances', '{f}') == {'balances': [
        {'name': 'ALICE', 'cents': 0}, {'name': 'bOb', 'cents': -4}, {'name': 'ZoE', 'cents': 4}]}
    assert success(ledger, 'settle', '{f}') == 'bOb -> ZoE 0.04\n'


def test_first_appearance_and_zero_balances():
    ledger = HEADER + ('2025-01-01, aLiCe ,1.00,ALICE;  bOb ; CAROL,\n'
                       '2025-01-02,BOB,1.00,alice;bob;carol,\n'
                       '2025-01-03,carol,1.00,alice;bob;carol,\n'
                       '2025-01-04,Zero,0.01,zero,\n')
    assert success(ledger, 'balances', '{f}') == 'aLiCe -0.02\nbOb +0.01\nCAROL +0.01\nZero 0.00\n'
    assert success(ledger, 'settle', '{f}') == 'aLiCe -> bOb 0.01\naLiCe -> CAROL 0.01\n'


@pytest.mark.parametrize('amount,participants,expected', [
    ('0.01', 'zed;Amy;bob', {'Amy': -1, 'bob': 0, 'zed': 0}),
    ('0.02', 'zed;Amy;bob', {'Amy': -1, 'bob': -1, 'zed': 0}),
    ('0.05', 'Amy:1;bob:2;zed:3', {'Amy': -1, 'bob': -2, 'zed': -2}),
    ('0.02', 'Amy:1;bob:1;zed:3', {'Amy': -1, 'bob': 0, 'zed': -1}),
    ('0.07', 'Amy:5;bob:3;zed:2', {'Amy': -4, 'bob': -2, 'zed': -1}),
    ('12.5', 'Amy:100;bob:1;zed:1', {'Amy': -1226, 'bob': -12, 'zed': -12}),
])
def test_weighted_largest_remainders(amount, participants, expected):
    ledger = HEADER + f'2025-01-01,Payer,{amount},{participants},\n'
    data = as_json(ledger, 'balances', '{f}')['balances']
    got = {x['name']: x['cents'] for x in data}
    assert {name: got[name] for name in expected} == expected
    assert got['Payer'] == -sum(expected.values())
    assert sum(got.values()) == 0


def test_remainder_order_not_payer_or_participant_order():
    ledger = HEADER + '2025-01-01,Zed,0.01,Zed;Bob;Ann,\n'
    assert success(ledger, 'balances', '{f}') == 'Ann -0.01\nBob 0.00\nZed +0.01\n'


def test_settlement_extremes_recomputed_and_ties():
    ledger = HEADER + ('2025-01-01,Zed,5,Bob,\n2025-01-02,ann,5,Bob,\n'
                       '2025-01-03,Zed,2,Amy,\n2025-01-04,ann,2,Amy,\n')
    expected = [{'from': 'Bob', 'to': 'ann', 'cents': 700},
                {'from': 'Amy', 'to': 'Zed', 'cents': 400},
                {'from': 'Bob', 'to': 'Zed', 'cents': 300}]
    assert as_json(ledger, 'settle', '{f}') == {'transfers': expected}
    assert success(ledger, 'settle', '{f}') == 'Bob -> ann 7.00\nAmy -> Zed 4.00\nBob -> Zed 3.00\n'


def test_settlement_ties_on_both_sides():
    ledger = HEADER + '2024-01-01,Zoe,1,zack,\n2024-01-02,Ann,1,bob,\n'
    assert success(ledger, 'settle', '{f}') == 'bob -> Ann 1.00\nzack -> Zoe 1.00\n'


@pytest.mark.parametrize('ledger', [HEADER, HEADER + '# comment\n  \n',
                                   HEADER + '2024-01-01,Ann,1,ann,\n'])
def test_empty_or_zero_reports(ledger):
    expected = 'Ann 0.00\n' if '2024-' in ledger else ''
    assert success(ledger, 'balances', '{f}') == expected
    assert success(ledger, 'settle', '{f}') == 'nothing to settle\n'
    assert as_json(ledger, 'settle', '{f}') == {'transfers': []}
    if not expected:
        assert as_json(ledger, 'balances', '{f}') == {'balances': []}


FILTER_LEDGER = HEADER + ('2024-01-01,Old,1,Outside,\n2024-01-02,Alice,2,Bob,\n'
                          '2024-01-03,Bob,3,Carol,\n2024-01-04,Dave,4,Eve,\n')


@pytest.mark.parametrize('options,expected', [
    (['--since', '2024-01-02'], 'Alice +2.00\nBob +1.00\nCarol -3.00\nDave +4.00\nEve -4.00\n'),
    (['--until', '2024-01-02'], 'Alice +2.00\nBob -2.00\nOld +1.00\nOutside -1.00\n'),
    (['--since', '2024-01-02', '--until', '2024-01-03'], 'Alice +2.00\nBob +1.00\nCarol -3.00\n'),
    (['--since', '2024-01-03', '--until', '2024-01-03'], 'Bob +3.00\nCarol -3.00\n'),
    (['--since', '2024-02-01'], ''),
    (['--since', '2024-01-04', '--until', '2024-01-02'], ''),
])
def test_inclusive_date_filtering(options, expected):
    assert success(FILTER_LEDGER, 'balances', '{f}', *options) == expected


def test_filter_names_settlement_and_options_before_positionals():
    ledger = HEADER + '2024-01-01,ALICE,1,BOB,\n2024-01-02,alice,2,bob,\n'
    assert as_json(ledger, '--since', '2024-01-02', 'settle', '{f}') == {
        'transfers': [{'from': 'bob', 'to': 'alice', 'cents': 200}]}
    result = success(ledger, '--format=json', 'balances', '{f}', '--since=2024-01-02')
    assert json.loads(result) == {'balances': [{'name': 'alice', 'cents': 200},
                                               {'name': 'bob', 'cents': -200}]}


def test_validate_every_row_even_outside_range_and_first_failure():
    ledger = HEADER + ('bad,,0,,\n2024-01-01, ,0,,\n2024-01-02,Ann,0,,\n'
                       '2024-01-03,Ann,1,,\n2024-01-04,Ann,1,Bob,\n2024-01-05,Bob,wrong,Bob,\n')
    expected = ('line 2: bad-date\nline 3: bad-payer\nline 4: bad-amount\n'
                'line 5: bad-participants\nline 7: bad-amount\n')
    for command in ('balances', 'settle'):
        failure(ledger, expected, command, '{f}', '--since', '2024-01-04', '--until', '2024-01-04')


def invalid_values(column, values, reason):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    cols = ['date', 'payer', 'amount', 'participants', 'note']
    writer.writerow(cols)
    for value in values:
        fields = {'date': '2024-02-29', 'payer': 'Ann', 'amount': '1', 'participants': 'Bob', 'note': ''}
        fields[column] = value
        writer.writerow([fields[k] for k in cols])
    expected = ''.join(f'line {i}: {reason}\n' for i in range(2, len(values) + 2))
    failure(stream.getvalue(), expected, 'balances', '{f}')


def test_bad_dates():
    invalid_values('date', ['', '2023-02-29', '2024-02-30', '2024-13-01', '2024-00-01',
                            '2024-01-00', '2024-1-01', '24-01-01', '20240101', '2024/01/01',
                            '2024-01-01T00:00:00', '0000-01-01'], 'bad-date')


def test_bad_payers():
    invalid_values('payer', ['', ' ', '\t'], 'bad-payer')


def test_bad_amounts():
    invalid_values('amount', ['', '0', '0.0', '00.00', '-1', '+1', '1.', '.5', '1.234',
                              '1e2', '1E2', '1,000', '1 0', 'NaN', 'inf', '--1'], 'bad-amount')


def test_bad_participants():
    invalid_values('participants', ['', '  ', ';', 'Ann;', ';Ann', 'Ann;;Bob', 'Ann; ;Bob',
                                   ':2', 'Ann:', 'Ann:0', 'Ann:-1', 'Ann:+1', 'Ann:1.0',
                                   'Ann:1e2', 'Ann:1:2', 'Ann;ann', ' Ann :2 ; aNN:3'], 'bad-participants')


def test_valid_amounts_weights_and_whitespace():
    ledger = HEADER + ('2024-02-29, Payer , 0001.2 ,  A : 02 ; B ,\n'
                       '2024-03-01,payer,0.01,A:1;B:1,\n2024-03-02,PAYER,2,A,\n')
    assert success(ledger, 'balances', '{f}') == 'A -2.81\nB -0.40\nPayer +3.21\n'


def test_large_cents_exact_arithmetic():
    ledger = HEADER + '2024-01-01,Payer,10000000000000000.01,Bob;Ann,\n'
    assert success(ledger, 'balances', '{f}') == (
        'Ann -5000000000000000.01\nBob -5000000000000000.00\nPayer +10000000000000000.01\n')


def test_physical_line_numbers_with_comments_blank_and_crlf():
    ledger = ('# comment\r\n\r\n' + HEADER.replace('\n', '\r\n') +
              '2024-01-01,Ann,1,Bob,\r\n  # skipped\r\n \t\r\n'
              'bad,,0,,\r\n2024-01-01,,0,,\r\n')
    failure(ledger, 'line 7: bad-date\nline 8: bad-payer\n', 'balances', '{f}')


@pytest.mark.parametrize('header,missing', [
    ('', 'date'), ('# comment\n  \n', 'date'), ('payer,amount,participants,note\n', 'date'),
    ('date,amount,participants,note\n', 'payer'), ('date,payer,participants,note\n', 'amount'),
    ('date,payer,amount,note\n', 'participants'), ('date,payer,amount,participants\n', 'note'),
    ('date,note\n', 'payer'), ('Date,payer,amount,participants,note\n', 'date'),
])
def test_missing_headers(header, missing):
    failure(header + ('bad,,,,\n' if header and not header.startswith('#') else ''),
            f'header: missing {missing}\n', 'balances', '{f}')


def test_short_rows():
    ledger = HEADER + '2024-01-01\n2024-01-01,Ann\n2024-01-01,Ann,1\n'
    failure(ledger, 'line 2: bad-payer\nline 3: bad-amount\nline 4: bad-participants\n', 'settle', '{f}')


def test_unreadable_and_bad_utf8(tmp_path):
    missing = tmp_path / 'missing.csv'
    assert run('', 'balances', str(missing)) == (3, '', f'error: cannot read {missing}\n')
    failure('', 'error: cannot read {f}\n', 'settle', '{f}', code=3, raw=b'\xff\xfe\xff')


@pytest.mark.parametrize('args', [
    (), ('balances',), ('{f}',), ('unknown', '{f}'), ('balances', '{f}', 'extra'),
    ('balances', '{f}', '--wat'), ('balances', '{f}', '--format', 'xml'),
    ('balances', '{f}', '--format'), ('balances', '{f}', '--since'), ('balances', '{f}', '--until'),
    ('balances', '{f}', '--since', '2024-2-01'), ('balances', '{f}', '--since', '2023-02-29'),
    ('balances', '{f}', '--until', '2024-13-01'), ('--help',),
])
def test_usage_errors(args):
    rc, out, err = run(HEADER, *args)
    assert rc == 64 and out == '' and err.strip()


def test_reference_oracle_on_many_expenses_and_filtered_range():
    """Compare both reports with an independent rational-arithmetic model."""
    rng = random.Random(3854)
    people = ['Ada', 'bEN', 'Cleo', 'dOt', 'Eli', 'Fay', 'Gus', 'Hana']
    rows = []
    ledger = io.StringIO(newline='')
    writer = csv.writer(ledger, lineterminator='\n')
    writer.writerow(['date', 'payer', 'amount', 'participants', 'note'])
    for index in range(80):
        date = dt.date(2024, 1, 1) + dt.timedelta(days=index % 20)
        payer = rng.choice(people)
        parts = [(name, rng.randint(1, 9)) for name in rng.sample(people, rng.randint(1, 6))]
        cents = rng.randint(1, 12003)
        def spelling(s):
            return rng.choice([s, s.lower(), s.upper()])
        payer = spelling(payer)
        parts = [(spelling(n), w) for n, w in parts]
        rows.append((date, payer, cents, parts))
        writer.writerow([date.isoformat(), payer, f'{cents // 100}.{cents % 100:02d}',
                         ';'.join(f'{name}:{weight}' for name, weight in parts), 'note, quoted'])

    for options, since, until in [([], None, None),
                                  (['--since', '2024-01-06', '--until', '2024-01-14'],
                                   dt.date(2024, 1, 6), dt.date(2024, 1, 14))]:
        names, balances = {}, {}
        for date, payer, cents, parts in rows:
            if (since and date < since) or (until and date > until):
                continue
            for name in [payer] + [p[0] for p in parts]:
                names.setdefault(name.lower(), name)
                balances.setdefault(name.lower(), 0)
            balances[payer.lower()] += cents
            weight_sum = sum(w for _, w in parts)
            quotas = [(name.lower(), Fraction(cents * w, weight_sum)) for name, w in parts]
            floors = {name: int(q) for name, q in quotas}
            extra = cents - sum(floors.values())
            order = sorted(quotas, key=lambda item: (-(item[1] - int(item[1])), item[0]))
            for name, _ in order[:extra]:
                floors[name] += 1
            for name, share in floors.items():
                balances[name] -= share
        expected = [{'name': names[k], 'cents': balances[k]} for k in sorted(names)]
        data = as_json(ledger.getvalue(), 'balances', '{f}', *options)
        assert data == {'balances': expected}
        assert all(type(row['cents']) is int for row in data['balances'])
        assert sum(balances.values()) == 0
        transfers = []
        while any(balances.values()):
            debtor = min(balances, key=lambda k: (balances[k], k))
            creditor = min(balances, key=lambda k: (-balances[k], k))
            cents = min(-balances[debtor], balances[creditor])
            assert cents > 0
            transfers.append({'from': names[debtor], 'to': names[creditor], 'cents': cents})
            balances[debtor] += cents
            balances[creditor] -= cents
        assert as_json(ledger.getvalue(), 'settle', '{f}', *options) == {'transfers': transfers}
