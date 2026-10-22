"""The Algo's orders to Stellar over the real FIX session, against a fake
Stellar on the socket: what goes out (tag by tag), what comes back (as
engine events), and the positions Stellar cannot provide."""
import time
from types import SimpleNamespace

import pytest

from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway
from fixtrader.models import (Intent, OrderRequest, OrderType, PositionEffect,
                              SessionState, Side)
from tests.fake_stellar import Exchange, install, legs_of, party


def stellar_venue(**extra):
    return VenueConfig(name='STELLAR-UAT', environment='UAT', host='or.example', port=9001,
                       fix_version='FIX.4.4', use_tls=False,
                       sender_comp_id='STELLAR_UAT', target_comp_id='STELLAR',
                       md_host='md.example', md_port=9002, md_sender_comp_id='STELLAR_UAT_MD',
                       username='USER', account='ACC1', **extra)


@pytest.fixture
def connect(monkeypatch, tmp_path):
    def make(**venue_extra):
        stellar = Exchange()
        stellar.list('123', 0.54, 0.56, tick=0.01, symbol='FEF', exchange='XSES',
                     tick_value=1.0, maturity='202612')
        stellar.list('SPR', -0.5, -0.4, tick=0.01, symbol='GE', exchange='XCME',
                     tick_value=25.0, cfi='MXXXXX', subtype='TSPR',
                     legs=[{'600': 'GE', '610': '201712', '623': '1', '624': '1'},
                           {'600': 'GE', '610': '201806', '623': '1', '624': '2'}],
                     leg_prices={'201712': 98.81, '201806': 99.26})
        install(monkeypatch, stellar)
        gw = FixGateway(stellar_venue(**venue_extra), manual_path=str(tmp_path / 'm.db'))
        gw.start()
        deadline = time.monotonic() + 5
        while gw.state() != SessionState.LOGGED_ON and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gw.state() == SessionState.LOGGED_ON
        gw.subscribe(SimpleNamespace(key='fef', name='Iron ore', symbol='FEF',
                                     security_id='123', security_exchange='XSES',
                                     tick_size=0.01, tick_value=1.0))
        gw.subscribe(SimpleNamespace(key='ge', name='GE Dec/Jun', symbol='GE',
                                     security_id='SPR', security_exchange='XCME',
                                     tick_size=0.01, tick_value=25.0))
        # Stellar's Security List entries arrive: an order can name them now.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all(
                gw.terminal.orderable(gw.terminal.watch.get(k) or {}) for k in ('123', 'SPR')):
            gw.drain_events()
            time.sleep(0.01)
        return gw, stellar
    return make


def wait_for(gw, kinds, timeout=2.0):
    """Drain until every kind in `kinds` has been seen, or time runs out."""
    seen, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen += gw.drain_events()
        if all(any(e.kind == k for e in seen) for k in kinds):
            break
        time.sleep(0.01)
    return seen


def sent(stellar, kind):
    return [f for f in stellar.orders_in if f['35'] == kind]


def test_an_algo_entry_goes_out_as_an_OPEN_naming_the_instrument_and_fills(connect):
    gw, stellar = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.SELL, qty=2,
                                   order_type=OrderType.MARKET,
                                   reason='H to L z +2.61'))
        events = wait_for(gw, ['ACK', 'FILL'])
        d = sent(stellar, 'D')[-1]
        assert d['11'] == cid and cid.startswith('FT-')
        # The instrument by symbol, CFI, maturity and exchange — never 48.
        assert d['55'] == 'FEF' and d['461'] == 'FXXXXX' and d['200'] == '202612'
        assert d['207'] == 'XSES' and d['21'] == '1' and '48' not in d
        assert party(d['raw'], '11') == 'USER' and party(d['raw'], '24') == 'ACC1'
        assert '1' not in d                        # never tag 1: the Parties say it
        assert d['54'] == '2' and d['38'] == '2' and d['40'] == '1'
        assert d['77'] == 'O'                      # it says it OPENS
        assert '1028' not in d and '18' not in d   # TT's tags are gone
        fill = next(e for e in events if e.kind == 'FILL')
        assert fill.clordid == cid and fill.contract_key == 'fef'
        assert fill.fill.qty == 2 and fill.fill.price == pytest.approx(0.54)
        assert fill.order.state.value == 'FILLED'
    finally:
        gw.stop()


def test_a_close_says_CLOSE_and_names_its_position_and_tickets(connect):
    gw, stellar = connect()
    try:
        gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=2,
                             order_type=OrderType.MARKET, intent=Intent.CLOSE,
                             reduce_only=True,
                             position_effect=PositionEffect.CLOSE,
                             position_id=7, close_tickets=['X-1', 'X-2']))
        wait_for(gw, ['FILL'])
        d = sent(stellar, 'D')[-1]
        assert d['77'] == 'C'                      # never a bare opposite order
        assert d['54'] == '1' and d['38'] == '2'
        # The position and its tickets, in Stellar's ExchangeText party.
        assert party(d['raw'], '1002') == 'Close P7 X-1 X-2'
    finally:
        gw.stop()


def test_a_venue_that_refuses_77_can_switch_it_off_and_the_close_still_names_itself(connect):
    gw, stellar = connect(send_position_effect=False)
    try:
        gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1,
                             order_type=OrderType.MARKET, intent=Intent.CLOSE,
                             position_effect=PositionEffect.CLOSE, position_id=3,
                             close_tickets=['X-9']))
        wait_for(gw, ['FILL'])
        d = sent(stellar, 'D')[-1]
        assert '77' not in d
        assert party(d['raw'], '1002') == 'Close P3 X-9'
    finally:
        gw.stop()


def test_a_listed_strategy_goes_as_a_multileg_order_and_its_legs_are_never_booked(connect):
    gw, stellar = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='ge', side=Side.BUY, qty=1,
                                   order_type=OrderType.MARKET))
        events = wait_for(gw, ['FILL'])
        ab = sent(stellar, 'AB')[-1]
        assert ab['11'] == cid and ab['461'] == 'MXXXXX' and ab['167'] == 'MLEG'
        assert ab['762'] == 'TSPR' and ab['555'] == '2' and '200' not in ab
        assert [(l['600'], l['610'], l['624']) for l in legs_of(ab['raw'])] == \
            [('GE', '201712', '1'), ('GE', '201806', '2')]
        fills = [e for e in events if e.fill is not None]
        assert len(fills) == 1 and fills[0].fill.price == pytest.approx(-0.4)   # the spread
        time.sleep(0.2)
        assert not [e for e in gw.drain_events() if e.fill is not None]        # no legs
    finally:
        gw.stop()


def test_a_duplicate_trade_report_is_not_a_second_fill(connect):
    gw, stellar = connect()
    try:
        gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1,
                             order_type=OrderType.MARKET))
        first = wait_for(gw, ['FILL'])
        trade = next(f for f in reversed(list(stellar.or_sessions['STELLAR_UAT']['sent'].values()))
                     if dict(f).get('150') == 'F')
        stellar.deliver(trade)                      # Stellar sends the same trade again
        again = wait_for(gw, ['FILL'], timeout=0.3)
        assert sum(e.kind == 'FILL' for e in first) == 1
        assert not any(e.kind == 'FILL' for e in again)
    finally:
        gw.stop()


def test_cancel_and_replace_go_by_stellars_order_id_and_keep_the_root_id(connect):
    gw, stellar = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=3,
                                   order_type=OrderType.LIMIT, price=0.5))
        wait_for(gw, ['ACK'])
        assert sent(stellar, 'D')[-1]['44'] == '0.5'
        order_id = gw.algo.orders[cid]['order'].venue_id
        assert order_id
        gw.amend(cid, price=0.51)
        replaced = wait_for(gw, ['REPLACED'])
        g = sent(stellar, 'G')[-1]
        assert g['41'] == cid and g['37'] == order_id and g['44'] == '0.51' and g['77'] == 'O'
        event = next(e for e in replaced if e.kind == 'REPLACED')
        assert event.clordid == cid                 # the ROOT id, not the new one
        assert event.order.price == pytest.approx(0.51)
        gw.cancel(cid)
        cancelled = wait_for(gw, ['CANCELLED'])
        f = sent(stellar, 'F')[-1]
        assert f['41'] == g['11'] and f['37'] == order_id   # the CURRENT id, by OrderID
        assert party(f['raw'], '24') == 'ACC1'
        assert next(e for e in cancelled if e.kind == 'CANCELLED').clordid == cid
    finally:
        gw.stop()


def test_a_cancel_asked_for_before_the_ack_goes_when_the_order_id_arrives(connect):
    gw, stellar = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1,
                                   order_type=OrderType.LIMIT, price=0.5))
        gw.algo.orders[cid]['order'].venue_id = None     # as if the ack had not come
        gw.cancel(cid)
        assert not sent(stellar, 'F')                   # held: Stellar cancels by 37
        assert gw.algo.orders[cid]['cancel_wanted']
        gw.algo.orders[cid]['order'].venue_id = next(iter(stellar.resting))   # the ack's 37
        cancelled = wait_for(gw, ['CANCELLED'])
        assert sent(stellar, 'F') and any(e.kind == 'CANCELLED' for e in cancelled)
    finally:
        gw.stop()


def test_a_cancel_reject_carries_stellars_words(connect):
    gw, stellar = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1,
                                   order_type=OrderType.LIMIT, price=0.5))
        wait_for(gw, ['ACK'])
        stellar.resting.clear()                     # Stellar no longer has it
        gw.cancel(cid)
        events = wait_for(gw, ['CANCEL_REJECTED'])
        text = next(e for e in events if e.kind == 'CANCEL_REJECTED').text
        assert 'order not found' in text
    finally:
        gw.stop()


def test_an_order_with_no_stellar_user_name_is_refused_in_words_and_never_sent(connect):
    gw, stellar = connect()
    try:
        gw.venue.username = ''
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1))
        refused = gw.algo.drain()
        assert refused[-1].kind == 'REJECTED' and refused[-1].clordid == cid
        assert '452=11' in refused[-1].text
        assert not sent(stellar, 'D')
    finally:
        gw.stop()


def test_a_manual_tickets_report_is_not_the_algos(connect):
    gw, stellar = connect()
    try:
        stellar.deliver([('35', '8'), ('11', 'FTM-abc'), ('37', 'MAN-1'), ('17', 'M1'),
                         ('150', 'F'), ('39', '2'), ('32', '1'), ('31', '0.5')])
        assert not any(e.kind == 'FILL' for e in wait_for(gw, [], 0.3))
    finally:
        gw.stop()


def test_nothing_but_our_own_orders_can_be_sent(connect):
    gw, stellar = connect()
    try:
        session = gw._sessions['Order Routing']
        for kind in ('D', 'AB', 'F', 'G', 'AC', 'H'):
            with pytest.raises(NotImplementedError):
                session.send(kind, [('11', 'SOMEONE-ELSE-1')])
    finally:
        gw.stop()


# -- positions -------------------------------------------------------------------

def test_stellar_provides_no_positions_and_they_are_unknown_never_flat(connect):
    """Stellar's Order Router lists no positions request: nothing is asked,
    and positions are UNKNOWN (None, never an empty account), with the
    reason."""
    gw, stellar = connect()
    try:
        deadline = time.monotonic() + 2
        while gw.positions_status()['status'] != 'unavailable' and time.monotonic() < deadline:
            gw.drain_events()
            time.sleep(0.01)
        assert gw.positions() is None and gw.orders() is None
        assert "does not provide positions" in gw.positions_status()['why']
        assert not [f for f in stellar.orders_in if f['35'] not in ('D', 'AB', 'F', 'G', 'AC', 'H')]
    finally:
        gw.stop()
