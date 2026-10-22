"""Spread points into money, and back. The ONE conversion.

Every money figure on the screen runs through here. It is one short module on
purpose: the system this is ported from priced one leg's bid-ask in the other
leg's units once, and reported $1,200 for a cost whose real value was $28. A
single function that everything calls cannot drift out of step with itself.

    money  = points x (tick_value / tick_size) x qty
    points = money / ((tick_value / tick_size) x qty)

`tick_value / tick_size` is the money one whole point of the spread is worth
for ONE contract. Both come from the venue's own security definition.
"""

from typing import Optional


def money_per_point(tick_size: Optional[float],
                    tick_value: Optional[float]) -> Optional[float]:
    """What one point of this spread is worth, for one contract.

    None where either input is missing or nonsensical. **Unmeasured is not
    zero**: a 0.0 here would silently price every cost in the system at
    nothing, and a break-even of "the price you paid" would look correct.
    """
    if not tick_size or not tick_value:
        return None
    if tick_size <= 0 or tick_value <= 0:
        return None
    return tick_value / tick_size


def to_money(points: Optional[float], tick_size: Optional[float],
             tick_value: Optional[float], qty: float = 1.0) -> Optional[float]:
    """`points` of spread movement expressed in money, or None."""
    mpp = money_per_point(tick_size, tick_value)
    if mpp is None or points is None or qty is None:
        return None
    return points * mpp * qty


def to_points(money: Optional[float], tick_size: Optional[float],
              tick_value: Optional[float], qty: float = 1.0) -> Optional[float]:
    """`money` expressed as a spread move, or None.

    A qty of 0 has no answer — dividing by it would raise, and returning 0
    would claim the move is free.
    """
    mpp = money_per_point(tick_size, tick_value)
    if mpp is None or money is None or not qty:
        return None
    return money / (mpp * qty)


def to_ticks(points: Optional[float],
             tick_size: Optional[float]) -> Optional[float]:
    if points is None or not tick_size or tick_size <= 0:
        return None
    return points / tick_size


def round_to_tick(price: Optional[float], tick_size: Optional[float],
                  direction: str = "nearest") -> Optional[float]:
    """A price the venue will accept.

    `direction` is 'up', 'down' or 'nearest'. A limit is rounded AWAY from
    the market — a buy down, a sell up — so that rounding never turns a
    resting order into a crossing one.
    """
    if price is None or not tick_size or tick_size <= 0:
        return None
    n = price / tick_size
    if direction == "up":
        import math
        n = math.ceil(n - 1e-9)
    elif direction == "down":
        import math
        n = math.floor(n + 1e-9)
    else:
        n = round(n)
    # Snap to the tick grid and clean the binary dust that would otherwise
    # print 0.6899999999999999 on a screen of four-decimal prices.
    return round(n * tick_size, 10)


def clamp_qty(qty: float, min_qty: Optional[float] = None,
              qty_step: Optional[float] = None,
              max_qty: Optional[float] = None) -> float:
    """A quantity the venue will accept: on the step, inside the bounds.

    Rounds DOWN to the step. Rounding up would send more than the caller
    asked for, and on a close that is how an exit becomes a reversal.
    """
    q = float(qty)
    if qty_step and qty_step > 0:
        import math
        q = math.floor(q / qty_step + 1e-9) * qty_step
        q = round(q, 10)
    if max_qty is not None and q > max_qty:
        q = max_qty
    if min_qty is not None and 0 < q < min_qty:
        return 0.0
    return max(0.0, q)
