"""Algo or hand — per contract, never both on the same one (the MT5 desk's
one switch per ladder: Off / Dry run / Trades).

Two hands on one book fight: the algo closes a hand-placed position at its
own target, or re-enters the moment the trader gets flat. So an Algo that
TRADES a contract refuses NEW hand orders on it; Off and Dry run leave it to
the hand. A switch is refused while the side being left still holds
something on that contract, and nothing ever refuses a close or a cancel.
"""
import pytest

from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract
from tests.conftest import ALGO_TEST_SETTINGS
from tests.test_engine import put_book_at_z, warm_the_window
from tests.test_manual_terminal import report, sent, terminal, ticket  # noqa: F401


def desk(tmp_path, terminal, mode_path=None):
    """The simulator engine, with a real manual terminal beside it."""
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        security_id='101',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        window_minutes=1e6, min_history_minutes=29 / 60.0,
        sample_interval_sec=1.0, stats_update_interval_sec=1e9,
        entry_threshold=2.0, margin_per_contract=260.0,
        quantity=5.0, exit_at_mean=False, max_hold_minutes=0.0,
        entry_cooldown_seconds=0.0, entry_order_type='MARKET',
        exit_order_type='MARKET', commission_per_contract=1.0,
        profit_target_pct=2.0, **ALGO_TEST_SETTINGS)
    cfg.settings['MAX_QUOTE_AGE_SEC'] = 90.0
    gw = FakeGateway([SimContract('fef', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    gw.terminal = terminal
    engine = Engine(cfg, gw, db=Database(str(tmp_path / 't.db')),
                    simulated=True,
                    mode_path=mode_path or str(tmp_path / 'status.json.mode.json'))
    engine.start()
    return engine, gw


def manual_fill(terminal):
    p = terminal.preview(ticket(quantity='2'))
    oid = terminal.submit({'token': p['token'], 'confirmed': True})['order_id']
    report(terminal, {'35': '8', '11': oid, '39': '2', '150': '2', '17': 'F1',
                      '14': '2', '151': '0', '32': '2', '31': '-0.5', '6': '-0.5'})
    return oid


# -- an Algo that TRADES a contract refuses a hand on it ------------------------

def test_a_trading_algo_refuses_a_new_manual_order_on_its_contract(tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    assert engine.set_algo_state('fef', 'TRADE')['ok']
    assert engine.algo_state('fef') == 'LIVE'           # the simulator trades
    with pytest.raises(ValueError, match='TRADING'):
        terminal.preview(ticket())
    # ...and the screen says so, before anybody fills a ticket
    c = engine.snapshot()['contracts'][0]
    assert c['algo_state'] == 'LIVE' and 'TRADING' in c['manual_block']


def test_off_and_dry_run_let_the_hand_trade(tmp_path, terminal):
    """The control: the same contract with its Algo Off, or in a Dry run
    (signals only), takes a hand order — as on the MT5 desk."""
    engine, _ = desk(tmp_path, terminal)
    for state in ('OFF', 'DRY'):
        assert engine.set_algo_state('fef', state)['ok']
        assert terminal.preview(ticket())['ok']
        assert engine.snapshot()['contracts'][0]['manual_block'] is None


def test_a_hand_order_on_another_instrument_is_never_refused(tmp_path, terminal):
    """Only the contract the Algo trades is refused — not the account."""
    engine, _ = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'TRADE')
    assert engine._manual_block({'security_id': '999'}) is None
    assert engine._manual_block({'security_id': '101'})       # the control


def test_a_ticket_reviewed_by_hand_is_not_sent_once_the_algo_trades(tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'OFF')
    reviewed = terminal.preview(ticket())
    assert engine.set_algo_state('fef', 'TRADE')['ok']
    with pytest.raises(ValueError, match='TRADING'):
        terminal.submit({'token': reviewed['token'], 'confirmed': True})


# -- Off and Dry run: the Algo does not enter -----------------------------------

def test_a_dry_run_shows_the_signal_and_enters_nothing(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    assert engine.set_algo_state('fef', 'DRY')['ok']
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    assert rt.algo.recent and rt.algo.recent[0]['mode'] == 'DRY RUN'
    assert engine.algo_state('fef') == 'DRY'


def test_a_trading_algo_enters(tmp_path, terminal):
    """The control: the same market, its Algo set to trade, is an entry."""
    engine, gw = desk(tmp_path, terminal)
    assert engine.set_algo_state('fef', 'TRADE')['ok']
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None


def test_off_withholds_every_entry(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    assert engine.set_algo_state('fef', 'OFF')['ok']
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None and engine.algo_state('fef') == 'OFF'


def test_trading_one_contract_does_not_set_the_others_trading(tmp_path, terminal):
    """Automatic trading switched on for ONE contract leaves every other
    armed contract a dry run — never trading behind the trader's back."""
    engine, _ = desk(tmp_path, terminal)
    engine.auto_trade_enabled = False                  # as after a restart
    cfg = engine.config
    cfg.contracts['two'] = ContractConfig(key='two', name='Two', symbol='TWO',
                                          venue='SIM', algo_on=True)
    from fixtrader.engine import ContractRuntime
    engine.runtimes['two'] = ContractRuntime(cfg.contracts['two'], cfg.settings)
    assert engine.algo_state('two') == 'DRY'
    assert engine.set_algo_state('fef', 'TRADE')['ok']
    assert engine.algo_state('fef') == 'LIVE'
    assert engine.algo_state('two') == 'DRY'


# -- a switch never leaves the other side's business on the contract ---------

def test_the_algo_cannot_be_set_off_while_it_holds_a_position(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'TRADE')
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    for state in ('OFF', 'DRY'):
        refused = engine.set_algo_state('fef', state)
        assert refused['ok'] is False and 'open algo position' in refused['error']
    with pytest.raises(ValueError, match='TRADING'):
        terminal.preview(ticket())
    # CLOSE NOW still works; flat, it is the hand's again
    engine.close_now('fef'); engine.poll(now=gw.now)
    assert rt.position is None
    assert engine.algo_state('fef') == 'OFF'
    assert terminal.preview(ticket())['ok']


def test_the_algo_cannot_trade_a_contract_a_hand_is_holding(tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'OFF')
    oid = manual_fill(terminal)
    refused = engine.set_algo_state('fef', 'TRADE')
    assert refused['ok'] is False and 'not yet closed' in refused['error']
    assert engine.algo_state('fef') == 'OFF'
    # closing it is always allowed; closed, the switch goes through
    close = terminal.preview_close({'order_id': oid})
    cid = terminal.submit({'token': close['token'], 'confirmed': True})['order_id']
    report(terminal, {'35': '8', '11': cid, '39': '2', '150': '2', '17': 'F2',
                      '14': '2', '151': '0', '32': '2', '31': '-0.5'})
    assert engine.set_algo_state('fef', 'TRADE')['ok']


def test_a_hand_position_withholds_the_algos_entries(tmp_path, terminal):
    """Even armed, the Algo does not enter a contract a hand is holding."""
    engine, gw = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'OFF')
    manual_fill(terminal)
    engine.config.contracts['fef'].algo_on = True       # forced, as a restart might
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None and 'a hand is holding' in rt.blocked_by


def test_a_manual_close_is_never_refused(tmp_path, terminal):
    """If a hand position is ever on a contract its Algo trades — a restart,
    a recovered order — the way out is never the thing that is refused."""
    engine, _ = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'OFF')
    oid = manual_fill(terminal)
    engine.dry_run.discard('fef')
    engine.config.contracts['fef'].algo_on = True       # forced: trading now
    assert engine.algo_state('fef') == 'LIVE'
    close = terminal.preview_close({'order_id': oid})
    assert terminal.submit({'token': close['token'], 'confirmed': True})['ok']


def test_a_dry_run_survives_a_restart(tmp_path, terminal):
    path = str(tmp_path / 'mode.json')
    engine, _ = desk(tmp_path, terminal, mode_path=path)
    engine.set_algo_state('fef', 'DRY')
    again, _ = desk(tmp_path, terminal, mode_path=path)
    assert again.algo_state('fef') == 'DRY'
    assert again.snapshot()['contracts'][0]['algo_state'] == 'DRY'


# -- SIGNALS: the Algo signals and watches, the trader trades -------------------

def test_signals_mode_watches_the_traders_position_with_the_algos_levels(tmp_path, terminal):
    """The trader's own position, in SIGNALS mode, gets the Algo's break-even,
    target and stop — the same `levels` the Algo prices its own with — and
    the screen shows it as THEIRS."""
    from fixtrader import algo as algo_mod
    engine, gw = desk(tmp_path, terminal)
    assert engine.set_algo_state('fef', 'SIGNALS')['ok']
    assert engine.algo_state('fef') == 'DRY'
    rt = warm_the_window(engine, gw)
    manual_fill(terminal)                                # BUY 2 @ -0.5
    engine.poll(now=gw.now)
    watched = engine.snapshot()['contracts'][0]['algo']['positions'][0]
    assert watched['manual'] is True and watched['side'] == 'BUY'
    assert watched['quantity'] == 2 and watched['entry'] == -0.5
    money = engine._algo_costs(rt.contract, engine.config.effective('fef'), 2)
    be, tp, sl, _ = algo_mod.levels(
        'BUY', -0.5, money['fee_points'], rt.algo.params, money['k'] * 2,
        engine._margin(rt.contract, engine.config.effective('fef'), 2),
        rt.manual_watch['atr'])
    assert (watched['break_even'], watched['tp'], watched['sl']) == (be, tp, sl)
    assert rt.position is None                           # nothing booked as the Algo's


def test_a_watched_position_at_its_target_is_signalled_never_closed(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'SIGNALS')
    rt = warm_the_window(engine, gw)
    manual_fill(terminal)                                # far below: TP already reached
    before = len(sent(terminal, 'D'))                    # the entry ticket
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    alert = rt.signal_alert
    assert alert is not None and alert['kind'] == 'EXIT'
    assert 'close your BUY 2' in alert['text']
    # said once: the same reason on the next pass is not a second alert
    seq = alert['seq']
    engine.poll(now=gw.now)
    assert rt.signal_alert['seq'] == seq
    # and nothing was closed for the trader
    assert terminal.open_business()
    assert len(sent(terminal, 'D')) == before == 1       # no close was sent


def test_signals_mode_alerts_an_entry_and_sends_nothing(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    engine.set_algo_state('fef', 'SIGNALS')
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    assert rt.signal_alert is not None and rt.signal_alert['kind'] == 'ENTRY'
    assert 'H to L' in rt.signal_alert['text']


def test_no_signal_alert_when_the_algo_trades_or_is_off(tmp_path, terminal):
    """The control: a trading Algo ENTERS (no alert to take it by hand), and
    an Algo that is Off neither enters nor alerts."""
    for state, entered in (('TRADE', True), ('OFF', False)):
        where = tmp_path / state
        where.mkdir()
        engine, gw = desk(where, terminal)
        engine.set_algo_state('fef', state)
        rt = warm_the_window(engine, gw)
        put_book_at_z(engine, gw, 2.5)
        engine.poll(now=gw.now); engine.poll(now=gw.now)
        assert (rt.position is not None) is entered
        assert rt.signal_alert is None
