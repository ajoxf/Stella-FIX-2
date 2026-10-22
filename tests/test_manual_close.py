import pytest
from tests.test_manual_terminal import terminal, ticket, report, sent


def filled(terminal):
    p=terminal.preview(ticket(quantity='2'))
    oid=terminal.submit({'token':p['token'],'confirmed':True})['order_id']
    report(terminal,{'35':'8','11':oid,'39':'2','150':'2','17':'OPEN-FILL','14':'2','151':'0','32':'2','31':'-0.5'})
    return oid


def test_close_review_opposite_side_close_flag_and_duplicate_reservation(terminal):
    oid=filled(terminal)
    first=terminal.preview_close({'order_id':oid})
    second=terminal.preview_close({'order_id':oid})
    assert first['ticket']['side']=='SELL'
    assert first['ticket']['quantity']=='2.0'
    fields=dict(first['fields'])
    assert fields['40']=='1' and fields['77']=='C' and fields['48']=='101'
    assert '44' not in fields
    close=terminal.submit({'token':first['token'],'confirmed':True})['order_id']
    terminal.submit({'token':first['token'],'confirmed':True})
    assert len(sent(terminal,'D'))==2
    with pytest.raises(ValueError):terminal.submit({'token':second['token'],'confirmed':True})
    assert terminal.closeable(terminal.orders[oid])==0
    report(terminal,{'35':'8','11':close,'39':'2','150':'2','17':'CLOSE-FILL','14':'2','151':'0','32':'2','31':'-0.5'})
    assert terminal.closeable(terminal.orders[oid])==0
    assert next(o for o in terminal.snapshot()['orders'] if o['id']==oid)['closed_qty']==2
    with pytest.raises(ValueError):terminal.preview_close({'order_id':close})


def test_rejected_close_releases_reservation_unknown_does_not(terminal):
    oid=filled(terminal)
    p=terminal.preview_close({'order_id':oid});cid=terminal.submit({'token':p['token'],'confirmed':True})['order_id']
    terminal.orders[cid]['status']='UNKNOWN'
    assert terminal.closeable(terminal.orders[oid])==0
    report(terminal,{'35':'8','11':cid,'39':'8','150':'8','14':'0','151':'0','58':'Rejected'})
    assert terminal.closeable(terminal.orders[oid])==2


def test_partial_close_then_cancel_only_remaining_fill_can_close(terminal):
    oid=filled(terminal)
    p=terminal.preview_close({'order_id':oid});cid=terminal.submit({'token':p['token'],'confirmed':True})['order_id']
    report(terminal,{'35':'8','11':cid,'39':'4','150':'4','14':'1','151':'0'})
    assert terminal.preview_close({'order_id':oid})['ticket']['quantity']=='1.0'


def test_no_close_for_unfilled_or_still_working_source(terminal):
    p=terminal.preview(ticket());oid=terminal.submit({'token':p['token'],'confirmed':True})['order_id']
    with pytest.raises(ValueError):terminal.preview_close({'order_id':oid})
    report(terminal,{'35':'8','11':oid,'39':'1','150':'1','14':'1','151':'1','17':'PART'})
    with pytest.raises(ValueError):terminal.preview_close({'order_id':oid})
