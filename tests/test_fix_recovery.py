"""TT FIX Recovery for the Order Routing session.

TT replays what a FIX client missed over a separate, non-persistent
connection on the SAME login: log on, wait for "Recovery is complete", send
ONE Recovery Request (U2) — 18002=Y for the messages TT has not delivered
since its weekly reset, or StartDate/EndDate (916/917) for a window — and
TT sends the Execution Reports (8) and Cancel Rejects (9), then logs out.

A fill the program never received (the line was down, the PC restarting)
is otherwise simply missing: the screen says flat while TT holds the
position. Recovered, it is applied once — a fill already booked is never
booked again, and an order is never moved back from a final state.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fixtrader import gateway as gw_mod
from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway, last_session_reset
from tests.test_uat_orders import open_desk


def wait(test, timeout=15.0, what='it'):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


@pytest.fixture
def rdesk(tmp_path, monkeypatch):
    with open_desk(tmp_path, monkeypatch, recovery_host='rec.example',
                   recovery_port=11508) as d:
        yield d


def recovery(d):
    return (d.d.snapshot().get('engine') or {}).get('fix_recovery') or {}


def done(d):
    return wait(lambda: recovery(d).get('state') in ('DONE', 'REFUSED', 'FAILED', 'OFF')
                and recovery(d), what='the FIX Recovery to finish')


# -- where it is, and which request ------------------------------------------------

def test_the_recovery_address_is_beside_order_routing():
    v = VenueConfig('TT', environment='UAT', host='fixorderrouting-ext-uat-cert.trade.tt',
                    port=11502)
    assert v.recovery_endpoint() == ('fixrecovery-ext-uat-cert.trade.tt', 11508)
    v = VenueConfig('TT', environment='PROD', host='127.0.0.1', port=11702)
    assert v.recovery_endpoint() == ('127.0.0.1', 11708)          # through stunnel
    v = VenueConfig('TT', environment='UAT', host='or.example', port=11502)
    assert v.recovery_endpoint() == (None, None)                 # said, never guessed
    v = VenueConfig('TT', environment='UAT', host='or.example', port=11502,
                    recovery_host='rec.example', recovery_port=12000)
    assert v.recovery_endpoint() == ('rec.example', 12000)       # the operator's wins


def test_tts_weekly_reset_is_saturday_22_utc():
    sat = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc)      # a Saturday
    assert last_session_reset(sat) == datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)
    before = datetime(2026, 10, 10, 21, 0, tzinfo=timezone.utc)
    assert last_session_reset(before) == datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
    wed = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    assert last_session_reset(wed) == datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A gateway that captures the Recovery Request instead of connecting."""
    sent = []

    class Capture:
        def __init__(self, svc, state, cfg, request, on_done):
            sent.append(SimpleNamespace(cfg=cfg, request=dict(request)))

        def start(self):
            pass

        def is_running(self):
            return False

    monkeypatch.setattr(gw_mod, 'FixRecoverySession', Capture)
    monkeypatch.setenv('RECOVERY_TEST_PW', 'pw')
    venue = VenueConfig('TT', environment='UAT', host='fixorderrouting-ext-uat-cert.trade.tt',
                        port=11502, fix_version='FIX.4.2', sender_comp_id='ME',
                        target_comp_id='TT', password_env='RECOVERY_TEST_PW')
    g = FixGateway(venue, manual_path=str(tmp_path / 'm.db'))
    g._order_cfg = {'host': venue.host, 'port': 11502, 'sender_comp_id': 'ME',
                    'target_comp_id': 'TT', 'password': 'pw'}
    return g, sent


def heard(g, when):
    g.terminal._save('session', 'order_routing', {'last_heard': when.isoformat()})


def test_heard_since_the_reset_asks_tt_for_what_it_did_not_deliver(bare):
    g, sent = bare
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    heard(g, now - timedelta(hours=2))
    assert g.recover_missed(now=now)['ok']
    assert sent[-1].request == {'18002': 'Y'}                    # never with 916/917
    assert (sent[-1].cfg['host'], sent[-1].cfg['port']) == ('fixrecovery-ext-uat-cert.trade.tt', 11508)
    assert sent[-1].cfg['sender_comp_id'] == 'ME'                # the SAME login
    assert g.recovery['mode'] == 'RECONCILE'


def test_off_since_before_the_reset_asks_for_a_window_from_then(bare):
    g, sent = bare
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    heard(g, datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc))   # before Sat's reset
    g.recover_missed(now=now)
    assert sent[-1].request == {'916': '20261009-15:29:00', '917': '20261014-08:59:59'}
    assert '18002' not in sent[-1].request
    assert g.recovery['mode'] == 'WINDOW' and g.recovery['note'] is None


def test_off_for_longer_than_tt_keeps_is_said(bare):
    g, sent = bare
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    heard(g, now - timedelta(days=40))
    g.recover_missed(now=now)
    start = datetime.strptime(sent[-1].request['916'], '%Y%m%d-%H:%M:%S').replace(tzinfo=timezone.utc)
    assert now - start < timedelta(hours=720)                   # never a rejected start
    assert '720 hours' in g.recovery['note'] and 'statement' in g.recovery['note']


def test_an_unknown_recovery_address_is_said(tmp_path, monkeypatch):
    venue = VenueConfig('TT', environment='UAT', host='or.example', port=11502)
    g = FixGateway(venue, manual_path=str(tmp_path / 'm.db'))
    g._order_cfg = {}
    out = g.recover_missed()
    assert not out['ok'] and 'Exchanges page' in out['error']
    assert g.recovery['state'] == 'OFF'


# -- against the fake TT, through the real sessions ----------------------------------

def test_after_the_logon_tt_is_asked_for_what_was_missed(rdesk):
    r = done(rdesk)
    assert r['state'] == 'DONE' and r['mode'] == 'RECONCILE', r
    assert rdesk.tt.recovery_requests and rdesk.tt.recovery_requests[0].get('18002') == 'Y'
    assert '916' not in rdesk.tt.recovery_requests[0]


def test_a_manual_fill_missed_on_the_line_is_recovered_and_booked_once(rdesk):
    done(rdesk)
    run = rdesk.runner()
    rdesk.tt.drop_reports = 2                       # the ack and the fill never arrive
    oid = run.manual('BUY', 'MARKET')
    import time
    time.sleep(0.5)
    # The control: without Recovery the fill is simply missing.
    assert (run.manual_order(oid) or {}).get('status') != 'FILLED'
    assert not [f for f in run.terminal().get('fills') or [] if f.get('order_id') == oid]

    assert rdesk.d.command('recover_missed', '', {}).get('ok')
    wait(lambda: recovery(rdesk).get('state') == 'DONE' and recovery(rdesk).get('replayed') == 2,
         what='two reports replayed')
    run.manual_status(oid, ('FILLED',))
    fills = [f for f in run.terminal().get('fills') or [] if f.get('order_id') == oid]
    assert len(fills) == 1 and fills[0]['price'] == pytest.approx(90.52)

    # A window replays EVERYTHING — the ack after the fill included: still
    # one fill, still FILLED (never moved back to NEW). Its end is a second
    # before now (TT refuses a future one), so ask once that second is past.
    time.sleep(1.5)
    assert rdesk.d.command('recover_missed', '', {'window': True}).get('ok')
    wait(lambda: recovery(rdesk).get('state') == 'DONE'
         and recovery(rdesk).get('mode') == 'WINDOW', what='the window replay')
    assert recovery(rdesk)['replayed'] >= 2
    assert run.manual_order(oid)['status'] == 'FILLED'
    assert len([f for f in run.terminal().get('fills') or [] if f.get('order_id') == oid]) == 1


def test_an_algo_fill_missed_on_the_line_is_recovered_onto_the_book_once(rdesk):
    done(rdesk)
    assert rdesk.d.command('execution', '', {'mode': 'LIVE', 'confirm': True})['ok']
    run = rdesk.runner()
    rdesk.tt.drop_reports = 2
    run.algo_open('BUY', 'MARKET')
    import time
    time.sleep(0.5)
    assert run.algo_position() is None              # the control: not known
    assert rdesk.d.command('recover_missed', '', {}).get('ok')
    pos = wait(lambda: run.algo_position(), what='the recovered position')
    assert pos['qty'] == pytest.approx(1)
    time.sleep(1.5)                                 # the window ends a second ago
    assert rdesk.d.command('recover_missed', '', {'window': True}).get('ok')
    wait(lambda: recovery(rdesk).get('state') == 'DONE'
         and recovery(rdesk).get('mode') == 'WINDOW', what='the window replay')
    assert recovery(rdesk)['replayed'] >= 2                      # the fill came again
    time.sleep(0.5)
    assert run.algo_position()['qty'] == pytest.approx(1)        # not 2
    assert rdesk.d.command('close_now', 'clz6').get('ok')
    run.algo_flat('flat again')


def test_a_refusal_is_said_in_tts_words(tmp_path, monkeypatch):
    with open_desk(tmp_path, monkeypatch, recovery_host='rec.example',
                   recovery_port=11508) as d:
        done(d)
        d.tt.recovery_reject = 'Start date is earlier than permissible limit of 720 hours'
        assert d.d.command('recover_missed', '', {}).get('ok')
        r = wait(lambda: recovery(d).get('state') == 'REFUSED' and recovery(d),
                 what='the refusal')
        assert 'permissible limit of 720 hours' in r['text']


def test_tt_saying_nothing_is_a_failure_not_a_success(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, 'RECOVERY_TIMEOUT_SEC', 1.0)
    with open_desk(tmp_path, monkeypatch, recovery_host='rec.example',
                   recovery_port=11508) as d:
        done(d)
        d.tt.recovery_silent = True
        assert d.d.command('recover_missed', '', {}).get('ok')
        r = wait(lambda: recovery(d).get('state') == 'FAILED' and recovery(d),
                 what='the failure')
        assert 'did not finish' in r['text']


def test_without_a_recovery_address_nothing_is_attempted_and_it_says_so(tmp_path, monkeypatch):
    """The control for the whole service: no address, no connection."""
    with open_desk(tmp_path, monkeypatch) as d:
        r = done(d)
        assert r['state'] == 'OFF' and 'Exchanges page' in r['text']
        assert not d.tt.recovery_requests


# -- entries wait while TT replays ----------------------------------------------------

def test_algo_entries_wait_while_tt_replays_what_was_missed(tmp_path):
    """A position the replay is about to reveal must not be entered beside;
    exits are never held (health holds ENTRIES only)."""
    from tests.test_engine_signal import build
    engine, gw, _ = build(tmp_path)
    gw.recovery_running = lambda: True
    engine.poll(now=gw.now)
    assert 'TT FIX Recovery' in (engine.runtimes['fef'].algo.body.get('health') or '')


def test_with_no_replay_running_entries_are_not_held_for_it(tmp_path):
    """The control."""
    from tests.test_engine_signal import build
    engine, gw, _ = build(tmp_path)
    gw.recovery_running = lambda: False
    engine.poll(now=gw.now)
    assert 'TT FIX Recovery' not in (engine.runtimes['fef'].algo.body.get('health') or '')


def test_the_logon_itself_does_not_hide_a_week_off(bare):
    """What was heard BEFORE this logon decides: the logon is heard at once,
    and read after it every gap would look like seconds."""
    g, sent = bare
    now = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    heard(g, datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc))
    g._heard_before_logon = g.last_heard()            # what start() records
    g._last_heard_saved = 0.0
    g.heard(now)                                      # the logon arrives
    g.recover_missed(now=now)
    assert '916' in sent[-1].request and g.recovery['mode'] == 'WINDOW'
