"""What the Order Router missed, recovered from Stellar.

Stellar has no recovery service of its own. Two things stand in for it:

- The Order Router KEEPS its sequence numbers across restarts and Stellar
  honours a ResendRequest: a session that comes back on the numbers it left
  at is sent what it missed — the fills that happened while the line was
  down. Market Data, which Stellar does not resend, resets on every logon.
- After each logon, every order of ours still recorded as working is asked
  about with an Order Status Request (H): the order status sweep.

A fill the program never received is otherwise simply missing: the screen
says flat while Stellar holds the position. Recovered, it is applied once —
a fill already booked is never booked again, and an order is never moved
back from a final state.
"""
import time

import pytest

from fixtrader import gateway as gw_mod
from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway
from fixtrader.models import SessionState
from tests.fake_stellar import Exchange, install
from tests.test_uat_orders import open_desk


def wait(test, timeout=15.0, what='it'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


@pytest.fixture
def rdesk(tmp_path, monkeypatch):
    with open_desk(tmp_path, monkeypatch) as d:
        yield d


def recovery(d):
    return (d.d.snapshot().get('engine') or {}).get('fix_recovery') or {}


def done(d):
    return wait(lambda: recovery(d).get('state') in ('DONE', 'REFUSED', 'FAILED')
                and recovery(d), what='the order status sweep to finish')


def reveal_the_gap(d):
    """Any message Stellar sends next shows the numbers it skipped."""
    d.tt.or_peer().reply([('35', '0')])


# -- the sweep, after each logon -------------------------------------------------------

def test_after_the_logon_our_working_orders_are_asked_about(rdesk):
    r = done(rdesk)
    assert r['state'] == 'DONE' and r['mode'] == 'ORDER_STATUS', r
    assert not rdesk.tt.status_requests              # nothing of ours was working


def test_a_resting_order_is_asked_about_by_its_stellar_order_id(rdesk):
    done(rdesk)
    run = rdesk.runner()
    bid, ask, tick, dec = run.book()
    oid = run.manual('BUY', 'LIMIT', run.px(bid - 5 * tick, tick, dec))
    order = run.manual_status(oid, ('NEW',))
    assert rdesk.d.command('recover_missed', '', {}).get('ok')
    r = wait(lambda: recovery(rdesk).get('state') == 'DONE'
             and recovery(rdesk).get('replayed') == 1 and recovery(rdesk), what='the sweep')
    assert 'answered for 1 order' in r['text']
    asked = rdesk.tt.status_requests[-1]
    assert asked['37'] == order['venue_order_id'] and asked['11'] == oid and asked['790']
    assert run.manual_order(oid)['status'] == 'NEW'                 # still working
    run.manual_cancel(oid)


def test_stellar_saying_nothing_is_a_failure_not_a_success(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, 'RECOVERY_TIMEOUT_SEC', 1.0)
    with open_desk(tmp_path, monkeypatch) as d:
        done(d)
        run = d.runner()
        bid, ask, tick, dec = run.book()
        oid = run.manual('BUY', 'LIMIT', run.px(bid - 5 * tick, tick, dec))
        run.manual_status(oid, ('NEW',))
        d.tt.status_silent = True
        assert d.d.command('recover_missed', '', {}).get('ok')
        r = wait(lambda: recovery(d).get('state') == 'FAILED' and recovery(d),
                 what='the failure')
        assert 'did not finish' in r['text'] and oid in r['text']
        run.manual_cancel(oid)


# -- what the line missed comes back by the sequence numbers -----------------------------

def test_a_manual_fill_missed_on_the_line_is_resent_and_booked_once(rdesk):
    done(rdesk)
    run = rdesk.runner()
    rdesk.tt.drop_reports = 2                       # the ack and the fill never arrive
    oid = run.manual('BUY', 'MARKET')
    time.sleep(0.5)
    # The control: without the resend the fill is simply missing.
    assert (run.manual_order(oid) or {}).get('status') != 'FILLED'
    assert not [f for f in run.terminal().get('fills') or [] if f.get('order_id') == oid]
    reveal_the_gap(rdesk)
    run.manual_status(oid, ('FILLED',))
    fills = [f for f in run.terminal().get('fills') or [] if f.get('order_id') == oid]
    assert len(fills) == 1 and fills[0]['price'] == pytest.approx(90.52)
    # The same trade sent again — a new message, the same ExecID: one fill.
    session = rdesk.tt.or_sessions['STELLAR_UAT']
    trade = next(f for f in reversed(list(session['sent'].values()))
                 if dict(f).get('150') == 'F')
    rdesk.tt.deliver(trade)
    time.sleep(0.5)
    assert run.manual_order(oid)['status'] == 'FILLED'
    assert run.manual_order(oid)['filled_qty'] == pytest.approx(1)
    assert len([f for f in run.terminal().get('fills') or [] if f.get('order_id') == oid]) == 1


def test_an_algo_fill_missed_on_the_line_is_resent_onto_the_book_once(rdesk):
    done(rdesk)
    assert rdesk.d.command('execution', '', {'mode': 'LIVE', 'confirm': True})['ok']
    run = rdesk.runner()
    rdesk.tt.drop_reports = 2
    run.algo_open('BUY', 'MARKET')
    time.sleep(0.5)
    assert run.algo_position() is None              # the control: not known
    reveal_the_gap(rdesk)
    pos = wait(lambda: run.algo_position(), what='the resent position')
    assert pos['qty'] == pytest.approx(1)
    session = rdesk.tt.or_sessions['STELLAR_UAT']
    trade = next(f for f in reversed(list(session['sent'].values()))
                 if dict(f).get('150') == 'F')
    rdesk.tt.deliver(trade)                          # sent again: not a second fill
    time.sleep(0.5)
    assert run.algo_position()['qty'] == pytest.approx(1)        # not 2
    assert rdesk.d.command('close_now', 'clz6').get('ok')
    run.algo_flat('flat again')


def stellar_venue(**extra):
    return VenueConfig('STELLAR-UAT', environment='UAT', host='or.example', port=9001,
                       fix_version='FIX.4.4', use_tls=False, sender_comp_id='STELLAR_UAT',
                       target_comp_id='STELLAR', md_host='md.example', md_port=9002,
                       md_sender_comp_id='STELLAR_UAT_MD', username='USER',
                       account='ACC1', **extra)


def logged_on(gw):
    wait(lambda: gw.state() == SessionState.LOGGED_ON, 5, 'the logon')


def logons(stellar, comp):
    return [p for p in stellar.peers if p.comp == comp]


def test_the_order_routers_numbers_survive_a_restart_and_what_was_missed_is_resent(
        tmp_path, monkeypatch):
    stellar = Exchange()
    install(monkeypatch, stellar)
    db = str(tmp_path / 'manual.db')
    gw = FixGateway(stellar_venue(), manual_path=db)
    gw.start()
    logged_on(gw)
    out_before = gw._sessions['Order Routing'].state.out_seq
    in_before = gw._sessions['Order Routing'].state.in_seq
    gw.stop()
    # While the program is down, Stellar numbers two reports for it.
    for p in stellar.peers:
        p.closed = True
    for n in (1, 2):
        stellar.or_record('STELLAR_UAT', [('35', '8'), ('11', f'FT-x-{n}'), ('37', str(n)),
                                          ('17', f'ORD:{n}'), ('150', '0'), ('39', '0')])
    again = FixGateway(stellar_venue(), manual_path=db)
    again.start()
    logged_on(again)
    session = again._sessions['Order Routing']
    wait(lambda: session.state.in_seq >= in_before + 3, 5, 'the resend')
    first = logons(stellar, 'STELLAR_UAT')[-1]
    # It came back on its own numbers — neither reset (141) nor from 1 …
    assert session.state.out_seq > out_before
    # … and Stellar was asked for exactly what it had sent while we were down.
    assert session.state.in_seq == stellar.or_sessions['STELLAR_UAT']['seq']
    assert not session.pending and session.ready()
    assert first.kind == 'OR'
    again.stop()


def test_reset_on_every_logon_is_the_venues_choice_and_says_so_on_the_wire(
        tmp_path, monkeypatch):
    """The control: a venue set to reset sends 141=Y and starts from 1."""
    stellar = Exchange()
    sent = []
    install(monkeypatch, stellar)
    real = gw_mod.NativeFixSession.send

    def spy(self, msg_type, fields):
        if msg_type == 'A':
            sent.append((self.session_name, dict(fields)))
        return real(self, msg_type, fields)
    monkeypatch.setattr(gw_mod.NativeFixSession, 'send', spy)
    gw = FixGateway(stellar_venue(reset_seq_on_logon=True), manual_path=str(tmp_path / 'm.db'))
    gw.start()
    logged_on(gw)
    gw.stop()
    by_session = dict(sent)
    assert by_session['Order Routing'].get('141') == 'Y'
    assert by_session['Market Data'].get('141') == 'Y'        # Market Data always resets


def test_market_data_resets_while_the_order_router_keeps_its_numbers(tmp_path, monkeypatch):
    stellar = Exchange()
    sent = []
    install(monkeypatch, stellar)
    real = gw_mod.NativeFixSession.send

    def spy(self, msg_type, fields):
        if msg_type == 'A':
            sent.append((self.session_name, dict(fields)))
        return real(self, msg_type, fields)
    monkeypatch.setattr(gw_mod.NativeFixSession, 'send', spy)
    gw = FixGateway(stellar_venue(), manual_path=str(tmp_path / 'm.db'))
    gw.start()
    logged_on(gw)
    gw.stop()
    by_session = dict(sent)
    assert by_session['Market Data'].get('141') == 'Y'
    assert '141' not in by_session['Order Routing']


def test_without_a_password_the_logon_carries_none(tmp_path, monkeypatch):
    """Stellar's Logon lists 98 and 108 only: a session authorised by its
    address sends no credentials; one with a password in .env sends 553/554."""
    stellar = Exchange()
    install(monkeypatch, stellar)
    logons_sent = []
    real = gw_mod.NativeFixSession.send

    def spy(self, msg_type, fields):
        if msg_type == 'A':
            logons_sent.append(dict(fields))
        return real(self, msg_type, fields)
    monkeypatch.setattr(gw_mod.NativeFixSession, 'send', spy)
    monkeypatch.delenv('STELLAR_UAT_PASSWORD', raising=False)
    gw = FixGateway(stellar_venue(password_env='NO_SUCH_PW_VAR'), manual_path=str(tmp_path / 'a.db'))
    gw.start()
    logged_on(gw)
    gw.stop()
    assert logons_sent and all('554' not in f and '553' not in f for f in logons_sent)
    monkeypatch.setenv('STELLAR_TEST_PW', 's3cret')
    logons_sent.clear()
    gw = FixGateway(stellar_venue(password_env='STELLAR_TEST_PW'), manual_path=str(tmp_path / 'b.db'))
    gw.start()
    logged_on(gw)
    gw.stop()
    order_logon = logons_sent[0] if logons_sent[0].get('554') else logons_sent[1]
    assert order_logon['553'] == 'USER' and order_logon['554'] == 's3cret'


# -- entries wait while Stellar answers ------------------------------------------------

def test_algo_entries_wait_while_stellar_answers_for_our_orders(tmp_path):
    """A position the answers are about to reveal must not be entered beside;
    exits are never held (health holds ENTRIES only)."""
    from tests.test_engine_signal import build
    engine, gw, _ = build(tmp_path)
    gw.recovery_running = lambda: True
    engine.poll(now=gw.now)
    assert 'Stellar is answering' in (engine.runtimes['fef'].algo.body.get('health') or '')


def test_with_no_sweep_running_entries_are_not_held_for_it(tmp_path):
    """The control."""
    from tests.test_engine_signal import build
    engine, gw, _ = build(tmp_path)
    gw.recovery_running = lambda: False
    engine.poll(now=gw.now)
    assert 'Stellar is answering' not in (engine.runtimes['fef'].algo.body.get('health') or '')
