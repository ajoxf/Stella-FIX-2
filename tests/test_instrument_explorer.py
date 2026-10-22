"""The instrument explorer names and files contracts the way TT's does.

A spread's FIX symbol (`CL`) and its exchange contract code (`CLX6-CLJ0`)
say neither which way round it is nor what the other leg is, and an
inter-commodity spread filed under one leg's symbol cannot be found by
looking for it under TT's own product, `CL|BZ`.
"""
from types import SimpleNamespace

import pytest

from fixtrader.manual_terminal import (ManualTerminal, month_label,
                                       product_key, structure, tt_name)


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
    """One SecurityDefinition (d), legs and all, as TT puts it on the wire."""
    fields = [('35', 'd'), ('320', request_id), ('48', security_id),
              ('55', symbol), ('207', 'CME'),
              ('167', 'MLEG' if legs else 'FUT')] + list(extra.items())
    if legs:
        fields.append(('555', str(len(legs))))
        for leg_symbol, month, side in legs:
            fields += [('600', leg_symbol), ('602', leg_symbol + month),
                       ('610', month), ('623', '1'), ('624', side)]
    raw = '\x01'.join(f'{k}={v}' for k, v in fields) + '\x01'
    terminal.on_message('Market Data', dict(fields), raw)


def test_names_read_the_way_tt_writes_them():
    cal = {'symbol': 'CL', 'legs': [
        {'600': 'CL', '610': '202611', '623': '1', '624': '1'},
        {'600': 'CL', '610': '203004', '623': '1', '624': '2'}]}
    inter = {'symbol': 'CL', 'legs': [
        {'600': 'CL', '610': '202611', '623': '1', '624': '1'},
        {'600': 'BZ', '610': '202611', '623': '1.0', '624': '2'}]}
    fut = {'symbol': 'CL', 'security_type': 'FUT', 'maturity': '202612'}
    assert tt_name(cal) == '+1xCL Nov26:-1xCL Apr30'
    assert tt_name(inter) == '+1xCL Nov26:-1xBZ Nov26'
    assert tt_name(fut) == 'CL Dec26'
    assert product_key(cal) == 'CL'
    assert product_key(inter) == 'CL|BZ'
    assert product_key(fut) == 'CL'
    assert structure(cal) == 'Calendar'
    assert structure(inter) == 'Inter-commodity'
    assert structure(fut) == 'Future'
    assert month_label('20261120') == 'Nov26'
    assert month_label('') == '' and month_label('Z6') == 'Z6'


def test_the_catalogue_carries_the_tt_name_and_product(terminal):
    rid = terminal.lookup({'exchange': 'CME', 'symbol': 'CL',
                           'security_type': 'MLEG'})['request_id']
    definition(terminal, rid, '9001', 'CL',
               [('CL', '202611', '1'), ('BZ', '202611', '2')],
               **{'107': 'Crude Oil Futures | Brent Last Day Financial'})
    row = terminal.snapshot()['catalogue'][0]
    assert row['tt_name'] == '+1xCL Nov26:-1xBZ Nov26'
    assert row['product_key'] == 'CL|BZ'
    assert row['structure'] == 'Inter-commodity'
    assert row['first_month'] == '202611'
    assert row['leg_months'] == '202611,202611'
    assert row['description'].startswith('Crude Oil Futures')


def test_a_second_search_sent_WITH_append_keeps_the_first_ones_answers(terminal):
    """CL|BZ is searched once per leg product. Answers to the first search
    that arrive after the second was sent are still definitions TT gave."""
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


def test_an_inter_product_name_can_be_asked_for_as_tt_writes_it(terminal):
    """TT's explorer lists `CL|BZ` as a product of its own. The bar is not a
    FIX delimiter, so it goes on the wire as the product symbol."""
    terminal.lookup({'exchange': 'CME', 'symbol': 'CL|BZ',
                     'security_type': 'MLEG'})
    msg, fields = terminal.gateway._sessions['Market Data'].sent[-1]
    assert msg == 'c' and dict(fields)['55'] == 'CL|BZ'
    assert terminal.search['symbol'] == 'CL|BZ'


def test_a_control_character_in_a_product_symbol_is_still_refused(terminal):
    """The control: the bar is allowed, SOH and friends are not."""
    with pytest.raises(ValueError):
        terminal.lookup({'exchange': 'CME', 'symbol': 'CL\x01BZ',
                         'security_type': 'MLEG'})
    with pytest.raises(ValueError):
        terminal.lookup({'exchange': 'CME', 'symbol': 'CL||',
                         'security_type': 'MLEG'})
