"""The rolling window: mean, sigma, z, Hurst, half-life — and the touches.

One instance per contract. No I/O and no clock of its own: the caller passes
the timestamp, so every test is deterministic and nothing here has to be
mocked.

Three things worth knowing before changing anything in here:

- **Mean and sigma are recomputed on an INTERVAL; z is recomputed every
  update.** Bands that move under the price on every tick cannot be traded
  against — the trader is aiming at a number that has already gone. So the
  bands stand still for `stats_update_interval_sec` and the z runs live
  against them.
- **The window must be WARM before anything trades** — `min_history_minutes`
  of continuous history. A sigma from ten minutes of a two-hour window is not
  a small version of the right answer, it is a different number, and entries
  priced off it fire at levels that do not exist.
- **A touch is counted once per CROSSING**, not once per update. A z that sits
  at 2.1 for four minutes is one touch; counting it per update would make the
  quietest, stickiest level look like the most significant one in the study.
"""

import math
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import TouchEvent, TouchState

#: The levels the touch study reports on.
SD_LEVELS: Tuple[float, ...] = (-3.0, -2.0, -1.0, 1.0, 2.0, 3.0)

#: How long an unresolved touch waits before it is called TIMED_OUT, as a
#: multiple of the half-life. Two half-lives is the reversion the strategy is
#: betting on having had time to happen twice over.
RESOLVE_HALF_LIVES = 2.0

#: Where there is no half-life (a trending or random series), a touch still
#: has to stop waiting eventually, or every level reads as unresolved forever.
RESOLVE_FALLBACK_SAMPLES = 400


def _regime(hurst: Optional[float]) -> str:
    if hurst is None:
        return "UNKNOWN"
    if hurst < 0.4:
        return "MEAN_REVERTING"
    if hurst > 0.6:
        return "TRENDING"
    return "NEUTRAL"


class StatsWindow:
    """The rolling statistics for one contract, over a window of TIME.

    The window is the last `window_minutes` of prices, sampled at most once
    every `sample_interval_sec` — the engine polls ten times a second, and a
    window of polls would be a window of however fast the loop happened to
    run. It is WARM once it holds `min_history_minutes` of CONTINUOUS history:
    a gap longer than a few samples (a restart, a stalled feed) is not
    history, and counting it would let a window that saw ten minutes of market
    either side of a two-hour outage call itself two hours deep.
    """

    def __init__(self, contract_key: str, window_minutes: float = 150.0,
                 min_history_minutes: float = 120.0,
                 sample_interval_sec: float = 1.0,
                 stats_update_interval_sec: float = 300.0,
                 entry_threshold: float = 2.5):
        self.contract_key = contract_key
        self.window_minutes = float(window_minutes)
        self.min_history_minutes = float(min_history_minutes)
        self.sample_interval_sec = max(0.0, float(sample_interval_sec))
        self.stats_update_interval_sec = float(stats_update_interval_sec)
        self.entry_threshold = float(entry_threshold)

        #: (ts, price), oldest first.
        self.samples_ts: deque = deque()
        #: Seconds of continuous history in the window — gaps excluded.
        self.history_sec: float = 0.0

        self.mean: Optional[float] = None
        self.std: Optional[float] = None
        self.z: Optional[float] = None
        self.hurst: Optional[float] = None
        self.half_life: Optional[float] = None

        self.last_price: Optional[float] = None
        self.last_ts: Optional[datetime] = None
        self._last_stats_ts: Optional[datetime] = None
        self._last_shape_ts: Optional[datetime] = None
        #: Whether a computation has been done since the window went warm.
        self._warm_stats_done: bool = False
        self._samples_seen: int = 0

        #: Which SD band the z was in last update. The touch is emitted on the
        #: CHANGE, which is what makes it one event per crossing.
        self._last_band: float = 0.0
        self._open_touches: List[TouchEvent] = []
        #: The most recent touch, so the engine can mark the one an entry
        #: actually acted on. The touch study's "traded" column is what says
        #: whether the level the algo fires at is the level that reverts.
        self.last_touch: Optional[TouchEvent] = None

    # -- configuration ----------------------------------------------------

    def update_config(self, window_minutes: Optional[float] = None,
                      min_history_minutes: Optional[float] = None,
                      sample_interval_sec: Optional[float] = None,
                      stats_update_interval_sec: Optional[float] = None,
                      entry_threshold: Optional[float] = None) -> bool:
        """Apply new settings. Returns True if the window was RESHAPED.

        A changed window length keeps the samples that still fit, but the
        cached mean and sigma were computed over a different window, so they
        are invalidated and recomputed on the next update rather than left
        standing as the answer to a question nobody asked.
        """
        reshaped = False
        if entry_threshold is not None:
            self.entry_threshold = float(entry_threshold)
        if stats_update_interval_sec is not None:
            self.stats_update_interval_sec = float(stats_update_interval_sec)
        if sample_interval_sec is not None:
            self.sample_interval_sec = max(0.0, float(sample_interval_sec))
        if min_history_minutes is not None and \
                float(min_history_minutes) != self.min_history_minutes:
            self.min_history_minutes = float(min_history_minutes)
            self._warm_stats_done = False
            reshaped = True
        if window_minutes is not None and float(window_minutes) != self.window_minutes:
            self.window_minutes = float(window_minutes)
            self._trim()
            self._last_stats_ts = None       # force a recompute
            self._warm_stats_done = False
            reshaped = True
        self._measure_history()      # the gap tolerance may have moved too
        return reshaped

    def clear(self) -> None:
        """Start again from nothing — the contract changed underneath us."""
        self.samples_ts.clear()
        self.history_sec = 0.0
        self.mean = self.std = self.z = None
        self.hurst = self.half_life = None
        self.last_price = self.last_ts = None
        self._last_stats_ts = self._last_shape_ts = None
        self._warm_stats_done = False
        self._last_band = 0.0
        self._open_touches.clear()
        self.last_touch = None

    # -- the window -------------------------------------------------------

    @property
    def prices(self) -> List[float]:
        return [p for _, p in self.samples_ts]

    @property
    def samples(self) -> int:
        return len(self.samples_ts)

    @property
    def gap_tolerance_sec(self) -> float:
        """The longest step between two samples that still counts as
        continuous history. Beyond it, the market was not being watched."""
        return max(5.0, 10.0 * self.sample_interval_sec)

    @property
    def history_minutes(self) -> float:
        return self.history_sec / 60.0

    @property
    def is_warm(self) -> bool:
        """Enough continuous history. Nothing enters before this is True."""
        return (self.samples >= 2
                and self.history_sec >= self.min_history_minutes * 60.0)

    @property
    def warm_pct(self) -> float:
        if self.min_history_minutes <= 0:
            return 100.0
        return min(100.0, 100.0 * self.history_sec
                   / (self.min_history_minutes * 60.0))

    @property
    def half_life_sec(self) -> Optional[float]:
        """The half-life in SECONDS — samples times the sampling interval."""
        if self.half_life is None:
            return None
        return self.half_life * (self.sample_interval_sec or 1.0)

    def z_of(self, price: Optional[float]) -> Optional[float]:
        """The z of ANY price against the standing bands — the bid, the
        offer, a target. None where there are no bands yet."""
        if price is None or self.mean is None or not self.std or self.std <= 0:
            return None
        return (float(price) - self.mean) / self.std

    def bands(self, threshold: Optional[float] = None
              ) -> Tuple[Optional[float], Optional[float]]:
        """The two entry levels as PRICES — (buy at, sell at).

        A trader checks a level against the book, not against a z. The low
        band is where a BUY fires (the offer is cheap against the mean) and
        the high band is where a SELL does.
        """
        if self.mean is None or self.std is None or self.std <= 0:
            return None, None
        t = self.entry_threshold if threshold is None else float(threshold)
        return self.mean - t * self.std, self.mean + t * self.std

    def price_at_z(self, z: float) -> Optional[float]:
        if self.mean is None or self.std is None or self.std <= 0:
            return None
        return self.mean + z * self.std

    # -- the update -------------------------------------------------------

    def _trim(self) -> None:
        """Drop samples older than the window, measured from the newest."""
        if not self.samples_ts:
            self.history_sec = 0.0
            return
        newest = self.samples_ts[-1][0]
        span = self.window_minutes * 60.0
        tol = self.gap_tolerance_sec
        while self.samples_ts and \
                (newest - self.samples_ts[0][0]).total_seconds() > span:
            old_ts, _ = self.samples_ts.popleft()
            if self.samples_ts:
                step = (self.samples_ts[0][0] - old_ts).total_seconds()
                if 0 < step <= tol:
                    self.history_sec -= step
        if not self.samples_ts or len(self.samples_ts) < 2:
            self.history_sec = 0.0
        self.history_sec = max(0.0, self.history_sec)

    def _measure_history(self) -> None:
        tol = self.gap_tolerance_sec
        total, prev = 0.0, None
        for ts, _ in self.samples_ts:
            if prev is not None:
                step = (ts - prev).total_seconds()
                if 0 < step <= tol:
                    total += step
            prev = ts
        self.history_sec = total

    def prime(self, rows) -> int:
        """Bulk-load recorded (ts, price) rows — a restart resuming its window.

        One recomputation at the end rather than one per row: a 150-minute
        window at one sample a second is nine thousand rows. Touches are not
        replayed — they were recorded when they happened.
        """
        added = 0
        for row in rows:
            ts, price = row[0], row[1]
            if price is None or not math.isfinite(float(price)):
                continue
            if self.samples_ts and ts <= self.samples_ts[-1][0]:
                continue
            self.samples_ts.append((ts, float(price)))
            self.last_price, self.last_ts = float(price), ts
            added += 1
        if added:
            self._trim()
            self._measure_history()
            self._samples_seen += added
            self._last_stats_ts = None
            self._recompute(self.last_ts, force=True)
        return added

    def add(self, price: Optional[float], ts: datetime,
            algo_armed: bool = False) -> List[TouchEvent]:
        """Add one observation. Returns touch events created or resolved.

        `price` is the MID OF THE BOOK. A None or non-finite price is not an
        observation and is dropped without disturbing the window — a zero
        appended here would drag the mean toward it and quietly move every
        band on the screen. The z is updated on every call; a SAMPLE is only
        taken once per `sample_interval_sec`.
        """
        if price is None or not math.isfinite(price):
            return []

        self.last_price = float(price)
        self.last_ts = ts
        sampled = (not self.samples_ts
                   or (ts - self.samples_ts[-1][0]).total_seconds()
                   >= self.sample_interval_sec)
        if sampled:
            if self.samples_ts:
                step = (ts - self.samples_ts[-1][0]).total_seconds()
                if 0 < step <= self.gap_tolerance_sec:
                    self.history_sec += step
            self.samples_ts.append((ts, float(price)))
            self._samples_seen += 1
            self._trim()
        self._recompute(ts, sampled=sampled)

        events: List[TouchEvent] = []
        events.extend(self._resolve_open_touches(ts))
        touch = self._detect_touch(ts, algo_armed)
        if touch is not None:
            events.append(touch)
        return events

    #: Hurst and half-life are estimates of the window's SHAPE — readings on
    #: the screen, not gates on an entry. They are expensive on a full window
    #: and a minute does not change them, so they are recomputed at most this
    #: often (and on the update that warms the window). Recomputing them with
    #: every sample made a replay over an hour of mids take minutes.
    SHAPE_EVERY_SEC = 60.0

    #: And never over more than this many of the most recent samples.
    SHAPE_MAX_SAMPLES = 4000

    def _recompute(self, ts: datetime, sampled: bool = True,
                   force: bool = False) -> None:
        n = len(self.samples_ts)
        if n < 2:
            return

        # While the window is still WARMING, recompute on every sample. The
        # interval exists to hold the bands still for a trader to aim at, and
        # there is nothing to aim at yet — freezing here would compute the
        # mean and sigma from the first two samples and stand by them for the
        # whole interval. And once MORE on the update that warms it, so the
        # bands that then stand are drawn from the full history.
        due = force or (sampled and (
            not self.is_warm
            or not self._warm_stats_done
            or self._last_stats_ts is None
            or self.stats_update_interval_sec <= 0
            or (ts - self._last_stats_ts).total_seconds()
            >= self.stats_update_interval_sec))

        if due:
            values = [p for _, p in self.samples_ts]
            self.mean = sum(values) / n
            var = sum((v - self.mean) ** 2 for v in values) / (n - 1)
            self.std = math.sqrt(var)
            shape_due = (force or self._last_shape_ts is None
                         or (self.is_warm and not self._warm_stats_done)
                         or (ts - self._last_shape_ts).total_seconds()
                         >= self.SHAPE_EVERY_SEC)
            if n >= 20 and shape_due:
                recent = values[-self.SHAPE_MAX_SAMPLES:]
                self.hurst = self._hurst(recent)
                self.half_life = self._half_life(recent)
                self._last_shape_ts = ts
            self._last_stats_ts = ts
            if self.is_warm:
                self._warm_stats_done = True

        # z is ALWAYS current: the live price against the standing bands.
        self.z = self.z_of(self.last_price)

    # -- Hurst and half-life ----------------------------------------------

    @staticmethod
    def _hurst(levels: List[float]) -> Optional[float]:
        """Rescaled-range Hurst exponent, on the INCREMENTS of the series.

        H < 0.5 anti-persistent (mean-reverting), 0.5 a random walk, > 0.5
        trending. None where the series cannot support the estimate — better
        than 0.5, which would read as a measurement of a random walk.

        **On the increments, not the levels**, and the distinction is not
        academic. R/S measures how a cumulative sum spreads; the price series
        is already a cumulative sum, so running it over the levels measures
        the cumulative sum of a cumulative sum and reads ~1.0 for everything —
        a plain random walk included. With `hurst_enabled` on and a threshold
        of 0.5, that silently withholds every entry on every contract, for
        ever, while the screen shows a filter that merely looks strict.

        A caveat that sits under a threshold the operator will set: R/S is
        biased UPWARD on short windows (Anis-Lloyd). A 400-sample random walk
        reads nearer 0.6 than 0.5, so 0.5 is not a knife edge between
        "reverting" and "trending" — it is a conservative line, and the
        reading is most useful compared against the same contract's own
        history rather than read as an absolute.
        """
        if len(levels) < 21:
            return None
        series = [levels[i + 1] - levels[i] for i in range(len(levels) - 1)]
        n = len(series)
        if n < 20:
            return None
        max_k = min(n // 2, 50)
        min_k = 10
        if max_k <= min_k:
            return None

        rs_values: List[float] = []
        n_values: List[int] = []
        for k in range(min_k, max_k + 1, 5):
            rs_list = []
            for start in range(0, n - k + 1, k):
                sub = series[start:start + k]
                if len(sub) < k:
                    continue
                m = sum(sub) / k
                dev = [v - m for v in sub]
                cum, run = [], 0.0
                for d in dev:
                    run += d
                    cum.append(run)
                r = max(cum) - min(cum)
                var = sum((v - m) ** 2 for v in sub) / (k - 1)
                s = math.sqrt(var)
                if s > 0:
                    rs_list.append(r / s)
            if rs_list:
                rs_values.append(sum(rs_list) / len(rs_list))
                n_values.append(k)

        if len(rs_values) < 2:
            return None
        xs = [math.log(v) for v in n_values]
        ys = [math.log(v) for v in rs_values if v > 0]
        if len(ys) != len(xs):
            return None
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            return None
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        return max(0.0, min(1.0, slope))

    @staticmethod
    def _half_life(series: List[float]) -> Optional[float]:
        """Half-life of mean reversion, in samples, via OLS on the OU process.

        d(price) = theta x (mean - price) + noise, half-life = ln2 / theta.
        **None where theta <= 0** — a series that is not mean-reverting has no
        half-life, and returning a large number instead would let a trending
        contract pass a "half-life below 60" filter.
        """
        n = len(series)
        if n < 10:
            return None
        lag = series[:-1]
        diff = [series[i + 1] - series[i] for i in range(n - 1)]
        m = sum(lag) / len(lag)
        xs = [m - v for v in lag]
        denom = sum(x * x for x in xs)
        if denom == 0:
            return None
        theta = sum(x * y for x, y in zip(xs, diff)) / denom
        if theta <= 0:
            return None
        hl = math.log(2) / theta
        return round(max(1.0, min(hl, float(n))), 1)

    # -- touches ----------------------------------------------------------

    @staticmethod
    def _band_of(z: Optional[float]) -> float:
        """Which SD band a z sits in: 0, +/-1, +/-2, +/-3.

        Floor toward zero, so 2.7 is the +2 band and 3.1 is the +3. A z of
        2.999 has not touched 3.
        """
        if z is None:
            return 0.0
        a = abs(z)
        if a < 1.0:
            return 0.0
        level = min(3.0, float(int(a)))
        return level if z > 0 else -level

    def _detect_touch(self, ts: datetime,
                      algo_armed: bool) -> Optional[TouchEvent]:
        band = self._band_of(self.z)
        previous = self._last_band
        self._last_band = band

        # Only a move INTO a further-out band is a touch. Coming back toward
        # the mean is the reversion, not a new event.
        if band == 0.0 or band == previous:
            return None
        if previous != 0.0 and abs(band) <= abs(previous) and \
                (band > 0) == (previous > 0):
            return None

        touch = TouchEvent(
            contract_key=self.contract_key, ts=ts, level=band,
            direction="UP" if band > 0 else "DOWN",
            price=self.last_price, z=self.z,
            mean=self.mean, std=self.std, half_life=self.half_life,
            algo_armed=algo_armed, state=TouchState.UNRESOLVED,
        )
        # Carried on the event so resolution can be measured without going
        # back to a window that has since moved on.
        touch._opened_at_sample = self._samples_seen     # type: ignore[attr-defined]
        touch._peak_z = self.z                           # type: ignore[attr-defined]
        self._open_touches.append(touch)
        self.last_touch = touch
        return touch

    def _resolve_open_touches(self, ts: datetime) -> List[TouchEvent]:
        """Close out touches that have reverted or run out of time.

        Reverted means the price reached the rolling MEAN — the thing the
        strategy is betting on. A touch still running is left UNRESOLVED and
        reported as such: it is not a failure, and counting it as one would
        understate every level, the widest ones most, because those are the
        ones still open.
        """
        if not self._open_touches or self.mean is None:
            return []

        resolved: List[TouchEvent] = []
        still_open: List[TouchEvent] = []

        for touch in self._open_touches:
            # Track how much further it went the wrong way before turning.
            peak = getattr(touch, '_peak_z', touch.z)
            if self.z is not None:
                if touch.level > 0 and self.z > peak:
                    peak = self.z
                elif touch.level < 0 and self.z < peak:
                    peak = self.z
                touch._peak_z = peak                     # type: ignore[attr-defined]

            reverted = (
                (touch.level > 0 and self.last_price <= self.mean) or
                (touch.level < 0 and self.last_price >= self.mean)
            )
            if reverted:
                touch.state = TouchState.REVERTED
                touch.resolved_at = ts
                touch.seconds_to_revert = (ts - touch.ts).total_seconds()
                touch.adverse_sigma = abs(peak - touch.z) if peak is not None else None
                resolved.append(touch)
                continue

            window = (RESOLVE_HALF_LIVES * touch.half_life
                      if touch.half_life else RESOLVE_FALLBACK_SAMPLES)
            elapsed = self._samples_seen - getattr(touch, '_opened_at_sample', 0)
            if elapsed >= window:
                touch.state = TouchState.TIMED_OUT
                touch.resolved_at = ts
                touch.adverse_sigma = abs(peak - touch.z) if peak is not None else None
                resolved.append(touch)
                continue

            still_open.append(touch)

        self._open_touches = still_open
        return resolved

    @property
    def unresolved_touches(self) -> int:
        return len(self._open_touches)

    # -- for the snapshot -------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        lo, hi = self.bands()
        return {
            'mean': self.mean,
            'std': self.std,
            'z': self.z,
            'buy_at': lo,
            'sell_at': hi,
            'hurst': self.hurst,
            'regime': _regime(self.hurst),
            'half_life': self.half_life,
            'samples': self.samples,
            'history_min': round(self.history_minutes, 1),
            'need_min': self.min_history_minutes,
            'window_min': self.window_minutes,
            'half_life_sec': self.half_life_sec,
            'warm_pct': round(self.warm_pct, 1),
            'is_warm': self.is_warm,
            'unresolved_touches': self.unresolved_touches,
        }
