"""Round trip, break-even, target — and the em dash where a figure is missing."""
import pytest

from fixtrader import costs
from fixtrader.models import Side, TargetBasis


BASE = {
    'commission_per_contract': 1.20,
    'exchange_fee_per_contract': 0.55,
    'clearing_fee_per_contract': 0.15,
    'slippage_budget_ticks': 0.0,
    'profit_target_pct': 2.0,
    'profit_target_basis': TargetBasis.MARGIN,
}


def test_fees_are_charged_per_contract_per_side():
    # (1.20 + 0.55 + 0.15) x 2 sides x 5 contracts
    total = costs.round_trip_money(5, 1.0, 1.20, 0.55, 0.15, 0.0)
    assert total == pytest.approx(19.0)


def test_slippage_budget_is_charged_both_sides():
    # 0.5 ticks x 2 sides x $1.00 a tick x 5 = $5 on top of the $19 of fees
    total = costs.round_trip_money(5, 1.0, 1.20, 0.55, 0.15, 0.5)
    assert total == pytest.approx(24.0)


def test_a_slippage_budget_without_a_tick_value_refuses_to_answer():
    """Returning the fees alone would report a cost that is knowably
    incomplete, and the edge filter would then pass on it."""
    assert costs.round_trip_money(5, None, 1.20, 0.55, 0.15, 0.5) is None
    # ...with no budget there is nothing to price, so it can still answer.
    assert costs.round_trip_money(5, None, 1.20, 0.55, 0.15, 0.0) == pytest.approx(19.0)


def test_break_even_sits_above_a_long_and_below_a_short():
    """Get this sign wrong and every target is on the wrong side of entry."""
    long_be = costs.break_even(0.6000, Side.BUY, 5, 0.01, 1.0, BASE)
    short_be = costs.break_even(0.6000, Side.SELL, 5, 0.01, 1.0, BASE)
    # $19 over 5 contracts at $100 a point = 0.038
    assert long_be == pytest.approx(0.6380)
    assert short_be == pytest.approx(0.5620)


def test_target_is_break_even_plus_a_percentage_of_margin():
    """The desk's answer: a percentage of what the position ties up."""
    tgt = costs.target_price(0.6000, Side.BUY, 5, 0.01, 1.0, BASE,
                             margin_locked=7250.0)
    # 2% of $7,250 = $145 -> 0.29 of spread over 5 contracts at $100/point
    assert tgt == pytest.approx(0.6380 + 0.29)


def test_a_short_target_is_below_its_break_even():
    tgt = costs.target_price(0.6000, Side.SELL, 5, 0.01, 1.0, BASE,
                             margin_locked=7250.0)
    assert tgt == pytest.approx(0.5620 - 0.29)


def test_no_margin_means_no_target_rather_than_a_guess():
    """It must NOT silently fall back to notional: the number would change
    meaning while going on looking like the same number."""
    assert costs.target_price(0.6000, Side.BUY, 5, 0.01, 1.0, BASE,
                              margin_locked=None,
                              contract_multiplier=100.0) is None
    why = costs.missing_for_target(BASE, None, 100.0, 0.08, 1.0)
    assert 'margin' in why


def test_the_other_bases_work_when_they_are_asked_for():
    notional = dict(BASE, profit_target_basis=TargetBasis.NOTIONAL)
    tgt = costs.target_price(0.6000, Side.BUY, 5, 0.01, 1.0, notional,
                             contract_multiplier=100.0)
    assert tgt is not None
    sigma = dict(BASE, profit_target_basis=TargetBasis.ENTRY_SIGMA)
    tgt2 = costs.target_price(0.6000, Side.BUY, 5, 0.01, 1.0, sigma,
                              entry_std=0.086)
    assert tgt2 is not None


def test_a_target_of_zero_percent_means_leave_at_break_even():
    """Which is a real instruction, and a different one from 'no target'."""
    zero = dict(BASE, profit_target_pct=0.0)
    tgt = costs.target_price(0.6000, Side.BUY, 5, 0.01, 1.0, zero,
                             margin_locked=7250.0)
    assert tgt == pytest.approx(0.6380)


def test_missing_tick_value_is_named_before_anything_else():
    why = costs.missing_for_target(BASE, 7250.0, 100.0, 0.08, None)
    assert 'tick value' in why


def test_net_pnl_without_reported_fees_is_none_not_gross():
    """A net P&L that quietly means gross is what makes a losing system look
    profitable."""
    r = costs.net_pnl(Side.SELL, 5, 0.6930, 0.6465, 0.01, 1.0, fees_paid=None)
    assert r['gross'] == pytest.approx(23.25)
    assert r['net'] is None
    r2 = costs.net_pnl(Side.SELL, 5, 0.6930, 0.6465, 0.01, 1.0, fees_paid=19.0)
    assert r2['net'] == pytest.approx(4.25)


def test_a_short_that_moves_against_it_loses():
    r = costs.net_pnl(Side.SELL, 5, 0.6000, 0.6500, 0.01, 1.0, fees_paid=19.0)
    assert r['gross'] == pytest.approx(-25.0)
    assert r['net'] == pytest.approx(-44.0)
