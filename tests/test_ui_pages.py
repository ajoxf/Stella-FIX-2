"""The Settings and Exchanges pages, under a real browser.

Same reason as the desk suite: a Python test cannot see a ReferenceError that
aborts a script block and silently unregisters a handler. And the same trap:
these pages poll for the environment badge, so `networkidle` never fires.
"""

import json
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from fixtrader.config import ContractConfig, TraderConfig, VenueConfig  # noqa: E402
from fixtrader.webapp import create_app  # noqa: E402

BROWSER_CANDIDATES = [None, '/opt/pw-browsers/chromium-1194/chrome-linux/chrome']


def _launch(p):
    last = None
    for path in BROWSER_CANDIDATES:
        try:
            return p.chromium.launch(executable_path=path, args=['--no-sandbox'])
        except Exception as e:                              # noqa: BLE001
            last = e
    pytest.skip(f"no chromium available ({last})")


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        currency='USD', enabled=True)
    cfg.save()
    (tmp_path / 'status.json').write_text(json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True, 'loop_ms': 4.2, 'environment': 'SIMULATED',
                   'simulated': True, 'refresh_sec': 0.5},
        'contracts': []}))
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 'status.json'),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", tmp_path
    srv.shutdown()


def open_page(p, url, errors, ready):
    browser = _launch(p)
    page = browser.new_page(viewport={'width': 1400, 'height': 950})
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    # `pageerror` is a real JavaScript exception and always a failure. A
    # console error is not always one: a deliberate refusal (a 400 from a
    # venue with no environment) logs "Failed to load resource", and the test
    # that provoked it asserts the refusal reached the operator instead.
    page.on('console', lambda m: errors.append('console: ' + m.text)
            if m.type == 'error' and 'Failed to load resource' not in m.text
            else None)
    # NOT networkidle: the badge polls, so it never fires.
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_selector(ready, timeout=5000)
    return browser, page


# -- Settings --------------------------------------------------------------

def test_settings_loads_every_field_from_the_config(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/settings', errors, '[data-key]')
        page.wait_for_timeout(600)
        assert page.locator('[data-key="PRICE_REFRESH_SEC"]').input_value() == '0.5'
        assert page.locator('[data-key="MAX_QUOTE_AGE_SEC"]').input_value() == '15'
        assert page.locator('[data-key="ALGO_MASTER_ENABLED"]').is_checked()
        # the Algo's own defaults: 15-minute candles, 2.5 sigma, a stop loss
        # ON, and the mean exit OFF until a contract asks for it
        assert page.locator('[data-key="DEFAULT_TIMEFRAME_MIN"]').input_value() == '15'
        assert page.locator('[data-key="DEFAULT_ENTRY_THRESHOLD"]').input_value() == '2.5'
        assert page.locator('[data-key="DEFAULT_STOP_LOSS_ON"]').is_checked()
        assert not page.locator('[data-key="DEFAULT_EXIT_AT_MEAN"]').is_checked()
        browser.close()
    assert errors == []


def test_settings_shows_what_the_engine_achieved_beside_what_was_asked(server):
    """A refresh interval nobody meets is a number that reads as a promise."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/settings', errors, '[data-key]')
        page.wait_for_timeout(700)
        assert 'ms' in page.locator('#loop-achieved').inner_text()
        browser.close()
    assert errors == []


def test_saving_names_only_the_settings_that_need_a_restart(server):
    """Warning on every save teaches the operator to ignore the line that
    matters."""
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/settings', errors, '[data-key]')
        page.wait_for_timeout(500)

        # a hot-applying setting: no restart notice
        page.fill('[data-key="MAX_QUOTE_AGE_SEC"]', '20')
        page.click('#save')
        page.wait_for_timeout(500)
        assert page.locator('#restart-note').is_hidden()

        # a structural one: named
        page.fill('[data-key="PRICE_REFRESH_SEC"]', '0.25')
        page.click('#save')
        page.wait_for_timeout(500)
        note = page.locator('#restart-note')
        assert not note.is_hidden()
        assert 'PRICE_REFRESH_SEC' in note.inner_text()
        assert 'restart' in note.inner_text()

        saved = json.loads((tmp / 'config.json').read_text())
        assert saved['settings']['MAX_QUOTE_AGE_SEC'] == 20
        browser.close()
    assert errors == []


def test_a_blank_number_is_saved_as_unset_not_as_zero(server):
    """Blank means unset and 0 does not — an empty guard is not a guard set
    to zero, which is a real instruction."""
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/settings', errors, '[data-key]')
        page.wait_for_timeout(500)
        page.fill('[data-key="DAILY_MAX_LOSS_TOTAL"]', '')
        page.click('#save')
        page.wait_for_timeout(500)
        saved = json.loads((tmp / 'config.json').read_text())
        assert saved['settings']['DAILY_MAX_LOSS_TOTAL'] is None
        browser.close()
    assert errors == []


# -- Exchanges -------------------------------------------------------------

def add_uat_venue(page, name='Orient SGX UAT', password='sup3rsecret'):
    page.click('#new-venue')
    page.fill('#v-name', name)
    page.click('#lbl-uat input')
    page.fill('#v-sender_comp_id', 'AJOX')
    if password:
        page.fill('#v-password', password)
    page.click('#v-save')
    page.wait_for_timeout(700)


def test_a_venue_is_created_and_its_password_never_reaches_the_config(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-venue')
        add_uat_venue(page)
        assert page.locator('#venue-list .row').count() == 1
        assert 'password set' in page.locator('#venue-list .row').inner_text()

        assert 'sup3rsecret' not in (tmp / 'config.json').read_text()
        assert 'FIX_ORIENT_SGX_UAT' in (tmp / 'config.json').read_text()
        assert 'sup3rsecret' in (tmp / '.env').read_text()
        browser.close()
    assert errors == []


def test_the_password_box_is_never_filled_back_in(server):
    """A masked value is one that gets echoed into the form and saved over the
    real one."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-venue')
        add_uat_venue(page)
        page.click('#venue-list .row')
        page.wait_for_timeout(400)
        assert page.locator('#v-password').input_value() == ''
        assert 'leave blank' in page.locator('#v-password').get_attribute('placeholder')
        browser.close()
    assert errors == []


def test_a_venue_with_no_environment_is_refused_in_words(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-venue')
        page.click('#new-venue')
        page.fill('#v-name', 'Nameless')
        page.click('#v-save')
        page.wait_for_selector('.toast')
        assert 'UAT' in page.locator('.toast .msg').inner_text()
        browser.close()
    assert errors == []


def test_choosing_PROD_asks_once_before_it_is_saved(server):
    """Turning a venue live is a decision, made once, out loud."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-venue')
        page.click('#new-venue')
        page.fill('#v-name', 'Orient PROD')
        page.click('#lbl-prod input')
        page.click('#v-save')
        page.wait_for_selector('#modal:not(.hidden)')
        assert 'live account' in page.locator('#modal-body').inner_text()
        page.click('#modal-cancel')            # unanswered means NO
        page.wait_for_timeout(400)
        assert page.locator('#venue-list .row').count() == 0
        browser.close()
    assert errors == []


def test_diagnose_reports_the_password_as_set_and_never_as_a_value(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-venue')
        add_uat_venue(page)
        page.click('#v-diagnose')
        page.wait_for_selector('.diag .dline')
        text = page.locator('.diag').inner_text()
        assert 'sup3rsecret' not in text
        assert 'FIX_ORIENT_SGX_UAT' in text
        browser.close()
    assert errors == []


def test_a_missing_password_is_a_failure_that_carries_its_own_fix(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-venue')
        add_uat_venue(page, name='Orient No Password', password='')
        page.click('#v-test')
        page.wait_for_selector('.diag .dline')
        assert 'NOT set' in page.locator('.diag').inner_text()
        assert page.locator('.diag .dfix').count() > 0
        browser.close()
    assert errors == []


def test_reading_specs_from_the_venue_offers_but_does_not_apply(server):
    """A specification changed under a running desk is every money figure on
    that window changing without anybody being told."""
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-contract')
        add_uat_venue(page)
        page.locator('#contract-rows tr').first.locator('button', has_text='Edit').click()
        page.wait_for_timeout(300)
        # The contract has to route through a venue before there is a venue to
        # ask — "read from the venue" with no venue is refused, and says so.
        page.select_option('#c-venue', 'Orient SGX UAT')
        page.fill('#c-tick_size', '0.05')          # disagree with the venue
        page.click('#c-save')
        page.wait_for_timeout(600)
        page.locator('#contract-rows tr').first.locator('button', has_text='Edit').click()
        page.wait_for_timeout(300)
        page.click('#c-read')
        page.wait_for_selector('#spec-compare table')
        text = page.locator('#spec-compare').inner_text()
        assert 'Nothing has been applied' in text
        # Against the simulator it must SAY that it confirms nothing, or a
        # green comparison reads as a check that was never performed.
        assert 'confirms nothing' in text
        # the config still holds what the operator typed until they accept
        assert TraderConfig.from_file(str(tmp / 'config.json')) \
            .contracts['fef'].tick_size == 0.05
        browser.close()
    assert errors == []


def test_a_new_contract_is_added_and_appears_in_the_table(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-contract')
        page.click('#new-contract')
        page.fill('#c-name', 'A50 Oct/Nov')
        page.fill('#c-symbol', 'CNV6-CNX6')
        page.fill('#c-tick_size', '2.5')
        page.fill('#c-tick_value', '2.5')
        page.click('#c-save')
        page.wait_for_timeout(700)
        assert page.locator('#contract-rows tr').count() == 2
        assert 'A50 Oct/Nov' in page.locator('#contract-rows').inner_text()
        browser.close()
    assert errors == []


def test_there_are_no_native_dialogs_on_either_page(server):
    url, _ = server
    errors, dialogs = [], []
    with sync_playwright() as p:
        browser, page = open_page(p, url + '/exchanges', errors, '#new-contract')
        page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
        page.locator('#contract-rows tr').first.locator('button', has_text='Delete').click()
        page.wait_for_timeout(400)
        page.click('#modal-cancel')
        browser.close()
    assert dialogs == []
    assert errors == []
