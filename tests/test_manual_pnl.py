from tests.test_manual_terminal import terminal, ticket, report


def fill_open(terminal, quantity='2', price='-0.5'):
    preview = terminal.preview(ticket(quantity=quantity))
    order_id = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(terminal, {'35':'8','11':order_id,'39':'2','150':'2','17':'ENTRY',
                      '14':quantity,'151':'0','32':quantity,'31':price,'6':price})
    return order_id


def test_floating_pnl_uses_fresh_tt_mid_and_tick_value(terminal):
    order_id = fill_open(terminal)
    request = terminal.subscriptions['101']
    raw = f'35=W\x01262={request}\x01268=2\x01269=0\x01270=0.5\x01271=3\x01269=1\x01270=1.0\x01271=4\x01'
    terminal.on_message('Market Data', {'35':'W','34':'42'}, raw)
    pnl = terminal.snapshot()['pnl']
    position = next(p for p in pnl['positions'] if p['entry_order_id'] == order_id)
    assert position['mark_price'] == 0.75
    assert position['floating_pnl'] == 125.0
    assert position['mark_source'] == 'TT FIX bid/ask midpoint'
    assert pnl['account']['balance'] is None


def test_realized_pnl_pairs_actual_close_execution(terminal):
    order_id = fill_open(terminal)
    preview = terminal.preview_close({'order_id': order_id})
    close_id = terminal.submit({'token': preview['token'], 'confirmed': True})['order_id']
    report(terminal, {'35':'8','11':close_id,'39':'2','150':'2','17':'EXIT',
                      '14':'2','151':'0','32':'2','31':'0.75','6':'0.75'})
    pnl = terminal.snapshot()['pnl']
    trade = next(t for t in pnl['trades'] if t['entry_order_id'] == order_id)
    assert trade['exit_order_id'] == close_id
    assert trade['realized_pnl'] == 125.0
    assert pnl['realized_total'] == 125.0
