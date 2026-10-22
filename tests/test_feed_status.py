"""Why a contract has no prices — said in words a trader can act on.

"The data feed is not coming for every contract" has three different
causes with three different fixes: TT REFUSED the price request (market-
data permission on the account — Orient / TT), TT answered but NOBODY is
quoting it (a quiet UAT market), or TT never ANSWERED (a Security ID it
does not know). The screen must say which."""
import time

from tests.test_price_units import terminal  # noqa: F401  (the fixture)


def status(terminal, key='CL1'):
    return terminal.feed_status(key)


def test_prices_arriving_is_live(terminal):
    request = terminal.subscriptions['CL1']
    terminal.on_message('Market Data', {'35': 'W'},
                        f'35=W\x01262={request}\x01268=2\x01269=0\x01270=9050\x01271=3\x01'
                        f'269=1\x01270=9052\x01271=4\x01')
    assert status(terminal)['state'] == 'LIVE'


def test_a_refusal_is_said_with_tts_reason_and_who_to_ask(terminal):
    request = terminal.subscriptions['CL1']
    terminal.on_message('Market Data', {'35': 'Y', '262': request, '281': '3',
                                        '58': 'Not entitled'}, '')
    s = status(terminal)
    assert s['state'] == 'REFUSED'
    assert 'no market-data permission' in s['text'] and 'Not entitled' in s['text']
    assert 'Orient' in s['text']
    other = status(terminal, 'GC1')                 # the control: GC untouched
    assert other['state'] != 'REFUSED'


def test_a_session_reject_of_a_price_request_reaches_the_unanswered_books(terminal):
    terminal.on_message('Market Data', {'35': '3', '45': '7', '372': 'V',
                                        '58': 'Invalid SecurityID'}, '')
    assert status(terminal)['state'] == 'REFUSED'


def test_an_answer_with_no_quotes_is_a_quiet_market_not_a_fault(terminal):
    request = terminal.subscriptions['CL1']
    terminal.on_message('Market Data', {'35': 'W'},
                        f'35=W\x01262={request}\x0148=CL1\x01268=0\x01')
    s = status(terminal)
    assert s['state'] == 'EMPTY' and 'nobody is quoting' in s['text']


def test_no_answer_is_said_after_a_while_and_not_before(terminal):
    assert status(terminal)['state'] == 'WAITING'   # the control: just asked
    terminal.books['CL1']['requested_at'] = time.time() - 60
    s = status(terminal)
    assert s['state'] == 'NO_ANSWER' and 'Security ID CL1' in s['text']


def test_the_watchlist_carries_it(terminal):
    rows = {r['instrument']['security_id']: r['quote'] for r in terminal.snapshot()['watchlist']}
    assert rows['CL1']['feed_status']['state'] in ('WAITING', 'NO_ANSWER')


def test_a_contract_added_before_tt_is_ready_waits_and_is_subscribed_after(tmp_path):
    """Found by a test under load: subscribing a contract after the Market
    Data logon but BEFORE TT's "Recovery is complete" raised, instead of
    waiting — the terminal's poll subscribes it once the session is ready."""
    from types import SimpleNamespace
    from fixtrader.config import VenueConfig
    from fixtrader.gateway import FixGateway
    gw = FixGateway(VenueConfig('TT', environment='UAT'), manual_path=str(tmp_path / 'm.db'))
    sent = []
    gw.terminal._subscribe = lambda key, action='1': sent.append(key)
    ready = {'now': False}
    gw._sessions['Market Data'] = SimpleNamespace(
        state=SimpleNamespace(status='CONNECTED'), is_running=lambda: True,
        ready=lambda: ready['now'], cfg={})
    contract = SimpleNamespace(key='gc', name='GC', symbol='GC', security_id='GC1',
                               security_exchange='CME', tick_size=0.1, tick_value=10.0)
    gw.subscribe(contract)                    # not ready: no error, nothing sent
    assert sent == []
    ready['now'] = True
    gw.subscribe(contract)                    # the control: ready, it goes at once
    assert sent == ['GC1']
