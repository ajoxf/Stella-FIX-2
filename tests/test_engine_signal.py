"""The engine around the Algo: confirmation in fresh quotes, paper fills on
a gateway that cannot trade, the band rebuilt from the recording across a
restart, the warm-up, the levels and the day's limits."""
from datetime import timedelta

import pytest

from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract
from fixtrader.models import ExitReason, Side
from tests.conftest import ALGO_TEST_SETTINGS, book_at_z, fill_candles


class PaperGateway(FakeGateway):
    """Quotes like a venue, but — like TT today — cannot take an algo order,
    and reports no margin. Any order reaching it is a test failure."""
    connection_only = True

    def send(self, order):
        raise AssertionError("a paper engine sent an order to the venue")

    def margin_for(self, key, qty):
        return None


def contract(**over):
    return ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        **{**ALGO_TEST_SETTINGS,
           **{'window_minutes': 1e6, 'min_history_minutes': 1.0,
              'entry_threshold': 2.0, 'max_entry_z': 3.5,
              'confirm_samples': 3, 'margin_per_contract': 260.0,
              'quantity': 5.0, 'entry_cooldown_seconds': 0.0,
              'entry_order_type': 'MARKET', 'exit_order_type': 'MARKET',
              'commission_per_contract': 1.0, 'profit_target_pct': 2.0},
           **over})


def build(tmp_path, gateway_cls=FakeGateway, db_name='t.db', now=None,
          **over):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['MAX_QUOTE_AGE_SEC'] = 90.0
    cfg.contracts['fef'] = contract(**over)
    gw = gateway_cls([SimContract('fef', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    if now is not None:
        gw.now = now
    db = Database(str(tmp_path / db_name))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    return engine, gw, db


def step(engine, gw, seconds=1.0):
    engine.poll(now=gw.now)
    gw.now = gw.now + timedelta(seconds=seconds)


def wiggle(gw, px, n):
    """A NEW quote around `px`: the same price polled again is not one."""
    gw.set_book('fef', round(px - 0.005 + n * 1e-4, 4),
                round(px + 0.005 + n * 1e-4, 4), 50, 50)


# -- the confirmation ---------------------------------------------------------

def test_the_confirmation_counts_QUOTES_not_passes(tmp_path):
    """Ten passes over the same quote are one confirmation. Three NEW quotes
    through the band are what enter."""
    engine, gw, _ = build(tmp_path)
    rt = fill_candles(engine, gw)
    px = book_at_z(engine, gw, 2.6)
    for _ in range(10):                          # the same quote, ten passes
        engine.poll(now=gw.now)
    assert rt.position is None
    assert rt.algo.body['state'] == 'CONFIRMING'
    for n in range(1, 4):
        wiggle(gw, px, n)
        engine.poll(now=gw.now)
    engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.SELL


def test_a_quote_back_inside_the_band_resets_the_count(tmp_path):
    engine, gw, _ = build(tmp_path)
    rt = fill_candles(engine, gw)
    px = book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    wiggle(gw, px, 1)
    engine.poll(now=gw.now)
    assert rt.algo.body['streak']['SELL'] == 2
    book_at_z(engine, gw, 0.5)
    engine.poll(now=gw.now)
    assert rt.algo.body['streak']['SELL'] == 0 and rt.position is None


# -- re-entry -------------------------------------------------------------------

def test_reentry_arms_at_the_band_and_enters_on_the_way_back(tmp_path):
    """A trend rides the band and never comes back: the touch alone only
    ARMS. The entry is the way back in, inside the window."""
    engine, gw, _ = build(tmp_path, reentry_on=True, confirm_samples=1)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None and rt.algo.body['armed']['SELL'] is True
    book_at_z(engine, gw, 1.3)                   # back in: 1.5 .. 0.75
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.SELL


def test_reentry_off_enters_on_the_touch(tmp_path):
    """The control."""
    engine, gw, _ = build(tmp_path, reentry_on=False, confirm_samples=1)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None


def test_a_side_that_comes_back_too_far_disarms(tmp_path):
    engine, gw, _ = build(tmp_path, reentry_on=True, confirm_samples=1)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    book_at_z(engine, gw, 0.3)                   # past the window's far edge
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None and rt.algo.body['armed']['SELL'] is False


# -- paper ----------------------------------------------------------------------

def test_a_gateway_that_cannot_trade_is_traded_on_PAPER(tmp_path):
    """TT: the market is live, the order path is not. Filled at the live BID
    for a sale, never the mid — and not one order reaches the venue."""
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1)
    assert engine.paper is True
    assert engine.set_auto_trade(True)['ok']
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    pos = rt.position
    assert pos is not None and pos.side is Side.SELL
    assert pos.avg_price == pytest.approx(rt.book.bid)
    assert pos.is_simulated is True
    assert pos.tickets and pos.tickets[0].startswith('PAPER-')
    assert pos.margin_locked == pytest.approx(260.0 * 5)
    assert pos.target_price is not None and pos.target_price < pos.avg_price
    assert rt.algo.recent[0]['mode'] == 'PAPER' and rt.algo.recent[0]['done']

    # down to the target: bought back on the OFFER
    gw.set_book('fef', pos.target_price - 0.02, pos.target_price - 0.01, 50, 50)
    engine.poll(now=gw.now)
    assert rt.position is None
    closed = db.closed_positions('fef')[0]
    assert closed.is_simulated is True
    assert closed.exit_reason is ExitReason.TARGET
    assert closed.exit_price == pytest.approx(rt.book.ask)
    assert closed.net_pnl is not None and closed.net_pnl > 0


def test_auto_trade_off_is_a_DRY_RUN(tmp_path):
    """The control: the same market with Auto trade off records the signal
    and fills nothing."""
    engine, gw, _ = build(tmp_path, PaperGateway, confirm_samples=1)
    assert engine.set_auto_trade(False)['ok']
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None
    assert rt.proposal and rt.proposal['action'] == 'OPEN'
    last = rt.algo.recent[0]
    assert last['action'] == 'ENTER' and last['mode'] == 'DRY RUN'


def test_close_now_on_paper_closes_at_the_live_price(tmp_path):
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1)
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is not None
    assert engine.close_now('fef')['ok']
    assert rt.position is None
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.CLOSE_NOW


# -- the levels -------------------------------------------------------------------

def test_the_stop_and_target_are_a_percent_of_the_margin_from_break_even(
        tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, stop_loss_on=True,
                          stop_loss_pct=2.0, profit_target_pct=3.0)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    # k = 1.00 / 0.01 = 100 per contract, 5 contracts, margin 1300.
    per_point = 100.0 * 5
    assert pos.break_even < pos.avg_price                 # a short
    assert pos.target_price == pytest.approx(
        pos.break_even - 0.03 * 1300 / per_point)
    assert pos.stop_price == pytest.approx(
        pos.break_even + 0.02 * 1300 / per_point)


def test_the_stop_loss_closes_a_losing_position(tmp_path):
    engine, gw, db = build(tmp_path, confirm_samples=1, stop_loss_on=True,
                           stop_loss_pct=2.0)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    sl = rt.position.stop_price
    gw.set_book('fef', sl + 0.01, sl + 0.02, 50, 50)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.STOP_LOSS


def test_atr_mode_sizes_the_levels_off_the_atr_at_entry(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, stop_loss_on=True,
                          stop_mode='ATR', target_mode='ATR',
                          atr_stop_mult=2.0, atr_target_mult=1.5)
    rt = fill_candles(engine, gw)
    atr = rt.algo.atr()
    assert atr is not None and atr > 0
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    assert pos.target_price == pytest.approx(pos.break_even - 1.5 * rt.entry_atr)
    assert pos.stop_price == pytest.approx(pos.break_even + 2.0 * rt.entry_atr)


def test_with_no_margin_anywhere_nothing_enters_and_the_window_says_why(
        tmp_path):
    engine, gw, _ = build(tmp_path, PaperGateway, confirm_samples=1,
                          margin_per_contract=0.0)
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None
    assert 'no margin entered' in rt.blocked_by


def test_with_no_margin_entered_the_venues_stands_in(tmp_path):
    """The control: FakeGateway reports a margin, so the levels are priced."""
    engine, gw, _ = build(tmp_path, confirm_samples=1, margin_per_contract=0.0)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position.margin_locked == pytest.approx(260.0 * 5)


def test_the_entered_margin_wins_over_the_venues(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, margin_per_contract=400.0)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position.margin_locked == pytest.approx(400.0 * 5)


# -- the gates ----------------------------------------------------------------------

def test_the_warm_up_holds_entries_until_live_prices_were_watched(tmp_path):
    # A minute between passes is a gap, not watching: each counts 10 s.
    engine, gw, _ = build(tmp_path, confirm_samples=1, warmup_min=60.0)
    rt = fill_candles(engine, gw, n=30)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None and 'warming up' in rt.blocked_by


def test_the_warm_up_control(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, warmup_min=3.0)
    rt = fill_candles(engine, gw, n=30)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None


def test_the_days_trade_limit_stops_entries_not_exits(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, max_trades_per_day=1)
    rt = fill_candles(engine, gw)
    rt.algo.day['trades'] = 1
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None and "day's limit" in rt.blocked_by


def test_the_edge_filter_blocks_an_entry_that_cannot_pay(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, edge_on=True,
                          edge_multiple=1000.0)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None and 'edge filter' in rt.blocked_by
    assert rt.algo.last_blocked['side'] == 'SELL'


def test_the_edge_filter_control(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, edge_on=True,
                          edge_multiple=0.1)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None


def test_direction_restricts_entries(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1,
                          trade_direction='BUY_ONLY')
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    book_at_z(engine, gw, -2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.BUY


# -- restart ----------------------------------------------------------------------

def test_a_restart_rebuilds_the_band_from_the_recording(tmp_path):
    """FIX has no bars to backfill from. The band comes back from the mids
    this system recorded, so a restart does not collect for hours."""
    engine, gw, db = build(tmp_path, db_name='r.db')
    fill_candles(engine, gw)
    engine.stop()

    engine2, gw2, _ = build(tmp_path, db_name='r.db', now=gw.now)
    rt = engine2.runtimes['fef']
    engine2.poll(now=gw2.now)
    assert rt.algo.candles.stats()['ready']
    assert 'recorded' in rt.algo.history['note']


def test_a_fresh_book_collects(tmp_path):
    """The control: nothing recorded, nothing to build from."""
    engine, gw, _ = build(tmp_path, db_name='empty.db')
    rt = engine.runtimes['fef']
    engine.poll(now=gw.now)
    assert not rt.algo.candles.stats()['ready']


def test_a_quick_restart_carries_the_warm_up(tmp_path):
    engine, gw, db = build(tmp_path, db_name='w.db', warmup_min=60.0)
    fill_candles(engine, gw, n=40)
    watched = engine.runtimes['fef'].algo.live_sec
    assert watched > 0
    engine.stop()
    engine2, gw2, _ = build(tmp_path, db_name='w.db', warmup_min=60.0,
                            now=gw.now)
    engine2.poll(now=gw2.now)
    assert engine2.runtimes['fef'].algo.live_sec >= watched - 60


def test_a_long_gap_starts_the_warm_up_again(tmp_path):
    """The control: back an hour later, the feed watched is not this one."""
    engine, gw, db = build(tmp_path, db_name='w2.db', warmup_min=60.0)
    fill_candles(engine, gw, n=40)
    engine.stop()
    engine2, gw2, _ = build(tmp_path, db_name='w2.db', warmup_min=60.0,
                            now=gw.now + timedelta(hours=1))
    engine2.poll(now=gw2.now)
    assert engine2.runtimes['fef'].algo.live_sec == 0


def test_standing_the_algo_down_resets_the_warm_up(tmp_path):
    engine, gw, _ = build(tmp_path, warmup_min=60.0)
    rt = fill_candles(engine, gw, n=10)
    assert rt.algo.live_sec > 0
    engine.set_algo('fef', False)
    engine.poll(now=gw.now)
    assert rt.algo.live_sec == 0
