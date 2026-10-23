"""What Stellar's 2025 Order Router specification added, and what its FIX
Conformance Self Test asks this program to show on UAT before Production.

- Mass Order Status Request (AF, spec 2.19): an order of ours whose OrderID
  was never heard cannot be asked about by H (which names the OrderID) nor
  cancelled; the sweep asks for it by AF, and Stellar's answer names it by
  ClOrdID with its OrderID. Stellar's own example answers with 150=5: that
  is a status, never a replace.
- TradeID (1003, spec 3.4): a bust or correction names the exchange trade.
- Session Management: a client-initiated Logout is answered by Stellar's own
  Logout (clean, not an error); a Logon whose number skips ahead is asked to
  resend and answered with a GapFill.
- Security Definitions: a Security List Request by CFI code, symbol, market,
  or any combination, sends only what was given.
"""
import time

import pytest

from fixtrader import gateway as gw_mod
from fixtrader import stellar_exec
from fixtrader.gateway import FixGateway
from tests.fake_stellar import Exchange, install
from tests.test_fix_recovery import done, logged_on, recovery, stellar_venue, wait
from tests.test_stellar_exec import router, trade, working
from tests.test_price_units import terminal  # noqa: F401
from tests.test_uat_orders import open_desk


# -- Mass Order Status (AF) -----------------------------------------------------------

def mass_answer(**over):
    """A report as Stellar's example 8.20 answers an AF: ExecType 5."""
    f = {'35': '8', '11': 'FT-x-1', '37': 'OID-9', '150': '5', '39': '0', '17': '0',
         '14': '0', '151': '1', '584': 'MS-1', '911': '1', '912': 'Y'}
    f.update(over)
    return f


def test_a_mass_status_answer_is_a_status_whatever_its_exec_type():
    assert stellar_exec.kind(mass_answer()) == stellar_exec.STATUS
    assert stellar_exec.is_status_answer(mass_answer())
    # The control: the same report without 584 is a replace, not a status.
    plain = mass_answer()
    del plain['584']
    assert stellar_exec.kind(plain) is None and not stellar_exec.is_status_answer(plain)
    # A trade is a trade, whatever else it carries.
    assert stellar_exec.kind(trade(**{'584': 'MS-1'})) == stellar_exec.FILL


def test_the_algo_learns_an_unheard_order_id_from_a_mass_status_answer(terminal):
    answered = []
    r = router(terminal)
    r.gw.status_answered = answered.append
    vo = working(r)
    assert r.unheard_orders() == ['FT-x-1']
    r.on_message('8', mass_answer(), '')
    assert vo.venue_id == 'OID-9' and r.unheard_orders() == []
    assert answered == ['MS-1']
    # Not read as a replace: no REPLACED event, the order is as it was.
    assert [e.kind for e in r.drain()] == []
    assert vo.qty == 1


def test_only_the_last_report_of_a_mass_answer_ends_it(terminal):
    answered = []
    r = router(terminal)
    r.gw.status_answered = answered.append
    working(r)
    r.on_message('8', mass_answer(**{'912': 'N', '911': '2'}), '')
    assert answered == []


def test_a_bust_names_the_exchange_trade_id():
    text = stellar_exec.correction_text(trade(**{'150': 'H', '1003': 'X-77'}), stellar_exec.BUST)
    assert 'exchange trade X-77' in text
    # The control: without 1003 nothing is invented.
    assert 'exchange trade' not in stellar_exec.correction_text(trade(**{'150': 'H'}),
                                                                stellar_exec.BUST)


@pytest.fixture
def cdesk(tmp_path, monkeypatch):
    with open_desk(tmp_path, monkeypatch) as d:
        yield d


def test_an_order_whose_order_id_was_never_heard_is_found_by_mass_status(cdesk):
    done(cdesk)
    run = cdesk.runner()
    bid, ask, tick, dec = run.book()
    cdesk.tt.swallow_reports = 1                # its acknowledgement never comes
    oid = run.manual('BUY', 'LIMIT', run.px(bid - 5 * tick, tick, dec))
    time.sleep(0.5)
    assert not (run.manual_order(oid) or {}).get('venue_order_id')
    assert cdesk.d.command('recover_missed', '', {}).get('ok')
    r = wait(lambda: recovery(cdesk).get('state') == 'DONE' and recovery(cdesk),
             what='the sweep')
    assert cdesk.tt.mass_requests and cdesk.tt.mass_requests[-1]['585'] == '7'
    assert r['replayed'] == 1
    order = wait(lambda: (run.manual_order(oid) or {}).get('venue_order_id')
                 and run.manual_order(oid), what='the OrderID')
    assert order['status'] == 'NEW'
    run.manual_cancel(oid)                       # now it CAN be cancelled
    run.manual_status(oid, ('CANCELED',))


def test_with_every_order_id_known_no_mass_status_is_sent(cdesk):
    """The control."""
    done(cdesk)
    run = cdesk.runner()
    bid, ask, tick, dec = run.book()
    oid = run.manual('BUY', 'LIMIT', run.px(bid - 5 * tick, tick, dec))
    run.manual_status(oid, ('NEW',))
    assert cdesk.d.command('recover_missed', '', {}).get('ok')
    wait(lambda: recovery(cdesk).get('state') == 'DONE', what='the sweep')
    assert cdesk.tt.status_requests and not cdesk.tt.mass_requests
    run.manual_cancel(oid)


# -- Security Definitions: any combination of 55 / 461 / 207 ---------------------------

@pytest.mark.parametrize('args, sent', [
    ({'symbol': 'CL', 'exchange': 'XCME', 'security_type': 'FUT'},
     {'55': 'CL', '461': 'FXXXXX', '207': 'XCME'}),
    ({'symbol': 'CL', 'exchange': '', 'security_type': 'ANY'}, {'55': 'CL'}),
    ({'symbol': '', 'exchange': '', 'security_type': 'FUT'}, {'461': 'FXXXXX'}),
    ({'symbol': '', 'exchange': 'XCME', 'security_type': 'ANY'}, {'207': 'XCME'}),
    ({'symbol': 'CL', 'exchange': 'XCME', 'security_type': 'ANY'},
     {'55': 'CL', '207': 'XCME'}),
])
def test_a_security_list_request_sends_only_what_was_given(cdesk, args, sent):
    before = len(cdesk.tt.list_requests)
    result = cdesk.d.command('terminal_search', '', args)
    assert result.get('ok'), result
    asked = wait(lambda: cdesk.tt.list_requests[before:], what='the request')[0]
    assert {t: asked[t] for t in ('55', '461', '207') if t in asked} == sent
    assert asked['559'] == '0'
    found = wait(lambda: [i for i in cdesk.d.snapshot()['engine']['manual_terminal']
                          .get('instruments', []) if i['security_id'] == 'CL1'],
                 what='the answer')
    assert found


def test_a_search_with_nothing_to_search_by_is_refused(cdesk):
    result = cdesk.d.command('terminal_search', '',
                             {'symbol': '', 'exchange': '', 'security_type': 'ANY'})
    assert not result.get('ok') and 'symbol' in result.get('error', '')


# -- Session Management ----------------------------------------------------------------

def test_a_client_logout_is_answered_by_stellars_and_is_not_an_error(tmp_path, monkeypatch):
    stellar = Exchange()
    install(monkeypatch, stellar)
    gw = FixGateway(stellar_venue(), manual_path=str(tmp_path / 'm.db'))
    gw.start()
    logged_on(gw)
    sessions = dict(gw._sessions)
    gw.stop()
    assert [k for k, _ in stellar.logouts] == ['OR', 'MD'] or \
        sorted(k for k, _ in stellar.logouts) == ['MD', 'OR']
    for session in sessions.values():
        assert session.logout_answered.is_set()
        assert session.state.status == 'DISCONNECTED' and session.state.error == ''


def test_a_logout_stellar_never_answers_still_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, 'LOGOUT_WAIT_SEC', 0.3)
    stellar = Exchange()
    stellar.logout_silent = True
    install(monkeypatch, stellar)
    gw = FixGateway(stellar_venue(), manual_path=str(tmp_path / 'm.db'))
    gw.start()
    logged_on(gw)
    sessions = dict(gw._sessions)
    started = time.monotonic()
    gw.stop()
    assert time.monotonic() - started < 10
    assert all(not s.logout_answered.is_set() for s in sessions.values())
    assert all(not s.thread.is_alive() for s in sessions.values())


def test_a_logout_stellar_starts_is_still_said_as_an_error(tmp_path, monkeypatch):
    """The control: a Logout we did not ask for is Stellar closing us."""
    stellar = Exchange()
    install(monkeypatch, stellar)
    gw = FixGateway(stellar_venue(), manual_path=str(tmp_path / 'm.db'))
    gw.start()
    logged_on(gw)
    session = gw._sessions['Order Routing']
    stellar.or_peer().reply([('35', '5'), ('58', 'Session closed by Stellar')])
    wait(lambda: session.state.status == 'ERROR', 5, 'the logout')
    assert 'closed by Stellar' in session.state.error
    gw.stop()


def test_skipping_ahead_is_asked_for_and_answered_with_a_gap_fill(tmp_path, monkeypatch):
    stellar = Exchange()
    install(monkeypatch, stellar)
    db = str(tmp_path / 'm.db')
    gw = FixGateway(stellar_venue(), manual_path=db)
    gw.start()
    logged_on(gw)
    # Refused while the session is up.
    assert not gw.skip_sequence(10)['ok']
    session = gw._sessions['Order Routing']
    gw.stop()
    out_before = session.state.out_seq          # the Logout took a number too
    for p in stellar.peers:
        p.closed = True
    result = gw.skip_sequence(10)
    assert result['ok'] and result['next_out'] == out_before + 11
    gw = FixGateway(stellar_venue(), manual_path=db)        # the program restarted
    gw.start()
    logged_on(gw)
    session = gw._sessions['Order Routing']
    wait(lambda: stellar.gap_fills, 5, 'the GapFill')
    comp, gap, last = stellar.resend_requests_sent[-1]
    assert comp == 'STELLAR_UAT' and gap == out_before + 1
    filled_from, new_seq = stellar.gap_fills[-1][1], stellar.gap_fills[-1][2]
    assert filled_from == gap and new_seq > last
    assert session.state.status == 'CONNECTED'
    gw.stop()


def test_without_a_skip_stellar_asks_for_nothing(tmp_path, monkeypatch):
    """The control: numbers carried across a restart leave no gap."""
    stellar = Exchange()
    install(monkeypatch, stellar)
    db = str(tmp_path / 'm.db')
    gw = FixGateway(stellar_venue(), manual_path=db)
    gw.start()
    logged_on(gw)
    gw.stop()
    for p in stellar.peers:
        p.closed = True
    gw = FixGateway(stellar_venue(), manual_path=db)
    gw.start()
    logged_on(gw)
    time.sleep(0.5)
    assert not stellar.resend_requests_sent and not stellar.gap_fills
    gw.stop()
