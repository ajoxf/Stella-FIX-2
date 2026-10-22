"""Slippage, measured: each fill against the price its decision was made at,
positive a cost, negative an improvement, unmeasured never a zero."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import slippage
from fixtrader.config import ContractConfig
from fixtrader.database import Database
from fixtrader.fake_gateway import FakeGateway
from fixtrader.models import ExitReason, Position, Side
from tests.conftest import book_at_z, fill_candles
from tests.test_engine_signal import PaperGateway, build


# -- the sign -------------------------------------------------------------------

def test_positive_is_a_cost_for_either_side():
    assert slippage.slip('BUY', 10.00, 10.02) == pytest.approx(0.02)
    assert slippage.slip('SELL', 10.00, 9.98) == pytest.approx(0.02)


def test_negative_is_an_improvement():
    assert slippage.slip('BUY', 10.00, 9.99) == pytest.approx(-0.01)
    assert slippage.slip('SELL', 10.00, 10.01) == pytest.approx(-0.01)


def test_unmeasured_is_none_not_zero():
    assert slippage.slip('BUY', None, 10.0) is None
    assert slippage.slip('SELL', 10.0, None) is None


# -- the report -----------------------------------------------------------------

CONTRACT = ContractConfig(key='fef', name='Iron ore', symbol='FEF',
                          tick_size=0.01, tick_value=1.0)


def pos(entry=None, exit_=None, closed=True, tickets=('E1',), kind='MARKET',
        qty=5.0, pid=1):
    t0 = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    return Position(id=pid, contract_key='fef', side=Side.SELL, qty=0.0,
                    opened_qty=qty, avg_price=0.6, opened_at=t0,
                    closed_at=t0 + timedelta(minutes=5) if closed else None,
                    exit_price=0.55 if closed else None,
                    entry_slippage=entry, exit_slippage=exit_,
                    entry_order_type=kind, tickets=list(tickets))


def test_money_goes_through_the_one_conversion():
    r = slippage.row(pos(entry=0.02, exit_=0.01), CONTRACT)
    assert r['entry_ticks'] == pytest.approx(2.0)
    assert r['entry_money'] == pytest.approx(2.0 * 1.0 * 5)     # t x $ x qty
    assert r['round_trip_ticks'] == pytest.approx(3.0)


def test_an_unmeasured_fill_is_counted_apart_never_averaged_as_zero():
    out = slippage.report([pos(entry=0.02, pid=1), pos(entry=None, pid=2)],
                          {'fef': CONTRACT})
    entry = out['overall']['entry']
    assert entry['measured'] == 1 and entry['unmeasured'] == 1
    assert entry['ticks_mean'] == pytest.approx(2.0)          # not 1.0


def test_a_round_turn_needs_both_ends():
    out = slippage.report([pos(entry=0.02, exit_=None)], {'fef': CONTRACT})
    assert out['overall']['round_trip']['measured'] == 0
    assert out['overall']['round_trip']['ticks_mean'] is None


def test_an_open_position_has_no_exit_yet_not_an_unmeasured_one():
    out = slippage.report([pos(entry=0.01, closed=False)], {'fef': CONTRACT})
    assert out['overall']['exit']['unmeasured'] == 0
    assert out['counts']['open'] == 1


def test_paper_fills_are_not_measured_and_say_so():
    out = slippage.report([pos(entry=None, tickets=('PAPER-1',))],
                          {'fef': CONTRACT})
    assert out['counts']['paper'] == 1
    assert out['overall']['entry']['measured'] == 0
    assert out['overall']['entry']['unmeasured'] == 0     # not "unmeasured"


def test_market_and_limit_side_by_side_and_the_worst_first():
    out = slippage.report([pos(entry=0.03, kind='MARKET', pid=1),
                           pos(entry=-0.01, kind='LIMIT', pid=2),
                           pos(entry=0.01, kind='LIMIT', pid=3)],
                          {'fef': CONTRACT}, budgets={'fef': 0.5})
    assert set(out['by_order_type']) == {'MARKET', 'LIMIT'}
    limit = out['by_order_type']['LIMIT']['entry']
    assert limit['paid'] == 1 and limit['earned'] == 1
    assert out['worst'][0]['position_id'] == 1
    assert out['by_contract']['fef']['budget_ticks'] == 0.5


# -- measured in the engine --------------------------------------------------------

class SlippyGateway(FakeGateway):
    """Moves the book `slip` against every market order before filling it —
    the market moving between the decision and the fill."""
    slip = 0.02

    def send(self, order):
        book = self._books.get(order.contract_key)
        if book is not None and self.slip:
            move = self.slip if order.side is Side.BUY else -self.slip
            self.set_book(order.contract_key, round(book.bid + move, 4),
                          round(book.ask + move, 4), book.bid_size,
                          book.ask_size)
        return super().send(order)


def test_the_entry_and_exit_are_measured_against_the_decision(tmp_path):
    engine, gw, db = build(tmp_path, SlippyGateway, confirm_samples=1,
                           stop_loss_on=False)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    p = rt.position
    assert p is not None and p.side is Side.SELL
    assert p.entry_slippage == pytest.approx(0.02)      # sold 2 ticks lower
    assert p.entry_order_type == 'MARKET'
    snap = engine.snapshot(now=gw.now)['contracts'][0]
    assert snap['algo']['positions'][0]['entry_slip_ticks'] == pytest.approx(2.0)

    engine.close_now('fef')
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    closed = db.closed_positions('fef')[0]
    assert closed.exit_reason is ExitReason.CLOSE_NOW
    assert closed.exit_slippage == pytest.approx(0.02)  # bought 2 ticks higher
    day = rt.algo.day
    assert day['slip_sides'] == 2 and day['slip_ticks'] == pytest.approx(4.0)
    assert day['slip_money'] == pytest.approx(4.0 * 1.0 * 5)


def test_a_fill_at_the_decision_price_measures_zero(tmp_path):
    """The control: the book does not move, so the slippage is a measured
    0.00 — which is a statement, unlike None."""
    SlippyGateway.slip = 0.0
    try:
        engine, gw, _ = build(tmp_path, SlippyGateway, confirm_samples=1)
        rt = fill_candles(engine, gw)
        book_at_z(engine, gw, 2.6)
        engine.poll(now=gw.now); engine.poll(now=gw.now)
        assert rt.position.entry_slippage == pytest.approx(0.0)
    finally:
        SlippyGateway.slip = 0.02


def test_a_paper_fill_is_not_reported_as_a_perfect_one(tmp_path):
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1)
    engine.set_auto_trade(True)
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is not None
    assert rt.position.entry_slippage is None
    assert rt.position.entry_order_type == 'PAPER'


def test_the_slippage_survives_a_restart(tmp_path):
    engine, gw, db = build(tmp_path, SlippyGateway, confirm_samples=1,
                           db_name='s.db')
    rt = fill_candles(engine, gw)
    book_at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    again = Database(str(tmp_path / 's.db')).open_positions()[0]
    assert again.entry_slippage == pytest.approx(0.02)
    assert again.entry_order_type == 'MARKET'


def test_a_book_from_an_older_build_gains_the_columns(tmp_path):
    """A database written before slippage was kept opens, keeps its rows,
    and reads their slippage as unmeasured."""
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY "
                 "AUTOINCREMENT, contract_key TEXT NOT NULL, side TEXT NOT "
                 "NULL, qty REAL NOT NULL, opened_qty REAL, avg_price REAL NOT "
                 "NULL, opened_at TEXT, entry_z REAL, entry_mean REAL, "
                 "entry_std REAL, entry_half_life REAL, margin_locked REAL, "
                 "break_even REAL, target_price REAL, stop_price REAL, "
                 "closed_at TEXT, exit_price REAL, exit_z REAL, exit_reason "
                 "TEXT, gross_pnl REAL, fees_paid REAL, net_pnl REAL, "
                 "pnl_pct_on_margin REAL, is_simulated INTEGER DEFAULT 0, "
                 "tickets TEXT DEFAULT '[]')")
    conn.execute("INSERT INTO positions (contract_key, side, qty, avg_price) "
                 "VALUES ('fef', 'BUY', 1, 0.5)")
    conn.commit()
    conn.close()
    db = Database(path)
    old = db.open_positions()[0]
    assert old.entry_slippage is None and old.avg_price == 0.5


# -- the route ---------------------------------------------------------------------

def test_the_report_route_and_its_csv(tmp_path):
    from fixtrader.config import TraderConfig
    from fixtrader.webapp import create_app
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'r.db')
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore', symbol='FEF', tick_size=0.01,
        tick_value=1.0, slippage_budget_ticks=0.5)
    cfg.save()
    db = Database(str(tmp_path / 'r.db'))
    now = datetime.now(timezone.utc)
    db.save_position(Position(contract_key='fef', side=Side.SELL, qty=0.0,
                              opened_qty=5.0, avg_price=0.6,
                              opened_at=now - timedelta(hours=1),
                              closed_at=now, exit_price=0.55,
                              entry_slippage=0.02, exit_slippage=None,
                              entry_order_type='MARKET', tickets=['E1']))
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 's.json'),
                     str(tmp_path / 'c.jsonl'), str(tmp_path / 'res.json'))
    c = app.test_client()
    out = c.get('/api/slippage?period=all&mode=live').get_json()
    assert out['ok'] and out['overall']['entry']['ticks_mean'] == pytest.approx(2.0)
    assert out['overall']['exit']['unmeasured'] == 1
    assert out['by_contract']['fef']['budget_ticks'] == 0.5
    # simulated positions are never blended into a live figure
    assert c.get('/api/slippage?mode=sim').get_json()['counts']['positions'] == 0
    csv = c.get('/api/slippage.csv?period=all&mode=live').get_data(as_text=True)
    header, line = csv.strip().splitlines()[:2]
    cols = header.split(',')
    cells = dict(zip(cols, line.split(',')))
    assert cells['entry_ticks'] == '2.0'
    assert cells['exit_ticks'] == ''                  # empty, never 0


def test_an_escalated_limit_keeps_its_decision_and_says_so():
    """The replacement is measured from the price the LIMIT was decided at —
    the wait is part of what it cost — and is labelled as escalated."""
    from fixtrader.executor import Executor
    d = Executor._escalated({'price': 0.6, 'order_type': 'LIMIT'})
    assert d == {'price': 0.6, 'order_type': 'LIMIT escalated'}
    assert Executor._escalated(d)['order_type'] == 'LIMIT escalated'
    assert Executor._escalated(None) is None


def test_manual_tickets_join_the_report_under_live_only(tmp_path):
    import json
    from fixtrader.config import TraderConfig
    from fixtrader.webapp import create_app
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'm.db')
    cfg.save()
    now = datetime.now(timezone.utc).isoformat()
    rows = [{'order_id': 'FTM-1', 'end': 'entry', 'order_type': 'MARKET',
             'slippage_ticks': 2.0, 'slippage_money': 25.0, 'time': now},
            {'order_id': 'FTM-2', 'end': 'exit', 'order_type': 'MARKET',
             'slippage_ticks': None, 'slippage_money': None, 'time': now}]
    status = {'ts': now, 'engine': {'alive': True, 'manual_terminal': {
        'slippage': slippage.manual_summary(rows)}}, 'contracts': []}
    (tmp_path / 's.json').write_text(json.dumps(status))
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 's.json'),
                     str(tmp_path / 'c.jsonl'), str(tmp_path / 'r.json'))
    c = app.test_client()
    live = c.get('/api/slippage?period=all&mode=live').get_json()
    assert live['manual']['entry']['ticks_mean'] == pytest.approx(2.0)
    assert live['manual']['exit']['unmeasured'] == 1
    # never blended into a simulated figure
    assert c.get('/api/slippage?period=all&mode=sim').get_json()['manual'] is None
