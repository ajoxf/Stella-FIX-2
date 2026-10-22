"""The Algo's entry filters: is this stretch WORTH trading?

Ported from the MT5 desk's Algo, on ONE contract: the venue lists the
spread itself, so there is no leg A, no beta, no second commission. Pure:
it is handed numbers and returns numbers and reasons. It never reads a
price or a venue.

All money is for the Algo's own size: `k` is money per 1.00 of price per
contract (`tick_value / tick_size`, the one conversion in `sizing`) and
`qty` is the Algo qty in contracts.

- **Round-trip cost** — what one trade costs, all in: the bid-ask
  crossed once each way (entering on one touch, leaving on the other is
  one full width), the fees both ways, and the slippage budget.
- **Edge** — the stat-arb rule "only take a trade I know can be
  profitable after ALL costs": the expected capture, `capture_frac x |z|
  x sigma`, in money, must be at least `multiple x` the round-trip cost.
  Capture is a FRACTION of the full reversion (0.5 by default) because a
  trade rarely gets all of it.
- **Regime** — a TRENDING spread is not mean-reverting, and a band on a
  trend is a band the price walks through. Kaufman's efficiency ratio
  (net move over path) high AND few crossings of the mean = TRENDING.
- **Half-life** — AR(1) on the candles: how long a stretch takes to
  halve. Too fast is noise; too slow will not revert inside a hold.

**Unmeasured is not zero.** Anything that cannot be computed is None,
and a filter that cannot be evaluated BLOCKS an entry, saying why — an
edge nobody could price is not an edge.
"""

import math


def round_trip_cost(width, k, qty, commission=None, slippage=None):
    """{crossing, commission, slippage, total} in money, or total None.

    `width` is the contract's bid-ask (offer - bid); `commission` is the
    fees for the round turn of `qty` contracts; `slippage` the budget. None is unknown: the total
    is then unknown too, never the sum of what happened to be known.
    """
    body = {'crossing': None, 'commission': commission,
            'slippage': slippage, 'total': None}
    if width is None or not k or not qty:
        return body
    body['crossing'] = float(width) * float(k) * float(qty)
    if commission is None:
        return body
    body['total'] = (body['crossing'] + float(commission)
                     + float(slippage or 0.0))
    return body


def edge(z, sigma, k, qty, cost, capture_frac=0.5, multiple=1.5):
    """Expected capture against the round-trip cost.

    {capture, cost, ratio, required, ok}. `ok` is None when it cannot be
    priced — which the caller treats as a block.
    """
    body = {'capture': None, 'cost': cost, 'ratio': None,
            'required': multiple, 'ok': None}
    if z is None or not sigma or not k or not qty:
        return body
    capture = (float(capture_frac) * abs(float(z)) * float(sigma)
               * float(k) * float(qty))
    body['capture'] = capture
    if cost is None:
        return body
    if cost <= 0:
        body['ok'] = True
        return body
    body['ratio'] = capture / float(cost)
    body['ok'] = body['ratio'] >= float(multiple)
    return body


def half_life(closes):
    """Mean-reversion half-life in CANDLES, by AR(1); None if the series
    is not reverting (phi outside 0..1) or too short to say."""
    values = [float(v) for v in closes or () if v is not None]
    if len(values) < 10:
        return None
    mean = sum(values) / len(values)
    y = [v - mean for v in values]
    lagged, ahead = y[:-1], y[1:]
    denominator = sum(a * a for a in lagged)
    if denominator <= 0:
        return None
    phi = sum(a * b for a, b in zip(ahead, lagged)) / denominator
    if phi <= 0 or phi >= 1:
        return None
    return math.log(2.0) / -math.log(phi)


def regime(closes, er_max=0.6, min_crossings=4):
    """{state, efficiency_ratio, crossings, slope} over `closes`.

    TRENDING when the efficiency ratio is at least `er_max` AND the
    series crossed its own mean `min_crossings` times or fewer; RANGE
    otherwise; COLLECTING while there are too few closes to say.
    """
    values = [float(v) for v in closes or () if v is not None]
    body = {'state': 'COLLECTING', 'efficiency_ratio': None,
            'crossings': None, 'slope': None}
    if len(values) < 10:
        return body
    path = sum(abs(b - a) for a, b in zip(values, values[1:]))
    net = values[-1] - values[0]
    er = abs(net) / path if path > 1e-12 else 0.0
    mean = sum(values) / len(values)
    signs = [(v > mean) - (v < mean) for v in values]
    crossings = sum(1 for a, b in zip(signs, signs[1:]) if a * b < 0)
    body.update(efficiency_ratio=er, crossings=crossings, slope=net,
                state=('TRENDING' if er >= float(er_max)
                       and crossings <= int(min_crossings) else 'RANGE'))
    return body


def atr(closes, period=14):
    """The average true range of the contract, close to close, Wilder's
    smoothing - in price points, or None with too few candles.

    The candles are built from sampled mids and keep their close only, so
    the true range is the size of each move from one close to the next:
    period + 1 closes give the first value.
    """
    period = int(period)
    values = [float(c) for c in closes or () if c is not None]
    if period < 2 or len(values) < period + 1:
        return None
    moves = [abs(b - a) for a, b in zip(values, values[1:])]
    value = sum(moves[:period]) / period
    for move in moves[period:]:
        value = (value * (period - 1) + move) / period
    return value
