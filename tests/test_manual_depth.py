from tests.test_manual_terminal import terminal


def update(terminal, body, message='X'):
    raw=f'35={message}\x01262={terminal.subscriptions["101"]}\x01'+body.replace('|','\x01')
    terminal.on_message('Market Data',{'35':message},raw)


def test_depth_updates_keep_best_price_and_promote_deleted_levels(terminal):
    terminal.depth({'security_id':'101','enabled':True})
    assert terminal.watch['101']['full_depth'] is True
    assert dict(terminal.gateway._sessions['Market Data'].sent[-1][1])['264']=='0'
    update(terminal,'268=3|269=0|290=1|270=10|271=2|269=0|290=2|270=9.75|271=4|269=1|290=1|270=10.25|271=3|','W')
    assert terminal.books['101']['bid']==10
    assert len(terminal.books['101']['bid_levels'])==2
    update(terminal,'268=1|279=2|269=0|290=1|')
    assert terminal.books['101']['bid']==9.75
    assert terminal.books['101']['bid_size']==4
    update(terminal,'268=1|279=0|269=0|290=1|270=10|271=7|')
    assert terminal.books['101']['bid']==10
    assert len(terminal.books['101']['bid_levels'])==2
    update(terminal,'268=1|279=1|269=0|290=1|271=6|')
    assert terminal.books['101']['bid']==10
    assert terminal.books['101']['bid_size']==6


def test_depth_by_entry_id_and_empty_book(terminal):
    update(terminal,'268=2|269=0|278=a|270=10|271=2|269=0|278=b|270=9|271=3|','W')
    update(terminal,'268=1|279=2|278=a|')
    assert terminal.books['101']['bid']==9
    update(terminal,'268=1|279=1|269=0|278=c|280=b|270=8|271=4|')
    assert terminal.books['101']['bid']==8
    assert len(terminal.books['101']['bid_levels'])==1
    update(terminal,'268=1|279=0|269=J|')
    assert terminal.books['101']['bid'] is None
    assert terminal.books['101']['ask'] is None
    assert terminal.books['101']['bid_levels']==[]


def test_catalogue_keeps_products_from_previous_searches(terminal):
    assert terminal.catalogue['101']['symbol']=='ES'
    terminal.lookup({'exchange':'CME','symbol':'CL','security_type':'FUT'})
    assert not terminal.instruments
    assert terminal.catalogue['101']['symbol']=='ES'
    assert any(i['symbol']=='ES' for i in terminal.snapshot()['catalogue'])


def test_multi_action_message_uses_positions_before_message(terminal):
    update(terminal,'268=3|269=0|290=1|270=10|271=2|269=0|290=2|270=9.75|271=4|269=0|290=3|270=9.5|271=6|','W')
    update(terminal,'268=4|279=2|269=0|290=1|279=0|269=0|290=1|270=10.25|271=8|279=1|269=0|290=2|271=7|279=2|269=0|290=3|')
    assert terminal.books['101']['bid_levels']==[{'price':10.25,'size':8},{'price':9.75,'size':7}]
    update(terminal,'268=1|279=1|269=0|290=2|271=9|')
    assert terminal.books['101']['bid_levels'][1]['size']==9


def test_readding_instrument_preserves_saved_depth(terminal):
    import json
    terminal.depth({'security_id':'101','enabled':True})
    terminal.add({'security_id':'101'})
    row=terminal.db.execute("SELECT data FROM manual_state WHERE kind='watch' AND id='101'").fetchone()
    assert json.loads(row[0])['full_depth'] is True


def test_last_traded_is_requested_and_updates_last_without_changing_book(terminal):
    fields=terminal.gateway._sessions['Market Data'].sent[-1][1]
    assert ('267','4') in fields and ('269','x') in fields
    update(terminal,'268=3|269=0|270=10|271=2|269=1|270=10.25|271=3|269=x|270=10.1|271=1|','W')
    assert terminal.books['101']['last']==10.1
    update(terminal,'268=1|279=1|269=x|270=10.2|271=2|')
    assert terminal.books['101']['last']==10.2
    assert terminal.books['101']['bid']==10
    assert terminal.books['101']['ask']==10.25
