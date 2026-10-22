"""A Take Profit or Stop Loss CLOSES the position it was raised for — by
that position's id and its venue tickets — and never opens the opposite
direction. Each layer is tested on its own: the order that is sent, and the
book that applies what comes back."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from fixtrader.fake_gateway import FakeGateway
from fixtrader.models import (ExitReason, Fill, Intent, PositionEffect, Side)
from tests.conftest import book_at_z, fill_candles
from tests.test_engine_signal import build


class Recording(FakeGateway):
    """The simulator, keeping every order request it was sent."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requests = []

    def send(self, order):
        self.requests.append(order)
        return super().send(order)


def a_short(tmp_path, **over):
    engine, gw, db = build(tmp_path, Recording, confirm_samples=1,
                           stop_loss_on=True, stop_loss_pct=2.0,
                           profit_target_pct=2.0, **over)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.SELL
    return engine, gw, db, rt


def the_close(gw):
    closes = [r for r in gw.requests if r.intent is Intent.CLOSE]
    assert len(closes) == 1, closes
    return closes[0]


@pytest.mark.parametrize('where,reason', [('target', ExitReason.TARGET),
                                          ('stop', ExitReason.STOP_LOSS)])
def test_a_take_profit_or_stop_loss_closes_by_position_and_ticket(
        tmp_path, where, reason):
    engine, gw, db, rt = a_short(tmp_path)
    pos = rt.position
    tickets, pid, qty = list(pos.tickets), pos.id, pos.qty
    level = pos.target_price if where == 'target' else pos.stop_price
    if where == 'target':
        gw.set_book('fef', round(level - 0.02, 4), round(level - 0.01, 4), 50, 50)
    else:
        gw.set_book('fef', round(level + 0.01, 4), round(level + 0.02, 4), 50, 50)
    engine.poll(now=gw.now); engine.poll(now=gw.now)

    close = the_close(gw)
    assert close.side is Side.BUY                     # buys back the short
    assert close.position_effect is not PositionEffect.OPEN
    assert close.position_effect is PositionEffect.CLOSE
    assert close.reduce_only is True                  # a cap, sent as well
    assert close.position_id == pid                   # by position id
    assert close.close_tickets == tickets             # and by its tickets
    assert close.qty == qty                           # never more than open
    assert rt.position is None
    assert db.closed_positions('fef')[0].exit_reason is reason
    assert db.open_positions() == []                  # nothing the other way
    # and the exit is not sent again on the next passes
    for _ in range(5):
        engine.poll(now=gw.now)
    assert len([r for r in gw.requests if r.intent is Intent.CLOSE]) == 1


def test_an_unknown_close_flag_is_still_a_close(tmp_path):
    engine, gw, db, rt = a_short(tmp_path, close_offset_mode='WHATEVER')
    engine.close_now('fef')
    assert the_close(gw).position_effect is PositionEffect.CLOSE


# -- what comes back is applied as a close, never as a new position ----------

def fill_event(rt, side, qty, price, clordid='FT-LOST'):
    fill = Fill(venue='SIM', exec_id='EX-' + clordid, clordid=clordid,
                contract_key=rt.contract.key, side=side, qty=qty, price=price)
    return SimpleNamespace(fill=fill, clordid=clordid, kind='FILL',
                           contract_key=rt.contract.key, text='', order=None)


def test_a_close_forgotten_by_a_restart_is_read_back_as_a_close(tmp_path):
    """The engine restarts with a close still working; cancel is a request,
    and the close fills. Its purpose is read from the orders table it was
    written to when sent — not defaulted to OPEN."""
    engine, gw, db, rt = a_short(tmp_path, exit_order_type='LIMIT',
                                 exit_limit_offset_ticks=5)
    engine.close_now('fef')                    # a limit far from the market
    wo = engine.executor.working_for('fef')[0]
    assert wo.is_close
    engine.executor.intents.clear()            # the restart forgets
    assert engine.executor.intent_of(wo.clordid) is Intent.CLOSE
    engine._handle_event(fill_event(rt, Side.BUY, rt.position.qty, 0.40,
                                    wo.clordid), gw.now)
    assert rt.position is None
    assert db.open_positions() == []


def test_an_opposite_fill_recorded_as_an_open_still_only_reduces(tmp_path):
    """The book's own guard, with the order record gone as well: a BUY fill
    against an open short is applied as a close. It is never averaged into
    the short, and never opens a long."""
    engine, gw, db, rt = a_short(tmp_path)
    engine._handle_event(fill_event(rt, Side.BUY, rt.position.qty, 0.40),
                         gw.now)
    assert rt.position is None
    assert db.open_positions() == []
    assert db.closed_positions('fef')[0].side is Side.SELL


def test_the_guard_control_a_same_side_open_still_adds(tmp_path):
    """The control: a SELL that really is an open, on a short, adds to it."""
    engine, gw, db, rt = a_short(tmp_path)
    before = rt.position.qty
    engine.executor.intents['FT-ADD'] = Intent.OPEN
    engine._handle_event(fill_event(rt, Side.SELL, 2.0, 0.60, 'FT-ADD'),
                         gw.now)
    assert rt.position.qty == before + 2.0


def test_a_close_bigger_than_the_position_books_no_excess(tmp_path):
    engine, gw, db, rt = a_short(tmp_path)
    open_qty = rt.position.qty
    engine.executor.intents['FT-BIG'] = Intent.CLOSE
    engine._handle_event(fill_event(rt, Side.BUY, open_qty + 3, 0.40,
                                    'FT-BIG'), gw.now)
    assert rt.position is None
    assert db.open_positions() == []
    said = [e['text'] for e in db.events(contract_key='fef')]
    assert any('excess is not booked' in t for t in said)
    assert db.closed_positions('fef')[0].net_pnl is not None


def test_a_close_with_nothing_open_opens_nothing(tmp_path):
    engine, gw, db = build(tmp_path, Recording)
    rt = engine.runtimes['fef']
    engine.executor.intents['FT-LATE'] = Intent.CLOSE
    engine._handle_event(fill_event(rt, Side.BUY, 5.0, 0.40, 'FT-LATE'),
                         gw.now)
    assert rt.position is None and db.open_positions() == []
    assert 'nothing open' in rt.last_event


def test_a_same_side_fill_recorded_as_a_close_is_not_applied(tmp_path):
    engine, gw, db, rt = a_short(tmp_path)
    before = rt.position.qty
    engine.executor.intents['FT-ODD'] = Intent.CLOSE
    engine._handle_event(fill_event(rt, Side.SELL, 1.0, 0.60, 'FT-ODD'),
                         gw.now)
    assert rt.position.qty == before                  # neither grown nor shrunk
    assert 'not applied' in rt.last_event


def test_a_paper_take_profit_closes_and_opens_nothing(tmp_path):
    from tests.test_engine_signal import PaperGateway
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1,
                           stop_loss_on=True, entry_cooldown_seconds=3600)
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    tp = rt.position.target_price
    gw.set_book('fef', tp - 0.02, tp - 0.01, 50, 50)
    for _ in range(5):
        engine.poll(now=gw.now)
        gw.now += timedelta(seconds=1)
    assert rt.position is None and db.open_positions() == []
    assert len(db.closed_positions('fef')) == 1


# -- a manual close (Instruments & orders) ------------------------------------

from tests.test_manual_terminal import terminal  # noqa: E402,F401  the fixture


def test_a_manual_close_names_its_ticket_and_says_close_to_tt(terminal):
    """Closing a hand ticket sends the opposite side flagged CLOSE (tag 77=C),
    tied to the ticket it closes, capped at that ticket's unclosed fills —
    never an opposite order that opens."""
    from tests.test_manual_terminal import report, sent, ticket
    term = terminal
    preview = term.preview(ticket(quantity='2'))
    oid = term.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(term, {'35': '8', '11': oid, '37': 'TT-1', '39': '2', '150': '2',
                  '17': 'EX-A', '14': '2', '151': '0', '32': '2', '31': '-0.5',
                  '6': '-0.5'})
    close = term.preview_close({'order_id': oid})
    term.submit({'token': close['token'], 'confirmed': True})
    fields = sent(term, 'D')[-1]
    assert fields['54'] == '2'                  # the opposite side: SELL
    assert fields['77'] == 'C'                  # flagged CLOSE to TT
    assert float(fields['38']) == 2.0           # capped at the ticket's fills
    assert fields['58'] == 'Close ' + oid       # tied to the ticket it closes
    with pytest.raises(ValueError):             # nothing left: no second close
        term.preview_close({'order_id': oid})
