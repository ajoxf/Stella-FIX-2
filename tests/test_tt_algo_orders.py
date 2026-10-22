"""The Algo's orders to TT over the real FIX session, against a fake TT on the
socket: what goes out (tag by tag), what comes back (as engine events), and
the positions request — answered, refused, or ignored."""
import socket
import time
from types import SimpleNamespace

import pytest

from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway, encode_fix_message, parse_fix_message
from fixtrader.models import (Intent, OrderRequest, OrderType, PositionEffect,
                              SessionState, Side)

SOH = '\x01'


class TT:
    """A fake TT Order Routing / Market Data peer."""

    def __init__(self, positions='answer'):
        self.sent, self.received, self.closed = [], [], False
        self.seq = 1
        self.positions = positions
        self.fill_price = '0.55'

    def settimeout(self, timeout):
        pass

    def reply(self, fields):
        self.seq += 1
        comp = self.comp
        msg = encode_fix_message([('35', fields[0][1]), ('34', str(self.seq)),
                                  ('49', 'TT'), ('56', comp)] + fields[1:])
        self.received.append(msg)

    def sendall(self, payload):
        f = parse_fix_message(payload.decode('ascii'))
        self.sent.append(f)
        self.comp = f['49']
        kind = f['35']
        if kind == 'A':
            msg = encode_fix_message([('35', 'A'), ('34', '1'), ('49', 'TT'),
                                      ('56', f['49']), ('98', '0'),
                                      ('108', '30')])
            self.received.append(msg)
            self.reply([('35', 'B'), ('148', 'Recovery Complete'), ('33', '1'),
                        ('58', 'Recovery is complete')])
        elif kind == 'D':
            self.reply([('35', '8'), ('11', f['11']), ('37', 'TT-' + f['11']),
                        ('17', 'A-' + f['11']), ('150', '0'), ('39', '0'),
                        ('14', '0'), ('151', f['38'])])
            if f['40'] == '1':                       # market: filled at once
                self.reply([('35', '8'), ('11', f['11']),
                            ('37', 'TT-' + f['11']), ('17', 'X-' + f['11']),
                            ('150', '2'), ('39', '2'), ('32', f['38']),
                            ('31', self.fill_price), ('14', f['38']),
                            ('151', '0'), ('6', self.fill_price),
                            ('60', '20261008-10:00:00.000')])
        elif kind == 'F':
            self.reply([('35', '8'), ('11', f['11']), ('41', f['41']),
                        ('37', f.get('37', '')), ('17', 'C-' + f['11']),
                        ('150', '4'), ('39', '4'), ('14', '0')])
        elif kind == 'G':
            self.reply([('35', '8'), ('11', f['11']), ('41', f['41']),
                        ('17', 'R-' + f['11']), ('150', '5'), ('39', '0'),
                        ('44', f.get('44', '')), ('38', f['38']), ('14', '0')])
        elif kind == 'AN':
            if self.positions == 'answer':
                self.reply([('35', 'AO'), ('710', f['710']), ('728', '0'),
                            ('727', '1'), ('1', f['1'])])
                self.reply([('35', 'AP'), ('710', f['710']), ('727', '1'),
                            ('55', 'FEF'), ('48', '123'), ('702', '1'),
                            ('703', 'TQ'), ('704', '3'), ('705', '0')])
            elif self.positions == 'business_reject':
                self.reply([('35', 'j'), ('372', 'AN'), ('379', f['710']),
                            ('380', '3'), ('58', 'Unsupported message type')])
            elif self.positions == 'session_reject':
                self.reply([('35', '3'), ('45', f['34']), ('372', 'AN'),
                            ('58', 'Invalid MsgType')])
            # 'silent': no answer at all

    def recv(self, size):
        if self.received:
            return self.received.pop(0)
        time.sleep(0.002)
        raise socket.timeout()

    def close(self):
        self.closed = True


@pytest.fixture
def connect(monkeypatch):
    monkeypatch.setenv('TEST_OR', 'or-secret')
    monkeypatch.setenv('TEST_MD', 'md-secret')
    # These tests exercise the request path, kept for a venue that answers
    # it; TT's Order Routing does not, so it is off by default.
    from fixtrader.gateway import AlgoOrderRouter
    monkeypatch.setattr(AlgoOrderRouter, 'ASK_POSITIONS', True)

    def make(positions='answer'):
        peers = []

        def create(*a, **k):
            peer = TT(positions)
            peers.append(peer)
            return peer
        monkeypatch.setattr(socket, 'create_connection', create)
        venue = VenueConfig(name='TT-UAT', host='or.example', port=11502,
                            fix_version='FIX.4.2', use_tls=False,
                            sender_comp_id='ORDER', target_comp_id='TT',
                            password_env='TEST_OR', md_host='md.example',
                            md_port=11503, md_sender_comp_id='MARKET',
                            md_password_env='TEST_MD', account='ACC1')
        gw = FixGateway(venue)
        gw.start()
        deadline = time.monotonic() + 5
        while gw.state() != SessionState.LOGGED_ON and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gw.state() == SessionState.LOGGED_ON
        gw.subscribe(SimpleNamespace(key='fef', name='Iron ore', symbol='FEF',
                                     security_id='123', security_exchange='SGX',
                                     tick_size=0.01, tick_value=1.0))
        orr = next(p for p in peers if p.sent[0]['49'] == 'ORDER')
        return gw, orr
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


def sent(peer, kind):
    return [f for f in peer.sent if f['35'] == kind]


def test_an_algo_entry_goes_out_as_an_automated_OPEN_and_fills(connect):
    gw, tt = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.SELL, qty=2,
                                   order_type=OrderType.MARKET,
                                   reason='H to L z +2.61'))
        events = wait_for(gw, ['ACK', 'FILL'])
        d = sent(tt, 'D')[-1]
        assert d['11'] == cid and cid.startswith('FT-')
        assert d['1'] == 'ACC1' and d['48'] == '123' and d['22'] == '96'
        assert d['54'] == '2' and d['38'] == '2' and d['40'] == '1'
        assert d['77'] == 'O'                      # it says it OPENS
        assert d['1028'] == 'N'                    # automated, not manual
        assert d['18'] == 'o 2'                    # cancelled if we drop
        fill = next(e for e in events if e.kind == 'FILL')
        assert fill.clordid == cid and fill.contract_key == 'fef'
        assert fill.fill.qty == 2 and fill.fill.price == pytest.approx(0.55)
        assert fill.fill.exec_id == 'X-' + cid
        assert fill.order.state.value == 'FILLED'
    finally:
        gw.stop()


def test_a_close_says_CLOSE_and_names_its_position_and_tickets(connect):
    gw, tt = connect()
    try:
        gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=2,
                             order_type=OrderType.MARKET, intent=Intent.CLOSE,
                             reduce_only=True,
                             position_effect=PositionEffect.CLOSE,
                             position_id=7, close_tickets=['X-1', 'X-2']))
        wait_for(gw, ['FILL'])
        d = sent(tt, 'D')[-1]
        assert d['77'] == 'C'                      # never a bare opposite order
        assert d['54'] == '1' and d['38'] == '2'
        assert d['58'] == 'Close P7 X-1 X-2'
    finally:
        gw.stop()


def test_a_duplicate_execution_report_is_not_a_second_fill(connect):
    gw, tt = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1,
                                   order_type=OrderType.MARKET))
        first = wait_for(gw, ['FILL'])
        # TT resends the same fill
        tt.reply([('35', '8'), ('11', cid), ('17', 'X-' + cid), ('150', '2'),
                  ('39', '2'), ('32', '1'), ('31', '0.55'), ('14', '1')])
        again = wait_for(gw, ['FILL'], timeout=0.3)
        assert sum(e.kind == 'FILL' for e in first) == 1
        assert not any(e.kind == 'FILL' for e in again)
    finally:
        gw.stop()


def test_cancel_and_replace_keep_the_lineage_and_the_root_id(connect):
    gw, tt = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=3,
                                   order_type=OrderType.LIMIT, price=0.5))
        wait_for(gw, ['ACK'])
        assert sent(tt, 'D')[-1]['44'] == '0.5'
        gw.amend(cid, price=0.51)
        replaced = wait_for(gw, ['REPLACED'])
        g = sent(tt, 'G')[-1]
        assert g['41'] == cid and g['44'] == '0.51' and g['77'] == 'O'
        event = next(e for e in replaced if e.kind == 'REPLACED')
        assert event.clordid == cid                 # the ROOT id, not the new one
        assert event.order.price == pytest.approx(0.51)
        gw.cancel(cid)
        cancelled = wait_for(gw, ['CANCELLED'])
        f = sent(tt, 'F')[-1]
        assert f['41'] == g['11']                   # the CURRENT id is cancelled
        assert next(e for e in cancelled if e.kind == 'CANCELLED').clordid == cid
    finally:
        gw.stop()


def test_a_cancel_reject_carries_tts_words(connect):
    gw, tt = connect()
    try:
        cid = gw.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=1,
                                   order_type=OrderType.LIMIT, price=0.5))
        wait_for(gw, ['ACK'])
        gw.algo.orders[cid]['pending'] = None
        tt.reply([('35', '9'), ('11', 'FT-x'), ('41', cid), ('39', '0'),
                  ('434', '1'), ('58', 'Too late to cancel')])
        events = wait_for(gw, ['CANCEL_REJECTED'])
        assert next(e for e in events if e.kind == 'CANCEL_REJECTED').text == \
            'Too late to cancel'
    finally:
        gw.stop()


def test_a_manual_tickets_report_is_not_the_algos(connect):
    gw, tt = connect()
    try:
        tt.reply([('35', '8'), ('11', 'FTM-abc'), ('17', 'M1'), ('150', '2'),
                  ('39', '2'), ('32', '1'), ('31', '0.5'), ('14', '1')])
        assert not any(e.kind == 'FILL' for e in wait_for(gw, [], 0.3))
    finally:
        gw.stop()


def test_nothing_but_our_own_orders_can_be_sent(connect):
    gw, tt = connect()
    try:
        session = gw._sessions['Order Routing']
        with pytest.raises(NotImplementedError):
            session.send('D', [('11', 'SOMEONE-ELSE-1')])
    finally:
        gw.stop()


# -- positions -------------------------------------------------------------------

def poll_positions(gw, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        gw.drain_events()
        if gw.positions_status()['status'] in ('complete', 'unavailable'):
            break
        time.sleep(0.01)
    return gw.positions_status()


def test_positions_answered_by_tt_are_the_account(connect):
    gw, tt = connect('answer')
    try:
        assert poll_positions(gw)['status'] == 'complete'
        an = sent(tt, 'AN')[-1]
        assert an['1'] == 'ACC1' and an['724'] == '0'
        rows = gw.positions()
        assert len(rows) == 1 and rows[0].contract_key == 'fef'
        assert rows[0].qty == 3 and rows[0].long_qty == 3
    finally:
        gw.stop()


@pytest.mark.parametrize('how', ['business_reject', 'session_reject'])
def test_positions_refused_are_unknown_and_the_session_survives(connect, how):
    """TT may not support the request. Its refusal is an ANSWER: positions
    unknown (None, never an empty account) — and Order Routing stays up."""
    gw, tt = connect(how)
    try:
        status = poll_positions(gw)
        assert status['status'] == 'unavailable'
        assert 'rejected' in status['why']
        assert gw.positions() is None
        time.sleep(0.05)
        assert gw.state() == SessionState.LOGGED_ON
    finally:
        gw.stop()


def test_positions_unanswered_time_out_as_unknown(connect, monkeypatch):
    import fixtrader.gateway as g
    monkeypatch.setattr(g, 'POSITIONS_TIMEOUT_SEC', 0.05)
    gw, tt = connect('silent')
    try:
        status = poll_positions(gw)
        assert status['status'] == 'unavailable'
        assert 'did not answer' in status['why']
        assert gw.positions() is None
    finally:
        gw.stop()


def test_by_default_no_positions_request_goes_to_tt_and_positions_are_unknown(
        monkeypatch):
    """TT FIX Order Routing does not list Request For Positions (AN) and asks
    clients to send nothing it does not list: nothing is sent, and positions
    are UNKNOWN (never flat), with the reason."""
    monkeypatch.setenv('TEST_OR', 'or-secret')
    peers = []

    def create(*a, **k):
        p = TT()
        peers.append(p)
        return p
    monkeypatch.setattr(socket, 'create_connection', create)
    venue = VenueConfig(name='TT-UAT', host='or.example', port=11502,
                        fix_version='FIX.4.2', use_tls=False,
                        sender_comp_id='ORDER', target_comp_id='TT',
                        password_env='TEST_OR', account='ACC1')
    gw = FixGateway(venue)
    gw.start()
    try:
        deadline = time.monotonic() + 2
        while gw.state() != SessionState.LOGGED_ON and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        gw.algo.request_positions(time.monotonic())
        assert not [f for p in peers for f in p.sent if f['35'] == 'AN']
        assert gw.positions() is None
        assert 'Drop Copy' in gw.positions_status()['why']
    finally:
        gw.stop()
