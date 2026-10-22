"""Bollinger bands on the CONTRACT: candles, an EMA middle, and sigma.

The venue lists the spread itself as one contract, so this is the
contract's own mid — there are no legs to combine. The Algo's whole
statistical input, and nothing else. It is arithmetic on numbers it is
handed — it never reads a price, a venue or a clock of its own, so the
same code answers for a restart from the recorded mids and a live
quote, and a test can drive both.

The convention is TradingView's Bollinger with an EMA basis, because
that is the chart a trader checks it against:

- **candles** of `timeframe_sec`, each closing on the last spread seen
  in its bucket. The candle still FORMING counts: its close is the live
  mid spread, so the middle line and the band move with the market the
  way they do on a chart;
- **middle** = Pine's `ta.ema(close, N)`: seeded with the simple average
  of the first N closes, then `alpha = 2 / (N + 1)` over every close
  after it. The more history behind it, the closer it sits to the
  chart's own value, which is why more than N candles are kept;
- **sigma** = Pine's `ta.stdev(close, N)`: the POPULATION standard
  deviation of the last N closes, forming candle included.

**Unmeasured is not zero.** Fewer than N closes, or a flat series, is
not ready: `stats()` says how many it has and how many it needs, and
returns no mean and no sigma rather than a band drawn from nothing.
"""

import math

#: How many closed candles are KEPT, as a multiple of N. An EMA seeded
#: on N closes has not forgotten its seed until a few N later; keeping
#: five N is what makes the middle line agree with a chart's.
KEEP_MULTIPLE = 5

#: Below this, sigma is a flat line and a z-score is a division by it.
MIN_SIGMA = 1e-12


def bucket_of(at, timeframe_sec, anchor=0.0):
    """The start of the candle `at` (UTC seconds) falls in.

    `anchor` shifts the candle grid, in seconds; 0 puts 15-minute
    candles on the quarter hours of UTC.
    """
    tf = float(timeframe_sec)
    return math.floor((float(at) + anchor) / tf) * tf - anchor


class SpreadCandles:
    """Closed candles of one contract's mid, plus the one forming now."""

    def __init__(self, timeframe_sec, length, anchor=0.0, keep=None):
        self.timeframe_sec = float(timeframe_sec)
        self.length = int(length)
        self.anchor = float(anchor or 0.0)
        #: How many closed candles are kept: KEEP_MULTIPLE x N, or more when
        #: a reader needs a longer history (the trend filter's lookback).
        self.keep = max(self.length * KEEP_MULTIPLE, self.length + 1, int(keep or 0))
        #: bucket start -> close, for CLOSED candles only.
        self.closed = {}
        self.forming = None          # (bucket, close) or None

    # -- feeding it ---------------------------------------------------------

    def seed(self, rows):
        """Closed candles from history: [(bucket, close), ...].

        History never overwrites a candle this process watched close
        itself.
        The forming candle's bucket is skipped: it is not closed yet.
        """
        added = 0
        for bucket, close in rows or ():
            if close is None:
                continue
            bucket = float(bucket)
            if self.forming and bucket >= self.forming[0]:
                continue
            if bucket not in self.closed:
                self.closed[bucket] = float(close)
                added += 1
        self._trim()
        return added

    def observe(self, at, spread):
        """One live mid at `at` (UTC seconds).

        Returns the candle that CLOSED on this observation, as
        (bucket, close), or None — the caller saves closed candles so a
        restart does not start from nothing.
        """
        if spread is None:
            return None
        bucket = bucket_of(at, self.timeframe_sec, self.anchor)
        finished = None
        if self.forming is not None and bucket > self.forming[0]:
            finished = self.forming
            self.closed[finished[0]] = finished[1]
            self._trim()
        if self.forming is None or bucket >= self.forming[0]:
            self.forming = (bucket, float(spread))
        return finished

    def _trim(self):
        keep = self.keep
        if len(self.closed) > keep:
            for bucket in sorted(self.closed)[:len(self.closed) - keep]:
                del self.closed[bucket]

    # -- reading it ---------------------------------------------------------

    def closes(self):
        """Every close in time order, the forming candle last."""
        values = [self.closed[b] for b in sorted(self.closed)]
        if self.forming is not None:
            values.append(self.forming[1])
        return values

    def stats(self):
        """{'ready', 'count', 'needed', 'mean', 'sigma'}.

        Not ready is said with the count, so the screen can show
        "collecting 7/20 candles" instead of a band it cannot draw.
        """
        values = self.closes()
        body = {'ready': False, 'count': len(values), 'needed': self.length,
                'mean': None, 'sigma': None}
        if len(values) < self.length or self.length < 1:
            return body
        mean = ema(values, self.length)
        sigma = population_stdev(values[-self.length:])
        if mean is None or sigma is None or sigma < MIN_SIGMA:
            body['note'] = 'the spread has not moved — no band to measure'
            return body
        body.update(ready=True, mean=mean, sigma=sigma)
        return body


def ema(values, length):
    """Pine's `ta.ema`: an SMA of the first `length`, then recursive."""
    if length < 1 or len(values) < length:
        return None
    alpha = 2.0 / (length + 1.0)
    value = sum(values[:length]) / float(length)
    for close in values[length:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def population_stdev(values):
    """Pine's `ta.stdev`: divided by N, not N - 1."""
    if not values:
        return None
    mean = sum(values) / float(len(values))
    return math.sqrt(sum((v - mean) ** 2 for v in values) / float(len(values)))


def candles_from_samples(rows, timeframe_sec, anchor=0.0):
    """Recorded (ts, mid) samples as CLOSED candles: [(bucket, close)].

    FIX market data carries no history to backfill from, so the band's
    history is this system's own recording of the contract's mid. A
    candle closes on the last mid recorded in its bucket; a bucket with
    no sample is not a candle (the market was shut, or nobody watched) —
    it is skipped, never filled in.
    """
    out = {}
    for row in rows or ():
        ts, price = row[0], row[1]
        if price is None:
            continue
        at = ts.timestamp() if hasattr(ts, 'timestamp') else float(ts)
        out[bucket_of(at, timeframe_sec, anchor)] = float(price)
    return sorted(out.items())
