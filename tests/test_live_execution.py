"""PAPER or LIVE for the Algo's orders: off after every restart, armed only
by a person, confirmed every time — and, with the venue's positions unread,
on the trader's explicit word."""
from types import SimpleNamespace

import pytest

from fixtrader.database import Database
from fixtrader.fake_gateway import FakeGateway
from fixtrader.models import Intent, OrderState, Side
from tests.conftest import book_at_z, fill_candles
from tests.test_engine_signal import build


class Venue(FakeGateway):
    """The simulator dressed as a live venue: it has a venue, can be told
    its positions are unreadable, and keeps what it was sent."""
    venue = SimpleNamespace(name='TT-UAT', environment='UAT', account='ACC1')
    readable = True
    sent_ids = None

    def positions(self):
        return super().positions() if self.readable else None

    def positions_status(self):
        return ({'status': 'complete', 'why': None} if self.readable else
                {'status': 'unavailable',
                 'why': 'TT rejected the positions request: Unsupported'})

    def adopt(self, row):
        self.adopted = getattr(self, 'adopted', []) + [row['clordid']]
        return True

    def send(self, order):
        cid = super().send(order)
        self.sent_ids = (self.sent_ids or []) + [cid]
        return cid


def a_desk(tmp_path, readable=True, **over):
    engine, gw, db = build(tmp_path, Venue, confirm_samples=1, **over)
    gw.readable = readable
    if not readable:
        engine.book_complete = False
    return engine, gw, db


def test_a_live_venue_comes_back_on_PAPER(tmp_path):
    engine, gw, db = a_desk(tmp_path)
    assert engine.paper is True and engine.live_armed is False
    snap = engine.snapshot()['engine']['execution']
    assert snap['mode'] == 'PAPER' and snap['can_live'] is True


def test_LIVE_needs_a_confirmation_every_time(tmp_path):
    engine, gw, db = a_desk(tmp_path)
    ask = engine.set_execution('LIVE')
    assert ask['ok'] is False and ask['confirm'] is True
    assert 'REAL orders' in ask['text'] and 'ACC1' in ask['text']
    assert engine.paper is True                        # nothing armed by asking
    assert engine.set_execution('LIVE', confirm=True)['ok']
    assert engine.paper is False and engine.positions_waived is False


def test_unread_positions_are_said_and_waived_only_on_confirmation(tmp_path):
    engine, gw, db = a_desk(tmp_path, readable=False)
    assert engine.set_auto_trade(True)['ok']           # PAPER: always allowed
    engine.set_auto_trade(False)
    ask = engine.set_execution('LIVE')
    assert 'could NOT be read' in ask['text'] and 'Unsupported' in ask['text']
    assert engine.set_execution('LIVE', confirm=True)['positions_waived'] is True
    assert engine.set_auto_trade(True)['ok']           # allowed on the waiver


def test_without_the_waiver_live_auto_trade_waits_for_the_book(tmp_path):
    """The control: unread positions and NO confirmed waiver — automatic
    trading on a live venue is refused."""
    engine, gw, db = a_desk(tmp_path, readable=False)
    engine.live_armed = True                           # armed without the word
    engine.positions_waived = False
    engine.book_complete = False
    refused = engine.set_auto_trade(True)
    assert refused['ok'] is False and 'recovered account book' in refused['error']


def test_live_orders_reach_the_venue_and_paper_ones_do_not(tmp_path):
    engine, gw, db = a_desk(tmp_path, entry_order_type='MARKET')
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.tickets[0].startswith('PAPER-')
    assert not gw.sent_ids                             # PAPER: nothing sent
    engine.close_now('fef')
    engine.set_algo('fef', True)
    assert engine.set_execution('LIVE', confirm=True)['ok']
    engine.set_auto_trade(True)
    rt.algo.signal._cooldown_until = None
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert gw.sent_ids                                 # LIVE: sent
    assert rt.position is not None and not rt.position.tickets[0].startswith('PAPER-')


def test_a_switch_is_refused_while_anything_is_open(tmp_path):
    """A position at the venue does not become a paper one by a setting."""
    engine, gw, db = a_desk(tmp_path, entry_order_type='MARKET')
    assert engine.set_execution('LIVE', confirm=True)['ok']
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    refused = engine.set_execution('PAPER')
    assert refused['ok'] is False and 'open venue position' in refused['error']
    assert engine.paper is False


def test_a_refused_entry_says_so_in_the_venues_words(tmp_path):
    engine, gw, db = a_desk(tmp_path, entry_order_type='MARKET')
    assert engine.set_execution('LIVE', confirm=True)['ok']
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    gw._reject_reason = lambda order: 'Instrument not open for trading'
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    last = rt.algo.recent[0]
    assert last['action'] == 'ENTER' and last['done'] is False
    assert last['result'] == 'Instrument not open for trading'
    assert rt.algo.day['trades'] == 0                  # given back
    assert rt.algo.body['cooldown_sec'] is not None    # not resent next pass


def test_the_startup_sweep_cancels_what_a_previous_run_left_working(tmp_path):
    """Scoped to OUR ids: a manual ticket and somebody else's order are not
    touched; a done order is not cancelled again."""
    db = Database(str(tmp_path / 'sweep.db'))
    for cid, state in (('FT-abc-1', 'WORKING'), ('FT-abc-2', 'FILLED'),
                       ('FTM-9', 'WORKING'), ('OTHER-1', 'WORKING')):
        db.save_order({'clordid': cid, 'contract_key': 'fef', 'side': 'BUY',
                       'qty': 1, 'filled_qty': 0, 'order_type': 'LIMIT',
                       'intent': 'CLOSE', 'state': state, 'price': 0.5,
                       'sent_at': '2026-10-08T09:00:00+00:00',
                       'is_simulated': 0})
    engine, gw, _ = a_desk(tmp_path, db_name='sweep.db')
    engine.poll(now=gw.now)
    assert getattr(gw, 'adopted', []) == ['FT-abc-1']
    assert engine.executor.intent_of('FT-abc-1') is Intent.CLOSE


def test_a_refused_entry_is_not_resent_every_pass(tmp_path):
    """Cooldown 0 on the contract, and the venue refuses: the same order is
    NOT sent again three times a second — and is tried again after the
    retry floor (the control)."""
    from datetime import timedelta
    from fixtrader.algo import ENTRY_RETRY_SEC
    engine, gw, db = a_desk(tmp_path, entry_order_type='MARKET',
                            entry_cooldown_seconds=0)
    assert engine.set_execution('LIVE', confirm=True)['ok']
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    gw._reject_reason = lambda order: 'Instrument not open for trading'
    px = book_at_z(engine, gw, 2.6)
    for i in range(10):
        gw.set_book('fef', round(px - 0.005 + i * 1e-4, 4),
                    round(px + 0.005 + i * 1e-4, 4), 50, 50)
        engine.poll(now=gw.now)
    assert len(gw.sent_ids) == 1
    gw.now += timedelta(seconds=ENTRY_RETRY_SEC + 1)
    gw.set_book('fef', round(px - 0.004, 4), round(px + 0.006, 4), 50, 50)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert len(gw.sent_ids) == 2


def test_reconciling_mid_session_keeps_the_live_position_object(tmp_path):
    engine, gw, db = a_desk(tmp_path, entry_order_type='MARKET')
    assert engine.set_execution('LIVE', confirm=True)['ok']
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    live = rt.position
    assert live is not None
    engine.recover()                               # the venue's answer lands
    assert rt.position is live
