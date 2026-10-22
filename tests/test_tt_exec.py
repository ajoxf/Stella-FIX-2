"""What a TT Execution Report is, before anything is booked from it — from
TT FIX's Execution Report documentation. The spreads this desk trades are
exchange-listed: TT reports the spread fill AND one fill per leg (442=2) at
the leg's price. Booked as fills of the spread, a 1-lot fill became three."""
from types import SimpleNamespace

from fixtrader import tt_exec
from fixtrader.gateway import AlgoOrderRouter
from tests.test_market_as_limit import quote, market_ticket  # noqa: F401
from tests.test_price_units import terminal  # noqa: F401


def fill(**over):
    f = {'35': '8', '150': '2', '39': '2', '17': 'E1', '32': '1', '31': '9050',
         '14': '1', '151': '0', '6': '9050', '48': 'CL1', '54': '1'}
    f.update(over)
    return f


def test_the_kinds():
    assert tt_exec.kind(fill()) == tt_exec.FILL
    assert tt_exec.kind(fill(**{'442': '3'})) == tt_exec.FILL      # spread summary
    assert tt_exec.kind(fill(**{'442': '2'})) == tt_exec.LEG       # a leg
    assert tt_exec.kind(fill(**{'20': '1'})) == tt_exec.BUST
    assert tt_exec.kind(fill(**{'20': '2', '19': 'E0'})) == tt_exec.CORRECTION
    assert tt_exec.kind(fill(**{'20': '3', '150': 'D'})) == tt_exec.STATUS
    assert tt_exec.kind(fill(**{'150': '0', '32': '0'})) is None


def test_a_fill_is_known_by_ttss_unique_id_or_its_trade_date():
    assert tt_exec.exec_key(fill(**{'16612': 'g1'})) == 'U:g1'
    a = tt_exec.exec_key(fill(**{'75': '20261009'}))
    b = tt_exec.exec_key(fill(**{'75': '20261010'}))
    assert a != b                          # the same 17 on another day is another fill
    # A resend without the optional tags is still the same fill.
    assert tt_exec.exec_key(fill(**{'60': '20261008-10:00:00'})) == tt_exec.exec_key(fill())


def test_a_reject_says_tts_reason_code_in_words():
    assert tt_exec.reject_reason({'103': '2179'}, 'EXCH: rejected') == \
        'EXCH: rejected (order price outside bands)'
    assert tt_exec.reject_reason({}, 'EXCH: rejected') == 'EXCH: rejected'


def router(terminal):
    gw = SimpleNamespace(venue=SimpleNamespace(name='TT', account='ACC'),
                         contracts=[SimpleNamespace(key='cl', security_id='CL1')],
                         _redact=lambda t: t, terminal=terminal)
    return AlgoOrderRouter(gw)


def test_the_algo_books_the_spread_once_not_its_legs(terminal):
    from fixtrader.models import (Intent, OrderRequest, OrderType, VenueOrder,
                                  OrderState, Side)
    r = router(terminal)
    req = OrderRequest(contract_key='cl', side=Side.BUY, qty=1,
                       order_type=OrderType.MARKET, intent=Intent.OPEN)
    vo = VenueOrder(clordid='FT-x-1', contract_key='cl', side=Side.BUY, qty=1,
                    order_type=OrderType.MARKET, state=OrderState.WORKING, ts=None)
    r.orders['FT-x-1'] = {'order': vo, 'current': 'FT-x-1', 'pending': None,
                          'request': req, 'security_id': 'CL1'}
    r.ids['FT-x-1'] = 'FT-x-1'
    r.on_message('8', fill(**{'11': 'FT-x-1', '442': '3', '17': 'S1'}), '')
    r.on_message('8', fill(**{'11': 'FT-x-1', '442': '2', '17': 'L1', '31': '7000'}), '')
    r.on_message('8', fill(**{'11': 'FT-x-1', '442': '2', '17': 'L2', '31': '7100'}), '')
    fills = [e for e in r.drain() if e.fill is not None]
    assert len(fills) == 1 and fills[0].fill.price == 90.50      # the spread, once
    assert [t['exec_id'] for t in r.take_tape()] == ['S1']        # tape: no legs
    # A bust is said, never booked.
    r.on_message('8', fill(**{'11': 'FT-x-1', '20': '1', '17': 'B1', '19': 'S1'}), '')
    events = r.drain()
    assert [e.kind for e in events] == ['TRADE_CHANGE'] and 'NOT changed' in events[0].text


def test_a_manual_ticket_books_the_spread_once_and_says_a_bust(terminal):
    quote(terminal, 9050, 9052)
    p = terminal.preview(dict(market_ticket(), order_type='LIMIT', price='90.52'))
    oid = terminal.submit({'token': p['token'], 'confirmed': True})['order_id']
    cid = terminal.orders[oid]['current_id']
    terminal.on_message('Order Routing', fill(**{'11': cid, '442': '3', '17': 'S1', '31': '9052', '6': '9052'}), '')
    terminal.on_message('Order Routing', fill(**{'11': cid, '442': '2', '17': 'L1', '31': '7000', '6': '9052'}), '')
    fills = terminal.snapshot()['fills']
    assert len(fills) == 1 and fills[0]['price'] == 90.52
    terminal.on_message('Order Routing', fill(**{'11': cid, '20': '1', '17': 'B1', '19': 'S1'}), '')
    assert 'NOT changed' in terminal.orders[oid]['text']
    assert len(terminal.snapshot()['fills']) == 1            # nothing booked
