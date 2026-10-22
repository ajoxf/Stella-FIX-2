"""The Algo's pipeline end to end, against an independent calculation.

A gold-like contract (0.10 tick, $10 a tick) is driven second by second
through the REAL engine: the mid it is fed must be the mid it records, the
candles, EMA middle, population sigma and both z-scores must equal a
from-scratch computation, and a restart rebuilt from the recording must
read the band exactly as the live engine does at the same instant.
"""
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import algo as algo_mod
from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract


def desk(tmp_path, **over):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['gc'] = ContractConfig(
        key='gc', name='GC Dec26', symbol='GC', venue='SIM', tick_size=0.1,
        tick_value=10.0, contract_multiplier=100.0, enabled=True, algo_on=True,
        margin_per_contract=15000.0, timeframe_min=1, **over)
    gw = FakeGateway([SimContract('gc', mid=4221.5, tick_size=0.1, tick_value=10.0, size=5.0)])
    gw.now = datetime(2026, 10, 12, 13, 0, 0, tzinfo=timezone.utc)
    db = Database(str(tmp_path / 'desk.db'))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    return cfg, gw, db, engine


def walk(engine, gw, seconds, every=2, seed=7):
    random.seed(seed)
    mid, fed = 4221.5, []
    for _ in range(0, seconds, every):
        if random.random() < 0.5:
            mid = round(mid + random.choice([-0.1, 0.1]) * random.choice([1, 1, 2]), 1)
        gw.set_book('gc', round(mid - 0.1, 1), round(mid + 0.1, 1), 5, 5)
        engine.poll(now=gw.now)
        fed.append((gw.now.timestamp(), mid))
        gw.now += timedelta(seconds=every)
    return fed


def independent_band(fed, p):
    tf = p['timeframe_min'] * 60
    candles = {}
    for t, m in fed:
        candles[math.floor(t / tf) * tf] = m          # a candle closes on its last mid
    closes = [candles[k] for k in sorted(candles)][-(algo_mod.candles_kept(p) + 1):]
    n = p['length']
    alpha, ema = 2 / (n + 1), sum(closes[:n]) / n     # Pine's ta.ema
    for x in closes[n:]:
        ema = alpha * x + (1 - alpha) * ema
    last = closes[-n:]
    mu = sum(last) / n
    sigma = math.sqrt(sum((x - mu) ** 2 for x in last) / n)   # population
    return ema, sigma


def test_the_live_band_and_z_are_what_a_hand_calculation_says(tmp_path):
    cfg, gw, db, engine = desk(tmp_path)
    fed = walk(engine, gw, 50 * 60)
    p = algo_mod.params_from_settings(cfg.effective('gc'))
    body = engine.runtimes['gc'].algo.body
    mean, sigma = independent_band(fed, p)
    assert body['ready']
    assert body['mean'] == pytest.approx(mean, abs=1e-9)
    assert body['sigma'] == pytest.approx(sigma, abs=1e-9)
    book = engine.runtimes['gc'].book
    assert body['z_buy'] == pytest.approx((book.ask - mean) / sigma, abs=1e-9)   # a buy pays the offer
    assert body['z_sell'] == pytest.approx((book.bid - mean) / sigma, abs=1e-9)  # a sell gets the bid
    # What was recorded is exactly what was fed — in trader prices.
    fedmap = {int(t): m for t, m in fed}
    rows = db.samples_between('gc')
    assert len(rows) == len(fed)
    assert all(abs(fedmap[int(ts.timestamp())] - px) < 1e-9 for ts, px, *_ in rows)


def test_a_restart_reads_the_band_exactly_as_the_live_engine(tmp_path):
    cfg, gw, db, engine = desk(tmp_path)
    walk(engine, gw, 50 * 60)
    engine.poll(now=gw.now)
    live = engine.runtimes['gc'].algo.body
    again = Engine(cfg, gw, db=db, simulated=True)
    again.start()
    again.poll(now=gw.now)
    rebuilt = again.runtimes['gc'].algo.body
    assert rebuilt['mean'] == pytest.approx(live['mean'], abs=1e-9)
    assert rebuilt['sigma'] == pytest.approx(live['sigma'], abs=1e-9)


def test_the_trend_filter_is_measured_on_one_minute_candles(tmp_path):
    """Found checking the engine: the trend filter's 120-minute lookback on
    1-minute candles needs N + 120 closes, the band kept 5 x N = 100, and it
    said "not enough candles" — blocking every entry — for ever."""
    cfg, gw, db, engine = desk(tmp_path)
    p = algo_mod.params_from_settings(cfg.effective('gc'))
    assert p['trend_on'] and p['trend_lookback_min'] == 120
    walk(engine, gw, (p['length'] + 125) * 60, every=5)
    rt = engine.runtimes['gc']
    assert len(rt.algo.candles.closes()) >= p['length'] + 121
    assert rt.algo.body['filters']['trend']['drift_sigma'] is not None


def test_candles_kept_covers_the_lookback_and_never_less_than_five_n():
    """The control: 15-minute candles need only N + 8 — five N still kept."""
    base = algo_mod.params_from_settings({})
    assert base['timeframe_min'] == 15
    assert algo_mod.candles_kept(base) == 5 * base['length']
    one = dict(base, timeframe_min=1)
    assert algo_mod.candles_kept(one) == base['length'] + 120 + 1


def test_a_book_wider_than_the_limit_holds_entries_and_says_why(tmp_path):
    """"Book no wider than" was on the settings screen and enforced nowhere:
    on UAT gold went 4218.0 / 4230.0 and every round trip paid 12 points."""
    cfg, gw, db, engine = desk(tmp_path, max_book_spread_ticks=3)
    walk(engine, gw, 30 * 60)
    gw.set_book('gc', 4218.0, 4230.0, 5, 5)
    engine.poll(now=gw.now)
    health = engine.runtimes['gc'].algo.body.get('health') or ''
    assert 'book is 120 ticks wide' in health and 'over the 3-tick limit' in health


def test_a_book_inside_the_limit_or_no_limit_is_not_held(tmp_path):
    """The control: 2 ticks wide under a 3-tick limit, and 120 with none set."""
    cfg, gw, db, engine = desk(tmp_path, max_book_spread_ticks=3)
    walk(engine, gw, 30 * 60)
    assert 'ticks wide' not in (engine.runtimes['gc'].algo.body.get('health') or '')
    cfg2, gw2, db2, engine2 = desk(tmp_path / 'b' if (tmp_path / 'b').mkdir() is None else tmp_path)
    walk(engine2, gw2, 30 * 60)
    gw2.set_book('gc', 4218.0, 4230.0, 5, 5)
    engine2.poll(now=gw2.now)
    assert 'ticks wide' not in (engine2.runtimes['gc'].algo.body.get('health') or '')
