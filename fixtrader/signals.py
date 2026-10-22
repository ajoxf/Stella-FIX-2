"""Readings off the touch-study window, for the screen.

The Algo's rule is in `algo.py` — this is not it. What remains here are two
readings the snapshot still publishes from the rolling time window the
Analysis touch study uses: the edge ratio, and the z of the bid and offer.
"""

from typing import Any, Dict, Optional, Tuple

from . import costs as costs_mod
from .stats import StatsWindow


def _f(settings: Dict[str, Any], key: str, default: float) -> float:
    v = settings.get(key, default)
    return default if v is None else float(v)


def round_trip_ticks(qty: float, tick_size, tick_value,
                     settings: Dict[str, Any]) -> Optional[float]:
    """The round trip expressed in ticks of this contract, or None."""
    return costs_mod.cost_breakdown(qty, tick_size, tick_value,
                                    settings).get('round_trip_ticks')


def edge_ratio(window: StatsWindow, qty: float, tick_size, tick_value,
               settings: Dict[str, Any]) -> Optional[float]:
    """How many times sigma covers a round trip — shown on the window as a
    reading. The entry decision uses `can_it_pay`, which asks the sharper
    question: does a return to the mean pay the round trip AND the target."""
    if window.std is None or window.std <= 0:
        return None
    rt_points = costs_mod.cost_breakdown(
        qty, tick_size, tick_value, settings).get('round_trip_points')
    if rt_points is None or rt_points <= 0:
        return None
    return window.std / rt_points


def side_z(window: StatsWindow, book) -> Tuple[Optional[float], Optional[float]]:
    """(z of the BID, z of the OFFER) against the standing bands.

    A SHORT is sold into the bid, so it is the bid's z that says whether the
    spread is rich enough to sell; a LONG is bought on the offer. Deciding on
    the mid would fire on a level no order could get.
    """
    if book is None:
        return None, None
    return window.z_of(book.bid), window.z_of(book.ask)
