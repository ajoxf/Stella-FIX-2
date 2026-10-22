"""The instrument explorer names and files contracts by their legs.

A strategy's symbol (`CL`) says neither which way round it is nor what the
other leg is: Stellar's Security List gives its legs (555: 600 / 610 / 623
/ 624), and the explorer names it by them and files an inter-commodity
spread under both leg products, `CL|BZ`.
"""
from types import SimpleNamespace

import pytest

from fixtrader.manual_terminal import (ManualTerminal, month_label,
                                       product_key, structure, venue_name)


class Session:
    def __init__(self):
        self.state = SimpleNamespace(status='CONNECTED')
        self.sent = []
    def is_running(self): return True
    def send(self, msg, fields): self.sent.append((msg, fields))


@pytest.fixture
def terminal(tmp_path):
    gateway = SimpleNamespace(
        _sessions={'Market Data': Session(), 'Order Routing': Session()},
        venue=SimpleNamespace(account='UAT'), _redact=lambda v: v)
    t = ManualTerminal(gateway, str(tmp_path / 'manual.db'))
    t.poll()
    return t


def definition(terminal, request_id, security_id, symbol, legs=(), **extra):
    """One Security List (y) entry, legs and all, as Stellar puts it on the
    wire: the instrument opened by its symbol, the request's id after it."""
    fields = [('35', 'y'), ('146', '1'), ('55', symbol), ('15', 'USD'), ('48', security_id),
              ('207', 'CME'), ('461', 'MXXXXX' if legs else 'FXXXXX')] + list(extra.items())
    if legs:
        fields.append(('555', str(len(legs))))
        for leg_symbol, month, side in legs:
            fields += [('600', leg_symbol), ('608', 'FXXXXX'),
                       ('610', month), ('623', '1'), ('624', side)]
        fields.append(('762', 'TSPR'))
    fields += [('320', request_id), ('322', '1_R'), ('560', '0'), ('893', 'Y')]
    raw = '\x01'.join(f'{k}={v}' for k, v in fields) + '\x01'
    terminal.on_message('Market Data', dict(fields), raw)


def test_names_read_by_their_legs():
    cal = {'symbol': 'CL', 'legs': [
        {'600': 'CL', '610': '202611', '623': '1', '624': '1'},
        {'600': 'CL', '610': '203004', '623': '1', '624': '2'}]}
    inter = {'symbol': 'CL', 'legs': [
        {'600': 'CL', '610': '202611', '623': '1', '624': '1'},
        {'600': 'BZ', '610': '202611', '623': '1.0', '624': '2'}]}
    fut = {'symbol': 'CL', 'security_type': 'FUT', 'maturity': '202612'}
    assert venue_name(cal) == '+1xCL Nov26:-1xCL Apr30'
    assert venue_name(inter) == '+1xCL Nov26:-1xBZ Nov26'
    assert venue_name(fut) == 'CL Dec26'
    assert product_key(cal) == 'CL'
    assert product_key(inter) == 'CL|BZ'
    assert product_key(fut) == 'CL'
    assert structure(cal) == 'Calendar'
    assert structure(inter) == 'Inter-commodity'
    assert structure(fut) == 'Future'
    assert month_label('20261120') == 'Nov26'
    assert month_label('') == '' and month_label('Z6') == 'Z6'


def test_the_catalogue_carries_the_venue_name_and_product(terminal):
    rid = terminal.lookup({'exchange': 'CME', 'symbol': 'CL',
                           'security_type': 'MLEG'})['request_id']
    definition(terminal, rid, '9001', 'CL',
               [('CL', '202611', '1'), ('BZ', '202611', '2')],
               **{'107': 'Crude Oil Futures | Brent Last Day Financial'})
    row = terminal.snapshot()['catalogue'][0]
    assert row['venue_name'] == '+1xCL Nov26:-1xBZ Nov26'
    assert row['product_key'] == 'CL|BZ'
    assert row['structure'] == 'Inter-commodity'
    assert row['first_month'] == '202611'
    assert row['leg_months'] == '202611,202611'
    assert row['description'].startswith('Crude Oil Futures')


def test_a_second_search_sent_WITH_append_keeps_the_first_ones_answers(terminal):
    """CL|BZ is searched once per leg product. Answers to the first search
    that arrive after the second was sent are still definitions Stellar gave."""
    first = terminal.lookup({'exchange': 'CME', 'symbol': 'CL',
                             'security_type': 'MLEG'})['request_id']
    second = terminal.lookup({'exchange': 'CME', 'symbol': 'BZ',
                              'security_type': 'MLEG',
                              'append': True})['request_id']
    definition(terminal, first, '1', 'CL', [('CL', '202611', '1'), ('BZ', '202611', '2')])
    definition(terminal, second, '2', 'BZ', [('BZ', '202612', '1'), ('BZ', '202701', '2')])
    assert set(terminal.instruments) == {'1', '2'}


def test_a_new_search_without_append_still_discards_the_old_ones(terminal):
    """The control: a fresh search is a fresh result list, and a late answer
    to a search nobody is waiting for any more is not mixed into it."""
    first = terminal.lookup({'exchange': 'CME', 'symbol': 'CL',
                             'security_type': 'MLEG'})['request_id']
    second = terminal.lookup({'exchange': 'CME', 'symbol': 'ES',
                              'security_type': 'FUT'})['request_id']
    definition(terminal, first, '1', 'CL', [('CL', '202611', '1'), ('BZ', '202611', '2')])
    definition(terminal, second, '2', 'ES', **{'200': '202612'})
    assert set(terminal.instruments) == {'2'}


def test_a_search_is_a_security_list_request_by_symbol_and_cfi_code(terminal):
    """Stellar's Security List Request (x) names a symbol and a CFI code:
    a strategy is MXXXXX, a future FXXXXX — and an option is asked for as
    calls and puts both."""
    terminal.lookup({'exchange': 'XCME', 'symbol': 'GE', 'security_type': 'MLEG'})
    msg, fields = terminal.gateway._sessions['Market Data'].sent[-1]
    f = dict(fields)
    assert msg == 'x' and f['55'] == 'GE' and f['461'] == 'MXXXXX'
    assert f['559'] == '0' and f['207'] == 'XCME'
    terminal.lookup({'exchange': 'XCME', 'symbol': 'GE', 'security_type': 'OPT'})
    cfis = [dict(fl)['461'] for m, fl in terminal.gateway._sessions['Market Data'].sent[-2:]]
    assert cfis == ['OCXXXX', 'OPXXXX']


def test_an_expiry_month_filters_the_answer_stellar_cannot_be_asked_for_it(terminal):
    rid = terminal.lookup({'exchange': 'CME', 'symbol': 'ES', 'security_type': 'FUT',
                           'maturity': '202612'})['request_id']
    msg, fields = terminal.gateway._sessions['Market Data'].sent[-1]
    assert '200' not in dict(fields)
    definition(terminal, rid, '1', 'ES', **{'200': '202612'})
    definition(terminal, rid, '2', 'ES', **{'200': '202703'})
    assert set(terminal.instruments) == {'1'}


def test_a_refusal_of_the_search_is_said_in_words(terminal):
    rid = terminal.lookup({'exchange': 'CME', 'symbol': 'XX', 'security_type': 'FUT'})['request_id']
    raw = f'35=y\x01320={rid}\x01322=1_R\x01560=2\x01893=Y\x01'
    terminal.on_message('Market Data', {'35': 'y', '320': rid, '560': '2'}, raw)
    assert 'no instruments found' in terminal.search['error']


def test_a_control_character_or_a_bar_in_a_product_symbol_is_refused(terminal):
    """Stellar's request names ONE symbol: the explorer asks for CL|BZ leg
    by leg, and a bar, SOH and friends never reach the wire."""
    for symbol in ('CL\x01BZ', 'CL|BZ'):
        with pytest.raises(ValueError):
            terminal.lookup({'exchange': 'CME', 'symbol': symbol,
                             'security_type': 'MLEG'})
