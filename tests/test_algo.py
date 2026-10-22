"""The Algo's core, ported from the MT5 desk onto ONE contract: the band,
the signal, the filters, the levels and the backtest."""
import ast
import math
import os
import random
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import algo, algofilters, backtest, bands


def params(**over):
    p = algo.params_from_settings({})
    p.update(over)
    return p


def band(closes, length=20):
    c = bands.SpreadCandles(60.0, length)
    c.seed([(i * 60.0, v) for i, v in enumerate(closes)])
    return c.stats()


def alternating(n=40, mid=0.5, swing=0.1):
    return [mid + (swing if i % 2 else -swing) for i in range(n)]


# -- the band ---------------------------------------------------------------

def test_the_middle_is_pines_ema_seeded_with_an_sma():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    alpha = 2.0 / 4.0
    expected = sum(values[:3]) / 3.0
    for v in values[3:]:
        expected = alpha * v + (1 - alpha) * expected
    assert bands.ema(values, 3) == pytest.approx(expected)


def test_sigma_is_the_population_stdev():
    assert bands.population_stdev([1.0, 3.0]) == pytest.approx(1.0)


def test_too_few_candles_is_not_ready_and_says_how_many():
    stats = band(alternating(7))
    assert stats['ready'] is False and stats['count'] == 7
    assert stats['needed'] == 20 and stats['mean'] is None


def test_a_flat_series_has_no_band():
    stats = band([0.5] * 30)
    assert stats['ready'] is False and 'not moved' in stats['note']


def test_the_forming_candle_counts():
    c = bands.SpreadCandles(60.0, 3)
    c.seed([(0.0, 1.0), (60.0, 2.0)])
    c.observe(125.0, 9.0)
    assert c.closes() == [1.0, 2.0, 9.0]
    c.observe(130.0, 3.0)                     # same candle: its close moves
    assert c.closes() == [1.0, 2.0, 3.0]
    closed = c.observe(185.0, 4.0)            # next candle closes the last
    assert closed == (120.0, 3.0)


def test_candles_from_the_recording_close_on_the_last_mid_and_skip_gaps():
    t0 = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
    rows = [(t0, 1.0), (t0 + timedelta(seconds=30), 1.5),
            (t0 + timedelta(minutes=5), 2.0)]
    out = bands.candles_from_samples(rows, 60.0)
    assert [c for _, c in out] == [1.5, 2.0]          # no filled-in minutes


# -- the signal ----------------------------------------------------------------

def md(bid, ask, quote):
    return {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2.0,
            'quote_id': quote}


def ready_stats(mean=0.5, sigma=0.1):
    return {'ready': True, 'mean': mean, 'sigma': sigma, 'count': 20,
            'needed': 20}


def test_a_short_is_decided_on_the_BID_and_a_long_on_the_OFFER():
    s = algo.AlgoSignal(params(entry_z=2.0, reentry_on=False,
                               confirm_ticks=1))
    # mid z 2.0 exactly, but the bid is under it: no sale yet
    body = s.evaluate(0, md(0.69, 0.73, 1), ready_stats())
    assert body['signal'] is None
    body = s.evaluate(1, md(0.71, 0.73, 2), ready_stats())
    assert body['signal'] == 'SELL'
    assert body['intents'][0]['price'] == 0.71          # sold into the bid
    s2 = algo.AlgoSignal(params(entry_z=2.0, reentry_on=False,
                                confirm_ticks=1))
    body = s2.evaluate(0, md(0.28, 0.30, 1), ready_stats())
    assert body['signal'] == 'BUY' and body['intents'][0]['price'] == 0.30


def test_an_intent_is_reported_once_not_every_pass():
    s = algo.AlgoSignal(params(entry_z=2.0, reentry_on=False,
                               confirm_ticks=1))
    first = s.evaluate(0, md(0.72, 0.73, 1), ready_stats())
    again = s.evaluate(1, md(0.72, 0.73, 1), ready_stats())
    assert len(first['intents']) == 1 and again['intents'] == []


def test_confirmation_needs_fresh_quotes():
    s = algo.AlgoSignal(params(entry_z=2.0, reentry_on=False,
                               confirm_ticks=3))
    for _ in range(5):
        body = s.evaluate(0, md(0.72, 0.73, 1), ready_stats())
    assert body['signal'] is None and body['state'] == 'CONFIRMING'
    s.evaluate(0, md(0.72, 0.73, 2), ready_stats())
    body = s.evaluate(0, md(0.72, 0.73, 3), ready_stats())
    assert body['signal'] == 'SELL'


def test_past_the_cap_is_a_blow_out_not_an_entry():
    s = algo.AlgoSignal(params(entry_z=2.0, reentry_on=False,
                               confirm_ticks=1, max_entry_z=3.0))
    body = s.evaluate(0, md(0.85, 0.86, 1), ready_stats())
    assert body['signal'] is None and 'blow-out' in body['blocked']
    assert body['blocked_side'] == 'SELL'
    # the control: no cap
    s = algo.AlgoSignal(params(entry_z=2.0, reentry_on=False,
                               confirm_ticks=1, max_entry_z=0))
    assert s.evaluate(0, md(0.85, 0.86, 1), ready_stats())['signal'] == 'SELL'


def test_the_reentry_window():
    assert algo.reentry_window(params(entry_z=2.0, reentry_back=0.5,
                                      reentry_window_pct=50.0)) == \
        pytest.approx((1.5, 0.75))


def position(side, entry, tp=None, sl=None, be=None, opened_at=0.0):
    return {'position_id': 7, 'side': side, 'entry': entry, 'tp': tp,
            'sl': sl, 'break_even': be, 'opened_at': opened_at,
            'net_pnl': None}


def test_exits_read_the_CLOSING_side():
    s = algo.AlgoSignal(params())
    # a long closes on the bid: the offer at the target is not enough
    body = s.evaluate(0, md(0.59, 0.61, 1), ready_stats(),
                      [position('BUY', 0.50, tp=0.60, sl=0.40)])
    assert body['positions'][0]['exit'] is None
    body = s.evaluate(1, md(0.60, 0.62, 2), ready_stats(),
                      [position('BUY', 0.50, tp=0.60, sl=0.40)])
    assert body['positions'][0]['exit'] == 'PROFIT_TARGET'


def test_the_stop_loss_closes_a_short():
    s = algo.AlgoSignal(params())
    body = s.evaluate(0, md(0.65, 0.66, 1), ready_stats(),
                      [position('SELL', 0.60, tp=0.50, sl=0.65)])
    assert body['positions'][0]['exit'] == 'STOP_LOSS'


def test_a_gate_never_holds_back_an_exit():
    s = algo.AlgoSignal(params())
    gates = {'health': 'stale', 'mode': 'KILL ALL is on',
             'halt': 'the day is done'}
    body = s.evaluate(0, md(0.65, 0.66, 1), ready_stats(),
                      [position('SELL', 0.60, tp=0.50, sl=0.65)], gates)
    assert body['positions'][0]['exit'] == 'STOP_LOSS'
    assert body['intents'][0]['action'] == 'EXIT'


def test_the_mean_exit_only_in_profit():
    s = algo.AlgoSignal(params(reversion_on=True))
    # back at the mean but under break-even: stays
    body = s.evaluate(0, md(0.50, 0.51, 1), ready_stats(),
                      [position('BUY', 0.49, be=0.505)])
    assert body['positions'][0]['exit'] is None
    body = s.evaluate(1, md(0.51, 0.52, 2), ready_stats(),
                      [position('BUY', 0.49, be=0.505)])
    assert body['positions'][0]['exit'] == 'MEAN_REVERSION'


def test_the_optional_exits_are_off_by_default():
    p = algo.params_from_settings({})
    assert p['stop_z_on'] is False and p['reversion_on'] is False
    assert p['time_stop_min'] == 0
    assert p['stop_loss_on'] is True


def test_the_time_stop():
    s = algo.AlgoSignal(params(time_stop_min=30))
    body = s.evaluate(1801, md(0.50, 0.51, 1), ready_stats(),
                      [position('BUY', 0.49, opened_at=0.0)])
    assert body['positions'][0]['exit'] == 'TIME_STOP'


def test_a_closed_position_starts_the_cooldown():
    s = algo.AlgoSignal(params(cooldown_min=5, reentry_on=False,
                               confirm_ticks=1, entry_z=2.0))
    s.evaluate(0, md(0.50, 0.51, 1), ready_stats(),
               [position('BUY', 0.49)])
    body = s.evaluate(10, md(0.72, 0.73, 2), ready_stats(), [])
    assert body['signal'] is None and 'cooldown' in body['blocked']


def test_the_settings_map_onto_the_rule():
    p = algo.params_from_settings({'trade_direction': 'SELL_ONLY',
                                   'entry_threshold': '',
                                   'entry_cooldown_seconds': 120,
                                   'timeframe_min': 7})
    assert p['direction'] == 'H_TO_L'
    assert p['entry_z'] == 2.5                     # blank is the default
    assert p['cooldown_min'] == 2.0
    assert p['timeframe_min'] == 15                # not a candle size


# -- the filters ----------------------------------------------------------------

def test_the_round_trip_is_unknown_when_a_part_is():
    cost = algofilters.round_trip_cost(0.01, 100.0, 1, commission=None)
    assert cost['total'] is None and cost['crossing'] == pytest.approx(1.0)


def test_an_edge_nobody_could_price_blocks():
    p = params()
    _, check = algo.judge_filters(p, md(0.70, 0.72, 1), ready_stats(),
                                  alternating(), {'k': 100.0,
                                                  'commission': None})
    assert 'not priced' in check('SELL', 2.0)


def test_the_edge_filter_and_its_control():
    p = params(regime_on=False, trend_on=False, edge_multiple=1.5,
               edge_capture_frac=0.5)
    cost_in = {'k': 100.0, 'commission': 2.0, 'slippage': 0.0}
    _, check = algo.judge_filters(p, md(0.70, 0.71, 1), ready_stats(),
                                  alternating(), cost_in)
    # capture = 0.5 x 2 x 0.1 x 100 = 10; cost = 1 + 2 = 3: 3.3x
    assert check('SELL', 2.0) is None
    _, check = algo.judge_filters(dict(p, edge_multiple=5.0),
                                  md(0.70, 0.71, 1), ready_stats(),
                                  alternating(), cost_in)
    assert 'edge filter' in check('SELL', 2.0)


def test_a_trending_series_is_named():
    closes = [0.5 + 0.01 * i for i in range(40)]
    assert algofilters.regime(closes)['state'] == 'TRENDING'
    assert algofilters.regime(alternating())['state'] == 'RANGE'


def test_the_trend_filter_blocks_against_the_drift_only():
    closes = [0.5 + 0.01 * i for i in range(60)]
    p = params(regime_on=False, edge_on=False, trend_on=True,
               trend_sigma=1.0, trend_lookback_min=60, timeframe_min=15)
    _, check = algo.judge_filters(p, md(0.70, 0.71, 1),
                                  ready_stats(sigma=0.02), closes, {})
    assert 'ROSE' in check('SELL', 2.0)
    assert check('BUY', -2.0) is None


def test_atr_is_wilders_close_to_close():
    closes = [1.0, 2.0, 1.0, 2.0, 1.0]
    assert algofilters.atr(closes, 2) == pytest.approx(1.0)
    assert algofilters.atr(closes[:2], 2) is None


# -- the levels -----------------------------------------------------------------

def test_levels_from_break_even_in_margin_mode():
    p = params(target_pct=2.0, stop_loss_pct=1.0)
    be, tp, sl, why = algo.levels('BUY', 10.0, 0.02, p, 500.0, 1000.0, None)
    assert why is None
    assert be == pytest.approx(10.02)
    assert tp == pytest.approx(10.02 + 0.02 * 1000 / 500)
    assert sl == pytest.approx(10.02 - 0.01 * 1000 / 500)


def test_levels_without_a_margin_are_not_priced():
    be, tp, sl, why = algo.levels('SELL', 10.0, 0.02, params(), 500.0,
                                  None, None)
    assert tp is None and sl is None and 'no margin' in why


def test_levels_in_atr_mode_need_an_atr():
    p = params(stop_mode='ATR', target_mode='ATR')
    assert 'ATR' in algo.levels('BUY', 10.0, 0.0, p, 500.0, 1000.0, None)[3]
    be, tp, sl, why = algo.levels('SELL', 10.0, 0.0, p, 500.0, None, 0.2)
    assert why is None
    assert tp == pytest.approx(10.0 - 1.5 * 0.2)
    assert sl == pytest.approx(10.0 + 2.0 * 0.2)


def test_progress_runs_from_the_stop_to_the_target():
    assert algo.progress('BUY', 10.0, 10.5, 11.0, 9.0) == pytest.approx(0.5)
    assert algo.progress('SELL', 10.0, 10.5, 9.0, 11.0) == pytest.approx(-0.5)
    assert algo.progress('BUY', 10.0, None, 11.0, 9.0) is None


# -- the backtest -------------------------------------------------------------------

def reverting(n=600, seed=7):
    rng = random.Random(seed)
    px, rows = 0.60, []
    for i in range(n):
        px += (0.60 - px) * 0.25 + rng.gauss(0, 0.02)
        rows.append((i * 900.0, round(px, 4)))
    return rows


def test_the_backtest_trades_a_reverting_series_and_states_its_limits():
    p = params(regime_on=False, trend_on=False, edge_on=False,
               reentry_on=False, max_trades_day=0, max_losses_row=0,
               cooldown_min=0)
    out = backtest.run(reverting(), p, width=0.002, k=100.0, fee_points=0.0,
                       commission=0.0, margin=500.0)
    assert out['summary']['trades'] > 0
    assert out['summary']['candles'] == 600
    assert any('inside a candle' in c for c in out['caveats'])
    assert any('recorded' in c for c in out['caveats'])


def test_a_backtest_without_a_margin_holds_every_entry_and_says_why():
    """The control: the levels cannot be priced, so nothing is entered."""
    p = params(regime_on=False, trend_on=False, edge_on=False,
               reentry_on=False)
    out = backtest.run(reverting(), p, width=0.002, k=100.0, fee_points=0.0,
                       commission=0.0, margin=None)
    assert out['summary']['trades'] == 0
    assert any('margin' in reason for reason in out['held'])


# -- nothing here reaches an order ------------------------------------------------

@pytest.mark.parametrize('name', ['algo', 'algofilters', 'algodesk', 'bands',
                                  'backtest'])
def test_the_deciding_modules_cannot_reach_an_order(name):
    path = os.path.join(os.path.dirname(__file__), '..', 'fixtrader',
                        name + '.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or '')
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = {'executor', 'gateway', 'manual_terminal', 'fake_gateway',
                 'engine'}
    assert not (imported & forbidden), imported & forbidden
