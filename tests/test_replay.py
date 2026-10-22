"""The replay says what a DIFFERENT setting would have done to the same
market. Everything here turns on it being honest about what it does not
know: the book was not recorded, the costs are budgeted, and there is no
queue."""
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import bands
from fixtrader import replay as replay_mod
from fixtrader.config import DEFAULT_SETTINGS


def settings(**over):
    """Effective settings, as a contract would hand them over: one-minute
    candles, the band at N=20, entry on the touch at 2.0."""
    from tests.conftest import ALGO_TEST_SETTINGS
    base = dict(DEFAULT_SETTINGS)
    base = {
        **ALGO_TEST_SETTINGS,
        'entry_threshold': 2.0, 'max_entry_z': 9.0, 'confirm_samples': 1,
        'trade_direction': 'BOTH', 'stop_loss_z': 4.0, 'max_hold_minutes': 0,
        'exit_at_mean': False, 'margin_per_contract': 260.0,
        'quantity': 5.0, 'entry_cooldown_seconds': 0,
        'commission_per_contract': 1.0, 'exchange_fee_per_contract': 0.0,
        'clearing_fee_per_contract': 0.0, 'slippage_budget_ticks': 0.0,
        'profit_target_pct': 1.0, 'stop_loss_on': True, 'stop_loss_pct': 4.0,
        'target_mode': 'MARGIN', 'stop_mode': 'MARGIN',
        'entry_order_type': 'MARKET', 'exit_order_type': 'MARKET',
    }
    base.update(over)
    return base


def a_reverting_series(n=900, seed=7, minutes_apart=1.0, kick=0.03):
    """A mean-reverting mid, one sample a minute, with a fixed seed — what
    the replay finds is repeatable rather than lucky."""
    rng = random.Random(seed)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    px, rows = 0.60, []
    for i in range(n):
        px += (0.60 - px) * 0.3 + rng.gauss(0, kick)
        rows.append((t0 + timedelta(minutes=i * minutes_apart), round(px, 4)))
    return rows


def test_a_reverting_series_produces_trades_and_a_net_after_costs():
    out = replay_mod.replay(a_reverting_series(), settings(),
                            tick_size=0.01, tick_value=1.0, contract_key='fef')
    assert out['warm'] is True
    assert out['summary']['trades'] > 0
    for t in out['trades']:
        assert t['entry_z'] is not None and abs(t['entry_z']) >= 2.0
        assert t['net'] is not None and t['gross'] is not None
        assert t['costs'] > 0                      # the round trip is charged
        assert t['net'] == pytest.approx(t['gross'] - t['costs'])


def test_a_series_too_short_to_fill_the_band_says_so_rather_than_zero():
    """Zero trades because nothing triggered and zero trades because the
    band never filled are different statements."""
    out = replay_mod.replay(a_reverting_series(n=10), settings(), 0.01, 1.0)
    assert out['warm'] is False
    assert out['summary']['trades'] == 0
    assert 'the band needs 20' in out['blocked_by']

    warm = replay_mod.replay(a_reverting_series(),
                             settings(entry_threshold=99.0, max_entry_z=0),
                             0.01, 1.0)
    assert warm['warm'] is True                    # the control
    assert warm['summary']['trades'] == 0
    assert 'threshold' in warm['blocked_by']


def test_nothing_recorded_is_not_a_flat_result():
    out = replay_mod.replay([], settings(), 0.01, 1.0)
    assert out['summary']['net'] is None           # not 0.0
    assert 'nothing recorded' in out['blocked_by']


def test_every_result_carries_the_assumptions_it_ran_under():
    out = replay_mod.replay(a_reverting_series(), settings(), 0.01, 1.0)
    a = out['assumptions']
    assert a['assumed_spread_ticks'] == replay_mod.DEFAULT_ASSUMED_SPREAD_TICKS
    assert 'not recorded' in a['book']
    assert 'queue' in a['fills']
    assert 'BUDGET' in a['costs']


def test_the_replay_never_reports_a_measured_slippage():
    out = replay_mod.replay(a_reverting_series(),
                            settings(slippage_budget_ticks=2.0), 0.01, 1.0)
    assert out['summary']['slippage_measured'] is None
    assert out['assumptions']['round_trip_money'] > 0


def test_entries_and_exits_are_priced_on_the_executable_side():
    """A short is sold on the bid and bought back on the offer: every price
    is the candle's mid moved half the ASSUMED spread against the trade."""
    rows = a_reverting_series()
    closes = dict(bands.candles_from_samples(rows, 60.0))
    half = 3.0 * 0.01 / 2
    out = replay_mod.replay(rows, settings(), 0.01, 1.0,
                            assumed_spread_ticks=3.0)
    assert out['trades']
    for t in out['trades']:
        mid_in = closes[t['opened_at'] - 60.0]
        mid_out = closes[t['closed_at'] - 60.0]
        if t['side'] == 'SELL':
            assert t['entry'] == pytest.approx(mid_in - half)
            assert t['exit_price'] == pytest.approx(mid_out + half)
        else:
            assert t['entry'] == pytest.approx(mid_in + half)
            assert t['exit_price'] == pytest.approx(mid_out - half)


def test_a_position_open_at_the_end_is_excluded_and_counted():
    """An unclosed trade has no P&L, and pretending it has one at the last
    price is marking your own homework."""
    rows = a_reverting_series(n=200)
    last = rows[-1][0]
    rows += [(last + timedelta(minutes=1), 0.80)]       # enters, and stops
    out = replay_mod.replay(rows, settings(stop_loss_on=False), 0.01, 1.0)
    assert out['still_open'] == 1
    assert all(t['closed_at'] and t['exit_price'] is not None
               for t in out['trades'])


def test_a_sweep_names_the_level_that_PAID_not_the_one_that_reverted():
    rows = replay_mod.sweep(a_reverting_series(n=1500), settings(),
                            0.01, 1.0, thresholds=[1.0, 1.5, 2.0, 2.5])
    assert [r['entry_threshold'] for r in rows['rows']] == [1.0, 1.5, 2.0, 2.5]
    best = rows['best']
    if best is not None:
        assert best['net'] > 0 and best['enough_to_judge'] is True
        for r in rows['rows']:
            if r['net'] is not None and r['net'] > best['net']:
                assert not r['enough_to_judge']


def test_a_sweep_row_with_too_few_trades_is_never_the_best():
    """One lucky trade with a percentage sign after it is not a finding."""
    rows = [
        {'entry_threshold': 1.0, 'net': 5000.0, 'enough_to_judge': False},
        {'entry_threshold': 2.0, 'net': 120.0, 'enough_to_judge': True},
    ]
    assert replay_mod.best_of(rows)['entry_threshold'] == 2.0
    # and where nothing clears its costs, there is no best
    assert replay_mod.best_of(
        [{'entry_threshold': 1.0, 'net': -50.0, 'enough_to_judge': True}]) is None


def test_a_filter_that_withheld_every_entry_says_SO_not_the_threshold():
    """The edge filter can withhold every entry at every threshold, and
    reporting 'nothing crossed the threshold' sends the desk to change the
    number that was never the problem. The replay carries the Algo's own
    words."""
    blocked = replay_mod.replay(
        a_reverting_series(), settings(edge_on=True, edge_multiple=1000.0),
        0.01, 1.0)
    assert blocked['summary']['trades'] == 0
    assert 'edge filter' in blocked['blocked_by']
    assert blocked['withheld']

    passing = replay_mod.replay(                      # the control
        a_reverting_series(), settings(edge_on=False), 0.01, 1.0)
    assert passing['summary']['trades'] > 0


# -- a target that is a percentage of MARGIN ----------------------------------

def test_no_margin_anywhere_withholds_entries_and_says_so():
    out = replay_mod.replay(a_reverting_series(),
                            settings(margin_per_contract=0.0),
                            0.01, 1.0, contract_key='fef')
    assert out['summary']['trades'] == 0
    assert 'no margin entered' in out['blocked_by']
    assert 'not recorded' in out['assumptions']['margin']


def test_a_recorded_margin_stands_in_where_none_was_entered():
    out = replay_mod.replay(a_reverting_series(),
                            settings(margin_per_contract=0.0), 0.01, 1.0,
                            contract_key='fef', margin_per_contract=260.0)
    assert out['summary']['trades'] > 0
    assert '260.00 per contract' in out['assumptions']['margin']


def test_an_entered_margin_wins_over_a_recorded_one():
    out = replay_mod.replay(a_reverting_series(),
                            settings(margin_per_contract=20.0),
                            0.01, 1.0, margin_per_contract=260.0)
    assert '20.00 per contract, as entered' in out['assumptions']['margin']


def test_a_sweep_carries_the_margin_to_every_threshold():
    rows = a_reverting_series()
    s = settings(margin_per_contract=0.0)
    without = replay_mod.sweep(rows, s, 0.01, 1.0, thresholds=[1.5, 2.0])
    assert all(r['trades'] == 0 for r in without['rows'])
    assert 'no margin entered' in without['blocked_by']
    priced = replay_mod.sweep(rows, s, 0.01, 1.0, thresholds=[1.5, 2.0],
                              margin_per_contract=260.0)
    assert all(r['trades'] > 0 for r in priced['rows'])
    assert priced['blocked_by'] is None


def test_the_margin_the_replay_reads_is_what_was_charged(tmp_path):
    from fixtrader.database import Database
    from fixtrader.models import Position, Side
    db = Database(str(tmp_path / 'm.db'))
    assert db.margin_per_contract('fef') is None       # nothing recorded
    db.save_position(Position(contract_key='fef', side=Side.SELL, qty=0.0,
                              opened_qty=5.0, avg_price=0.6,
                              margin_locked=1300.0))
    db.save_position(Position(contract_key='other', side=Side.SELL, qty=2.0,
                              opened_qty=2.0, avg_price=0.6,
                              margin_locked=9000.0))
    # per contract, from the size it was OPENED at — `qty` is what remains
    assert db.margin_per_contract('fef') == pytest.approx(260.0)
