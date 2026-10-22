"""The one conversion. Every money figure on the screen runs through it."""
import pytest

from fixtrader import sizing


def test_money_per_point_is_tick_value_over_tick_size():
    # Iron ore: 0.01 tick worth $1.00 -> one whole point is $100.
    assert sizing.money_per_point(0.01, 1.0) == 100.0
    # Brent: 0.01 tick worth $10.00 -> one point is $1,000.
    assert sizing.money_per_point(0.01, 10.0) == 1000.0
    # A50: 2.5-point tick worth $2.50 -> one index point is $1.
    assert sizing.money_per_point(2.5, 2.5) == 1.0


def test_a_missing_spec_is_none_and_never_zero():
    """A 0.0 here would price every cost in the system at nothing, and a
    break-even of 'what you paid' would look perfectly correct."""
    assert sizing.money_per_point(None, 1.0) is None
    assert sizing.money_per_point(0.01, None) is None
    assert sizing.money_per_point(0.0, 1.0) is None
    assert sizing.money_per_point(0.01, -1.0) is None
    assert sizing.to_money(1.0, None, 1.0) is None
    assert sizing.to_points(100.0, 0.01, None) is None


def test_money_and_points_round_trip():
    money = sizing.to_money(0.0465, 0.01, 1.0, qty=5)
    assert money == pytest.approx(23.25)
    back = sizing.to_points(money, 0.01, 1.0, qty=5)
    assert back == pytest.approx(0.0465)


def test_different_tick_values_do_not_share_an_answer():
    """The regression this module exists for: pricing one contract's move in
    another contract's units. Same 0.05 move, ten times the money."""
    fef = sizing.to_money(0.05, 0.01, 1.0, qty=1)
    brent = sizing.to_money(0.05, 0.01, 10.0, qty=1)
    assert fef == pytest.approx(5.0)
    assert brent == pytest.approx(50.0)


def test_to_points_with_zero_quantity_has_no_answer():
    """Zero would claim the move is free; raising would take the screen down."""
    assert sizing.to_points(100.0, 0.01, 1.0, qty=0) is None


def test_limits_round_away_from_the_market():
    """Rounding a resting order the wrong way turns it into a crossing one."""
    assert sizing.round_to_tick(0.6849, 0.01, 'down') == 0.68
    assert sizing.round_to_tick(0.6801, 0.01, 'up') == 0.69
    assert sizing.round_to_tick(0.6849, 0.01, 'nearest') == 0.68


def test_rounding_leaves_no_binary_dust():
    """A screen of four-decimal prices must not print 0.6899999999999999."""
    assert sizing.round_to_tick(0.69, 0.01) == 0.69
    assert repr(sizing.round_to_tick(0.07, 0.01)) == '0.07'


def test_quantity_rounds_down_to_the_step():
    """Rounding UP would send more than was asked for — on a close, that is
    how an exit becomes a reversal."""
    assert sizing.clamp_qty(4.9, qty_step=1.0) == 4.0
    assert sizing.clamp_qty(7.0, max_qty=5.0) == 5.0
    assert sizing.clamp_qty(0.5, min_qty=1.0) == 0.0
