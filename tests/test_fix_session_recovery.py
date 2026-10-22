"""The FIX session recovers instead of stopping.

Stellar's Order Router honours ResendRequest (2) and resends every
execution report a client missed. The session must not STOP on a gap, on
Stellar's ResendRequest, on a SequenceReset or on a Reject — a disconnect
where FIX expects recovery. Each case below is driven over the real
NativeFixSession against a scripted Stellar."""
import socket
import time

import pytest

from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway, encode_fix_message, parse_fix_message
from fixtrader.models import SessionState


class ScriptedStellar:
    """A Stellar peer whose sequence numbers the test controls."""

    def __init__(self):
        self.sent, self.received = [], []
        self.comp = None

    def settimeout(self, t):
        pass

    def push(self, seq, fields, possdup=False):
        header = [('35', fields[0][1]), ('34', str(seq)), ('49', 'STELLAR'), ('56', self.comp)]
        if possdup:
            header.append(('43', 'Y'))
        self.received.append(encode_fix_message(header + fields[1:]))

    def sendall(self, payload):
        f = parse_fix_message(payload.decode('ascii'))
        self.sent.append(f)
        self.comp = f['49']
        if f['35'] == 'A':
            self.push(1, [('35', 'A'), ('98', '0'), ('108', '30')])

    def recv(self, n):
        if self.received:
            return self.received.pop(0)
        time.sleep(0.002)
        raise socket.timeout()

    def close(self):
        pass


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv('T_OR', 'x')
    peers = []

    def create(*a, **k):
        p = ScriptedStellar()
        peers.append(p)
        return p
    monkeypatch.setattr(socket, 'create_connection', create)
    venue = VenueConfig(name='STELLAR-UAT', host='or.example', port=9001, fix_version='FIX.4.4',
                        use_tls=False, sender_comp_id='STELLAR_UAT', target_comp_id='STELLAR',
                        password_env='T_OR', account='ACC1', username='USER')
    gw = FixGateway(venue)
    gw.start()
    deadline = time.monotonic() + 2
    while gw.state() != SessionState.LOGGED_ON and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gw.state() == SessionState.LOGGED_ON
    yield gw, peers[0], gw._sessions['Order Routing']
    gw.stop()


def settle():
    time.sleep(0.15)


def report(cid, exec_id, status):
    return [('35', '8'), ('11', cid), ('37', 'T1'), ('17', exec_id), ('150', status),
            ('39', status), ('14', '0'), ('151', '1')]


def test_a_gap_asks_stellar_for_the_missed_messages_and_keeps_order(session):
    gw, tt, s = session
    tt.push(4, [('35', '0')])                         # 2 and 3 missed
    settle()
    resend = [m for m in tt.sent if m['35'] == '2']
    assert resend and resend[-1]['7'] == '2'          # from the first one missed
    assert gw.state() == SessionState.LOGGED_ON       # still up
    assert s.state.in_seq == 1                        # 4 is held, not applied
    tt.push(2, [('35', '0')], possdup=True)
    tt.push(3, [('35', '0')], possdup=True)
    settle()
    assert s.state.in_seq == 4 and not s.pending      # filled, then the held one


def test_stellars_resend_request_is_answered_with_a_gap_fill_never_old_orders(session):
    gw, tt, s = session
    tt.push(2, [('35', '2'), ('7', '1'), ('16', '0')])
    settle()
    fill = [m for m in tt.sent if m['35'] == '4']
    assert fill and fill[-1]['123'] == 'Y' and fill[-1]['43'] == 'Y'
    assert fill[-1]['34'] == '1' and int(fill[-1]['36']) == s.state.out_seq + 1
    assert not [m for m in tt.sent if m['35'] == 'D']   # nothing replayed
    assert gw.state() == SessionState.LOGGED_ON


def test_a_sequence_reset_moves_the_expected_number(session):
    gw, tt, s = session
    tt.push(2, [('35', '4'), ('36', '50')])            # Reset mode
    settle()
    assert s.state.in_seq == 49
    tt.push(50, [('35', '0')])
    settle()
    assert s.state.in_seq == 50 and gw.state() == SessionState.LOGGED_ON


def test_a_reject_does_not_stop_the_session(session):
    gw, tt, s = session
    tt.push(2, [('35', '3'), ('45', '2'), ('372', 'H'), ('58', 'Unsupported')])
    tt.push(3, [('35', '3'), ('45', '3'), ('372', 'D'), ('58', 'Invalid tag')])
    settle()
    assert gw.state() == SessionState.LOGGED_ON and s.state.in_seq == 3


def test_a_resent_duplicate_is_not_applied_twice(session):
    gw, tt, s = session
    tt.push(2, [('35', '0')])
    tt.push(2, [('35', '0')], possdup=True)            # the same, again
    settle()
    assert s.state.in_seq == 2 and gw.state() == SessionState.LOGGED_ON


def test_too_low_without_possdup_still_stops_the_session(session):
    """The control: a number already used, NOT marked as a resend, is a
    broken session — stopped, in words, as before."""
    gw, tt, s = session
    tt.push(2, [('35', '0')])
    tt.push(2, [('35', '0')])
    settle()
    assert gw.state() != SessionState.LOGGED_ON
    assert 'sequence mismatch' in (s.state.error or '')


# -- after a logon that opened a gap: new orders wait for the resend --------------

def test_new_orders_wait_for_the_resend_but_a_close_never_does(session):
    gw, tt, s = session
    tt.push(4, [('35', '0')])                          # 2 and 3 missed: a resend is asked for
    settle()
    assert not s.ready()                               # logged on, not caught up
    with pytest.raises(ConnectionError, match='resending'):
        s.send('D', [('11', 'FTM-0123456789abcdef'), ('77', 'O'), ('54', '1')])
    # The control: a close goes whatever — a close is never held back.
    s.send('D', [('11', 'FTM-fedcba9876543210'), ('77', 'C'), ('54', '2')])
    assert [m for m in tt.sent if m['35'] == 'D' and m['77'] == 'C']
    tt.push(2, [('35', '0')], possdup=True)
    tt.push(3, [('35', '0')], possdup=True)
    settle()
    assert s.ready()
    s.send('D', [('11', 'FTM-0123456789abcdee'), ('77', 'O'), ('54', '1')])


def test_with_no_gap_the_session_is_ready_at_once(session):
    gw, tt, s = session
    assert s.ready()


def test_if_the_resend_never_completes_the_session_goes_ahead_after_a_short_wait(session, monkeypatch):
    gw, tt, s = session
    monkeypatch.setattr(type(s), 'READY_FALLBACK_SEC', 0.05)
    tt.push(4, [('35', '0')])
    settle()
    assert s.ready()
    gw.audit.pending.join()                          # the log is written in the background
    rows = gw.audit.read(50, '', 'Resend from Stellar not complete')
    assert rows


def test_a_market_data_gap_asks_for_no_resend_and_requests_prices_again(monkeypatch):
    """Stellar: "resend requests on Market Data connections are not
    supported". A gap there is said, the numbers move on, and every book is
    asked for afresh — never a ResendRequest."""
    peers = []

    def create(*a, **k):
        p = ScriptedStellar()
        peers.append(p)
        return p
    monkeypatch.setattr(socket, 'create_connection', create)
    venue = VenueConfig(name='STELLAR-UAT', host='or.example', port=9001, fix_version='FIX.4.4',
                        use_tls=False, sender_comp_id='STELLAR_UAT', target_comp_id='STELLAR',
                        md_host='md.example', md_port=9002, md_sender_comp_id='STELLAR_UAT_MD',
                        account='ACC1', username='USER')
    gw = FixGateway(venue)
    gw.start()
    try:
        deadline = time.monotonic() + 2
        while gw.state() != SessionState.LOGGED_ON and time.monotonic() < deadline:
            time.sleep(0.01)
        md_peer = next(p for p in peers if p.comp == 'STELLAR_UAT_MD')
        md = gw._sessions['Market Data']
        md_peer.push(5, [('35', '0')])                 # 2, 3 and 4 missed
        settle()
        assert md.state.in_seq == 5 and not md.pending
        assert not [m for m in md_peer.sent if m['35'] == '2']
        assert gw.terminal._resubscribe is True
        assert gw.state() == SessionState.LOGGED_ON
    finally:
        gw.stop()


def test_a_session_reject_is_logged_with_its_reason_in_words(session):
    gw, tt, s = session
    tt.push(2, [('35', '3'), ('45', '5'), ('371', '77'), ('372', 'D'), ('373', '5'),
                ('58', 'Bad value')])
    settle()
    gw.audit.pending.join()                          # the log is written in the background
    rows = gw.audit.read(50, '', 'Reject')
    assert any('incorrect value' in str(r.get('details')) for r in rows)
