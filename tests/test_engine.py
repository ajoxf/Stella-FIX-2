"""The loop, end to end against the simulator: open, manage, close, recover."""
import pytest

from fixtrader.config import DEFAULT_SETTINGS, ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract
from fixtrader.models import ContractState, ExitReason, OrderType, Side
from tests.conftest import ALGO_TEST_SETTINGS, book_at_z, fill_candles


def build(tmp_path, **overrides):
    contract = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        venue='SIM', tick_size=0.01, tick_value=1.0,
        contract_multiplier=100.0, min_qty=1.0, qty_step=1.0, max_qty=50.0,
        enabled=True, algo_on=True,
        # The window of the old "lookback 30" at one sample a second: warm at
        # its 30th sample. Entry at 2.0 so a book whose MID is placed at 2.5
        # has its bid comfortably through; one confirming sample, and a
        # margin so the target has a base.
        **{**ALGO_TEST_SETTINGS, **{'window_minutes': 1e6, 'min_history_minutes': 29 / 60.0,
                'sample_interval_sec': 1.0, 'stats_update_interval_sec': 1e9,
                'entry_threshold': 2.0, 'max_entry_z': 3.5,
                'confirm_samples': 1, 'margin_per_contract': 260.0,
                'quantity': 5.0, 'exit_at_mean': False,
                'max_hold_minutes': 0.0,
                'entry_cooldown_seconds': 0.0,
                'entry_order_type': 'MARKET', 'exit_order_type': 'MARKET',
                'commission_per_contract': 1.0,
                'exchange_fee_per_contract': 0.0,
                'clearing_fee_per_contract': 0.0,
                'slippage_budget_ticks': 0.0,
                'profit_target_pct': 2.0}, **overrides})
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    # One-minute candles: a quote held for a candle is not a stale one.
    cfg.settings['MAX_QUOTE_AGE_SEC'] = 90.0
    cfg.contracts['fef'] = contract
    gw = FakeGateway([SimContract('fef', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    db = Database(str(tmp_path / 'test.db'))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    return engine, gw, db, cfg


def warm_the_window(engine, gw, n=40):
    """Fill the band with a known sigma, one one-minute candle at a time."""
    return fill_candles(engine, gw, n=n)


def put_book_at_z(engine, gw, z):
    return book_at_z(engine, gw, z)


def test_auto_trade_off_still_produces_signal_proposal(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    assert engine.set_auto_trade(False)['ok']
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)
    assert rt.proposal['action'] == 'OPEN'
    assert rt.proposal['side'] == 'SELL'
    assert not engine.executor.working
    assert rt.position is None
    assert engine.snapshot()['engine']['auto_trade_enabled'] is False
    assert engine.set_auto_trade(True)['ok']
    engine.poll(now=gw.now)
    assert engine.executor.working


def test_auto_trade_off_disarms_pending_market_escalation(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    engine.executor.escalating['old-order'] = {'intent': 'OPEN'}
    assert engine.set_auto_trade(False)['ok']
    assert engine.executor.escalating == {}
    assert engine.auto_trade_enabled is False


def test_it_warms_before_it_trades(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = engine.runtimes['fef']
    assert engine.state_of(rt, gw.now) is ContractState.WARMING
    warm_the_window(engine, gw)
    assert rt.algo.body['ready']
    assert engine.state_of(rt, gw.now) is not ContractState.WARMING


def test_a_high_z_opens_a_short_and_records_the_state_at_entry(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)
    engine.poll(now=gw.now)              # the fill comes back on the next pass

    pos = rt.position
    assert pos is not None and pos.side is Side.SELL and pos.qty == 5
    # everything the Analysis window will need, written AT THE TIME
    assert pos.entry_z == pytest.approx(2.5, abs=0.1)
    assert pos.entry_mean is not None and pos.entry_std is not None
    assert pos.margin_locked == pytest.approx(260.0 * 5)
    assert pos.break_even is not None and pos.target_price is not None
    assert engine.state_of(rt, gw.now) is ContractState.IN


def test_the_short_target_is_below_the_entry_and_it_closes_there(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    assert pos.target_price < pos.avg_price          # a short leaves lower

    gw.set_book('fef', pos.target_price - 0.02, pos.target_price - 0.01, 50, 50)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    closed = db.closed_positions('fef')
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.TARGET
    assert closed[0].net_pnl is not None
    assert closed[0].pnl_pct_on_margin is not None


def test_the_stop_closes_even_with_every_filter_blocking_entries(tmp_path):
    engine, gw, db, cfg = build(tmp_path, stop_z_on=True, stop_loss_z=3.0)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    # Every entry filter now refusing: the edge needs a thousand times the
    # cost, and the direction says buy only. The exit must not care.
    cfg.contracts['fef'].overrides.update(edge_on=True, edge_multiple=1000.0,
                                          trade_direction='BUY_ONLY')

    put_book_at_z(engine, gw, 3.6)                   # through the stop
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.ZSCORE


def test_a_stale_quote_halts_entries_but_not_the_close(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None

    later = gw.now + __import__('datetime').timedelta(seconds=120)
    engine.poll(now=later)
    assert engine.state_of(rt, later) is ContractState.HALTED
    # ...and CLOSE NOW still works
    assert engine.close_now('fef')['ok'] is True
    engine.poll(now=later)
    assert rt.position is None


def test_close_now_is_recorded_as_its_own_reason(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    engine.close_now('fef')
    engine.poll(now=gw.now)
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.CLOSE_NOW


def test_kill_all_stands_down_without_flattening_by_default(tmp_path):
    """The button pressed in a hurry must not also cross every spread."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    engine.kill_all()
    assert engine.killed and not engine.master_algo
    assert rt.position is not None                    # untouched
    assert engine.state_of(rt, gw.now) is ContractState.HALTED
    # ...and it flattens when explicitly asked
    engine.kill_all(close_positions=True)
    engine.poll(now=gw.now)
    assert rt.position is None


def test_the_algo_switch_is_per_contract_and_persists(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    engine.set_algo('fef', False)
    assert cfg.contracts['fef'].algo_on is False
    reloaded = TraderConfig.from_file(cfg.path)
    assert reloaded.contracts['fef'].algo_on is False


def test_the_position_survives_a_restart(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    entry_z = engine.runtimes['fef'].position.entry_z

    engine2 = Engine(cfg, gw, db=db, simulated=True)
    engine2.start()
    pos = engine2.runtimes['fef'].position
    assert pos is not None and pos.entry_z == pytest.approx(entry_z)
    assert engine2.book_complete


def test_an_unreadable_venue_leaves_the_book_incomplete_not_flat(tmp_path):
    """The failure this rule exists for: an empty read that gets adopted as
    'the venue is flat' closes real positions as orphans."""
    engine, gw, db, cfg = build(tmp_path)
    gw.readable = False
    engine2 = Engine(cfg, gw, db=db, simulated=True)
    engine2.start()
    assert engine2.book_complete is False
    assert engine2.unclaimed == []


def test_a_position_the_book_cannot_explain_is_listed_never_closed(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    # something at the venue that this book knows nothing about
    gw._short['fef'] = {'qty': 2.0, 'avg': 0.5}
    engine2 = Engine(cfg, gw, db=db, simulated=True)
    engine2.start()
    assert engine2.unclaimed and 'Nothing has been closed' in engine2.unclaimed[0]['text']
    assert gw.positions()[0].qty == -2.0             # still there


def test_the_snapshot_carries_every_field_the_window_needs(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    snap = engine.snapshot(now=gw.now)
    c = snap['contracts'][0]
    for key in ('market', 'stats', 'filters', 'costs', 'position', 'state'):
        assert key in c
    for key in ('mean', 'std', 'z', 'buy_at', 'sell_at', 'hurst', 'half_life',
                'warm_pct', 'is_warm'):
        assert key in c['stats']
    assert snap['engine']['refresh_sec'] == 0.5


def test_the_snapshot_reports_unknowns_as_none_not_zero(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    snap = engine.snapshot(now=gw.now)          # nothing has warmed yet
    stats = snap['contracts'][0]['stats']
    assert stats['z'] is None and stats['mean'] is None
    assert snap['contracts'][0]['position'] is None


def test_specs_are_read_from_the_venue_and_labelled(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    contract = cfg.contracts['fef']
    assert contract.spec_source.get('tick_size') == 'venue'


def test_the_entry_z_recorded_is_the_one_the_decision_fired_at(tmp_path):
    """Not the z at the moment the fill lands. The market moves between the
    two, and every figure the Analysis window reports about WHY a trade
    happened is built on the z the algo actually acted on."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)                       # the order goes here
    # The z the rule fired on is the BID's — a short is sold into the bid —
    # not the mid's.
    decided_z = rt.algo.body['z_sell']
    decided_std = rt.algo.body['sigma']
    assert 2.0 <= decided_z < rt.algo.body['z_mid']

    put_book_at_z(engine, gw, 0.4)                # the market moves away...
    engine.poll(now=gw.now)                       # ...before the fill lands
    assert rt.algo.body['z_mid'] == pytest.approx(0.4, abs=0.05)

    assert rt.position is not None
    assert rt.position.entry_z == pytest.approx(decided_z)
    assert rt.position.entry_std == pytest.approx(decided_std)


def test_every_close_carries_an_explicit_close_flag_and_its_tickets(tmp_path):
    """A close is never a bare opposite order. It says it is closing, names
    the position it closes, and carries that position's venue tickets."""
    from fixtrader.models import PositionEffect
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    assert pos is not None and pos.tickets

    sent = []
    original = gw.send
    gw.send = lambda req: (sent.append(req), original(req))[1]

    engine.close_now('fef')
    assert len(sent) == 1
    req = sent[0]
    assert req.position_effect is PositionEffect.CLOSE
    assert req.position_effect.is_close
    assert req.reduce_only is True          # the cap, as well as the flag
    assert req.position_id == pos.id
    assert req.close_tickets == pos.tickets
    assert req.qty == pos.qty               # never more than is open


def test_a_close_leaves_no_second_position_at_the_venue(tmp_path):
    """End to end against a venue that keeps the two sides apart: after a
    close there is nothing left, not a matched pair posting margin."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert gw.positions()[0].short_qty == 5.0

    engine.close_now('fef')
    engine.poll(now=gw.now)
    assert gw.positions() == []
    assert rt.position is None
    # and the algo went down with it, so it does not re-enter on the z that
    # is still sitting where it was
    assert cfg.contracts['fef'].algo_on is False


def test_the_close_flag_follows_the_contract_setting(tmp_path):
    """SHFE and INE price a close-today differently from a close-yesterday,
    so the contract chooses. AUTO reads it from the day it was opened."""
    from fixtrader.models import PositionEffect
    from fixtrader.executor import close_effect
    from datetime import datetime, timezone

    now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

    class P:
        opened_session = '2026-09-08'

    class Older:
        opened_session = '2026-09-07'

    assert close_effect({'close_offset_mode': 'CLOSE'}, P(), now) \
        is PositionEffect.CLOSE
    assert close_effect({'close_offset_mode': 'AUTO'}, P(), now) \
        is PositionEffect.CLOSE_TODAY
    assert close_effect({'close_offset_mode': 'AUTO'}, Older(), now) \
        is PositionEffect.CLOSE_YESTERDAY


def test_an_unknown_close_flag_degrades_to_CLOSE_and_never_to_OPEN(tmp_path):
    """Unknown is not a reason to open a position. A close whose flag could
    not be worked out is still a close."""
    from fixtrader.models import PositionEffect
    from fixtrader.executor import close_effect
    from datetime import datetime, timezone
    now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

    class NoSession:
        opened_session = None

    assert close_effect({'close_offset_mode': 'AUTO'}, NoSession(), now) \
        is PositionEffect.CLOSE
    assert close_effect({'close_offset_mode': 'nonsense'}, None, now) \
        is PositionEffect.CLOSE
    assert close_effect({}, None, now) is PositionEffect.CLOSE


def test_an_escalated_close_is_still_a_close(tmp_path):
    """The limit times out and crosses at market — and the market order that
    replaces it must carry the same close flag, not default to an open."""
    from fixtrader.models import PositionEffect
    engine, gw, db, cfg = build(tmp_path, exit_order_type='LIMIT',
                                exit_limit_timeout_sec=1,
                                exit_on_timeout='CROSS_AT_MARKET')
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None

    engine.close_now('fef')                       # close_now crosses already
    sent = []
    original = gw.send
    gw.send = lambda req: (sent.append(req), original(req))[1]
    later = gw.now + __import__('datetime').timedelta(seconds=30)
    engine.poll(now=later)
    for req in sent:
        assert req.position_effect.is_close


# -- picking up an edited configuration ------------------------------------

def edited(cfg, **settings):
    """A second TraderConfig, as the web process would have written it."""
    from fixtrader.config import TraderConfig, ContractConfig
    new = TraderConfig(path=cfg.path)
    new.settings = dict(cfg.settings, **settings)
    for key, c in cfg.contracts.items():
        clone = ContractConfig(
            key=key, name=c.name, symbol=c.symbol, venue=c.venue,
            tick_size=c.tick_size, tick_value=c.tick_value,
            contract_multiplier=c.contract_multiplier, min_qty=c.min_qty,
            qty_step=c.qty_step, max_qty=c.max_qty, decimals=c.decimals,
            enabled=c.enabled, algo_on=c.algo_on,
            spec_source=dict(c.spec_source), **dict(c.overrides))
        new.contracts[key] = clone
    return new


def test_an_edited_setting_is_in_force_without_a_restart(tmp_path):
    """Settings are edited in the web process, which writes config.json. The
    engine reading it back is the whole point: a saved setting that sits on
    disk while the loop trades the old one is the worst of both."""
    engine, gw, db, cfg = build(tmp_path, entry_threshold=2.0)
    warm_the_window(engine, gw)
    assert engine.config.effective('fef')['entry_threshold'] == 2.0

    new = edited(cfg)
    new.contracts['fef'].overrides['entry_threshold'] = 3.5
    report = engine.apply_config(new)

    assert 'fef settings' in report['changed']
    assert engine.config.effective('fef')['entry_threshold'] == 3.5
    # and the window itself was told, not just the settings dict
    assert engine.runtimes['fef'].window.entry_threshold == 3.5


def test_a_reload_does_not_flip_a_switch_the_trader_just_moved(tmp_path):
    """`algo_on` is the live switch. config.json's copy is whatever the web
    process last wrote, and adopting it would stand a contract down that the
    trader had just armed — or arm one they had stood down."""
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    engine.set_algo('fef', False)
    assert engine.config.contracts['fef'].algo_on is False

    new = edited(cfg)
    new.contracts['fef'].algo_on = True          # the file is out of date
    engine.apply_config(new)
    assert engine.config.contracts['fef'].algo_on is False


def test_a_reload_never_touches_the_book_or_an_open_position(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = engine.runtimes['fef'].position
    assert pos is not None and pos.is_open
    before = (pos.id, pos.qty, pos.avg_price, list(pos.tickets))
    samples = len(engine.runtimes['fef'].window.prices)

    new = edited(cfg)
    new.contracts['fef'].overrides['quantity'] = 9.0
    engine.apply_config(new)

    after = engine.runtimes['fef'].position
    assert (after.id, after.qty, after.avg_price, list(after.tickets)) == before
    assert len(engine.runtimes['fef'].window.prices) == samples


def test_a_new_band_length_is_rebuilt_from_the_recording(tmp_path):
    """A new length is a different series: rebuilt from the recorded mids,
    not thrown away and collected again for hours."""
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    rt = engine.runtimes['fef']
    assert rt.algo.body['ready']

    new = edited(cfg)
    new.contracts['fef'].overrides['length'] = 30
    engine.apply_config(new)
    assert rt.algo.candles.length == 30
    assert rt.algo.candles.stats()['count'] >= 30     # from the recording
    engine.poll(now=gw.now)
    assert rt.algo.body['ready']


def test_a_structural_change_is_reported_and_not_half_applied(tmp_path):
    """A tick value changed under an open position rewrites what that
    position made; a contract added needs the gateway to subscribe. Those
    want a restart, and the screen says so rather than pretending."""
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)

    new = edited(cfg)
    new.contracts['fef'].tick_value = 5.0
    new.settings['DATABASE_PATH'] = 'somewhere-else.db'
    report = engine.apply_config(new)

    assert 'fef.tick_value' in report['restart_needed']
    assert 'DATABASE_PATH' in report['restart_needed']
    assert engine.config.contracts['fef'].tick_value == 1.0
    assert engine.config.settings['DATABASE_PATH'] != 'somewhere-else.db'
    # and the snapshot carries it, so a setting that looks saved and is not
    # never passes for one that is
    snap = engine.snapshot(now=gw.now)
    assert 'fef.tick_value' in snap['engine']['config_restart_needed']
    assert snap['engine']['config_reloaded_at'] is not None


def test_a_contract_added_or_removed_needs_a_restart(tmp_path):
    from fixtrader.config import ContractConfig
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)

    new = edited(cfg)
    new.contracts['other'] = ContractConfig(key='other', symbol='XYZ')
    del new.contracts['fef']
    report = engine.apply_config(new)

    assert 'contract other added' in report['restart_needed']
    assert 'contract fef removed' in report['restart_needed']
    assert set(engine.config.contracts) == {'fef'}       # nothing half-done


def test_a_display_change_is_adopted_live(tmp_path):
    """A rename or a decimals change re-prices nothing. It applies now."""
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    new = edited(cfg)
    new.contracts['fef'].name = 'Iron ore Nov/Dec'
    new.contracts['fef'].decimals = 2
    report = engine.apply_config(new)
    assert 'fef.name' in report['changed'] and 'fef.decimals' in report['changed']
    assert engine.snapshot(now=gw.now)['contracts'][0]['name'] == 'Iron ore Nov/Dec'


# -- escalating an unfilled limit ------------------------------------------

class SlowToCancel:
    """A venue that takes its time. `cancel` is a REQUEST, and until it is
    acted on the resting order is still live and can still fill."""

    def __init__(self, gw):
        self._gw = gw
        self.pending = []

    def __getattr__(self, name):
        return getattr(self._gw, name)

    def cancel(self, clordid):
        self.pending.append(clordid)         # asked for, not done

    def let_the_cancel_through(self):
        for clordid in self.pending:
            self._gw.cancel(clordid)
        self.pending = []


def a_position_with_a_working_limit_exit(tmp_path):
    engine, gw, db, cfg = build(tmp_path, exit_order_type='LIMIT',
                                exit_limit_offset_ticks=20,
                                exit_limit_timeout_sec=1,
                                exit_on_timeout='CROSS_AT_MARKET')
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    slow = SlowToCancel(gw)
    engine.gateway = slow
    engine.executor.gateway = slow
    engine.close_now('fef')                  # crosses at market, so...
    return engine, gw, slow, rt, cfg


def test_an_escalation_waits_for_the_cancel_to_land(tmp_path):
    """`cancel` is a REQUEST. The resting limit is still live at the venue
    until the venue says otherwise, and it can fill in between — so the
    market replacement must not be sent alongside it. Two orders for one
    position means, on a close, a second order with nothing to close; on an
    open, a doubled position."""
    import datetime as dt
    engine, gw, db, cfg = build(tmp_path, entry_order_type='LIMIT',
                                entry_limit_offset_ticks=20,
                                entry_limit_timeout_sec=1,
                                entry_on_timeout='CROSS_AT_MARKET')
    rt = warm_the_window(engine, gw)
    slow = SlowToCancel(gw)
    engine.gateway = slow
    engine.executor.gateway = slow
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)
    assert engine.executor.working_for('fef'), 'a limit entry should be working'

    sent = []
    original = gw.send
    gw.send = lambda req: (sent.append(req), original(req))[1]

    later = gw.now + dt.timedelta(seconds=30)
    engine.poll(now=later)                   # times out; the cancel is asked
    assert slow.pending, 'the cancel was not requested'
    assert sent == [], 'the replacement was sent before the cancel landed'

    slow.let_the_cancel_through()
    engine.poll(now=later)                   # the CANCELLED event is drained
    assert len(sent) == 1                    # NOW it crosses
    # "At market" as an exchange takes it: a limit THROUGH the touch, IOC.
    from fixtrader.models import TimeInForce
    req = sent[0]
    assert req.order_type is OrderType.LIMIT and req.tif is TimeInForce.IOC
    touch = gw.top_of_book('fef').executable(req.side)
    assert (req.price >= touch) if req.side.value == 'BUY' else (req.price <= touch)


def test_a_limit_that_fills_while_the_cancel_is_in_flight_is_not_replaced(tmp_path):
    """The race itself. The limit fills, so there is nothing left to
    escalate — and the market order that would have gone out is the one the
    venue answers with 'no position to close on that side'."""
    import datetime as dt
    engine, gw, db, cfg = build(tmp_path, entry_order_type='LIMIT',
                                entry_limit_offset_ticks=20,
                                entry_limit_timeout_sec=1,
                                entry_on_timeout='CROSS_AT_MARKET')
    rt = warm_the_window(engine, gw)
    slow = SlowToCancel(gw)
    engine.gateway = slow
    engine.executor.gateway = slow
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)
    working = list(engine.executor.working)
    assert working

    later = gw.now + dt.timedelta(seconds=30)
    engine.poll(now=later)                   # times out; cancel requested
    sent = []
    original = gw.send
    gw.send = lambda req: (sent.append(req), original(req))[1]

    vo = gw._orders[working[0]]               # ...and it fills anyway
    gw._fill(vo, vo.remaining, vo.price or 0.5)
    slow.let_the_cancel_through()            # the cancel arrives too late
    engine.poll(now=later)

    escalations = [r for r in sent if 'escalated' in (r.reason or '')]
    assert escalations == [], \
        'a replacement went out for an order that had already filled'
    # The fill stands as one entry, not two. (The engine may legitimately
    # send a CLOSE on the same pass — that is the position being managed,
    # not the escalation being duplicated.)
    assert rt.position is not None
    assert rt.position.opened_qty == 5.0


# -- marking a close on the window -----------------------------------------

def test_a_close_is_published_as_a_fact_not_as_a_sentence(tmp_path):
    """The screen highlights a close green or red. Reading that off the
    wording of the event line would mean a reworded message silently stops
    the window reporting, so the close is published as its own block with
    the net beside it."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    assert engine.snapshot(now=gw.now)['contracts'][0]['last_close'] is None

    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    engine.close_now('fef')
    engine.poll(now=gw.now); engine.poll(now=gw.now)

    close = engine.snapshot(now=gw.now)['contracts'][0]['last_close']
    assert close is not None
    assert close['seq'] == 1
    assert close['side'] in ('BUY', 'SELL')
    assert close['reason'] == 'CLOSE_NOW'
    assert 'net' in close                    # may be None; the key is there


def test_every_close_gets_its_own_sequence_so_two_in_a_row_both_show(tmp_path):
    """The screen marks a close off the COUNT changing. Two trades that
    happen to make the same money must not look like one."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    seqs = []
    for _ in range(2):
        put_book_at_z(engine, gw, 2.5)
        engine.poll(now=gw.now); engine.poll(now=gw.now)
        assert rt.position is not None
        engine.close_now('fef')
        engine.poll(now=gw.now); engine.poll(now=gw.now)
        seqs.append(rt.last_close['seq'])
        engine.set_algo('fef', True)          # close_now stands it down
    assert seqs == [1, 2]


def test_an_unmeasured_net_is_published_as_none_never_as_zero(tmp_path):
    """None is what the screen needs to colour the flash neither green nor
    red. A zero here would state a break-even the system never measured."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    # Make the fees unmeasurable, which is what makes the net unmeasurable.
    engine._fees_for = lambda *a, **k: None
    engine.close_now('fef')
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.last_close['net'] is None


def test_a_halted_window_says_why(tmp_path):
    """HALTED alone reads as a fault. On a quiet book the cause is a price
    that has not moved, and the window says so — with the limit it hit."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    assert engine.halted_by(rt, gw.now) is None          # the control

    later = gw.now + __import__('datetime').timedelta(seconds=120)
    engine.poll(now=later)
    assert engine.state_of(rt, later) is ContractState.HALTED
    why = engine.halted_by(rt, later)
    assert 'price unchanged' in why and 'MAX_QUOTE_AGE_SEC' in why
    snap = [c for c in engine.snapshot(now=later)['contracts']
            if c['key'] == 'fef'][0]
    assert snap['state'] == 'HALTED' and snap['halted_by'] == why

    engine.kill_all()
    assert 'KILL ALL' in engine.halted_by(rt, later)


def test_a_window_without_a_mid_says_what_the_venue_is_quoting(tmp_path):
    """A blank window reads as "not subscribed". On a quiet spread the truth
    is usually one side quoted, or nothing yet."""
    from fixtrader.models import BookTop
    engine, gw, db, cfg = build(tmp_path)
    rt = engine.runtimes['fef']
    rt.book = None
    assert 'no quote' in engine.market_note(rt)
    rt.book = BookTop(bid=None, ask=3.07)
    assert 'only an offer' in engine.market_note(rt)
    rt.book = BookTop(bid=3.05, ask=None)
    assert 'only a bid' in engine.market_note(rt)
    rt.book = BookTop(bid=3.05, ask=3.07)                 # the control
    assert engine.market_note(rt) is None
