"""The desk ladder's order book: every level TT publishes, or only its top.

Depth is asked for with the Market Data Request's MarketDepth (264=0 full,
264=1 top), read from TT's own incremental updates, and handed to the ladder
best first. Where there is no book to read it is None — unknown, never an
empty book."""
from types import SimpleNamespace

from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway
from tests.test_manual_terminal import terminal  # noqa: F401
from tests.test_trading_mode import desk


def gateway_with_book(tmp_path):
    venue = VenueConfig(name='test', environment='UAT', host='unused', port=1,
                        fix_version='FIX.4.2', sender_comp_id='OR', target_comp_id='TT',
                        password_env='TEST_OR', md_host='unused', md_port=2,
                        md_sender_comp_id='MD', md_password_env='TEST_MD')
    gateway = FixGateway(venue, manual_path=str(tmp_path / 'manual.db'))
    session = SimpleNamespace(state=SimpleNamespace(status='CONNECTED'),
                              is_running=lambda: True, send=lambda *args: None)
    gateway._sessions['Market Data'] = session
    gateway.subscribe(SimpleNamespace(key='gc', name='GC', symbol='GC',
                                      security_id='123', security_exchange='CME'))
    request = gateway.terminal.subscriptions['123']
    # A full book: three bids and two offers, positional (290) as TT sends them.
    raw = ('35=W\x0134=7\x01262=' + request + '\x01268=5\x01'
           '269=0\x01270=10.0\x01271=5\x01290=1\x01'
           '269=0\x01270=9.9\x01271=7\x01290=2\x01'
           '269=0\x01270=9.8\x01271=2\x01290=3\x01'
           '269=1\x01270=10.1\x01271=4\x01290=1\x01'
           '269=1\x01270=10.2\x01271=9\x01290=2\x01')
    gateway.terminal.on_message('Market Data', {'35': 'W', '34': '7'}, raw)
    return gateway, session


def test_the_ladder_is_given_every_level_best_first(tmp_path):
    gateway, _ = gateway_with_book(tmp_path)
    book = gateway.depth('gc')
    assert [(x['price'], x['size']) for x in book['bids']] == [(10.0, 5), (9.9, 7), (9.8, 2)]
    assert [(x['price'], x['size']) for x in book['asks']] == [(10.1, 4), (10.2, 9)]
    assert book['full'] is False                         # only asked for the top


def test_no_market_data_is_no_book_not_an_empty_one(tmp_path):
    gateway, session = gateway_with_book(tmp_path)
    session.state.status = 'DISCONNECTED'
    assert gateway.depth('gc') is None
    assert gateway.depth('not-a-contract') is None


def md_requests(terminal):
    return [dict(f) for kind, f in terminal.gateway._sessions['Market Data'].sent
            if kind == 'V']


def test_the_depth_button_asks_tt_for_the_full_book_and_back(tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    assert engine.set_depth('fef', True)['ok']
    request = md_requests(terminal)[-1]
    assert request['264'] == '0' and request['263'] == '1'
    assert terminal.watch['101']['full_depth'] is True
    assert engine.set_depth('fef', False)['ok']
    assert md_requests(terminal)[-1]['264'] == '1'
    assert terminal.watch['101']['full_depth'] is False


def test_depth_needs_a_tt_book(tmp_path, terminal):
    """The simulator has no TT book: refused in words, never pretended."""
    engine, gw = desk(tmp_path, terminal)
    gw.terminal = None
    del gw.terminal
    out = engine.set_depth('fef', True)
    assert out['ok'] is False and 'simulator' in out['error']
