from types import SimpleNamespace
from decimal import Decimal

import pytest

from fixtrader.manual_terminal import ManualTerminal, ORDER_TYPES


class Session:
    def __init__(self):
        self.state = SimpleNamespace(status='CONNECTED')
        self.sent = []
    def is_running(self): return True
    def send(self, msg, fields): self.sent.append((msg, fields))


@pytest.fixture
def terminal(tmp_path):
    gateway = SimpleNamespace(_sessions={'Market Data': Session(), 'Order Routing': Session()},
                              venue=SimpleNamespace(account='UAT_ACCOUNT'), _redact=lambda value: value)
    terminal = ManualTerminal(gateway, str(tmp_path / 'manual.db'))
    terminal.poll()
    result = terminal.lookup({'exchange':'CME', 'symbol':'ES', 'security_type':'FUT'})
    for key, month in [('101','202612'), ('102','202703')]:
        fields = {'35':'d', '320':result['request_id'], '48':key, '55':'ES', '207':'CME', '167':'FUT',
                  '200':month, '107':'ES '+month, '15':'USD', '969':'0.25', '231':'50'}
        terminal.on_message('Market Data', fields, '\x01'.join(k+'='+v for k,v in fields.items())+'\x01')
        terminal.add({'security_id':key})
    return terminal


def ticket(**changes):
    return dict({'security_id':'101', 'side':'BUY', 'order_type':'LIMIT', 'quantity':'2',
                 'price':'-0.5', 'tif':'DAY', 'account':'UAT_ACCOUNT'}, **changes)


def report(terminal, fields):
    terminal.on_message('Order Routing', fields, '')


def sent(terminal, msg):
    return [dict(fields) for kind,fields in terminal.gateway._sessions['Order Routing'].sent if kind==msg]


def test_exact_ids_separate_expiries_and_full_snapshot_clears_missing_side(terminal):
    for key in ('101','102'):
        request = terminal.subscriptions[key]
        raw = f'35=W\x01262={request}\x01268=2\x01269=0\x01270=0\x01271=3\x01269=1\x01270=0.25\x01271=4\x01'
        terminal.on_message('Market Data', {'35':'W'}, raw)
    terminal.on_message('Market Data', {'35':'X'}, f'35=X\x01262={terminal.subscriptions["101"]}\x01268=1\x01279=2\x01269=0\x01')
    rows = {r['instrument']['security_id']:r['quote'] for r in terminal.snapshot()['watchlist']}
    assert rows['101']['bid'] is None
    assert rows['102']['bid'] == 0
    assert rows['102']['spread'] == 0.25
    raw = f'35=W\x01262={terminal.subscriptions["102"]}\x01268=1\x01269=1\x01270=1\x01271=2\x01'
    terminal.on_message('Market Data', {'35':'W'}, raw)
    assert terminal.books['102']['bid'] is None


def test_review_is_required_idempotent_and_allows_zero_negative_prices(terminal):
    with pytest.raises(ValueError): terminal.submit({'token':'bogus','confirmed':True})
    preview = terminal.preview(ticket(price='0'))
    assert not sent(terminal,'D')
    result = terminal.submit({'token':preview['token'],'confirmed':True})
    terminal.submit({'token':preview['token'],'confirmed':True})
    assert len(sent(terminal,'D')) == 1
    fields = sent(terminal,'D')[0]
    assert fields['44']=='0' and fields['48']=='101' and fields['22']=='96' and fields['1028']=='Y'
    assert terminal.orders[result['order_id']]['status']=='PENDING'
    assert terminal.preview(ticket())['ticket']['price']=='-0.5'


def test_manual_risk_limits_persist_and_are_rechecked_at_submit(terminal):
    terminal.set_risk({'trading_enabled': True, 'max_order_qty': '2',
                       'max_open_qty_per_instrument': '3', 'daily_loss_limit': '0'})
    with pytest.raises(ValueError, match='per-order limit'):
        terminal.preview(ticket(quantity='3'))
    preview = terminal.preview(ticket(quantity='2'))
    terminal.set_risk({'trading_enabled': False, 'max_order_qty': '2',
                       'max_open_qty_per_instrument': '3', 'daily_loss_limit': '0'})
    with pytest.raises(ValueError, match='kill switch'):
        terminal.submit({'token': preview['token'], 'confirmed': True})
    path = terminal.db.execute('PRAGMA database_list').fetchone()[2]
    assert ManualTerminal(terminal.gateway, path).snapshot()['risk']['trading_enabled'] is False


def test_kill_switch_still_permits_risk_reducing_close(terminal):
    preview=terminal.preview(ticket(quantity='1'));oid=terminal.submit({'token':preview['token'],'confirmed':True})['order_id']
    report(terminal,{'35':'8','11':oid,'37':'TT-1','39':'2','150':'2','17':'EXEC-CLOSE','14':'1','151':'0','32':'1','31':'10','6':'10'})
    terminal.set_risk({'trading_enabled': False, 'max_order_qty': 0,
                       'max_open_qty_per_instrument': 0, 'daily_loss_limit': 0})
    close = terminal.preview_close({'order_id': oid})
    assert close['ticket']['side'] == 'SELL'
    terminal.submit({'token': close['token'], 'confirmed': True})
    assert len(sent(terminal, 'D')) == 2


@pytest.mark.parametrize('changes', [dict(quantity='NaN'),dict(quantity='0'),dict(price='Infinity'),dict(price='0.11'),
    dict(order_type='STOP',stop_price=''),dict(order_type='STOP_LIMIT',stop_price='1',price=''),
    dict(tif='GTD',expire_date=''),dict(display_qty='3'),dict(account='bad\x0135=D'),
    dict(tif='GTC',cancel_on_disconnect=True),dict(tif='IOC',min_qty='1')])
def test_invalid_tickets_never_transmit(terminal, changes):
    with pytest.raises(ValueError): terminal.preview(ticket(**changes))
    assert not sent(terminal,'D')


@pytest.mark.parametrize('kind', list(ORDER_TYPES))
def test_supported_order_types_have_required_fields(terminal, kind):
    p = terminal.preview(ticket(order_type=kind, stop_price='0'))
    fields = dict(p['fields'])
    assert fields['40']==ORDER_TYPES[kind]
    assert ('99' in fields)==(kind in ('STOP','STOP_LIMIT'))
    assert ('44' in fields)==(kind in ('LIMIT','STOP_LIMIT','LIMIT_ON_CLOSE','POST_ONLY'))


def test_gtd_and_cme_fok_parameters(terminal):
    p=terminal.preview(ticket(tif='GTD',expire_date='2099-12-31'))
    assert dict(p['fields'])['432']=='20991231'
    fields=dict(terminal.preview(ticket(tif='FOK'))['fields'])
    assert fields['59']=='3' and fields['110']=='2'


def test_cancel_waits_for_ack_and_replace_preserves_id_chain(terminal):
    preview=terminal.preview(ticket())
    result=terminal.submit({'token':preview['token'],'confirmed':True})
    oid=result['order_id']
    report(terminal,{'35':'8','11':oid,'37':'TT-1','39':'0','150':'0','14':'0','151':'2'})
    terminal.manage({'order_id':oid,'quantity':'3','price':'0'},replace=True)
    fields=sent(terminal,'G')[0]
    assert fields['41']==oid and fields['37']=='TT-1' and fields['44']=='0'
    assert terminal.orders[oid]['ticket']['quantity']=='2'
    report(terminal,{'35':'8','11':fields['11'],'41':oid,'39':'0','150':'5','14':'0','151':'3'})
    assert terminal.orders[oid]['ticket']['quantity']=='3'
    terminal.manage({'order_id':oid})
    cancel=sent(terminal,'F')[0]
    assert cancel['41']==fields['11']
    assert terminal.orders[oid]['status']=='NEW'
    report(terminal,{'35':'9','11':cancel['11'],'41':fields['11'],'39':'1','58':'Too late to cancel'})
    assert terminal.orders[oid]['pending'] is None
    assert terminal.orders[oid]['status']=='PARTIALLY_FILLED'
    assert terminal.orders[oid]['text']=='Too late to cancel'


def test_fills_deduplicate_and_restart_does_not_resend(terminal):
    p=terminal.preview(ticket());oid=terminal.submit({'token':p['token'],'confirmed':True})['order_id']
    fill={'35':'8','11':oid,'37':'TT-1','39':'1','150':'1','17':'EXEC1','14':'1','151':'1','32':'1','31':'-0.5','6':'-0.5'}
    report(terminal,fill);report(terminal,fill)
    assert len(terminal.snapshot()['fills'])==1
    path=terminal.db.execute('PRAGMA database_list').fetchone()[2]
    restored=ManualTerminal(terminal.gateway,path)
    assert restored.orders[oid]['status']=='UNKNOWN'
    restored.submit({'token':p['token'],'confirmed':True})
    assert len(sent(terminal,'D'))==1


def test_saved_watchlist_resubscribes_after_new_market_session(terminal):
    terminal.gateway._sessions['Market Data']=Session()
    terminal.poll()
    messages=terminal.gateway._sessions['Market Data'].sent
    assert len(messages)==2 and all(msg=='V' for msg,_ in messages)
    assert all(not r['quote'].get('timestamp') for r in terminal.snapshot()['watchlist'])


# -- slippage on manual tickets ----------------------------------------------

def quote(terminal, bid, ask, key='101', age_sec=0.0):
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    terminal.books[key].update(bid=bid, ask=ask, timestamp=stamp,
                               integrity_ok=True)


def test_a_manual_fill_is_measured_against_the_touch_when_it_was_sent(terminal):
    """The price you could have crossed at when you clicked Send: the offer
    for a buy. A buy filled two ticks above it cost two ticks."""
    quote(terminal, 10.0, 10.25)
    preview = terminal.preview(ticket(order_type='MARKET', quantity='1', price=''))
    oid = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    assert terminal.orders[oid]['decision']['price'] == 10.25
    report(terminal, {'35':'8','11':oid,'37':'TT-1','39':'2','150':'2','17':'EX1',
                      '14':'1','151':'0','32':'1','31':'10.75','6':'10.75'})
    fill = terminal.snapshot()['fills'][0]
    assert fill['slippage_points'] == pytest.approx(0.5)
    assert fill['slippage_ticks'] == pytest.approx(2.0)          # tick 0.25
    assert fill['slippage_money'] == pytest.approx(2.0 * 12.5)   # $12.50 a tick
    summary = terminal.snapshot()['slippage']
    assert summary['entry']['measured'] == 1
    assert summary['entry']['ticks_mean'] == pytest.approx(2.0)


def test_a_passive_limit_filled_inside_the_spread_is_an_improvement(terminal):
    quote(terminal, 10.0, 10.25)
    preview = terminal.preview(ticket(price='10', quantity='1'))
    oid = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(terminal, {'35':'8','11':oid,'37':'TT-1','39':'2','150':'2','17':'EX2',
                      '14':'1','151':'0','32':'1','31':'10','6':'10'})
    assert terminal.snapshot()['fills'][0]['slippage_ticks'] == pytest.approx(-1.0)


def test_no_fresh_quote_at_send_is_unmeasured_never_zero(terminal):
    quote(terminal, 10.0, 10.25, age_sec=600)            # stale
    preview = terminal.preview(ticket(order_type='MARKET', quantity='1', price=''))
    oid = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(terminal, {'35':'8','11':oid,'37':'TT-1','39':'2','150':'2','17':'EX3',
                      '14':'1','151':'0','32':'1','31':'10.25','6':'10.25'})
    fill = terminal.snapshot()['fills'][0]
    assert fill['slippage_ticks'] is None
    summary = terminal.snapshot()['slippage']
    assert summary['entry']['measured'] == 0 and summary['entry']['unmeasured'] == 1


def test_a_close_is_measured_as_an_exit_on_its_own_side(terminal):
    """The close of a long SELLS: measured against the bid when it was sent."""
    quote(terminal, 10.0, 10.25)
    preview = terminal.preview(ticket(order_type='MARKET', quantity='1', price=''))
    oid = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(terminal, {'35':'8','11':oid,'37':'TT-1','39':'2','150':'2','17':'EX4',
                      '14':'1','151':'0','32':'1','31':'10.25','6':'10.25'})
    quote(terminal, 11.0, 11.25)
    close = terminal.preview_close({'order_id': oid})
    cid = terminal.submit({'token': close['token'], 'confirmed': True})['order_id']
    report(terminal, {'35':'8','11':cid,'37':'TT-2','39':'2','150':'2','17':'EX5',
                      '14':'1','151':'0','32':'1','31':'10.75','6':'10.75'})
    summary = terminal.snapshot()['slippage']
    assert summary['exit']['measured'] == 1
    assert summary['exit']['ticks_mean'] == pytest.approx(1.0)  # sold 11.0 -> 10.75
    assert summary['entry']['ticks_mean'] == pytest.approx(0.0)  # a measured 0.00


def test_a_close_with_a_price_rests_as_a_limit_close(terminal):
    """The desk ladder's Close @ LMT on a manual position: a LIMIT at the
    trader's price, flagged CLOSE (77=C), capped at what the ticket has open
    — and, the control, without a price it crosses at MARKET."""
    preview = terminal.preview(ticket(quantity='2'))
    oid = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(terminal, {'35': '8', '11': oid, '37': 'TT-1', '39': '2', '150': '2', '17': 'EX-L',
                      '14': '2', '151': '0', '32': '2', '31': '10', '6': '10'})
    limit = terminal.preview_close({'order_id': oid, 'price': '10.5'})
    assert limit['ticket']['order_type'] == 'LIMIT' and limit['ticket']['price'] == '10.5'
    assert limit['ticket']['open_close'] == 'C' and float(limit['ticket']['quantity']) == 2
    fields = dict(limit['fields'])
    assert fields['40'] == '2' and fields['44'] == '10.5' and fields['77'] == 'C'
    market = terminal.preview_close({'order_id': oid})
    assert market['ticket']['order_type'] == 'MARKET' and market['ticket']['price'] is None
