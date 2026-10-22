"""Stellar's prices ARE the prices a trader knows.

Stellar sends a JGB as 150.25 and a Eurodollar as 99.180 — the price on
every other screen — and defines each instrument's tick as
MinPriceIncrement (969, "point increment") and its money as
MinPriceIncrementAmount (1146, "value of 1 point increment"): JGB 969=0.01,
1146=10000. There is no display factor, so nothing is rescaled in either
direction: a price goes out as typed and comes back as sent. The one place
a price crosses the FIX boundary (`to_display` / `to_fix`) is kept, at a
factor of 1, so it stays one place."""
from types import SimpleNamespace

import pytest

from fixtrader.gateway import AlgoOrderRouter
from fixtrader.manual_terminal import ManualTerminal


class Session:
    def __init__(self):
        self.state = SimpleNamespace(status='CONNECTED')
        self.sent = []

    def is_running(self):
        return True

    def send(self, msg, fields):
        self.sent.append((msg, fields))


def define(terminal, request_id, key, symbol='CL', cfi='FXXXXX', maturity='202612', **tags):
    """One Stellar Security List (y) entry."""
    fields = {'35': 'y', '320': request_id, '146': '1', '55': symbol, '15': 'USD', '48': key}
    if maturity:
        fields['200'] = maturity
    fields.update({'207': 'XCME', '461': cfi})
    fields.update(tags)
    fields.update({'560': '0', '893': 'Y'})
    terminal.on_message('Market Data', fields,
                        '\x01'.join(k + '=' + v for k, v in fields.items()) + '\x01')


@pytest.fixture
def terminal(tmp_path):
    gateway = SimpleNamespace(_sessions={'Market Data': Session(), 'Order Routing': Session()},
                              venue=SimpleNamespace(account='ACC', username='USER'),
                              _redact=lambda v: v)
    t = ManualTerminal(gateway, str(tmp_path / 'manual.db'))
    t.poll()
    rid = t.lookup({'exchange': 'XCME', 'symbol': 'CL', 'security_type': 'FUT'})['request_id']
    # Crude: tick 0.01 worth $10.
    define(t, rid, 'CL1', **{'969': '0.01', '1146': '10'})
    # Gold: tick 0.1 worth $10, so $100 a point.
    define(t, rid, 'GCZ6', symbol='GC', **{'969': '0.1', '1146': '10'})
    # The E-mini: tick 0.25 worth $12.50.
    define(t, rid, 'ESZ6', symbol='ES', **{'969': '0.25', '1146': '12.5'})
    # A JGB, as Stellar's own example defines it.
    define(t, rid, 'F.STS_OSE 10-year JGB.DEC17', symbol='JGBL', maturity='201712',
           **{'969': '0.01', '1146': '10000.00'})
    for key in ('CL1', 'GCZ6', 'ESZ6', 'F.STS_OSE 10-year JGB.DEC17'):
        t.add({'security_id': key})
    return t


def test_every_instrument_stellar_defines_has_a_factor_of_one(terminal):
    for key in ('CL1', 'GCZ6', 'ESZ6'):
        assert str(terminal.price_factor(key)) == '1'
    assert terminal.price_factor('nothing') is None          # unknown, not 1


def test_both_directions_are_the_price_as_typed(terminal):
    assert terminal.to_display('CL1', '90.57') == 90.57
    assert terminal.to_fix('CL1', 90.57) == '90.57'
    assert terminal.to_fix('ESZ6', '7837.25') == '7837.25'
    assert terminal.to_fix('CL1', '-0.5') == '-0.5'           # spreads go negative


def test_the_book_is_as_stellar_sends_it(terminal):
    request = terminal.subscriptions['CL1']
    raw = (f'35=W\x0148=CL1\x01262={request}\x01268=2\x01269=0\x01270=90.56\x01271=3\x01'
           f'290=1\x01269=1\x01270=90.57\x01271=4\x01290=1\x01')
    terminal.on_message('Market Data', {'35': 'W'}, raw)
    book = terminal.books['CL1']
    assert book['bid'] == 90.56 and book['ask'] == 90.57


def test_a_ticket_goes_out_as_typed_and_its_trade_comes_back_as_sent(terminal):
    preview = terminal.preview({'security_id': 'CL1', 'side': 'BUY', 'order_type': 'LIMIT',
                                'quantity': '1', 'price': '90.55', 'tif': 'DAY',
                                'account': 'ACC', 'open_close': 'O'})
    out = terminal.submit({'token': preview['token'], 'confirmed': True})
    sent = [dict(f) for k, f in terminal.gateway._sessions['Order Routing'].sent if k == 'D']
    assert sent[-1]['44'] == '90.55'
    order = terminal.orders[out['order_id']]
    assert order['units'] == 'display'
    terminal.on_message('Order Routing', {'35': '8', '11': order['current_id'], '37': 'ST1',
                                          '150': '0', '39': '0', '17': 'ORD:1', '14': '0',
                                          '151': '1'}, '')
    # The trade: no ClOrdID, AvgPx 0, CumQty 0 — Stellar's own example. The
    # average and the filled quantity are this ticket's own sums.
    terminal.on_message('Order Routing', {'35': '8', '37': 'ST1', '150': 'F', '39': '2',
                                          '17': '13297209', '527': '12059714', '32': '1',
                                          '31': '90.55', '6': '0', '14': '0', '151': '0'}, '')
    assert order['avg_price'] == 90.55 and order['filled_qty'] == 1
    assert order['status'] == 'FILLED'
    fill = terminal.snapshot()['fills'][0]
    assert fill['price'] == 90.55 and fill['trade_id'] == '12059714'


def test_a_ticket_off_the_tick_is_refused(terminal):
    with pytest.raises(ValueError, match='tick'):
        terminal.preview({'security_id': 'CL1', 'side': 'BUY', 'order_type': 'LIMIT',
                          'quantity': '1', 'price': '90.555', 'tif': 'DAY', 'account': 'ACC'})


def test_the_algo_router_takes_prices_as_sent(terminal):
    gw = SimpleNamespace(venue=SimpleNamespace(name='Stellar', account='ACC'),
                         contracts=[SimpleNamespace(key='cl', security_id='CL1')],
                         _redact=lambda t: t, terminal=terminal)
    r = AlgoOrderRouter(gw)
    assert r._to_fix('CL1', 90.57) == '90.57'
    r.on_message('8', {'35': '8', '150': 'F', '39': '2', '17': 'E9', '37': 'X-1',
                       '55': 'CL', '200': '202612', '54': '1', '32': '1', '31': '90.57'},
                 '35=8\x0137=X-1\x01')
    row = r.take_tape()[0]
    assert row['price'] == 90.57 and row['contract_key'] == 'cl'      # by symbol and month


def test_a_router_without_a_terminal_leaves_prices_alone():
    """The control: no Stellar definitions, no conversion."""
    gw = SimpleNamespace(venue=SimpleNamespace(name='Stellar', account='ACC'),
                         contracts=[], _redact=lambda t: t)
    r = AlgoOrderRouter(gw)
    assert r._to_fix('X', 90.57) == '90.57'
    assert r._to_display('X', '9057') == 9057.0


def test_tick_and_point_value_from_969_and_1146(terminal):
    """GC: 969=0.1, 1146=10. A tick of 0.10 worth $10, so $100 a point."""
    gc = terminal.watch['GCZ6']
    assert gc['tick_size'] == '0.1'
    assert float(gc['tick_value']) == 10.0
    assert float(gc['point_value']) == 100.0
    jgb = terminal.watch['F.STS_OSE 10-year JGB.DEC17']
    assert (jgb['tick_size'], float(jgb['tick_value'])) == ('0.01', 10000.0)
    assert float(jgb['point_value']) == 1000000.0


def test_an_es_ticket_on_its_quarter_tick_goes_and_off_it_is_refused(terminal):
    preview = terminal.preview({'security_id': 'ESZ6', 'side': 'BUY', 'order_type': 'LIMIT',
                                'quantity': '1', 'price': '7919.25', 'tif': 'DAY',
                                'account': 'ACC', 'open_close': 'O'})
    assert dict(preview['fields'])['44'] == '7919.25'
    with pytest.raises(ValueError, match='tick'):           # the control: off the tick
        terminal.preview({'security_id': 'ESZ6', 'side': 'BUY', 'order_type': 'LIMIT',
                          'quantity': '1', 'price': '7919.30', 'tif': 'DAY',
                          'account': 'ACC', 'open_close': 'O'})


def test_a_new_order_waits_for_stellars_definition_and_a_defined_one_goes(terminal):
    """Before Stellar's Security List entry arrives the order cannot name the
    instrument (CFI, maturity): a NEW order is refused, saying why; the
    control — a contract Stellar has defined — goes."""
    terminal.watch['RAW1'] = {'security_id': 'RAW1', 'symbol': 'GC', 'exchange': 'XCME',
                              'description': 'GC', 'tick_size': '0.1', 'parameters': {}}
    with pytest.raises(ValueError, match='definition'):
        terminal.preview({'security_id': 'RAW1', 'side': 'SELL', 'order_type': 'LIMIT',
                          'quantity': '1', 'price': '4216.3', 'tif': 'DAY', 'account': 'ACC'})
    assert terminal.preview({'security_id': 'GCZ6', 'side': 'SELL', 'order_type': 'LIMIT',
                             'quantity': '1', 'price': '4216.3', 'tif': 'DAY',
                             'account': 'ACC'})['ok']


def test_the_desk_shows_no_price_for_a_contract_it_does_not_know(terminal):
    from fixtrader.gateway import FixGateway
    gw = FixGateway.__new__(FixGateway)
    gw.terminal = terminal
    gw._contract_security_ids = {'raw': 'RAW2', 'cl': 'CL1'}
    gw._sessions = {'Market Data': SimpleNamespace(state=SimpleNamespace(status='CONNECTED'),
                                                   is_running=lambda: True)}
    terminal.books['RAW2'] = {'bid': 42160.0, 'ask': 42163.0, 'timestamp': ''}
    assert gw.top_of_book('raw') is None                     # never subscribed: nothing shown
    terminal.books['CL1'] = {'bid': 90.5, 'ask': 90.52, 'timestamp': ''}
    assert gw.top_of_book('cl').bid == 90.5                  # the control


# -- the recording the band is rebuilt from ------------------------------------

def test_a_recording_in_other_units_is_rescaled_once_and_entries_wait_without_a_factor(tmp_path):
    """Kept from the TT desk: a recording made in other units is rescaled
    ONCE when the factor is known, never mixed with the new — and while the
    instrument is unknown nothing is recorded and entries wait."""
    from datetime import timedelta
    from decimal import Decimal
    from tests.test_engine_signal import build

    engine, gw, db = build(tmp_path)
    rt = engine.runtimes['fef']
    rt.contract.security_id = 'CL1'
    now = gw.now
    db.save_samples('fef', [(now - timedelta(seconds=30 - i), 9050.0 + i)
                            for i in range(10)])
    factor = {'value': None}
    gw.terminal = SimpleNamespace(price_factor=lambda sid: factor['value'],
                                  open_business=lambda sid: [])

    engine.poll(now=now)
    halted = engine.halted_by(rt, now)
    assert 'not known to the Market Data session' in halted   # entries wait
    assert db.price_unit('fef') is None
    assert len(db.samples_between('fef')) == 10               # nothing recorded

    factor['value'] = Decimal('0.01')
    engine.poll(now=now)
    prices = [p for _, p in db.samples_between('fef')]
    assert prices[0] == pytest.approx(90.50)                  # rescaled
    assert all(p < 100 for p in prices)                       # one unit throughout
    assert db.price_unit('fef') == 0.01
    assert rt.units_note is None
    engine.poll(now=now)
    engine.poll(now=now)
    assert [p for _, p in db.samples_between('fef')][0] == pytest.approx(90.50)  # once


def test_a_recording_at_stellars_factor_of_one_is_left_alone(tmp_path):
    """The control, as on Stellar: a factor of 1 rescales nothing."""
    from datetime import timedelta
    from decimal import Decimal
    from tests.test_engine_signal import build

    engine, gw, db = build(tmp_path)
    engine.runtimes['fef'].contract.security_id = 'CL1'
    db.save_samples('fef', [(gw.now - timedelta(seconds=5), 90.5)])
    gw.terminal = SimpleNamespace(price_factor=lambda sid: Decimal('1'),
                                  open_business=lambda sid: [])
    engine.poll(now=gw.now)
    assert db.samples_between('fef')[0][1] == 90.5
