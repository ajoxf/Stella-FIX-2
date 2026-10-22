import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixtrader.config import DEFAULT_SETTINGS, ContractConfig, TraderConfig
from fixtrader.stats import StatsWindow

T0 = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def iron_ore():
    """A contract with the specs of an SGX iron-ore calendar spread:
    tick 0.01, tick value $1.00 (100 tonnes), so one point is $100."""
    return ContractConfig(
        key='fef_v6x6', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        venue='SGX-UAT', tick_size=0.01, tick_value=1.0,
        contract_multiplier=100.0, currency='USD',
        min_qty=1.0, qty_step=1.0, max_qty=50.0)


@pytest.fixture
def desk():
    return dict(DEFAULT_SETTINGS)


@pytest.fixture
def settings(iron_ore, desk):
    return iron_ore.settings_with_defaults(desk)


def warm(window: StatsWindow, values, start=0.0, step=1.0, armed=False):
    """Push a series through a window, returning every touch event raised."""
    events = []
    t = start
    for v in values:
        events.extend(window.add(v, at(t), algo_armed=armed))
        t += step
    return events


def window(n, interval=0.0, threshold=2.0, key='k', span_minutes=1e6):
    """A window that goes warm at its n-th one-second sample — the old
    "lookback of n" — with a span wide enough that a test jumping forward in
    time does not trim it. Tests about the span itself pass `span_minutes`."""
    return StatsWindow(key, window_minutes=span_minutes,
                       min_history_minutes=(n - 1) / 60.0,
                       sample_interval_sec=1.0,
                       stats_update_interval_sec=interval,
                       entry_threshold=threshold)


def flat_series(n, value=0.5):
    return [value] * n


#: Contract settings that make the Algo testable in a few passes: one-minute
#: candles, no live warm-up, entry on the TOUCH of the band (no re-entry),
#: and the filters that would need hours of history off. Tests about a
#: filter, the warm-up or re-entry turn theirs back on.
ALGO_TEST_SETTINGS = {
    'timeframe_min': 1, 'length': 20, 'warmup_min': 0.0,
    'reentry_on': False, 'trend_on': False, 'regime_on': False,
    'edge_on': False, 'cutoff_buffer_min': 0.0, 'confirm_samples': 1,
    'max_trades_per_day': 0.0, 'max_losses_row': 0, 'stop_loss_on': False,
}


def fill_candles(engine, gw, key='fef', n=40, mid=0.50, swing=0.10):
    """Alternate the book one candle at a time, so the band fills with a
    known sigma. Leaves the clock at the start of the NEXT candle."""
    from datetime import timedelta
    for i in range(n):
        px = mid + (swing if i % 2 else -swing)
        gw.set_book(key, round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
        engine.poll(now=gw.now)
        gw.now = gw.now + timedelta(seconds=60)
    return engine.runtimes[key]


def price_for_z(rt, now, z):
    """The MID that puts the band's z at `z` on the next pass, with that
    mid as the forming candle's close — it moves the mean and sigma too, so
    it is solved for, not read off the band."""
    from fixtrader import bands
    candles = rt.algo.candles
    closes = candles.closes()
    bucket = bands.bucket_of(now.timestamp(), candles.timeframe_sec)
    if candles.forming is not None and bucket <= candles.forming[0]:
        closes = closes[:-1]
    n = candles.length

    def z_of(px):
        values = closes + [px]
        mean = bands.ema(values, n)
        sigma = bands.population_stdev(values[-n:])
        return (px - mean) / sigma

    lo, hi = (min(closes) - 50, max(closes) + 50)
    for _ in range(200):
        midpoint = (lo + hi) / 2.0
        if z_of(midpoint) < z:
            lo = midpoint
        else:
            hi = midpoint
    return (lo + hi) / 2.0


def book_at_z(engine, gw, z, key='fef', half=0.005):
    rt = engine.runtimes[key]
    px = price_for_z(rt, gw.now, z)
    gw.set_book(key, round(px - half, 4), round(px + half, 4), 50, 50)
    return px
