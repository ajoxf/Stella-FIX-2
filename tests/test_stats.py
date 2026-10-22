"""The rolling window, and the touch study that the Analysis tab reads."""
import math

import pytest

from fixtrader.models import TouchState
from fixtrader.stats import StatsWindow
from tests.conftest import at, warm, window


def series_mean_std(values):
    n = len(values)
    m = sum(values) / n
    return m, math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def test_mean_and_sigma_match_the_series():
    w = window(5)
    warm(w, [1.0, 2.0, 3.0, 4.0, 5.0])
    m, s = series_mean_std([1, 2, 3, 4, 5])
    assert w.mean == pytest.approx(m)
    assert w.std == pytest.approx(s)
    assert w.z == pytest.approx((5.0 - m) / s)


def test_the_window_is_not_warm_until_it_is_full():
    """A sigma from ten minutes of a two-hour window is a different number,
    not a rough version of the right one."""
    w = window(10)
    warm(w, [1.0] * 9)
    assert not w.is_warm
    assert w.warm_pct == pytest.approx(100.0 * 8 / 9)   # 8 of 9 seconds
    w.add(1.0, at(9))
    assert w.is_warm
    assert w.warm_pct == 100.0


def test_bands_stand_still_between_recomputes_while_z_keeps_moving():
    """The whole reason for the interval: a band that moves under the price
    cannot be traded against."""
    w = window(20, interval=300)
    warm(w, [1.0 + 0.01 * i for i in range(20)], step=1.0)
    mean_before, std_before = w.mean, w.std
    z_before = w.z

    w.add(5.0, at(21))                       # 1 second later, a big move
    assert w.mean == mean_before             # bands have NOT moved
    assert w.std == std_before
    assert w.z != z_before                   # ...but z has

    w.add(5.0, at(400))                      # past the interval
    assert w.mean != mean_before             # now they move


def test_zero_interval_recomputes_every_update():
    w = window(5)
    warm(w, [1.0, 2.0, 3.0, 4.0, 5.0])
    first = w.mean
    w.add(10.0, at(6))
    assert w.mean != first


def test_a_missing_price_is_not_an_observation():
    """A zero appended here drags the mean and moves every band on screen."""
    w = window(5)
    warm(w, [1.0, 2.0, 3.0])
    assert w.add(None, at(4)) == []
    assert w.samples == 3
    assert w.add(float('nan'), at(5)) == []
    assert w.samples == 3


def test_bands_are_prices_and_name_the_side_they_fire():
    w = window(5, threshold=2.0)
    warm(w, [0.4, 0.5, 0.6, 0.5, 0.5])
    lo, hi = w.bands()
    assert lo == pytest.approx(w.mean - 2 * w.std)
    assert hi == pytest.approx(w.mean + 2 * w.std)


def _series(kind, n=400, seed=11):
    """Three regimes with known answers, from one seed so this is
    deterministic."""
    import random
    rng = random.Random(seed)
    out, x, e = [], 0.0, 0.0
    for _ in range(n):
        if kind == 'ou':
            x += 0.6 * (0.0 - x) + rng.gauss(0, 1)
        elif kind == 'walk':
            x += rng.gauss(0, 1)
        else:                                  # persistent: genuinely trending
            e = 0.7 * e + rng.gauss(0, 1)
            x += e
        out.append(x)
    return out


def _cumulate(series):
    total, out = 0.0, []
    for v in series:
        total += v
        out.append(total)
    return out


def test_hurst_orders_the_three_regimes_correctly():
    """The reading has to mean what the settings page says it means: below
    0.5 mean-reverting, about 0.5 a random walk, above it trending."""
    h = StatsWindow._hurst
    reverting, walk_h, trending = (h(_series('ou')), h(_series('walk')),
                                   h(_series('persistent')))
    assert reverting < walk_h < trending
    assert reverting < 0.5
    assert trending > 0.7


def test_hurst_is_computed_on_the_increments_not_the_levels():
    """R/S measures how a cumulative sum spreads, and a price series is
    already one. Run over the LEVELS it reads ~1.0 for everything — a plain
    random walk included — and a 0.5 threshold then withholds every entry on
    every contract, for ever. The integrated series is the control."""
    h = StatsWindow._hurst
    walk = _series('walk')
    assert h(walk) < 0.8                       # a walk is not maximally persistent
    assert h(_cumulate(walk)) > 0.9            # ...as it reads when integrated


def test_hurst_needs_enough_samples_and_says_so_with_none():
    """None, not 0.5. A default of 0.5 would read as a measurement saying
    'random walk' when nothing has been measured at all."""
    assert StatsWindow._hurst([1.0, 2.0, 3.0]) is None


def test_a_series_with_no_variation_has_no_hurst():
    """A perfectly straight line has constant increments and zero spread —
    there is no R/S to compute, and the filter withholds rather than guesses."""
    assert StatsWindow._hurst([float(i) for i in range(120)]) is None


def test_half_life_is_none_on_a_trend_rather_than_a_large_number():
    """A large number would let a trending contract pass a 'half-life under
    60' filter."""
    w = window(60, key='t')
    warm(w, [float(i) for i in range(60)])
    assert w.half_life is None


def test_half_life_exists_on_a_mean_reverting_series():
    w = window(60, key='r')
    warm(w, [0.5 + (0.05 if i % 2 else -0.05) for i in range(60)])
    assert w.half_life is not None
    assert w.half_life >= 1.0


# -- the touch study -------------------------------------------------------

def base_window():
    """Bands frozen after the first computation, so a price placed at z=2.2 is
    still at z=2.2 when it is read back. The interval behaviour has its own
    test above; here it would only make the arithmetic non-deterministic."""
    w = window(30, interval=1e9)
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(30)])
    return w


def test_a_touch_is_counted_once_per_crossing_not_once_per_tick():
    """A z that sits at 2.1 for four minutes is ONE touch. Counting per
    update makes the stickiest level look like the most significant."""
    w = base_window()
    hi = w.price_at_z(2.2)
    events = []
    for i in range(6):                       # six updates, all at the same z
        events.extend(w.add(hi, at(100 + i)))
    # A touch is one OBJECT for its whole life: it is raised once and then
    # resolved in place, so counting distinct events is what counts touches.
    # (Counting by state would count the same touch twice as it moved from
    # unresolved to resolved.)
    distinct = {id(e) for e in events}
    assert len(distinct) == 1
    assert events[0].level == 2.0


def test_a_touch_records_the_state_at_the_moment_it_happened():
    w = base_window()
    events = w.add(w.price_at_z(2.3), at(100))
    t = events[0]
    assert t.level == 2.0 and t.direction == "UP"
    assert t.z == pytest.approx(2.3, abs=0.05)
    assert t.mean == pytest.approx(w.mean)
    assert t.std == pytest.approx(w.std)


def test_going_further_out_raises_a_second_touch():
    w = base_window()
    w.add(w.price_at_z(2.2), at(100))
    events = w.add(w.price_at_z(3.4), at(101))
    assert [e.level for e in events if e.state is TouchState.UNRESOLVED] == [3.0]


def test_a_touch_resolves_as_reverted_when_the_price_reaches_the_mean():
    w = base_window()
    mean = w.mean
    w.add(w.price_at_z(2.4), at(100))
    events = w.add(mean, at(160))
    reverted = [e for e in events if e.state is TouchState.REVERTED]
    assert len(reverted) == 1
    assert reverted[0].seconds_to_revert == pytest.approx(60.0)


def test_an_unresolved_touch_is_not_counted_as_a_failure():
    """Rolling it in as a miss understates every level, and the widest levels
    most, because those are the ones still open. The control below resolves
    the same touch and asserts the count moves."""
    w = base_window()
    w.add(w.price_at_z(2.4), at(100))
    w.add(w.price_at_z(2.3), at(101))
    assert w.unresolved_touches == 1          # still running, not a miss

    # control: let it revert, and it leaves the unresolved count
    events = w.add(w.mean, at(150))
    assert w.unresolved_touches == 0
    assert any(e.state is TouchState.REVERTED for e in events)


def test_a_touch_that_never_reverts_times_out_rather_than_waiting_forever():
    w = base_window()
    w.add(w.price_at_z(2.4), at(100))
    events = []
    for i in range(500):                      # long past any half-life
        events.extend(w.add(w.price_at_z(2.4), at(200 + i)))
    assert any(e.state is TouchState.TIMED_OUT for e in events)


def test_adverse_excursion_measures_how_much_further_it_went_first():
    w = base_window()
    w.add(w.price_at_z(2.0), at(100))
    w.add(w.price_at_z(2.9), at(101))         # further against the reversion
    events = w.add(w.mean, at(150))
    reverted = [e for e in events if e.state is TouchState.REVERTED][0]
    assert reverted.adverse_sigma == pytest.approx(0.9, abs=0.1)


def test_whether_the_algo_was_armed_is_recorded_on_the_touch():
    w = base_window()
    armed = w.add(w.price_at_z(2.4), at(100), algo_armed=True)
    assert armed[0].algo_armed is True
    w2 = base_window()
    off = w2.add(w2.price_at_z(2.4), at(100), algo_armed=False)
    assert off[0].algo_armed is False


def test_changing_the_window_invalidates_the_cached_statistics():
    w = window(30, interval=300, span_minutes=10)
    warm(w, [0.5 + 0.01 * i for i in range(30)])
    before = w.mean
    assert w.update_config(window_minutes=10 / 60.0) is True
    w.add(0.5, at(31))
    assert w.mean != before
    assert w.samples <= 11                   # ten seconds, both ends counted


# -- a window of TIME -------------------------------------------------------

def test_the_window_holds_minutes_not_polls():
    """The engine polls ten times a second. A window of polls is a window of
    however fast the loop ran; this one samples once a second."""
    w = StatsWindow('k', window_minutes=1.0, min_history_minutes=0.5,
                    sample_interval_sec=1.0, stats_update_interval_sec=0)
    warm(w, [0.5] * 300, step=0.1)           # thirty seconds of 10 Hz polls
    assert w.samples == 30
    assert w.history_sec == pytest.approx(29.0)


def test_samples_older_than_the_window_fall_out():
    w = StatsWindow('k', window_minutes=1.0, min_history_minutes=0.5,
                    sample_interval_sec=1.0, stats_update_interval_sec=0)
    warm(w, [0.5] * 200)                     # two hundred seconds
    assert w.samples == 61                   # the last minute, both ends
    assert (w.samples_ts[-1][0] - w.samples_ts[0][0]).total_seconds() == 60


def test_a_gap_is_not_history():
    """Ten minutes either side of a two-hour outage is not a two-hour
    window. The control: the same samples without the gap are warm."""
    w = StatsWindow('k', window_minutes=200, min_history_minutes=20,
                    sample_interval_sec=1.0, stats_update_interval_sec=0)
    warm(w, [0.5] * 600)                                  # 10 min
    warm(w, [0.5] * 600, start=600 + 7200)                # 2 h later, 10 min
    assert w.history_minutes == pytest.approx(20.0, abs=0.1)
    assert not w.is_warm

    control = StatsWindow('k', window_minutes=200, min_history_minutes=20,
                          sample_interval_sec=1.0, stats_update_interval_sec=0)
    warm(control, [0.5] * 1202)
    assert control.is_warm


def test_the_z_of_any_price_uses_the_standing_bands():
    """So the bid and the offer can each be judged on their own."""
    w = window(5)
    warm(w, [1.0, 2.0, 3.0, 4.0, 5.0])
    assert w.z_of(w.mean) == pytest.approx(0.0)
    assert w.z_of(w.mean + 2 * w.std) == pytest.approx(2.0)
    assert w.z_of(None) is None


def test_prime_reloads_a_recorded_window_in_one_step():
    w = StatsWindow('k', window_minutes=2, min_history_minutes=1,
                    sample_interval_sec=1.0, stats_update_interval_sec=300)
    rows = [(at(i), 0.5 + (0.1 if i % 2 else -0.1)) for i in range(100)]
    assert w.prime(rows) == 100
    assert w.is_warm
    assert w.mean == pytest.approx(0.5, abs=1e-9)
    assert w.std > 0


def test_statistics_stay_live_until_the_window_is_warm():
    """The interval holds the bands still for a trader to aim at. Applied
    before the window is full it froze the mean and sigma computed from the
    first two samples — so a contract spent its first minute showing a sigma
    of about one tick and bands nothing could reach."""
    w = window(50, interval=300)
    warm(w, [0.5, 0.51], step=1.0)
    early_std = w.std
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(48)], start=2)
    assert w.is_warm
    assert w.std > early_std * 5            # it kept up with the real series

    # control: now that it IS warm, the interval holds it still
    frozen = w.std
    w.add(9.0, at(60))
    assert w.std == frozen


def test_the_running_history_matches_a_full_measurement():
    """History is kept incrementally — added on each sample, taken off as the
    window trims — so a long replay is not quadratic. It must still agree
    with measuring the window from scratch, gaps and trims included."""
    w = StatsWindow('k', window_minutes=2, min_history_minutes=1,
                    sample_interval_sec=1.0)
    t = 0.0
    for i in range(400):
        t += 60.0 if i in (50, 170) else 1.0          # two gaps
        w.add(0.5 + (i % 7) * 0.01, at(t))
        running = w.history_sec
        w._measure_history()
        assert w.history_sec == pytest.approx(running)
