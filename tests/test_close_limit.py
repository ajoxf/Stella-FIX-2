"""Close @ LMT on the desk ladder, CLOSE ALL over a working close, and the
TT fills tape behind the Account page's Fills tab.

A Close @ LMT rests ONE closing limit at the trader's price, by the
position's own tickets, and waits there: no re-peg, no timeout. CLOSE ALL
over it escalates it (cancel, then market for what is left) — never a second
close beside the first."""
from types import SimpleNamespace

from fixtrader.gateway import AlgoOrderRouter
from fixtrader.models import ExitReason, Intent, OrderState, OrderType, Side
from tests.conftest import book_at_z, fill_candles
from tests.test_engine_signal import PaperGateway, build
from tests.test_live_execution import a_desk


def paper_long(tmp_path):
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1)
    assert engine.paper
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, -2.6)                     # L to H: buy the offer
    engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.BUY
    return engine, gw, db, rt


def test_paper_close_limit_waits_then_fills_at_the_touch(tmp_path):
    engine, gw, db, rt = paper_long(tmp_path)
    target = round(rt.book.bid + 0.05, 2)
    r = engine.close_at_limit('fef', target)
    assert r['ok'] and r['paper'] and r['algo_stood_down']
    assert rt.contract.algo_on is False                  # stood down with it
    engine.poll(now=gw.now)
    assert rt.position is not None                       # not there yet
    assert engine.snapshot()['contracts'][0]['close_limit']['price'] == target
    gw.set_book('fef', target, round(target + 0.01, 2), 50, 50)
    engine.poll(now=gw.now)
    assert rt.position is None
    closed = db.closed_positions()[0]
    assert closed.exit_reason is ExitReason.CLOSE_LIMIT


def test_a_second_close_is_refused_while_one_rests(tmp_path):
    engine, gw, db, rt = paper_long(tmp_path)
    assert engine.close_at_limit('fef', rt.book.bid + 0.05)['ok']
    again = engine.close_at_limit('fef', rt.book.bid + 0.07)
    assert again['ok'] is False and 'already working' in again['error']


def test_close_all_over_a_paper_close_limit_closes_once(tmp_path):
    engine, gw, db, rt = paper_long(tmp_path)
    assert engine.close_at_limit('fef', rt.book.bid + 0.05)['ok']
    assert engine.close_now('fef')['ok']
    assert rt.position is None and rt.paper_close_limit is None
    assert len(db.closed_positions()) == 1


def test_close_limit_needs_a_position_and_a_price(tmp_path):
    engine, gw, db = build(tmp_path, PaperGateway)
    assert engine.close_at_limit('fef', 0.5)['ok'] is False
    engine, gw, db, rt = paper_long(tmp_path / 'b')
    assert engine.close_at_limit('fef', 'abc')['ok'] is False


def live_long(tmp_path):
    engine, gw, db = a_desk(tmp_path, entry_order_type='MARKET')
    assert engine.set_execution('LIVE', confirm=True)['ok']
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, -2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.BUY
    return engine, gw, db, rt


def test_live_close_limit_rests_pinned_by_ticket(tmp_path):
    engine, gw, db, rt = live_long(tmp_path)
    sent = len(gw.sent_ids)
    far = round(rt.book.bid + 0.20, 2)
    r = engine.close_at_limit('fef', far)
    assert r['ok']
    wo = engine.executor.working[r['clordid']]
    assert wo.pinned and wo.intent is Intent.CLOSE
    assert wo.order_type is OrderType.LIMIT and wo.price == far
    assert wo.side is Side.SELL and wo.qty == rt.position.qty
    assert len(gw.sent_ids) == sent + 1
    # Many passes and a moving book: the trader's price stays where it was.
    for i in range(5):
        gw.set_book('fef', round(rt.book.bid + 0.01, 2),
                    round(rt.book.ask + 0.01, 2), 50, 50)
        engine.poll(now=gw.now)
    assert wo.price == far and len(gw.sent_ids) == sent + 1


def test_an_unpinned_exit_limit_is_repriced(tmp_path):
    """The control: the Algo's own exit limit DOES follow the touch."""
    from datetime import datetime, timezone
    from fixtrader.executor import Executor, WorkingOrder

    class Gw:
        amended = []

        def amend(self, cid, price=None):
            self.amended.append((cid, price))

    ex = Executor(Gw())
    contract = SimpleNamespace(key='fef', tick_size=0.01)
    book = SimpleNamespace(bid=0.50, ask=0.51,
                           executable=lambda side: 0.51 if side is Side.BUY else 0.50)
    now = datetime.now(timezone.utc)
    for pinned in (True, False):
        wo = WorkingOrder('C-%s' % pinned, 'fef', Side.SELL, 1, OrderType.LIMIT,
                          Intent.CLOSE, 0.70, now, 0.50)
        wo.state = OrderState.WORKING
        wo.pinned = pinned
        ex.working[wo.clordid] = wo
    ex.manage(contract, {'exit_limit_offset_ticks': 0,
                         'repeg_dead_band_ticks': 1}, book, now)
    assert [cid for cid, _ in Gw.amended] == ['C-False']


def test_close_all_escalates_the_working_close_never_doubles_it(tmp_path):
    engine, gw, db, rt = live_long(tmp_path)
    r = engine.close_at_limit('fef', round(rt.book.bid + 0.20, 2))
    pinned = r['clordid']
    sent = len(gw.sent_ids)
    out = engine.close_now('fef')
    assert out['ok'] and out['escalated'] == 1
    assert len(gw.sent_ids) == sent                     # nothing new YET
    assert pinned in engine.executor.escalating
    for _ in range(3):
        engine.poll(now=gw.now)
    # The cancel landed: ONE market close for what was open, then flat.
    assert rt.position is None
    assert len(gw.sent_ids) == sent + 1


def test_close_all_with_nothing_working_sends_the_close(tmp_path):
    """The control: no close working, so CLOSE ALL sends one."""
    engine, gw, db, rt = live_long(tmp_path)
    sent = len(gw.sent_ids)
    out = engine.close_now('fef')
    assert out['ok'] and 'escalated' not in out
    engine.poll(now=gw.now)
    assert len(gw.sent_ids) == sent + 1 and rt.position is None


# -- the TT fills tape ----------------------------------------------------------

def router():
    gw = SimpleNamespace(venue=SimpleNamespace(name='TT', account='ACC'),
                         contracts=[SimpleNamespace(key='fef', security_id='777')],
                         _redact=lambda t: t)
    return AlgoOrderRouter(gw)


def report(**over):
    f = {'35': '8', '150': 'F', '39': '2', '17': 'E1', '11': 'OTHER-1',
         '37': 'TT9', '1': 'ACC', '48': '777', '55': 'FEF', '54': '1',
         '77': 'O', '32': '2', '31': '0.55', '14': '2', '151': '0',
         '60': '20261008-10:00:00.123'}
    f.update(over)
    return f


def test_every_tt_fill_is_taped_ours_or_not_and_once():
    r = router()
    r.on_message('8', report(), '')
    r.on_message('8', report(), '')                      # resent: one fill
    r.on_message('8', report(**{'17': 'E2', '11': 'FTM-3'}), '')
    r.on_message('8', report(**{'17': 'E3', '11': r.prefix + '-1',
                                '77': 'C', '54': '2'}), '')
    r.on_message('8', report(**{'17': 'E4', '150': '0', '32': '0'}), '')  # an ack
    tape = r.take_tape()
    assert [t['exec_id'] for t in tape] == ['E1', 'E2', 'E3']
    assert [t['ours'] for t in tape] == ['', 'MANUAL', 'ALGO']
    first = tape[0]
    assert first['side'] == 'BUY' and first['open_close'] == 'OPEN'
    assert first['qty'] == 2.0 and first['price'] == 0.55
    assert first['contract_key'] == 'fef' and first['tt_time'].startswith('20261008')
    assert tape[2]['open_close'] == 'CLOSE'
    assert r.take_tape() == []                           # taken once
    # A foreign fill is on the tape and NOT an event for the book.
    assert r.drain() == []


def test_the_tape_is_kept_and_served(tmp_path):
    from fixtrader.database import Database
    db = Database(str(tmp_path / 't.db'))
    r = router()
    r.on_message('8', report(), '')
    db.save_tt_fills(r.take_tape())
    db.save_tt_fills([dict(report(), exec_id='E1')])    # INSERT OR IGNORE
    rows = db.tt_fills()
    assert len(rows) == 1 and rows[0]['account'] == 'ACC'


# -- the Account page --------------------------------------------------------------

def test_the_account_page_and_its_journal(tmp_path, tick_value=1.0):
    """/account renders the Trading Monitor tabs; /api/journal serves the TT
    fills tape, with the P&L of a CLOSING fill of ours against the position
    it closed — and none on a fill that is not ours."""
    from datetime import datetime, timezone
    from fixtrader.config import ContractConfig, TraderConfig
    from fixtrader.database import Database
    from fixtrader.models import Position
    from fixtrader.webapp import create_app

    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'j.db')
    cfg.contracts['fef'] = ContractConfig(key='fef', name='Iron ore', symbol='FEF',
                                          tick_size=0.01, tick_value=tick_value,
                                          security_id='777')
    cfg.save()
    db = Database(str(tmp_path / 'j.db'))
    now = datetime.now(timezone.utc)
    pid = db.save_position(Position(contract_key='fef', side=Side.BUY, qty=0,
                                    opened_qty=2, avg_price=0.50, opened_at=now,
                                    closed_at=now, exit_price=0.55, net_pnl=8.0,
                                    tickets=['E1']))
    db.save_order({'clordid': 'FT-x-2', 'contract_key': 'fef', 'side': 'SELL',
                   'qty': 2, 'filled_qty': 2, 'order_type': 'MARKET',
                   'intent': 'CLOSE', 'state': 'FILLED', 'position_id': pid,
                   'sent_at': now.isoformat()})
    r = router()
    r.on_message('8', report(**{'17': 'E9', '11': 'FT-x-2', '54': '2', '77': 'C',
                                '31': '0.55'}), '')
    r.on_message('8', report(**{'17': 'E10'}), '')
    db.save_tt_fills(r.take_tape())
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 's.json'),
                     str(tmp_path / 'c.jsonl'), str(tmp_path / 'r.json'))
    c = app.test_client()
    page = c.get('/account').data
    for tab in (b'data-tab="positions"', b'data-tab="orders"', b'data-tab="fills"',
                b'data-tab="slippage"', b'data-tab="accounts"', b'data-tab="reconcile"',
                b'data-tab="analysis"'):
        assert tab in page
    body = c.get('/api/journal').get_json()
    fills = {f['exec_id']: f for f in body['tt_fills']}
    if tick_value is None:
        # No tick value: the P&L is unknown — a dash, never a page that fails.
        assert fills['E9']['pnl'] is None and body['orders']
        return
    assert fills['E9']['pnl'] == 10.0          # 5 ticks x $1 x 2 contracts
    assert fills['E9']['intent'] == 'CLOSE' and fills['E9']['name'] == 'Iron ore'
    assert fills['E10']['pnl'] is None and fills['E10']['ours'] == ''
    assert body['closed'][0]['net_pnl'] == 8.0
    csv = c.get('/api/tt_fills.csv').data.decode()
    assert 'exec_id' in csv.splitlines()[0] and 'E9' in csv
    import csv as csv_mod
    import io
    table = {r['exec_id']: r for r in csv_mod.DictReader(io.StringIO(csv))}
    assert csv.splitlines()[0].endswith(',pnl')
    assert float(table['E9']['pnl']) == 10.0         # the Fills tab's figure
    assert table['E10']['pnl'] == ''                  # not ours: empty, never 0


def test_the_journal_survives_a_contract_with_no_tick_value(tmp_path):
    """Found on TT UAT: a closing Algo fill on a contract added without a
    tick value made /api/journal fail (round(None)), and with it the Fills
    tab went empty and the order tests found no closing order."""
    test_the_account_page_and_its_journal(tmp_path, tick_value=None)


def test_close_all_escalates_with_auto_trade_off(tmp_path):
    """Found by the UAT order tests: the executor learned a contract only
    inside `manage`, which runs only with Auto trade on — so with it off
    (trading by hand) CLOSE ALL cancelled the resting Close @ LMT and then
    sent nothing, leaving the position open."""
    engine, gw, db, rt = live_long(tmp_path)
    engine.auto_trade_enabled = False
    engine.executor.contracts.clear()
    engine.executor.books.clear()
    engine.poll(now=gw.now)
    r = engine.close_at_limit('fef', round(rt.book.bid + 0.20, 2))
    sent = len(gw.sent_ids)
    assert engine.close_now('fef')['escalated'] == 1
    for _ in range(3):
        engine.poll(now=gw.now)
    assert rt.position is None                          # the market close went
    assert len(gw.sent_ids) == sent + 1


def test_the_fills_csv_prices_a_manual_close_against_the_ticket_it_closed(tmp_path):
    """The CSV carries the same P&L as the Fills tab: a manual close against
    the ticket it closed, with the DESK contract's tick (what the trader set)
    over the ticket's own old copy."""
    import csv as csv_mod
    import io
    import json
    from fixtrader.config import ContractConfig, TraderConfig
    from fixtrader.database import Database
    from fixtrader.webapp import create_app

    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'j.db')
    cfg.contracts['gc'] = ContractConfig(key='gc', name='GC Dec26', symbol='GC',
                                         tick_size=0.1, tick_value=10.0, security_id='GC1')
    cfg.save()
    db = Database(str(tmp_path / 'j.db'))
    base = {'orig_clordid': '', 'account': 'ACC', 'symbol': 'GC', 'contract_key': 'gc',
            'qty': 1.0, 'cum_qty': 1.0, 'leaves_qty': 0.0, 'exec_type': '2',
            'ord_status': '2', 'text': '', 'ours': 'MANUAL', 'security_id': 'GC1'}
    db.save_tt_fills([
        dict(base, exec_id='M1', clordid='FTM-a', order_id='T1', tt_time='t1', side='BUY',
             open_close='OPEN', price=4220.1, received='2026-10-09T18:46:33+00:00'),
        dict(base, exec_id='M2', clordid='FTM-b', order_id='T2', tt_time='t2', side='SELL',
             open_close='CLOSE', price=4218.0, received='2026-10-09T18:46:34+00:00')])
    status = tmp_path / 's.json'
    status.write_text(json.dumps({'engine': {'manual_terminal': {'orders': [
        {'id': 'FTM-a', 'ids': ['FTM-a'], 'avg_price': 4220.1,
         'ticket': {'security_id': 'GC1', 'side': 'BUY',
                    'instrument': {'tick_size': '1', 'tick_value': '10'}}},
        {'id': 'FTM-b', 'ids': ['FTM-b'], 'close_of': 'FTM-a',
         'ticket': {'security_id': 'GC1', 'side': 'SELL'}}], 'watchlist': []}}}))
    app = create_app(str(tmp_path / 'config.json'), str(status),
                     str(tmp_path / 'c.jsonl'), str(tmp_path / 'r.json'))
    c = app.test_client()
    table = {r['exec_id']: r for r in csv_mod.DictReader(io.StringIO(
        c.get('/api/tt_fills.csv').data.decode()))}
    assert float(table['M2']['pnl']) == -210.0        # -2.1 x $100, not -$21
    assert table['M1']['pnl'] == ''                    # an open has no P&L
    fills = {f['exec_id']: f for f in c.get('/api/journal').get_json()['tt_fills']}
    assert fills['M2']['pnl'] == -210.0                # the tab says the same
