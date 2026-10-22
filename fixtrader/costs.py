"""What a round trip costs, where break-even is, and where the target is.

The three are one chain and they are here together because separating them is
how they drift apart:

    round trip  = (commission + exchange + clearing) x 2 sides x qty
                + slippage_budget_ticks x 2 sides x tick_value x qty

    break-even  = fill +/- round_trip / (money_per_point x qty)
    target      = break-even +/- (pct/100 x basis) / (money_per_point x qty)

Two rules the whole module is built to keep:

- **Unmeasured is not zero.** No tick value, or no margin from the venue,
  means no break-even and no target: return None and let the screen render an
  em dash. A target of 0.00 reads as "get out at break-even", which is a
  different instruction, and a break-even of "the price you paid" reads as
  correct while being a lie.
- **The slippage figure here is a BUDGET, not a measurement.** The Analysis
  window reports the measured figure beside it so this gets corrected from
  data. Default it to 0 rather than inventing a cost and charging it against
  every trade the operator never agreed to.
"""

from typing import Any, Dict, Optional

from . import sizing
from .models import Side, TargetBasis


def round_trip_money(qty: float, tick_value: Optional[float],
                     commission_per_contract: float = 0.0,
                     exchange_fee_per_contract: float = 0.0,
                     clearing_fee_per_contract: float = 0.0,
                     slippage_budget_ticks: float = 0.0) -> Optional[float]:
    """Money to open AND close `qty` contracts, both sides counted.

    Fees are charged per contract per side, so each appears twice. Slippage is
    a per-side tick budget and is also charged twice.
    """
    if qty is None or qty <= 0:
        return None
    per_side_fees = (float(commission_per_contract or 0.0)
                     + float(exchange_fee_per_contract or 0.0)
                     + float(clearing_fee_per_contract or 0.0))
    fees = per_side_fees * 2.0 * qty
    if slippage_budget_ticks:
        if not tick_value:
            # A slippage budget in ticks cannot be priced without the tick's
            # value. Refuse rather than charge the fees alone and call it the
            # round trip — the edge filter would then pass on a cost that is
            # knowably incomplete.
            return None
        fees += float(slippage_budget_ticks) * 2.0 * float(tick_value) * qty
    return fees


def configured_margin(settings: Dict[str, Any], qty: float) -> Optional[float]:
    """The margin `qty` contracts tie up, as the OPERATOR entered it.

    TT does not report margin, so the profit target's base is a number the
    trader types per contract. None where it has not been entered — 0 is not
    a margin, it is a blank that would make every target read "leave at
    break-even".
    """
    per = settings.get('margin_per_contract')
    try:
        per = float(per) if per is not None else None
    except (TypeError, ValueError):
        per = None
    if not per or per <= 0 or not qty or qty <= 0:
        return None
    return per * float(qty)


def round_trip_for(qty: float, tick_value: Optional[float],
                   settings: Dict[str, Any]) -> Optional[float]:
    """The configured round trip for `qty` contracts."""
    return round_trip_money(
        qty, tick_value,
        settings.get('commission_per_contract', 0.0),
        settings.get('exchange_fee_per_contract', 0.0),
        settings.get('clearing_fee_per_contract', 0.0),
        settings.get('slippage_budget_ticks', 0.0))


def open_net(side: Side, qty: float, entry_price: Optional[float],
             close_price: Optional[float], tick_size: Optional[float],
             tick_value: Optional[float],
             settings: Dict[str, Any]) -> Optional[float]:
    """What an open position would make NET if closed at `close_price` — the
    executable side — after the whole round trip. None where either price or
    the cost cannot be priced."""
    if entry_price is None or close_price is None:
        return None
    fees = round_trip_for(qty, tick_value, settings)
    if fees is None:
        return None
    return net_pnl(side, qty, entry_price, close_price, tick_size,
                   tick_value, fees_paid=fees)['net']


def cost_breakdown(qty: float, tick_size: Optional[float],
                   tick_value: Optional[float],
                   settings: Dict[str, Any]) -> Dict[str, Any]:
    """The round trip, split so the Analysis window can report each part."""
    commission = float(settings.get('commission_per_contract', 0.0) or 0.0)
    exchange = float(settings.get('exchange_fee_per_contract', 0.0) or 0.0)
    clearing = float(settings.get('clearing_fee_per_contract', 0.0) or 0.0)
    slip_ticks = float(settings.get('slippage_budget_ticks', 0.0) or 0.0)

    total = round_trip_money(qty, tick_value, commission, exchange, clearing,
                             slip_ticks)
    slip_money = (slip_ticks * 2.0 * tick_value * qty
                  if (slip_ticks and tick_value and qty) else
                  (0.0 if not slip_ticks else None))
    return {
        'commission': commission * 2.0 * qty if qty else None,
        'exchange': exchange * 2.0 * qty if qty else None,
        'clearing': clearing * 2.0 * qty if qty else None,
        'slippage_budget': slip_money,
        'round_trip_money': total,
        'round_trip_points': sizing.to_points(total, tick_size, tick_value, qty),
        'round_trip_ticks': sizing.to_ticks(
            sizing.to_points(total, tick_size, tick_value, qty), tick_size),
    }


def break_even(fill_price: Optional[float], side: Side, qty: float,
               tick_size: Optional[float], tick_value: Optional[float],
               settings: Dict[str, Any]) -> Optional[float]:
    """The price at which this position is worth exactly nothing.

    A LONG has to come back UP through its costs, so break-even is above the
    fill; a SHORT's is below. Get this sign wrong and every target on the
    screen is on the wrong side of the entry.
    """
    if fill_price is None:
        return None
    total = round_trip_money(
        qty, tick_value,
        settings.get('commission_per_contract', 0.0),
        settings.get('exchange_fee_per_contract', 0.0),
        settings.get('clearing_fee_per_contract', 0.0),
        settings.get('slippage_budget_ticks', 0.0))
    move = sizing.to_points(total, tick_size, tick_value, qty)
    if move is None:
        return None
    return fill_price + move * side.sign


def target_basis_value(basis: TargetBasis, position_qty: float,
                       fill_price: Optional[float],
                       margin_locked: Optional[float] = None,
                       contract_multiplier: Optional[float] = None,
                       entry_std: Optional[float] = None,
                       tick_size: Optional[float] = None,
                       tick_value: Optional[float] = None) -> Optional[float]:
    """The money the target percentage is a percentage OF, or None.

    **None is a real answer here and must not be papered over.** The desk
    trades a percentage of MARGIN; where the venue will not report margin
    there is no target, and the window says which figure is missing. Falling
    back to notional without being asked would change what the number means
    while it went on looking like the same number.
    """
    if basis is TargetBasis.MARGIN:
        return margin_locked if margin_locked and margin_locked > 0 else None
    if basis is TargetBasis.NOTIONAL:
        if fill_price is None or not contract_multiplier or not position_qty:
            return None
        return abs(fill_price * contract_multiplier * position_qty)
    if basis is TargetBasis.ENTRY_SIGMA:
        if entry_std is None or entry_std <= 0:
            return None
        return sizing.to_money(entry_std, tick_size, tick_value, position_qty)
    return None


def target_price(fill_price: Optional[float], side: Side, qty: float,
                 tick_size: Optional[float], tick_value: Optional[float],
                 settings: Dict[str, Any],
                 margin_locked: Optional[float] = None,
                 contract_multiplier: Optional[float] = None,
                 entry_std: Optional[float] = None) -> Optional[float]:
    """Break-even plus the profit target, in the contract's own price.

    Returns None when break-even is unknown, or when the basis cannot be
    measured. A percentage of 0 is a real instruction — "leave at break-even"
    — and returns break-even, not None.
    """
    be = break_even(fill_price, side, qty, tick_size, tick_value, settings)
    if be is None:
        return None
    pct = settings.get('profit_target_pct', 0.0)
    if pct is None:
        return None
    pct = float(pct)
    if pct == 0.0:
        return be

    raw_basis = settings.get('profit_target_basis', TargetBasis.MARGIN)
    basis = raw_basis if isinstance(raw_basis, TargetBasis) else TargetBasis(raw_basis)
    basis_money = target_basis_value(
        basis, qty, fill_price, margin_locked=margin_locked,
        contract_multiplier=contract_multiplier, entry_std=entry_std,
        tick_size=tick_size, tick_value=tick_value)
    if basis_money is None:
        return None

    move = sizing.to_points(basis_money * pct / 100.0, tick_size, tick_value, qty)
    if move is None:
        return None
    return be + move * side.sign


def missing_for_target(settings: Dict[str, Any],
                       margin_locked: Optional[float],
                       contract_multiplier: Optional[float],
                       entry_std: Optional[float],
                       tick_value: Optional[float]) -> Optional[str]:
    """What is stopping the target from being computed, in words.

    The screen shows an em dash; this is the sentence beside it. "Check the
    log" is not an acceptable answer anywhere in this system.
    """
    if not tick_value:
        return "the venue has not reported this contract's tick value"
    raw = settings.get('profit_target_basis', TargetBasis.MARGIN)
    basis = raw if isinstance(raw, TargetBasis) else TargetBasis(raw)
    if basis is TargetBasis.MARGIN and not margin_locked:
        return ("no margin entered for this contract — the profit target "
                "is a % of it; set Margin per contract")
    if basis is TargetBasis.NOTIONAL and not contract_multiplier:
        return "the venue has not reported this contract's multiplier"
    if basis is TargetBasis.ENTRY_SIGMA and not entry_std:
        return "sigma at entry was not recorded"
    return None


def net_pnl(position_side: Side, qty: float, entry_price: float,
            exit_price: float, tick_size: Optional[float],
            tick_value: Optional[float],
            fees_paid: Optional[float] = None) -> Dict[str, Optional[float]]:
    """Gross, fees and net for a closed position.

    Fees are what the venue actually CHARGED where it reported them. Where it
    did not, fees is None and net is None with it — a net P&L that quietly
    means "gross" is the figure that makes a losing system look profitable.
    """
    points = (exit_price - entry_price) * position_side.sign
    gross = sizing.to_money(points, tick_size, tick_value, qty)
    if gross is None:
        return {'gross': None, 'fees': fees_paid, 'net': None}
    if fees_paid is None:
        return {'gross': gross, 'fees': None, 'net': None}
    return {'gross': gross, 'fees': fees_paid, 'net': gross - fees_paid}
