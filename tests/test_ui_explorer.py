"""The instrument explorer, driven in a real browser.

The catalogue is built by feeding SecurityDefinitions through the REAL
ManualTerminal parser, so what the page shows is what TT's messages produce.
"""
import itertools
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from fixtrader.manual_terminal import ManualTerminal  # noqa: E402
from fixtrader.webapp import create_app  # noqa: E402
from tests.test_ui_browser import _launch  # noqa: E402


class _Session:
    state = SimpleNamespace(status='CONNECTED')
    def is_running(self): return True
    def send(self, *args): pass


def _catalogue():
    gw = SimpleNamespace(_sessions={'Market Data': _Session(),
                                    'Order Routing': _Session()},
                         venue=SimpleNamespace(account='UAT'),
                         _redact=lambda v: v)
    t = ManualTerminal(gw, ':memory:')
    t.poll()
    ids = itertools.count(500)
    rid = t.lookup({'exchange': 'CME', 'symbol': 'CL',
                    'security_type': 'MLEG'})['request_id']

    def define(legs, desc):
        f = [('35', 'd'), ('320', rid), ('48', str(next(ids))), ('55', 'CL'),
             ('207', 'CME'), ('167', 'MLEG'), ('107', desc),
             ('16552', '0.01'), ('16554', '1000'), ('15', 'USD')]
        for symbol, month, side in legs:
            f += [('600', symbol), ('610', month), ('623', '1'), ('624', side)]
        t.on_message('Market Data', dict(f),
                     '\x01'.join(f'{k}={v}' for k, v in f) + '\x01')

    # Deliberately out of date order: the list must sort by the legs' dates.
    define([('CL', '202611', '1'), ('BZ', '202612', '2')], 'Crude | Brent')
    define([('CL', '202611', '1'), ('BZ', '202611', '2')], 'Crude | Brent')
    define([('CL', '202611', '1'), ('CL', '202612', '2')], 'Crude calendar')
    return t.snapshot()


@pytest.fixture
def server(tmp_path):
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True, 'refresh_sec': 30, 'environment': 'UAT',
                   'session': {'state': 'LOGGED_ON'},
                   'manual_terminal': _catalogue()},
        'contracts': []}))
    app = create_app(str(tmp_path / 'config.json'), str(status),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def test_an_inter_commodity_spread_is_found_by_its_tt_product(server):
    errors = []
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(server + 'instruments', wait_until='domcontentloaded')
        page.wait_for_function(
            "document.querySelector('#explore-open') && window.fetch")
        page.wait_for_timeout(1500)            # the first snapshot poll
        page.click('#explore-open')
        page.select_option('#explore-type', 'MLEG')
        # typed the way a person types it, not the way TT spells it
        page.fill('#explore-product-input', 'cl bz')
        page.wait_for_function(
            "document.querySelectorAll('#explore-contract option').length === 2")
        products = page.eval_on_selector_all(
            '#explore-product option', 'o => o.map(x => x.value)')
        assert products == ['CL|BZ']
        names = page.eval_on_selector_all(
            '#explore-contract option', 'o => o.map(x => x.textContent)')
        assert names == ['+1xCL Nov26:-1xBZ Nov26', '+1xCL Nov26:-1xBZ Dec26']

        # the legs in the other order find the same product
        page.fill('#explore-product-input', 'bz|cl')
        page.wait_for_function(
            "document.querySelectorAll('#explore-contract option').length === 2")
        # a product that is not cached leaves every cached product visible
        page.fill('#explore-product-input', 'HO|CL')
        page.wait_for_function(
            "document.querySelectorAll('#explore-product option').length === 2")
        assert 'HO|CL' in page.inner_text('#explore-status')
        page.fill('#explore-product-input', 'cl bz')
        page.wait_for_function(
            "document.querySelectorAll('#explore-contract option').length === 2")

        page.fill('#explore-contract-filter', 'dec26')
        page.wait_for_function(
            "document.querySelectorAll('#explore-contract option').length === 1")
        page.select_option('#explore-contract', index=0)
        assert 'Inter-commodity' in page.inner_text('#explore-detail')

        # Add to algo desk FILLS the contract form; it saves nothing.
        page.click('#explore-algo')
        page.wait_for_selector('#contract-form-card:not([hidden])')
        assert page.input_value('#c-name') == '+1xCL Nov26:-1xBZ Dec26'
        assert page.input_value('#c-symbol') == 'CL'
        assert page.input_value('#c-security_exchange') == 'CME'
        assert page.input_value('#c-tick_value') == '10.00'
        browser.close()
    assert errors == []
