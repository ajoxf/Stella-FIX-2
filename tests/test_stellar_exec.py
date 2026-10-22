"""What a Stellar Execution Report is, before anything is booked from it —
from the Stellar FIX Order Router specification (2.16, 3 and 5). A trade is
ExecType F and carries NO ClOrdID: it is matched by Stellar's OrderID (37).
A strategy's trade comes as the strategy (442=3) AND one report per leg
(442=2) at the leg's own price, with the SAME OrderID. Booked as fills of the
spread, a 1-lot fill became three."""
from types import SimpleNamespace

from fixtrader import stellar_exec
from fixtrader.gateway import AlgoOrderRouter
from tests.test_market_as_limit import quote, market_ticket  # noqa: F401
from tests.test_price_units import terminal  # noqa: F401


def trade(**over):
    """A trade as Stellar's example 8.9 sends it: no 11, AvgPx 0, CumQty 0."""
    f = {'35': '8', '150': 'F', '39': '2', '17': 'E1', '19': '0', '527': 'T1',
         '32': '1', '31': '90.50', '14': '0', '151': '0', '6': '0', '37': 'OID-1',
         '54': '1', '55': 'CL', '200': '202612', '442': '1'}
    f.update(over)
    return f


def test_the_kinds():
    assert stellar_exec.kind(trade()) == stellar_exec.FILL
    assert stellar_exec.kind(trade(**{'442': '3'})) == stellar_exec.FILL     # the strategy
    assert stellar_exec.kind(trade(**{'442': '2'})) == stellar_exec.LEG      # a leg
    assert stellar_exec.kind(trade(**{'150': 'H'})) == stellar_exec.BUST
    assert stellar_exec.kind(trade(**{'150': 'G'})) == stellar_exec.CORRECTION
    assert stellar_exec.kind(trade(**{'150': 'I', '17': '0'})) == stellar_exec.STATUS
    assert stellar_exec.kind(trade(**{'150': '9'})) == stellar_exec.STATUS   # held
    assert stellar_exec.kind(trade(**{'150': 'D'})) == stellar_exec.STATUS   # unheld
    assert stellar_exec.kind(trade(**{'150': '0', '32': '0'})) is None


def test_a_fill_is_known_by_stellars_exec_id():
    assert stellar_exec.exec_key(trade()) == 'E1'
    assert stellar_exec.exec_key(trade(**{'17': 'E2'})) != stellar_exec.exec_key(trade())
    # A resend without the optional tags is still the same fill.
    assert stellar_exec.exec_key(trade(**{'60': '20261008-10:00:00'})) == stellar_exec.exec_key(trade())


def test_a_reject_says_stellars_reason_code_in_words():
    assert stellar_exec.reject_reason({'103': '1'}, 'rejected') == 'rejected (unknown symbol)'
    # 99 "Other": Stellar's text says it all.
    assert stellar_exec.reject_reason({'103': '99'}, 'outside price limits') == 'outside price limits'
    assert stellar_exec.reject_reason({}, 'rejected') == 'rejected'


def test_the_parties_are_read_by_role():
    raw = [('453', '3'), ('448', 'Phil'), ('447', 'D'), ('452', '11'),
           ('448', 'ca'), ('447', 'D'), ('452', '24'), ('448', 'Y'), ('452', '35')]
    assert stellar_exec.parties(raw) == {'11': 'Phil', '24': 'ca', '35': 'Y'}


def router(terminal):
    gw = SimpleNamespace(venue=SimpleNamespace(name='Stellar', account='ACC'),
                         contracts=[SimpleNamespace(key='cl', security_id='CL1')],
                         _redact=lambda t: t, terminal=terminal,
                         status_answered=lambda req: None)
    return AlgoOrderRouter(gw)


def working(r):
    from fixtrader.models import (Intent, OrderRequest, OrderType, VenueOrder,
                                  OrderState, Side)
    req = OrderRequest(contract_key='cl', side=Side.BUY, qty=1,
                       order_type=OrderType.MARKET, intent=Intent.OPEN)
    vo = VenueOrder(clordid='FT-x-1', contract_key='cl', side=Side.BUY, qty=1,
                    order_type=OrderType.MARKET, state=OrderState.PENDING, ts=None)
    r.orders['FT-x-1'] = {'order': vo, 'current': 'FT-x-1', 'pending': None,
                          'request': req, 'security_id': 'CL1'}
    r.ids['FT-x-1'] = 'FT-x-1'
    return vo


def test_the_algos_trade_is_found_by_stellars_order_id(terminal):
    r = router(terminal)
    vo = working(r)
    r.on_message('8', {'35': '8', '11': 'FT-x-1', '37': 'OID-1', '150': '0', '39': '0',
                       '17': 'ORD:1', '14': '0', '151': '1'}, '')
    assert vo.venue_id == 'OID-1' and [e.kind for e in r.drain()] == ['ACK']
    r.on_message('8', trade(), '')                       # no ClOrdID on it
    fills = [e for e in r.drain() if e.fill is not None]
    assert len(fills) == 1 and fills[0].clordid == 'FT-x-1'
    assert fills[0].kind == 'FILL' and fills[0].fill.price == 90.50
    # The control: a trade on somebody else's OrderID is not ours.
    r.on_message('8', trade(**{'37': 'OID-OTHER', '17': 'E9'}), '')
    assert not [e for e in r.drain() if e.fill is not None]


def test_the_algo_books_the_strategy_once_not_its_legs(terminal):
    r = router(terminal)
    working(r)
    r.on_message('8', {'35': '8', '11': 'FT-x-1', '37': 'OID-1', '150': '0', '39': '0',
                       '17': 'ORD:1'}, '')
    r.drain()
    r.on_message('8', trade(**{'442': '3', '17': 'S1', '39': '0'}), '')
    r.on_message('8', trade(**{'442': '2', '17': 'L1', '31': '98.81'}), '')
    r.on_message('8', trade(**{'442': '2', '17': 'L2', '31': '98.95'}), '')
    fills = [e for e in r.drain() if e.fill is not None]
    assert len(fills) == 1 and fills[0].fill.price == 90.50      # the strategy, once
    # Stellar's own strategy fill says 39=0: the state is this order's own sum.
    assert fills[0].order.state.value == 'FILLED'
    assert [t['exec_id'] for t in r.take_tape()] == ['S1']        # tape: no legs
    # A bust is said, never booked.
    r.on_message('8', trade(**{'150': 'H', '17': 'B1', '527': 'T1'}), '')
    events = r.drain()
    assert [e.kind for e in events] == ['TRADE_CHANGE'] and 'NOT changed' in events[0].text


def test_a_manual_ticket_books_the_strategy_once_and_says_a_bust(terminal):
    quote(terminal, 90.50, 90.52)
    p = terminal.preview(dict(market_ticket(), order_type='LIMIT', price='90.52'))
    oid = terminal.submit({'token': p['token'], 'confirmed': True})['order_id']
    cid = terminal.orders[oid]['current_id']
    terminal.on_message('Order Routing', {'35': '8', '11': cid, '37': 'OID-7', '150': '0',
                                          '39': '0', '17': 'ORD:7', '14': '0', '151': '1'}, '')
    terminal.on_message('Order Routing', trade(**{'37': 'OID-7', '442': '3', '17': 'S1',
                                                  '31': '90.52'}), '')
    terminal.on_message('Order Routing', trade(**{'37': 'OID-7', '442': '2', '17': 'L1',
                                                  '31': '70.00'}), '')
    fills = terminal.snapshot()['fills']
    assert len(fills) == 1 and fills[0]['price'] == 90.52
    assert terminal.orders[oid]['status'] == 'FILLED'
    terminal.on_message('Order Routing', trade(**{'37': 'OID-7', '150': 'H', '17': 'B1'}), '')
    assert 'NOT changed' in terminal.orders[oid]['text']
    assert len(terminal.snapshot()['fills']) == 1            # nothing booked


def test_an_order_status_answer_that_shows_a_missed_trade_is_said_not_booked(terminal):
    """Stellar's answer to an Order Status Request (150=I) reports a CumQty
    this book never saw a trade for: said, never booked without a price."""
    r = router(terminal)
    vo = working(r)
    r.on_message('8', {'35': '8', '11': 'FT-x-1', '37': 'OID-1', '150': '0', '39': '0',
                       '17': 'ORD:1'}, '')
    r.drain()
    r.on_message('8', {'35': '8', '11': 'FT-x-1', '37': 'OID-1', '150': 'I', '39': '2',
                       '17': '0', '14': '1', '151': '0', '790': 'OS-1'}, '')
    events = r.drain()
    assert [e.kind for e in events] == ['TRADE_CHANGE']
    assert 'trade report was not received' in events[0].text
    assert vo.filled_qty == 0                                  # not booked
