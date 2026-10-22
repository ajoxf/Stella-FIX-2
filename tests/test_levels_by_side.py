"""A separate target and stop for H to L (a SELL) and L to H (a BUY).

Each direction may size its own TP and SL — % of the margin or a multiple of
the ATR — and a blank one is "same as both". Priced by the ONE `levels`
function the engine and the backtest both call."""
import pytest

from fixtrader import algo
from fixtrader.config import ContractConfig, TraderConfig
from tests.conftest import book_at_z, fill_candles
from tests.test_engine_signal import build

# k = 500 per 1.00 of price for the position; margin 1000; fees 0.02.
K, MARGIN, FEES = 500.0, 1000.0, 0.02


def test_each_direction_prices_its_own_percent_levels():
    p = algo.params_from_settings({
        'profit_target_pct': 2.0, 'stop_loss_pct': 1.0,
        'profit_target_pct_hl': 4.0, 'stop_loss_pct_hl': 3.0,      # H to L
        'profit_target_pct_lh': 1.0})                               # L to H
    be, tp, sl, why = algo.levels('SELL', 10.0, FEES, p, K, MARGIN, None)
    assert why is None and be == pytest.approx(9.98)
    assert tp == pytest.approx(9.98 - 0.04 * MARGIN / K)
    assert sl == pytest.approx(9.98 + 0.03 * MARGIN / K)
    be, tp, sl, why = algo.levels('BUY', 10.0, FEES, p, K, MARGIN, None)
    assert tp == pytest.approx(10.02 + 0.01 * MARGIN / K)
    assert sl == pytest.approx(10.02 - 0.01 * MARGIN / K)       # shared stop


def test_blank_is_same_as_both():
    """The control: nothing set per side, both directions read the shared
    levels — blanks and junk included, never as zero."""
    shared = algo.params_from_settings({'profit_target_pct': 2.0, 'stop_loss_pct': 1.0})
    blank = algo.params_from_settings({
        'profit_target_pct': 2.0, 'stop_loss_pct': 1.0,
        'profit_target_pct_hl': '', 'stop_loss_pct_lh': None,
        'target_mode_hl': '', 'stop_mode_lh': 'nonsense', 'atr_stop_mult_hl': 0})
    for side in ('BUY', 'SELL'):
        assert algo.levels(side, 10.0, FEES, blank, K, MARGIN, 0.3) == \
            algo.levels(side, 10.0, FEES, shared, K, MARGIN, 0.3)
    assert blank['sides'] == {'SELL': {}, 'BUY': {}}


def test_one_direction_in_atr_the_other_in_margin():
    p = algo.params_from_settings({
        'profit_target_pct': 2.0, 'stop_loss_pct': 1.0,
        'target_mode_hl': 'ATR', 'atr_target_mult_hl': 2.0,
        'stop_mode_hl': 'ATR', 'atr_stop_mult_hl': 1.0})
    atr = 0.10
    _, tp, sl, _ = algo.levels('SELL', 10.0, FEES, p, K, MARGIN, atr)
    assert tp == pytest.approx(9.98 - 2.0 * atr) and sl == pytest.approx(9.98 + 1.0 * atr)
    _, tp, sl, _ = algo.levels('BUY', 10.0, FEES, p, K, MARGIN, atr)
    assert tp == pytest.approx(10.02 + 0.02 * MARGIN / K)
    assert sl == pytest.approx(10.02 - 0.01 * MARGIN / K)


def test_the_levels_gate_holds_only_the_direction_it_cannot_price():
    """H to L in ATR with no ATR measured yet: H to L is held, L to H (in %
    of margin) may still enter. The shared case holds neither."""
    p = algo.params_from_settings({'target_mode_hl': 'ATR'})
    gate = algo.levels_gate(p, 10.0, FEES, K, MARGIN, None, 0.01)
    assert gate['SELL'] and 'ATR' in gate['SELL']
    assert gate['BUY'] is None
    shared = algo.levels_gate(algo.params_from_settings({}), 10.0, FEES, K, MARGIN, None, 0.01)
    assert shared == {'BUY': None, 'SELL': None}


def test_the_effective_settings_carry_the_per_side_fields():
    cfg = TraderConfig(path='unused.json')
    c = ContractConfig(key='x', profit_target_pct_hl='3.5', stop_mode_lh='atr',
                       target_mode_hl='junk')
    eff = c.settings_with_defaults(cfg.settings)
    assert eff['profit_target_pct_hl'] == 3.5
    assert eff['stop_mode_lh'] == 'ATR'
    assert eff['target_mode_hl'] is None                 # unknown: same as both
    assert eff['stop_loss_pct_lh'] is None               # unset: same as both


def test_an_algo_entry_freezes_its_own_directions_levels(tmp_path):
    """A real H to L entry through the engine takes the H to L target and
    stop; the shared ones are different and are not used."""
    engine, gw, _ = build(tmp_path, confirm_samples=1, stop_loss_on=True,
                          stop_loss_pct=2.0, profit_target_pct=3.0,
                          stop_loss_pct_hl=4.0, profit_target_pct_hl=5.0)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)                     # H to L: sell the bid
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    assert pos is not None and pos.side.value == 'SELL'
    per_point = 100.0 * 5                          # k per contract x 5 contracts
    assert pos.target_price == pytest.approx(pos.break_even - 0.05 * 1300 / per_point)
    assert pos.stop_price == pytest.approx(pos.break_even + 0.04 * 1300 / per_point)
