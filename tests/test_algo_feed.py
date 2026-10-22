import pytest
from datetime import datetime, timezone
from types import SimpleNamespace

from fixtrader.algo_feed import AlgoDataFeed, MarketDataEvent
from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway
from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.engine import Engine


def event(key, sequence):
    return MarketDataEvent(key, key, key, 10.0, 11.0, 10.5, 2.0, 3.0,
                           datetime.now(timezone.utc), sequence)


def test_feed_coalesces_and_deduplicates_without_blocking():
    feed = AlgoDataFeed(capacity=2)
    assert feed.publish(event('a', '1'))
    assert not feed.publish(event('a', '1'))
    assert feed.publish(event('a', '2'))
    assert feed.publish(event('b', '1'))
    assert feed.publish(event('c', '1'))
    assert [item.contract_key for item in feed.drain()] == ['b', 'c']
    assert feed.dropped == 2
    assert feed.drain() == []
    feed.clear()
    assert feed.publish(event('a', '1'))


def test_fix_book_enters_feed_and_disconnect_blocks_stale_book(tmp_path):
    venue = VenueConfig(name='test', environment='UAT', host='unused', port=1,
                        fix_version='FIX.4.2', sender_comp_id='OR', target_comp_id='TT',
                        password_env='TEST_OR', md_host='unused', md_port=2,
                        md_sender_comp_id='MD', md_password_env='TEST_MD')
    gateway = FixGateway(venue, manual_path=str(tmp_path / 'manual.db'))
    session = SimpleNamespace(state=SimpleNamespace(status='CONNECTED'),
                              is_running=lambda: True, send=lambda *args: None)
    gateway._sessions['Market Data'] = session
    gateway.subscribe(SimpleNamespace(key='spread', name='Spread', symbol='CL-BZ',
                                      security_id='123', security_exchange='CME'))
    gateway.terminal.watch['123']['display_factor'] = '1'      # TT defined it, no factor
    request = gateway.terminal.subscriptions['123']
    raw = ('35=W\x0134=7\x01262=' + request + '\x01268=3\x01269=0\x01270=10\x01'
           '269=1\x01270=11\x01269=2\x01270=10.5\x01')
    gateway.terminal.on_message('Market Data', {'35': 'W', '34': '7'}, raw)
    updates = gateway.drain_market_data()
    assert len(updates) == 1
    assert (updates[0].contract_key, updates[0].security_id, updates[0].symbol) == ('spread', '123', 'CL-BZ')
    assert (updates[0].bid, updates[0].ask, updates[0].last) == (10.0, 11.0, 10.5)
    assert gateway.top_of_book('spread').mid == 10.5
    session.state.status = 'DISCONNECTED'
    assert gateway.top_of_book('spread') is None


def test_security_id_subscription_does_not_require_exchange(tmp_path):
    venue = VenueConfig(name='test', environment='UAT', host='unused', port=1,
                        fix_version='FIX.4.2', sender_comp_id='OR', target_comp_id='TT',
                        password_env='TEST_OR', md_host='unused', md_port=2,
                        md_sender_comp_id='MD', md_password_env='TEST_MD')
    gateway = FixGateway(venue, manual_path=str(tmp_path / 'manual.db'))
    session = SimpleNamespace(state=SimpleNamespace(status='CONNECTED'),
                              is_running=lambda: True, send=lambda *args: None)
    gateway._sessions['Market Data'] = session
    gateway.subscribe(SimpleNamespace(key='spread', name='Spread', symbol='CL-BZ',
                                      security_id='123', security_exchange=''))
    assert '123' in gateway.terminal.subscriptions


def test_a_live_venue_starts_on_PAPER_and_LIVE_needs_arming(tmp_path):
    """A restart never comes back sending real orders: the Algo trades on
    PAPER until a person arms LIVE — which needs the venue logged on."""
    venue = VenueConfig(name='test', environment='UAT', host='unused', port=1,
                        fix_version='FIX.4.2', sender_comp_id='OR', target_comp_id='TT',
                        password_env='TEST_OR')
    gateway = FixGateway(venue, manual_path=str(tmp_path / 'manual.db'))
    config = TraderConfig(path=str(tmp_path / 'config.json'))
    config.contracts['x'] = ContractConfig(key='x', symbol='X', venue='test')
    engine = Engine(config, gateway)
    assert engine.auto_trade_enabled is False
    assert engine.paper is True and engine.live_armed is False
    result = engine.set_auto_trade(True)
    assert result['ok'] is True and result['paper'] is True
    refused = engine.set_execution('LIVE', confirm=True)
    assert refused['ok'] is False and 'not logged on' in refused['error']
    assert engine.paper is True
