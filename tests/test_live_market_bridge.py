from types import SimpleNamespace

from fixtrader.gateway import FixGateway
from fixtrader.config import VenueConfig


class MarketSession:
    def __init__(self):
        self.state = SimpleNamespace(status='CONNECTED')
        self.sent = []

    def is_running(self):
        return True

    def send(self, message_type, fields):
        self.sent.append((message_type, fields))


def venue():
    return VenueConfig(name='TT-UAT', environment='UAT', host='or.example',
                       port=11502, fix_version='FIX.4.2',
                       sender_comp_id='ORDER', target_comp_id='TT',
                       password_env='TEST_OR', md_host='md.example',
                       md_port=11503, md_sender_comp_id='MARKET',
                       md_password_env='TEST_MD')


def test_configured_contract_is_subscribed_with_tt_identity_and_book_is_live(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    session = MarketSession()
    gateway._sessions['Market Data'] = session
    contract = SimpleNamespace(key='spread-key', name='Spread',
                               symbol='LEG1-LEG2', security_id='98765',
                               security_exchange='CME')

    gateway.subscribe(contract)
    # TT's definition has arrived and names no factor: its prices are the
    # trader's (until it does, no price is shown).
    gateway.terminal.watch['98765']['display_factor'] = '1'

    assert len(session.sent) == 1
    message_type, fields = session.sent[0]
    assert message_type == 'V'
    field_map = dict(fields)
    assert field_map['55'] == 'LEG1-LEG2'
    assert field_map['48'] == '98765'
    assert field_map['207'] == 'CME'

    raw = ('35=W\x01262=%s\x01268=2\x01269=0\x01270=99.5\x01271=4'
           '\x01269=1\x01270=100.5\x01271=6\x01') % field_map['262']
    gateway.terminal.on_message('Market Data', {'35': 'W'}, raw)
    book = gateway.top_of_book('spread-key')
    assert book is not None
    assert (book.bid, book.ask, book.mid) == (99.5, 100.5, 100.0)


def test_live_bridge_does_not_fabricate_a_quote_for_unseen_contract(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    assert gateway.top_of_book('never-seen') is None


def test_a_one_sided_tt_book_reaches_the_window_but_is_not_a_usable_book(tmp_path):
    """TT quoting only an offer is what TT is publishing. Returning nothing
    left a blank window that read as "not subscribed"; returning the side
    that exists shows it — and BookTop.usable stays False, so there is no
    mid, no statistic and no entry off half a book."""
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    session = MarketSession()
    gateway._sessions['Market Data'] = session
    contract = SimpleNamespace(key='cl_bz', name='CL|BZ', symbol='CL|BZ',
                               security_id='555', security_exchange='CME')
    gateway.subscribe(contract)
    gateway.terminal.watch['555']['display_factor'] = '1'      # TT defined it, no factor
    request = dict(session.sent[0][1])['262']
    raw = f'35=W\x01262={request}\x01268=1\x01269=1\x01270=307\x01271=12\x01'
    gateway.terminal.on_message('Market Data', {'35': 'W'}, raw)

    book = gateway.top_of_book('cl_bz')
    assert book is not None
    assert (book.bid, book.ask) == (None, 307)
    assert book.usable is False and book.mid is None


def test_putting_a_contract_on_the_algo_desk_keeps_tts_own_definition(tmp_path):
    """subscribe() used to replace TT's definition with a stub, so the
    ladder for every algo-desk contract waited for ever for a tick size."""
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    gateway.terminal.catalogue['777'] = {
        'security_id': '777', 'symbol': 'CL', 'exchange': 'CME',
        'security_type': 'FUT', 'maturity': '202612', 'tick_size': '1',
        'parameters': {'16554': '10'}, 'legs': []}
    contract = SimpleNamespace(key='cl', name='CL', symbol='CL',
                               security_id='777', security_exchange='CME',
                               tick_size=0.01, tick_value=10.0)
    gateway.subscribe(contract)
    watched = gateway.terminal.watch['777']
    assert watched['tick_size'] == '1'                  # TT's, not the config's
    assert watched['security_type'] == 'FUT'
    assert watched['maturity'] == '202612'


def test_a_contract_tt_has_not_defined_yet_takes_its_configured_tick(tmp_path):
    """The control: with no TT definition cached, the contract's own tick
    size stands in, so the ladder can still draw."""
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    contract = SimpleNamespace(key='es', name='ES', symbol='ES',
                               security_id='888', security_exchange='CME',
                               tick_size=25.0, tick_value=12.5)
    gateway.subscribe(contract)
    watched = gateway.terminal.watch['888']
    assert watched['tick_size'] == '25'
    assert watched['tick_value'] == '12.5'
    assert watched['security_type'] == ''               # unknown, not "undefined"
