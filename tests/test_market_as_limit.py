"""A MARKET order goes as a LIMIT through the touch, immediate-or-cancel —
and a close that does not happen is said loudly.

Found on the TT desk this was ported from: a manual BUY at market to close
a short was REJECTED by CME, again and again — "Order price is outside bands: Bid of 7941.25
violates High Band 7872.00". A bare market order is given a protection
price by the exchange, and outside the price band it is refused: the
position stayed open, and the screen said so only in one line of an orders
table."""
import time
from types import SimpleNamespace

import pytest

from fixtrader.executor import through_price
from fixtrader.models import Side
from tests.test_price_units import terminal  # noqa: F401  (the fixture)
from tests.test_uat_orders import desk  # noqa: F401  (the fixture)

BAND = ("EXCH: Order price is outside bands 'Bid of 7941.25 violates High "
        "Band 7872.00 using Delta .00, CrvBnd equals 7852.00'")


def book(bid, ask):
    return SimpleNamespace(bid=bid, ask=ask,
                           executable=lambda side: ask if side is Side.BUY else bid)


def test_through_the_touch_by_n_ticks_and_never_short_of_it():
    b = book(7865.25, 7865.50)
    assert through_price(b, Side.BUY, 2, 0.25) == 7866.00
    assert through_price(b, Side.SELL, 2, 0.25) == 7864.75
    # The control: 0 ticks, no tick size or no touch is a true market order.
    assert through_price(b, Side.BUY, 0, 0.25) is None
    assert through_price(b, Side.BUY, 2, None) is None
    assert through_price(book(None, None), Side.BUY, 2, 0.25) is None


def quote(terminal, bid, ask):
    request = terminal.subscriptions['CL1']
    raw = (f'35=W\x01262={request}\x01268=2\x01269=0\x01270={bid}\x01271=3\x01'
           f'269=1\x01270={ask}\x01271=4\x01')
    terminal.on_message('Market Data', {'35': 'W'}, raw)


def market_ticket(side='BUY'):
    return {'security_id': 'CL1', 'side': side, 'order_type': 'MARKET',
            'quantity': '1', 'tif': 'DAY', 'account': 'ACC', 'open_close': 'O'}


def test_a_manual_market_ticket_goes_as_a_limit_through_the_offer(terminal):
    quote(terminal, 90.50, 90.52)                     # 90.50 / 90.52
    terminal.market_ticks = lambda sid: 2
    t = terminal.preview(market_ticket('BUY'))['ticket']
    assert (t['order_type'], t['price'], t['tif']) == ('LIMIT', '90.54', 'IOC')
    assert t['market_as_limit']['touch'] == 90.52
    s = terminal.preview(market_ticket('SELL'))['ticket']
    assert (s['order_type'], s['price'], s['tif']) == ('LIMIT', '90.48', 'IOC')
    sent = terminal.submit({'token': terminal.preview(market_ticket())['token'],
                            'confirmed': True})
    fields = [dict(f) for k, f in terminal.gateway._sessions['Order Routing'].sent if k == 'D'][-1]
    assert fields['40'] == '2' and fields['59'] == '3' and fields['44'] == '90.54'
    assert sent['ok']


def test_with_zero_ticks_a_manual_market_ticket_is_a_true_market_order(terminal):
    """The control."""
    quote(terminal, 90.50, 90.52)
    terminal.market_ticks = lambda sid: 0
    t = terminal.preview(market_ticket())['ticket']
    assert t['order_type'] == 'MARKET' and t['price'] is None


def test_a_market_ticket_never_carries_a_min_qty_stellar_does_not_list(terminal):
    """Stellar's Order Router lists no MinQty (110): neither an at-market
    ticket sent as an IOC nor a true market order carries one."""
    quote(terminal, 90.50, 90.52)
    for ticks in (2, 0):
        terminal.market_ticks = lambda sid, n=ticks: n
        terminal.submit({'token': terminal.preview(dict(market_ticket(), quantity='3',
                                                        min_qty='2'))['token'],
                         'confirmed': True})
        fields = [dict(f) for k, f in terminal.gateway._sessions['Order Routing'].sent if k == 'D'][-1]
        assert '110' not in fields
        assert fields['40'] == ('2' if ticks else '1')


def test_a_manual_market_ticket_with_no_fresh_quote_still_goes(terminal):
    """A close is never withheld for want of a price: no quote, a true
    market order."""
    terminal.market_ticks = lambda sid: 2
    t = terminal.preview(market_ticket())['ticket']
    assert t['order_type'] == 'MARKET'


def test_a_refused_manual_close_is_said_in_stellars_words(terminal):
    quote(terminal, 90.50, 90.52)
    preview = terminal.preview(dict(market_ticket('SELL'), order_type='LIMIT', price='90.50'))
    entry = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    order = terminal.orders[entry]
    terminal.on_message('Order Routing', {'35': '8', '11': order['current_id'], '37': 'T1',
                                          '150': '2', '39': '2', '17': 'E1', '32': '1',
                                          '31': '90.50', '6': '0', '14': '1', '151': '0'}, '')
    assert terminal.close_alerts() == []            # open, no close tried yet
    close = terminal.preview_close({'order_id': entry})
    cid = terminal.submit({'token': close['token'], 'confirmed': True})['order_id']
    terminal.on_message('Order Routing', {'35': '8', '11': terminal.orders[cid]['current_id'],
                                          '150': '8', '39': '8', '17': 'J1', '14': '0',
                                          '151': '0', '58': BAND}, '')
    alerts = terminal.close_alerts()
    assert len(alerts) == 1 and 'REJECTED' in alerts[0]['text']
    assert 'still short 1' in alerts[0]['text'] and 'outside bands' in alerts[0]['text']
    assert terminal.snapshot()['close_alerts'] == alerts
    # The control: the next close fills — no alert.
    again = terminal.preview_close({'order_id': entry})
    cid2 = terminal.submit({'token': again['token'], 'confirmed': True})['order_id']
    terminal.on_message('Order Routing', {'35': '8', '11': terminal.orders[cid2]['current_id'],
                                          '150': '2', '39': '2', '17': 'E2', '32': '1',
                                          '31': '90.52', '6': '0', '14': '1', '151': '0'}, '')
    assert terminal.close_alerts() == []


def test_the_algos_close_goes_through_the_touch_and_a_refusal_is_loud(desk):
    d = desk.d
    run = desk.runner()
    # One tick wide: the spread alone ($10) does not reach the 2% stop ($20).
    desk.tt.move('CL1', 90.51, 90.52)
    run.until('the one-tick book', lambda s: run.contract(s)['market'].get('bid') == 90.51)
    assert d.command('execution', '', {'mode': 'LIVE', 'confirm': True})['ok']
    assert d.command('uat_order', 'clz6', {'side': 'BUY', 'order_type': 'MARKET'})['ok']
    run.until('long', lambda s: run.algo_position(s))
    entry = desk.tt.orders_in[-1]
    assert entry['40'] == '2' and entry['59'] == '3'           # through the offer, IOC
    assert float(entry['44']) == 90.54                          # 90.52 + 2 ticks
    desk.tt.reject_text = BAND
    assert d.command('close_now', 'clz6')['ok']
    alert = run.until('the close alert', lambda s: run.contract(s).get('close_alert'))
    assert 'REJECTED' in alert['text'] and 'still long 1' in alert['text']
    assert 'outside bands' in alert['text']
    assert run.algo_position() is not None                      # still open: said so
    # The control: the exchange takes the next close — flat, and no alert.
    desk.tt.reject_text = None
    assert d.command('close_now', 'clz6')['ok']
    run.until('flat', lambda s: run.algo_position(s) is None)
    assert run.contract().get('close_alert') is None


def test_every_clordid_we_send_fits_stellars_32_characters(terminal):
    """Stellar: ClOrdID (11) is "a string up to 32" in length. Ours are 20
    or fewer — FTM- + 16 hex, FT-<ms hex>-<base 36>."""
    from types import SimpleNamespace
    from fixtrader.gateway import AlgoOrderRouter
    quote(terminal, 90.50, 90.52)
    p = terminal.preview(dict(market_ticket(), order_type='LIMIT', price='90.40'))
    oid = terminal.submit({'token': p['token'], 'confirmed': True})['order_id']
    order = terminal.orders[oid]
    terminal.on_message('Order Routing', {'35': '8', '11': order['current_id'], '37': 'T9',
                                          '150': '0', '39': '0', '17': 'A9', '14': '0',
                                          '151': '1'}, '')
    terminal.manage({'order_id': oid, 'price': '90.30', 'quantity': '1'}, replace=True)
    sent = [dict(f) for k, f in terminal.gateway._sessions['Order Routing'].sent if k in ('D', 'F', 'G')]
    assert sent and all(len(f['11']) <= 20 for f in sent), [f['11'] for f in sent]
    r = AlgoOrderRouter(SimpleNamespace(venue=None, contracts=[], _redact=lambda t: t))
    r.seq = 99999
    assert len(r._next_id()) <= 20


# -- the spread an order is reviewed against ---------------------------------

def test_the_review_shows_the_spread_and_flags_it_wider_than_the_limit(terminal):
    """Found on UAT gold, 4218.0 / 4230.0: an order at market crosses the
    spread, and on a thin book that is the whole cost of the trade. The review
    says it — in money — and flags it WIDE over the contract's limit."""
    quote(terminal, 90.50, 90.52)                     # 90.50 / 90.52: 2 ticks
    terminal.spread_limit = lambda sid: 1
    book = terminal.preview(market_ticket())['book']
    assert book['ticks'] == 2 and book['money'] == 20.0
    assert book['wide'] and 'WIDER' in book['text'] and '90.5 / 90.52' in book['text']


def test_a_spread_inside_the_limit_or_with_no_limit_is_not_flagged(terminal):
    """The control — and the review is never refused for it: the trader decides."""
    quote(terminal, 90.50, 90.52)
    terminal.spread_limit = lambda sid: 3
    review = terminal.preview(market_ticket())
    assert review['ok'] and not review['book']['wide'] and 'WIDER' not in review['book']['text']
    terminal.spread_limit = lambda sid: 0
    assert not terminal.preview(market_ticket())['book']['wide']


def test_with_no_quote_the_review_says_the_spread_is_not_known(terminal):
    terminal.market_ticks = lambda sid: 2
    book = terminal.preview(market_ticket())['book']
    assert book['spread'] is None and 'not known' in book['text'] and not book['wide']
