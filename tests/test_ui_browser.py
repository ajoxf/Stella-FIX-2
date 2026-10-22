"""The UI, under a real browser, reading `pageerror`.

Python tests cannot see a temporal-dead-zone ReferenceError that aborts a
script block and silently unregisters a handler — and that is exactly what
happened in the system this is ported from. These do.

They skip cleanly where no browser is installed. Note the one trap: this page
polls twice a second, so `networkidle` NEVER fires — wait for
`domcontentloaded` and then for the thing you actually care about.
"""

import json
import re
import os
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from fixtrader.webapp import create_app  # noqa: E402

#: Where this box keeps its browser. Playwright's own default is tried first.
BROWSER_CANDIDATES = [
    None,
    '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
]


def _launch(p):
    last = None
    for path in BROWSER_CANDIDATES:
        try:
            return p.chromium.launch(executable_path=path,
                                     args=['--no-sandbox'])
        except Exception as e:                       # noqa: BLE001
            last = e
    pytest.skip(f"no chromium available ({last})")


SNAPSHOT = {
    'engine': {
        'alive': True, 'loop_ms': 4.2, 'master_algo': True, 'killed': False,
        'environment': 'SIMULATED', 'simulated': True, 'book_complete': True,
        'unclaimed': [], 'refresh_sec': 0.5, 'sound': False,
        'confirm_close': True,
        'session': {'state': 'LOGGED_ON', 'text': 'simulator'},
        'notify': {'orders': False, 'fills': True, 'positions': True,
                   'rejects': True, 'withheld': True},
    },
    'contracts': [{
        'key': 'fef', 'name': 'Iron ore Oct/Nov', 'symbol': 'FEFV6-FEFX6',
        'venue': 'SIM', 'decimals': 4, 'tick_size': 0.01, 'state': 'IN',
        'algo_on': True,
        'algo_state': 'PAPER',
        'manual_block': None,
        'market': {'bid': 0.48, 'ask': 0.49, 'bid_size': 25, 'ask_size': 25,
                   'mid': 0.485},
        'feed': {'age_sec': 0.2, 'stale': False, 'settling': False},
        'stats': {'mean': 0.5, 'std': 0.08, 'z': -0.19, 'buy_at': 0.34,
                  'sell_at': 0.66, 'hurst': 0.42, 'half_life': 18.0,
                  'samples': 120, 'need': 120, 'warm_pct': 100.0,
                  'is_warm': True, 'unresolved_touches': 0},
        'filters': {'edge_ratio': 2.1, 'edge_ok': True, 'blocked_by': None},
        'costs': {'round_trip_money': 19.0, 'round_trip_ticks': 0.38},
        'settings': {'entry_threshold': 2.0, 'stop_loss_z': 4.0, 'quantity': 5},
        'position': {'side': 'BUY', 'qty': 5, 'avg_price': 0.48,
                     'entry_z': -2.14, 'break_even': 0.518, 'target': 0.57,
                     'stop': 0.18, 'margin_locked': 1300.0,
                     'opened_at': datetime.now(timezone.utc).isoformat(),
                     'open_pnl': -20.0},
        'orders': [], 'pnl_today': 0.0, 'trades_today': 0, 'last_close': None,
        'last_event': 'BUY 5 @ 0.48', 'target_missing': None,
        # The Algo window's block, shaped as `AlgoRun.block` publishes it.
        'algo': {
            'mode': 'PAPER', 'state': 'IN_POSITION', 'ready': True,
            'count': 100, 'needed': 20, 'mean': 0.50, 'sigma': 0.04,
            'upper': 0.58, 'lower': 0.42, 'z_sell': -0.5, 'z_buy': -0.25,
            'z_mid': -0.375, 'signal': None, 'blocked': None, 'health': None,
            'cooldown_sec': None, 'armed': {'BUY': False, 'SELL': True},
            'streak': {'BUY': 0, 'SELL': 0},
            'warmup': {'sec': 5400, 'need_sec': 5400, 'done': True},
            'atr': 0.012, 'atr_period': 14, 'timeframe_min': 15,
            'length': 20,
            'params': {'entry_z': 2.0, 'direction': 'BOTH',
                       'timeframe_min': 15, 'length': 20,
                       'confirm_ticks': 3, 'reentry_on': True,
                       'reentry_back': 0.5, 'reentry_window_pct': 50.0,
                       'stop_loss_on': True, 'stop_mode': 'MARGIN',
                       'target_mode': 'MARGIN', 'atr_period': 14,
                       'edge_capture_frac': 0.5, 'max_trades_day': 10,
                       'algo_qty': 5, 'progress_bar': True},
            'positions': [{
                'position_id': 7, 'side': 'BUY', 'entry': 0.48,
                'closing': 0.48, 'z_close': -0.5, 'tp': 0.57, 'sl': 0.18,
                'break_even': 0.518, 'net_pnl': -20.0, 'exit': None,
                'progress': -0.1, 'quantity': 5, 'age_sec': 125.0,
                'entry_z': -2.14, 'tp_money': 260.0, 'sl_money': -1500.0,
                'paper': True}],
            'filters': {
                'ready': True, 'qty': 5, 'k': 100.0,
                'cost': {'crossing': 5.0, 'commission': 10.0,
                         'slippage': 0.0, 'total': 15.0},
                'edge': {'on': True, 'ok': True, 'ratio': 2.0,
                         'required': 1.5, 'capture': 30.0, 'cost': 15.0},
                'regime': {'on': True, 'state': 'RANGE',
                           'efficiency_ratio': 0.2, 'crossings': 9,
                           'slope': 0.01},
                'trend': {'on': True, 'drift_sigma': 0.2, 'state': 'FLAT',
                          'limit': 1.0, 'lookback_min': 120},
                'half_life_minutes': 45.0, 'half_life_band': [0, 0],
                'warmup': {'sec': 5400, 'need_sec': 5400, 'done': True}},
            'day': {'date': '2026-10-08', 'trades': 1, 'losses_row': 0,
                    'pnl': 10.8},
            'history': {'note': '100 candles from the recorded mids',
                        'candles': 100},
            'last_blocked': {'side': 'SELL', 'z': 2.31, 'at': 1791460000,
                             'reason': 'edge filter: capture 0.9x the cost, '
                                       'under the 1.5x required'},
            'recent': [{'action': 'ENTER', 'side': 'BUY', 'z': -2.14,
                        'at': 1791460100, 'mode': 'PAPER', 'done': True,
                        'result': None}],
        },
    }],
    'portfolio': {
        'rows': [{
            'key': 'fef', 'name': 'Iron ore Oct/Nov', 'symbol': 'FEFV6-FEFX6',
            'decimals': 4, 'side': 'BUY', 'qty': 5, 'avg_price': 0.48,
            'entry_z': -2.14, 'break_even': 0.518, 'target': 0.57,
            'stop': 0.18, 'opened_at': datetime.now(timezone.utc).isoformat(),
            'open_pnl': -20.0, 'margin_locked': 1300.0,
            'tickets': ['E000004', 'E000005'], 'mid': 0.485,
            'venue_qty': 5.0, 'venue_long': 5.0, 'venue_short': None,
            'venue_readable': True, 'both_sides_open': False, 'agrees': True,
        }],
        'venue_readable': True, 'open_pnl': -20.0, 'margin': 1300.0,
        'realised_today': 62.0, 'trades_today': 2,
    },
}


@pytest.fixture
def server(tmp_path):
    status = tmp_path / 'status.json'
    snap = dict(SNAPSHOT, ts=datetime.now(timezone.utc).isoformat())
    status.write_text(json.dumps(snap))
    app = create_app(str(tmp_path / 'config.json'), str(status),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}/", tmp_path
    srv.shutdown()


def open_page(p, url, errors):
    browser = _launch(p)
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    # `pageerror` is a real JavaScript exception and always a failure. A
    # console error is not: a 404 for a contract that is on the screen but not
    # in this fixture's configuration is a state the window handles in words.
    page.on('console', lambda m: errors.append('console: ' + m.text)
            if m.type == 'error' and 'Failed to load resource' not in m.text
            else None)
    # NOT networkidle: the page polls twice a second and it never fires.
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_selector('.win', timeout=5000)
    return browser, page


def test_the_window_renders_every_field_without_a_page_error(server):
    """The Algo window, as the MT5 desk draws it, on one contract: H to L on
    the BID, L to H on the OFFER, the statistics, the filters, the last
    signal held back and the last order — and its ladder beside it."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        assert page.locator('.contractwin').count() == 1
        win = page.locator('.contractwin')
        assert win.locator('.state').inner_text() == 'IN'
        assert win.locator('.title').inner_text() == 'Iron ore Oct/Nov · Algo'
        # ONE word for the contract's Algo, on its window and its ladder alike
        assert win.locator('.algo-btn').inner_text().startswith('ALGO PAPER')
        assert page.locator('.ladderwin .algo-btn').inner_text().startswith('ALGO PAPER')
        assert win.locator('.aw-tile.sell .aw-tile-price').inner_text() == '0.4800'
        assert win.locator('.aw-tile.buy .aw-tile-price').inner_text() == '0.4900'
        assert win.locator('.aw-tile.sell .aw-tile-z').inner_text() == '-0.50'
        assert win.locator('.aw-tile.buy .aw-tile-z').inner_text() == '-0.25'
        assert 'ARMED' in win.locator('.aw-tile.sell .aw-tile-entry').inner_text()
        assert win.locator('.aw-pos').inner_text() == 'LONG 5'
        stats = win.locator('.aw-stats').inner_text()
        assert 'Mean (EMA)' in stats and '0.5000' in stats
        assert 'candles ready' in stats and 'warmed up' in stats
        filters = win.locator('.aw-filters').inner_text()
        assert '2.00×' in filters and 'req 1.5×' in filters
        assert 'edge filter' in filters                 # last signal blocked
        assert 'filled on paper' in filters             # last order
        signal = win.locator('.aw-signal').inner_text()
        assert 'TP 0.5700' in signal and 'SL 0.1800' in signal
        assert '% to SL' in signal                       # the progress bar
        # the ladder, with the Algo's levels against the book
        ladder = page.locator('.ladderwin')
        assert ladder.count() == 1
        marks = ladder.locator('td.work').all_inner_texts()
        assert any('ENTRY' in m for m in marks)
        assert any('TP' in m for m in marks)
        assert 'manual orders are off' in ladder.locator('.ld-lock').inner_text()
        # the MT5 ladder's furniture: the five columns, the rail, the bar
        heads = ladder.locator('.ld-grid th').all_inner_texts()
        assert [h.strip() for h in heads] == ['Work', 'Bids', 'Price', 'Asks', 'LTQ']
        assert ladder.locator('.ld-flatten').is_enabled()      # a close always closes
        assert not ladder.locator('.ld-buy').is_enabled()      # no manual order here
        assert ladder.locator('.ld-counts').inner_text().split() == ['B:0', 'S:0', 'W:0']
        assert ladder.locator('tr.mid-line').count() == 1
        browser.close()
    assert errors == []


def test_a_missing_figure_renders_as_an_em_dash_never_as_zero(server):
    """The rule the whole system turns on, checked where the operator reads
    it: unmeasured is not zero."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    algo = snap['contracts'][0]['algo']
    algo.update({'mean': None, 'sigma': None, 'atr': None, 'z_sell': None,
                 'z_buy': None, 'upper': None, 'lower': None})
    algo['filters']['half_life_minutes'] = None
    algo['filters']['edge']['ratio'] = None
    algo['filters']['cost']['total'] = None
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)

        def value(label):
            return page.locator('.aw-kv', has=page.locator(
                'span', has_text=label)).first.locator('b').inner_text()
        assert value('Mean (EMA)') == '—'
        assert value('Std dev') == '—'
        assert value('ATR(14)') == '—'
        assert value('Half-life') == '—'
        assert value('Round trip') == '—'
        assert value('Capture / cost').startswith('—')
        assert page.locator('.aw-tile.sell .aw-tile-z').inner_text() == '—'
        browser.close()
    assert errors == []


def test_the_algo_switch_sends_a_command(server):
    """One switch: Off / Signals / Paper / UAT — on a UAT venue the choice
    that sends orders is called UAT; LIVE is only ever a live market."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['engine'].setdefault('execution', {})['send_word'] = 'UAT'
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        win = page.locator('.contractwin')
        win.locator('.algo-btn').click()
        items = win.locator('.algo-menu button').all_inner_texts()
        page.wait_for_timeout(300)
        win.locator('.algo-btn').click()
        win.locator('.algo-btn').click()
        items = win.locator('.algo-menu button').all_inner_texts()
        assert [i.split()[0] for i in items] == ['Off', 'Signals', 'Paper', 'UAT']
        assert 'LIVE' not in ' '.join(items)
        win.locator('.algo-menu button[data-algo="DRY"]').click()
        page.wait_for_timeout(400)
        sent = json.loads((tmp / 'commands.jsonl').read_text().strip().splitlines()[-1])
        assert sent['action'] == 'algo_state' and sent['args']['state'] == 'DRY'
        assert sent['contract'] == 'fef'
        browser.close()
    assert errors == []


def test_setting_the_algo_to_trade_asks_first(server):
    """Trades takes the contract from the hand — so it asks, and says
    PAPER or LIVE. An unanswered question sends nothing (the control)."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0]['algo_state'] = 'DRY'
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        ladder = page.locator('.ladderwin')
        page.wait_for_timeout(600)
        ladder.locator('.algo-btn').click()
        ladder.locator('.algo-menu button[data-algo="PAPER"]').click()
        page.wait_for_selector('#modal:not(.hidden)')
        assert 'refused while it trades' in page.locator('#modal-body').inner_text()
        page.locator('#modal-cancel').click()
        page.wait_for_timeout(300)
        log = tmp / 'commands.jsonl'
        assert not log.exists() or 'algo_state' not in log.read_text()
        ladder.locator('.algo-btn').click()
        ladder.locator('.algo-menu button[data-algo="PAPER"]').click()
        page.locator('#modal-confirm').click()
        page.wait_for_timeout(400)
        sent = json.loads(log.read_text().strip().splitlines()[-1])
        assert sent['action'] == 'algo_state' and sent['args']['state'] == 'TRADE'
        browser.close()
    assert errors == []


def test_close_now_asks_once_and_an_unanswered_prompt_means_no(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.locator('.close-now').click()
        page.wait_for_selector('#modal:not(.hidden)')
        page.locator('#modal-cancel').click()          # answered NO
        page.wait_for_timeout(300)
        assert not os.path.exists(tmp / 'commands.jsonl') or \
            'close_now' not in (tmp / 'commands.jsonl').read_text()
        # control: say yes and the command goes
        page.locator('.close-now').click()
        page.wait_for_selector('#modal:not(.hidden)')
        page.locator('#modal-confirm').click()
        page.wait_for_timeout(400)
        assert 'close_now' in (tmp / 'commands.jsonl').read_text()
        browser.close()
    assert errors == []


def test_there_are_no_native_dialogs_anywhere(server):
    """A native confirm() blocks the whole page and cannot be styled or
    tested. If one comes back, this fails the build."""
    url, _ = server
    errors, dialogs = [], []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
        page.locator('.close-now').click()
        page.wait_for_timeout(300)
        page.locator('#modal-cancel').click()
        page.locator('#kill').click()
        page.wait_for_timeout(300)
        browser.close()
    assert dialogs == []
    assert errors == []


def test_a_stale_quote_greys_the_prices_and_says_so(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0]['feed'] = {'age_sec': 41.2, 'stale': True,
                                    'settling': False}
    snap['contracts'][0]['state'] = 'HALTED'
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)
        assert 'stale' in (page.locator('.contractwin').get_attribute('class') or '')
        assert page.locator('.state').inner_text() == 'HALTED'
        # ...and the way OUT is still available
        assert page.locator('.close-now').is_enabled()
        browser.close()
    assert errors == []


def test_a_withheld_entry_says_why_on_the_window(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    c = snap['contracts'][0]
    c['position'] = None
    c['state'] = 'BLOCKED'
    c['algo'].update({'state': 'BLOCKED', 'positions': [],
                      'blocked': 'edge filter: capture 0.70x the cost, '
                                 'under the 1.5x required — round trip'})
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)
        text = page.locator('.aw-line').inner_text()
        assert text.startswith('held:') and 'round trip' in text
        assert page.locator('.aw-pos').inner_text() == 'FLAT'
        assert page.locator('.close-now').is_disabled()
        browser.close()
    assert errors == []


def test_the_taskbar_minimises_and_restores_a_window(server):
    """The ladder can be put away: its taskbar button takes it off the desk
    and brings it back, and a reload remembers."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        ladder = page.locator('.ladderwin')
        ladder.locator('.min').click()
        assert not ladder.is_visible()
        tab = page.locator('#tabs .tk', has_text='Iron ore Oct/Nov').first
        assert 'minimised' in tab.get_attribute('class')
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('.contractwin')
        page.wait_for_timeout(600)
        assert not page.locator('.ladderwin').is_visible()   # remembered
        page.locator('#tabs .tk.minimised').first.click()
        assert page.locator('.ladderwin').is_visible()
        browser.close()
    assert errors == []


def test_eight_windows_fit_a_desk_without_the_body_scrolling_sideways(server):
    """The size budget: eight contracts on a 1920x1080 screen."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    one = snap['contracts'][0]
    snap['contracts'] = []
    for i in range(8):
        c = json.loads(json.dumps(one))
        c['key'] = f'c{i}'
        c['name'] = f'Contract {i}'
        snap['contracts'].append(c)
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('.contractwin')
        page.wait_for_timeout(900)
        assert page.locator('.contractwin').count() == 8
        overflow = page.evaluate(
            "document.body.scrollWidth - document.body.clientWidth")
        assert overflow <= 0
        browser.close()
    assert errors == []


# -- the Trading Monitor window ---------------------------------------------

POS_ROW = '.tmonwin .mon-pane tr.pos'


def test_the_trading_monitor_lists_every_open_position_with_its_tickets(server):
    """The screen that answers 'what am I in, across everything' — the
    Trading Monitor's Positions tab, on the desk."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector(POS_ROW)
        tabs = page.locator('.tmonwin .mon-tabs button').all_inner_texts()
        # the MT5 Trading Monitor's own tabs, in its own order
        assert [t.split()[0] for t in tabs] == ['Positions', 'Working', 'Fills', 'Slippage',
                                                'Accounts', 'Reconciler', 'Analysis']
        row = page.locator(POS_ROW).first.inner_text()
        detail = page.locator('.tmonwin .mon-pane tr.detail').first.inner_text()
        for expected in ('Iron ore Oct/Nov', 'BUY', '0.4800', '-2.14',
                         '0.5180', '0.5700'):
            assert expected in row, f"{expected!r} missing from {row!r}"
        for expected in ('$1,300', 'E000004'):
            assert expected in detail, f"{expected!r} missing from {detail!r}"
        assert 'Trading Monitor' in page.locator('#tabs').inner_text()
        browser.close()
    assert errors == []


def test_both_sides_open_is_called_out_and_never_netted_to_flat(server):
    """Long and short at once is a close that went out as an open. Netting it
    to zero would show the desk as flat while it pays margin on both."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['portfolio']['rows'][0].update({
        'venue_qty': 0.0, 'venue_long': 5.0, 'venue_short': 5.0,
        'both_sides_open': True, 'agrees': False})
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector(POS_ROW)
        page.wait_for_timeout(900)
        row = page.locator(POS_ROW).first
        text = row.inner_text()
        assert 'LONG 5' in text and 'SHORT 5' in text
        assert 'hedged' in (row.get_attribute('class') or '')
        browser.close()
    assert errors == []


def test_an_unreadable_venue_says_so_instead_of_showing_an_empty_table(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['portfolio']['venue_readable'] = False
    snap['portfolio']['rows'][0]['venue_readable'] = False
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector(POS_ROW)
        page.wait_for_timeout(900)
        check = page.locator('.tmonwin .mon-check')
        assert 'NOT confirmation' in check.inner_text()
        assert 'could not read' in page.locator(POS_ROW).first.inner_text()
        browser.close()
    assert errors == []


def test_closing_from_the_trading_monitor_asks_and_then_sends(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector(POS_ROW)
        page.locator(POS_ROW + ' .close-pos').first.click()
        page.wait_for_selector('#modal:not(.hidden)')
        assert 'tickets' in page.locator('#modal-body').inner_text()
        page.locator('#modal-confirm').click()
        page.wait_for_timeout(400)
        sent = json.loads((tmp / 'commands.jsonl').read_text().strip().splitlines()[-1])
        assert sent['action'] == 'close_now' and sent['contract'] == 'fef'
        browser.close()
    assert errors == []


# -- hand trading on the desk ladder (its Algo Off or Dry run) ---------------------

def manual_desk(tmp, mode='MANUAL'):
    """MANUAL: the contract's Algo is Off — the hand's. ALGO: its Algo
    trades it, and the engine says hand orders on it are refused."""
    snap = json.loads((tmp / 'status.json').read_text())
    for c in snap['contracts']:
        c['algo_state'] = 'OFF' if mode == 'MANUAL' else 'PAPER'
        c['algo_on'] = mode != 'MANUAL'
        c['manual_block'] = (None if mode == 'MANUAL' else
                             'the Algo is TRADING Iron ore Oct/Nov')
    snap['engine'].update({'session': {'state': 'LOGGED_ON', 'text': 'up'},
                           'manual_terminal': {'account': 'ACC1', 'orders': [],
                                               'pnl': {'positions': []}}})
    for c in snap['contracts']:
        c['security_id'] = '777'
        c['position'] = None
    snap['portfolio']['rows'] = []
    (tmp / 'status.json').write_text(json.dumps(snap))


def test_manual_mode_ladder_buy_goes_to_the_manual_ticket_review(server):
    """MANUAL: BUY on the desk ladder asks the manual ticket for a REVIEW of
    a buy at the offer, flagged OPEN — never straight to the venue."""
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        ladder = page.locator('.ladderwin')
        page.wait_for_selector('.ladderwin.manual-on')
        assert ladder.locator('.ld-buy').is_enabled()
        assert 'Bids buys' in ladder.locator('.ld-lock').inner_text()
        assert 'ALGO OFF' in ladder.locator('.ld-lock').inner_text()
        ladder.locator('.ld-buy').click()
        page.wait_for_timeout(600)
        sent = json.loads((tmp / 'commands.jsonl').read_text().strip().splitlines()[-1])
        assert sent['action'] == 'terminal_preview'
        args = sent['args']
        assert args['side'] == 'BUY' and args['security_id'] == '777'
        assert args['open_close'] == 'O' and args['account'] == 'ACC1'
        assert args['order_type'] == 'LIMIT'
        ask = json.loads((tmp / 'status.json').read_text())['contracts'][0]['market']['ask']
        assert float(args['price']) == ask
        browser.close()
    assert errors == [e for e in errors if 'NO ANSWER' in e]


def test_a_click_in_asks_reviews_a_sell_at_that_price(server):
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        row = page.locator('.ladderwin .ld-grid tbody tr').nth(3)
        price = row.locator('td.price').inner_text()
        row.locator('td.ask').click()
        page.wait_for_timeout(600)
        sent = json.loads((tmp / 'commands.jsonl').read_text().strip().splitlines()[-1])
        assert sent['action'] == 'terminal_preview'
        assert sent['args']['side'] == 'SELL'
        assert float(sent['args']['price']) == float(price)
        browser.close()


def test_algo_mode_ladder_takes_no_hand_order(server):
    """The control: in ALGO mode the same ladder's order controls are off and
    a click in the book sends nothing."""
    url, tmp = server
    manual_desk(tmp, mode='ALGO')
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin .ld-grid tbody tr')
        page.wait_for_timeout(600)
        ladder = page.locator('.ladderwin')
        assert 'manual-on' not in (ladder.get_attribute('class') or '')
        assert not ladder.locator('.ld-buy').is_enabled()
        ladder.locator('.ld-grid tbody tr').nth(3).locator('td.ask').click()
        page.wait_for_timeout(400)
        log = tmp / 'commands.jsonl'
        assert not log.exists() or 'terminal_preview' not in log.read_text()
        browser.close()
    assert errors == []


# -- the Analysis window ---------------------------------------------------

def with_analysis(tmp_path, report=None, desk=None):
    """A config and a database the analysis routes can actually read."""
    from fixtrader.config import ContractConfig, TraderConfig
    from fixtrader.database import Database
    from fixtrader.models import ExitReason, Position, Side, TouchEvent, TouchState
    from tests.conftest import ALGO_TEST_SETTINGS
    from datetime import timedelta

    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'a.db')
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        quantity=5, commission_per_contract=1.0, slippage_budget_ticks=0.5,
        entry_threshold=2.0,
        # The replay runs the contract's OWN Algo over the recorded mids:
        # one-minute candles, and a margin for the target to be a % of.
        margin_per_contract=260.0, profit_target_pct=1.0, stop_loss_pct=4.0,
        **ALGO_TEST_SETTINGS)
    cfg.save()

    db = Database(str(tmp_path / 'a.db'))
    base = datetime.now(timezone.utc) - timedelta(hours=4)
    for i in range(12):
        db.save_position(Position(
            contract_key='fef', side=Side.SELL, qty=0.0, opened_qty=5.0,
            avg_price=0.693, opened_at=base + timedelta(minutes=i * 10),
            closed_at=base + timedelta(minutes=i * 10 + 45),
            entry_z=2.14 + i * 0.01, exit_z=0.31, exit_price=0.6465,
            margin_locked=1300.0, exit_reason=ExitReason.TARGET,
            gross_pnl=60.0, fees_paid=19.0, net_pnl=41.0,
            pnl_pct_on_margin=3.15))
    # a level that reverts often but cannot pay, and one that can
    for i in range(8):
        db.save_touch(TouchEvent(contract_key='fef', ts=base, level=1.0,
                                 direction='UP', std=0.08,
                                 state=TouchState.REVERTED,
                                 seconds_to_revert=300.0, adverse_sigma=0.9))
    for i in range(6):
        db.save_touch(TouchEvent(contract_key='fef', ts=base, level=2.0,
                                 direction='UP', std=0.08,
                                 state=TouchState.REVERTED,
                                 seconds_to_revert=120.0, adverse_sigma=0.4,
                                 became_trade=(i < 3)))
    db.save_touch(TouchEvent(contract_key='fef', ts=base, level=3.0,
                             direction='UP', std=0.08,
                             state=TouchState.UNRESOLVED))

    # Fills carrying MEASURED slippage, so the costs panel has something to
    # compare the budget against — and one that could not be priced, which
    # must be counted separately rather than averaged in as zero.
    # Recorded mids for the replay card to run over: a mean-reverting series
    # with a fixed seed, so what it finds is repeatable rather than lucky.
    import random
    rng = random.Random(7)
    px, rows = 0.60, []
    for i in range(900):
        px += (0.60 - px) * 0.3 + rng.gauss(0, 0.03)
        rows.append((base - timedelta(hours=12) + timedelta(minutes=i),
                     round(px, 4)))
    db.save_samples('fef', rows)

    from fixtrader.models import Fill
    for i in range(6):
        db.save_fill(Fill(venue='SIM', exec_id=f'E{i:03d}', clordid='FT-1',
                          contract_key='fef', side=Side.SELL, qty=5.0,
                          price=0.693, fees=19.0,
                          our_ts=base + timedelta(minutes=i),
                          slippage_ticks=0.9))
    db.save_fill(Fill(venue='SIM', exec_id='E999', clordid='FT-1',
                      contract_key='fef', side=Side.SELL, qty=5.0,
                      price=0.693, our_ts=base, slippage_ticks=None))
    return cfg


@pytest.fixture
def analysis_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with_analysis(tmp_path)
    snap = dict(SNAPSHOT, ts=datetime.now(timezone.utc).isoformat())
    (tmp_path / 'status.json').write_text(json.dumps(snap))
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 'status.json'),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/", tmp_path
    srv.shutdown()


def open_analysis(p, url, errors, mode='live'):
    """The Analysis window, on a chosen mode.

    The window OPENS on what the engine is — a desk running the simulator
    shown "Live only" is shown an empty window for the session it just
    watched. The fixture's rows are live, so these tests say so.
    """
    browser, page = open_page(p, url, errors)
    page.wait_for_selector('.win[data-key="__analysis__"]')
    page.select_option('.an-mode', mode)
    page.wait_for_timeout(400)
    page.wait_for_selector('.an-tiles .tile')
    return browser, page


def test_the_analysis_window_shows_the_tiles_and_the_journal(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(800)
        assert page.locator('.an-tiles .tile').count() == 9
        tiles = page.locator('.an-tiles').inner_text()
        assert '12' in tiles                       # trades
        assert '100.0%' in tiles                   # win rate
        assert page.locator('.an-journal tbody tr').count() == 12
        # the journal carries the z each decision fired at
        assert '+2.14' in page.locator('.an-journal').inner_text()
        browser.close()
    assert errors == []


def test_a_level_that_cannot_cover_its_costs_is_shown_but_marked(analysis_server):
    """It reverts more often than any other — and it is the one that cannot
    pay for the trade. Both facts have to be on the screen at once."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(800)
        rows = page.locator('.an-touches tbody tr')
        assert rows.count() >= 2
        # The unmeasured fill must be counted, not averaged in as zero.
        assert 'unmeasured' in page.locator('.an-costs').inner_text()
        finding = page.locator('.an-touch-finding').inner_text()
        assert 'covers the round trip' in finding or 'No level covers' in finding
        browser.close()
    assert errors == []


def test_an_unresolved_touch_is_reported_separately_on_the_screen(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(800)
        assert 'unresolved' in page.locator('.an-foot').inner_text()
        browser.close()
    assert errors == []


def test_the_cost_finding_offers_a_correction_and_does_not_apply_it(analysis_server):
    """Nothing in this system applies its own findings."""
    url, tmp = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(900)
        finding = page.locator('.an-cost-finding')
        assert not finding.is_hidden()
        text = finding.inner_text()
        # It names both numbers and proposes the measured one.
        assert '0.50' in text and '0.90' in text
        assert 'Set the budget to 0.90' in text
        # ...and the contract still holds what it was configured with.
        from fixtrader.config import TraderConfig
        assert TraderConfig.from_file(str(tmp / 'config.json')) \
            .contracts['fef'].overrides['slippage_budget_ticks'] == 0.5

        # pressing it applies the correction, once, deliberately
        page.locator('.an-cost-finding button').click()
        page.wait_for_timeout(900)
        assert TraderConfig.from_file(str(tmp / 'config.json')) \
            .contracts['fef'].overrides['slippage_budget_ticks'] == 0.9
        browser.close()
    assert errors == []


def test_the_all_contracts_tab_withholds_a_verdict_on_thin_evidence(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.locator('.an-tabs button', has_text='All contracts').click()
        page.wait_for_selector('.an-desk-table tbody tr')
        page.wait_for_timeout(600)
        text = page.locator('.an-desk').inner_text()
        assert 'too few to judge' in text
        assert 'blended' in text                   # the total row says so
        browser.close()
    assert errors == []


def test_simulated_trades_are_not_shown_in_a_live_figure(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(700)
        assert page.locator('.an-journal tbody tr').count() == 12
        page.select_option('.an-mode', 'sim')      # a chosen filter stands
        # Waited for, not slept through: a fixed pause passes alone and
        # fails beside its neighbours, which makes it noise.
        page.wait_for_function(
            "() => document.querySelector('.an-journal')"
            ".innerText.includes('no closed trades')", timeout=10000)
        # the fixture's trades are all live, so simulated-only is empty
        assert 'no closed trades' in page.locator('.an-journal').inner_text()
        browser.close()
    assert errors == []


# -- windows that overlap --------------------------------------------------

def overlap_the_windows(page):
    """Put Analysis squarely on top of Positions, as a desk ends up after a
    few drags."""
    page.evaluate("""() => {
      document.getElementById('desktop').classList.add('free');
      const place = (k, x, y) => {
        const e = document.querySelector('.win[data-key="' + k + '"]');
        if (!e) return;
        e.classList.add('placed'); e.style.left = x + 'px'; e.style.top = y + 'px';
      };
      place('__positions__', 0, 0);
      place('__analysis__', 430, 0);
      document.querySelectorAll('.win.contractwin').forEach((e) => {
        e.classList.add('placed'); e.style.left = '0px'; e.style.top = '900px';
      });
    }""")
    page.wait_for_timeout(400)


def test_a_sticky_table_header_does_not_punch_through_the_window_above_it(analysis_server):
    """A child with a z-index is placed against the whole page unless its
    window is its own stacking context. The Positions column headers were
    painting straight across the middle of the Analysis window."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        overlap_the_windows(page)
        covered = page.evaluate("""() => {
          const pos = document.querySelector('.win[data-key="__positions__"]');
          const an = document.querySelector('.win[data-key="__analysis__"]');
          const anR = an.getBoundingClientRect();
          // a header cell that genuinely lies underneath the Analysis window
          const cell = Array.from(pos.querySelectorAll('table.grid th, table.mon th'))
            .map((t) => ({t, r: t.getBoundingClientRect()}))
            .find((o) => o.r.x > anR.x + 20 && o.r.y > anR.y && o.r.bottom < anR.bottom);
          if (!cell) return {found: false};
          const top = document.elementFromPoint(
            Math.round(cell.r.x + cell.r.width / 2),
            Math.round(cell.r.y + cell.r.height / 2));
          return {found: true, punchesThrough: pos.contains(top),
                  coveredByAnalysis: an.contains(top)};
        }""")
        assert covered['found'], "the windows did not overlap; nothing was tested"
        assert covered['punchesThrough'] is False
        assert covered['coveredByAnalysis'] is True
        browser.close()
    assert errors == []


def test_clicking_a_window_brings_it_to_the_front(analysis_server):
    """A desk of draggable windows with a fixed paint order has one you can
    never read."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        overlap_the_windows(page)
        point = page.evaluate("""() => {
          const bar = document.querySelector('.win[data-key="__positions__"] .titlebar')
            .getBoundingClientRect();
          return [Math.round(bar.x + 30), Math.round(bar.y + 7)];
        }""")
        page.mouse.click(point[0], point[1])
        page.wait_for_timeout(300)
        after = page.evaluate("""() => {
          const pos = document.querySelector('.win[data-key="__positions__"]');
          const r = pos.getBoundingClientRect();
          const top = document.elementFromPoint(Math.round(r.right - 100),
                                                Math.round(r.y + 50));
          return {raised: pos.classList.contains('raised'),
                  onTop: pos.contains(top)};
        }""")
        assert after['raised'] is True
        assert after['onTop'] is True
        browser.close()
    assert errors == []


# -- one contract's settings, behind the gear ------------------------------

def open_config(page):
    page.locator('.contractwin .cog').click()
    page.wait_for_selector('.cfgwin')
    page.wait_for_selector('.cfgwin .cf-row')
    return page.locator('.cfgwin')


def test_the_gear_opens_this_contracts_settings(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        cfg = open_config(page)
        assert cfg.locator('.title').inner_text() == 'Iron ore Oct/Nov'
        # its OWN setting is in the box, and marked as its own
        box = cfg.locator('#cf-entry_threshold')
        assert box.input_value() == '2'
        assert 'own' in (cfg.locator('.cf-row:has(#cf-entry_threshold) .cf-eff')
                         .get_attribute('class'))
        # a field it does not set is BLANK, with the desk default in grey
        blank = cfg.locator('#cf-max_entry_z')
        assert blank.input_value() == ''
        eff = cfg.locator('.cf-row:has(#cf-max_entry_z) .cf-eff')
        assert eff.inner_text() == '3.5'
        assert 'own' not in (eff.get_attribute('class') or '')
        browser.close()
    assert errors == []


def test_a_blank_box_clears_an_override_and_zero_sets_one(analysis_server):
    """The rule the panel turns on: blank means 'use the desk default', and
    0 is a real number. Confusing the two is how a contract quietly ends up
    with no cooldown, or with a target of break-even."""
    from fixtrader.config import TraderConfig
    url, tmp = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        cfg = open_config(page)
        cfg.locator('#cf-entry_threshold').fill('')       # back to the default
        cfg.locator('#cf-max_entry_z').fill('0')          # a real number
        cfg.locator('.cfg-save').click()
        page.wait_for_timeout(600)

        saved = TraderConfig.from_file(str(tmp / 'config.json'))
        overrides = saved.contracts['fef'].overrides
        assert overrides.get('entry_threshold') is None
        assert overrides.get('max_entry_z') == 0
        # and the panel now reads back what was actually saved
        assert cfg.locator('#cf-entry_threshold').input_value() == ''
        assert cfg.locator('#cf-max_entry_z').input_value() == '0'
        browser.close()
    assert errors == []


def test_every_group_of_settings_renders(analysis_server):
    """The comprehensiveness is the point — a desk that cannot set a
    contract's own costs trades eight instruments on one's assumptions."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        cfg = open_config(page)
        for group, probe in [('Exit', '#cf-margin_per_contract'),
                             ('Exit by side', '#cf-profit_target_pct_hl'),
                             ('Book', '#cf-max_book_spread_ticks'),
                             ('Size & risk', '#cf-max_position'),
                             ('Execution', '#cf-exit_on_timeout'),
                             ('Costs', '#cf-commission_per_contract'),
                             ('Display', '#cf-decimals')]:
            cfg.locator('.cfg-tabs button', has_text=re.compile('^' + re.escape(group) + '$')).click()
            page.wait_for_selector('.cfgwin ' + probe)
            if group == 'Exit by side':
                # blank is "same as both", said in the box and the select
                assert 'same as both' in cfg.locator('.cfg-body').inner_text()
        browser.close()
    assert errors == []


def test_a_setting_that_is_saved_but_not_in_force_says_so(server):
    """A setting that looks saved and is not is worse than one that plainly
    needs the engine bounced. The engine names them; the banner reads them."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['engine']['config_restart_needed'] = ['fef.tick_value', 'DATABASE_PATH']
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('#restart-banner:not(.hidden)')
        text = page.locator('#restart-banner').inner_text()
        assert 'fef.tick_value' in text and 'DATABASE_PATH' in text
        # the control: nothing waiting, nothing shown
        snap['engine']['config_restart_needed'] = []
        (tmp / 'status.json').write_text(json.dumps(snap))
        page.wait_for_function(
            "document.getElementById('restart-banner')"
            ".classList.contains('hidden')")
        browser.close()
    assert errors == []


# -- what a different threshold would have done ----------------------------

def test_the_replay_card_reads_the_recording_and_shows_its_assumptions(
        analysis_server):
    """The touch table says which level reverts. This says which level PAID,
    and it prints what it does not know underneath — a backtest whose
    assumptions are not on the page is one somebody will quote without
    them."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_selector('.an-replay tbody tr')
        rows = page.locator('.an-replay tbody tr')
        assert rows.count() >= 4
        note = page.locator('.an-replay-assumptions').inner_text()
        assert 'signal' in note and 'not a fill simulator' in note
        assert 'not recorded' in note            # the book was assumed
        assert 'BUDGET' in note or 'budget' in note
        browser.close()
    assert errors == []


def test_a_replay_with_nothing_recorded_says_so_rather_than_showing_zeros(
        analysis_server, tmp_path):
    """Zero because nothing was recorded and zero because nothing paid are
    different statements."""
    url, tmp = analysis_server
    from fixtrader.database import Database
    db = Database(str(tmp / 'a.db'))
    with db._connect() as conn:
        conn.execute("DELETE FROM samples")
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(1200)
        finding = page.locator('.an-replay-finding').inner_text()
        assert 'Nothing to replay' in finding
        assert page.locator('.an-replay tbody tr').count() == 0
        browser.close()
    assert errors == []


# -- marking the window ----------------------------------------------------

def set_snapshot(tmp, change):
    """Rewrite status.json with one change applied to the first contract."""
    snap = json.loads((tmp / 'status.json').read_text())
    change(snap['contracts'][0])
    snap['ts'] = datetime.now(timezone.utc).isoformat()
    (tmp / 'status.json').write_text(json.dumps(snap))


def test_an_open_position_holds_the_window_blue(server):
    """A STATE, read off the snapshot — so it is still right after a reload
    and it cannot get stuck on a contract that is flat."""
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        win = page.locator('.contractwin')
        page.wait_for_selector('.contractwin.in-position')

        set_snapshot(tmp, lambda c: c.update(position=None))   # the control
        page.wait_for_function(
            "() => !document.querySelector('.contractwin')"
            ".classList.contains('in-position')")
        assert 'in-position' not in (win.get_attribute('class') or '')
        browser.close()
    assert errors == []


def test_a_close_in_profit_flashes_green_and_a_loss_flashes_red(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        win = page.locator('.contractwin')
        page.wait_for_timeout(700)          # the first snapshot is not an event

        set_snapshot(tmp, lambda c: c.update(
            position=None,
            last_close={'seq': 1, 'net': 41.0, 'side': 'BUY', 'qty': 5,
                        'price': 0.52, 'reason': 'TARGET', 'ts': None}))
        page.wait_for_selector('.contractwin.closed-up')

        set_snapshot(tmp, lambda c: c.update(
            position=None,
            last_close={'seq': 2, 'net': -60.0, 'side': 'BUY', 'qty': 5,
                        'price': 0.44, 'reason': 'STOP_LOSS', 'ts': None}))
        page.wait_for_selector('.contractwin.closed-down')
        assert 'closed-up' not in (win.get_attribute('class') or '')
        browser.close()
    assert errors == []


def test_a_close_that_made_nothing_measurable_is_neither_green_nor_red(server):
    """Unmeasured is not zero, and it is certainly not a loss. Colouring a
    null red would state a loss the system never measured — and a net of
    exactly zero is not a win."""
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(700)
        for seq, net in ((1, None), (2, 0.0)):
            set_snapshot(tmp, lambda c, s=seq, n=net: c.update(
                position=None,
                last_close={'seq': s, 'net': n, 'side': 'BUY', 'qty': 5,
                            'price': 0.5, 'reason': 'TARGET', 'ts': None}))
            page.wait_for_selector('.contractwin.closed-flat')
            cls = page.locator('.contractwin').get_attribute('class') or ''
            assert 'closed-up' not in cls and 'closed-down' not in cls
            page.wait_for_function(
                "() => !document.querySelector('.contractwin')"
                ".classList.contains('closed-flat')", timeout=15000)
        browser.close()
    assert errors == []


def test_opening_the_page_does_not_flash_a_trade_that_already_happened(server):
    """Otherwise a desk that reloads after lunch gets eight windows flashing
    at once for closes nobody was watching."""
    url, tmp = server
    set_snapshot(tmp, lambda c: c.update(
        position=None,
        last_close={'seq': 7, 'net': -60.0, 'side': 'BUY', 'qty': 5,
                    'price': 0.44, 'reason': 'STOP_LOSS', 'ts': None}))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(1500)
        cls = page.locator('.contractwin').get_attribute('class') or ''
        assert 'closed-down' not in cls and 'closed-up' not in cls
        # the control: the NEXT close does flash
        set_snapshot(tmp, lambda c: c.update(
            last_close={'seq': 8, 'net': -60.0, 'side': 'BUY', 'qty': 5,
                        'price': 0.44, 'reason': 'STOP_LOSS', 'ts': None}))
        page.wait_for_selector('.contractwin.closed-down')
        browser.close()
    assert errors == []


def test_the_flash_fades_rather_than_leaving_the_window_coloured(server):
    """A window left red says "this is losing", which is a different
    statement from "the last trade lost"."""
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(700)
        set_snapshot(tmp, lambda c: c.update(
            position=None,
            last_close={'seq': 3, 'net': 41.0, 'side': 'BUY', 'qty': 5,
                        'price': 0.52, 'reason': 'TARGET', 'ts': None}))
        page.wait_for_selector('.contractwin.closed-up')
        page.wait_for_function(
            "() => !document.querySelector('.contractwin')"
            ".classList.contains('closed-up')", timeout=15000)
        browser.close()
    assert errors == []


def test_text_size_and_window_size_are_the_traders_and_are_kept(server):
    """A- / A+ scale every font on the desk, and a window dragged to a size
    keeps it across a reload. Neither may lay one window over another."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        size = lambda: page.evaluate(
            "parseFloat(getComputedStyle(document.querySelector('.win .title')).fontSize)")
        before = size()
        page.locator('#text-bigger').click()
        page.locator('#text-bigger').click()
        assert size() > before * 1.1
        assert 'Text ' in page.inner_text('#text-size')

        # windows in the grid do not overlap, however large the text
        boxes = [page.locator(f'.win[data-key="{k}"]').bounding_box()
                 for k in ('fef', '__analysis__')]
        assert boxes[0]['y'] + boxes[0]['height'] <= boxes[1]['y'] + 1

        # a size set by the trader survives a reload
        page.evaluate("""() => { const w = document.querySelector('.win[data-key="fef"]');
                                w.style.width = '520px'; w.style.height = '380px'; }""")
        page.wait_for_timeout(600)                  # the observer's debounce
        page.reload(wait_until='domcontentloaded')
        page.wait_for_selector('.win[data-key="fef"]')
        box = page.locator('.win[data-key="fef"]').bounding_box()
        assert abs(box['width'] - 520) < 2 and abs(box['height'] - 380) < 2
        assert size() > before * 1.1               # and so does the text size
        browser.close()
    assert errors == []


def test_the_slippage_card_reports_what_was_measured(analysis_server):
    """The Analysis window's slippage card. The fixture's trades were
    recorded without a decision price: every entry is UNMEASURED, said in
    its own column — never a mean of 0.00."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.locator('.an-mode').select_option('live')
        page.wait_for_function(
            "document.querySelectorAll('.an-slip tbody tr').length > 0")
        first = page.locator('.an-slip tbody tr').first.inner_text()
        assert 'Entries' in first and 'unmeasured' in first
        assert '0.00 t' not in first
        assert '/api/slippage.csv' in page.locator('.an-slip-csv').get_attribute('href')
        browser.close()
    assert errors == []


def test_the_algo_window_shows_todays_slippage_beside_its_budget(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    algo = snap['contracts'][0]['algo']
    algo['day'].update(slip_sides=4, slip_ticks=2.0, slip_money=10.0,
                       slip_unmeasured=0)
    algo['slip_budget_ticks'] = 0.5
    algo['positions'][0]['entry_slip_ticks'] = 1.0
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)
        row = page.locator('.aw-kv', has=page.locator(
            'span', has_text='Slippage today'))
        assert row.locator('b').inner_text() == '+0.50 t/side · $10.00'
        assert 'budget of 0.5' in row.get_attribute('title')
        slip = page.locator('.aw-kv', has=page.locator(
            'span', has_text='Entry slip')).locator('b').inner_text()
        assert slip == '+1.00 t'
        browser.close()
    assert errors == []


def test_the_execution_button_says_PAPER_or_LIVE_and_the_waiver(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['engine']['execution'] = {'mode': 'PAPER', 'can_live': True,
                                   'positions': {'status': 'unavailable',
                                                 'why': 'TT did not answer'},
                                   'positions_waived': False}
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        button = page.locator('#execution-toggle')
        page.wait_for_function(
            "!document.getElementById('execution-toggle').classList.contains('hidden')")
        assert button.inner_text() == 'Orders: PAPER'
        assert 'TT did not answer' in button.get_attribute('title')
        # On a UAT venue sending is called UAT — "LIVE" is a live market.
        snap['engine']['execution'].update(mode='LIVE', positions_waived=True,
                                           send_word='UAT')
        (tmp / 'status.json').write_text(json.dumps(snap))
        page.wait_for_function(
            "document.getElementById('execution-toggle').classList.contains('live')")
        assert button.inner_text() == 'Orders: UAT'
        assert 'could not be read' in page.locator('#engine-banner').inner_text()
        browser.close()
    assert errors == []


def test_the_algo_windows_ladder_button_brings_the_ladder_back(server):
    """No chart button: the Algo window opens its contract's LADDER — from
    minimised or closed — and there is ONE settings gear per contract, on the
    Algo window, not a second one on the ladder."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        win = page.locator('.contractwin')
        assert win.locator('.winbtn.chart').count() == 0
        assert page.locator('.ladderwin .cog, .ladderwin .ld-cog').count() == 0
        assert win.locator('.winbtn.cog').count() == 1
        ladder = page.locator('.ladderwin')
        ladder.locator('.min').click()
        assert not ladder.is_visible()
        win.locator('.open-ladder').click()
        assert page.locator('.ladderwin').is_visible()
        page.locator('.ladderwin .close').click()
        page.wait_for_timeout(300)
        assert page.locator('.ladderwin').count() == 0
        win.locator('.open-ladder').click()
        page.wait_for_selector('.ladderwin')
        assert page.locator('.ladderwin').is_visible()
        browser.close()
    assert errors == []


def test_a_trading_algo_locks_its_ladder_and_says_how_to_unlock_it(server):
    """The Algo trading THIS contract is what takes it from the hand — the
    banner says so on the ladder, and points at the switch on it."""
    url, tmp = server
    manual_desk(tmp, mode='ALGO')
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin .ld-grid tbody tr')
        page.wait_for_timeout(500)
        ladder = page.locator('.ladderwin')
        assert 'ALGO PAPER' in ladder.locator('.ld-lock').inner_text()
        assert 'manual orders are off' in ladder.locator('.ld-lock').inner_text()
        assert 'Off or Signals' in ladder.locator('.ld-lock').get_attribute('title')
        browser.close()
    assert errors == []


def test_a_dry_run_leaves_the_ladder_to_the_hand(server):
    """The control, as on the MT5 desk: a dry run shows signals and the
    trader still trades the contract by hand."""
    url, tmp = server
    manual_desk(tmp)
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0].update({'algo_state': 'DRY', 'algo_on': True})
    (tmp / 'status.json').write_text(json.dumps(snap))
    with sync_playwright() as p:
        browser, page = open_page(p, url, [])
        page.wait_for_selector('.ladderwin.manual-on')
        assert 'SIGNALS' in page.locator('.ladderwin .ld-lock').inner_text()
        assert page.locator('.ladderwin .algo-btn').inner_text().startswith('ALGO SIGNALS')
        assert page.locator('.ladderwin .ld-buy').is_enabled()
        browser.close()


def test_a_signal_alerts_once_and_a_reload_does_not_replay_it(server):
    """SIGNALS mode: a new call from the Algo is a toast (and a chime) that
    stays until dismissed — read off `signal_alert.seq`, so the alert that
    was already there when the page loaded is not said again."""
    url, tmp = server
    manual_desk(tmp)
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0].update({'algo_state': 'DRY', 'algo_on': True,
        'signal_alert': {'seq': 1, 'kind': 'ENTRY', 'text': 'old call', 'at': ''}})
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        page.wait_for_timeout(800)
        assert 'old call' not in page.locator('#toasts').inner_text()
        snap['contracts'][0]['signal_alert'] = {
            'seq': 2, 'kind': 'ENTRY', 'at': '',
            'text': 'H to L — SELL 1 @ 0.48 (z +2.60)'}
        (tmp / 'status.json').write_text(json.dumps(snap))
        page.wait_for_selector('#toasts .toast.SIGNAL')
        assert 'H to L — SELL 1' in page.locator('#toasts .toast.SIGNAL').inner_text()
        page.wait_for_timeout(1500)
        assert page.locator('#toasts .toast.SIGNAL').count() == 1   # once
        browser.close()
    assert errors == []


def test_signals_mode_puts_the_traders_tp_and_sl_on_the_ladder(server):
    """The trader's own position, watched in SIGNALS mode: its TP and SL
    from the Algo are on the ladder and in the Algo window, marked as theirs."""
    url, tmp = server
    manual_desk(tmp)
    snap = json.loads((tmp / 'status.json').read_text())
    c = snap['contracts'][0]
    c.update({'algo_state': 'DRY', 'algo_on': True})
    c['algo']['positions'] = [{
        'position_id': 'manual', 'manual': True, 'side': 'BUY', 'entry': 0.48,
        'quantity': 1, 'break_even': 0.49, 'tp': 0.52, 'sl': 0.45,
        'closing': 0.48, 'net_pnl': -1.0, 'tp_money': 3.0, 'sl_money': -4.0,
        'progress': 0.0, 'opened_at': 0, 'age_sec': 30}]
    snap['engine']['manual_terminal']['pnl']['positions'] = [{
        'entry_order_id': 'FTM-1', 'security_id': '777', 'side': 'BUY',
        'quantity': 1, 'entry_price': 0.48, 'floating_pnl': 0.5}]
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin .ld-grid tbody tr')
        page.wait_for_timeout(800)
        work = ' '.join(page.locator('.ladderwin td.work').all_inner_texts())
        assert 'TP' in work and 'SL' in work and 'BE' in work
        assert 'TP 0.5200' in page.locator('.ladderwin .ld-pos').inner_text()
        assert 'YOUR LONG' in page.locator('.contractwin .aw-pos').inner_text()
        browser.close()
    assert errors == []


def test_the_taskbar_buttons_hold_still_while_the_figures_change(server):
    """The loop timer changes every second, at the END of a right-aligned bar:
    without a fixed width it pushed every button sideways each time."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(600)
        moved = page.evaluate("""() => {
          const bar = document.getElementById('taskbar');
          const stat = document.getElementById('loop-stat');
          const badge = document.getElementById('link-badge');
          const where = () => [...bar.querySelectorAll('button, a')]
            .map((b) => Math.round(b.getBoundingClientRect().left));
          stat.textContent = 'loop 2ms · 0.2s'; badge.textContent = 'OK';
          const a = where();
          stat.textContent = 'loop 12.4ms · 10.5s'; badge.textContent = 'CONNECTED';
          const b = where();
          return a.filter((x, i) => x !== b[i]).length;
        }""")
        assert moved == 0
        browser.close()
    assert errors == []


def test_the_ladder_draws_the_full_book(server):
    """Depth: FULL — every level TT sends, each size at its own price, the
    touch in bold; the button reads FULL and asks TT for the top only when
    pressed. Without depth (the control) only the touch carries a size."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0]['depth'] = {
        'full': True,
        'bids': [{'price': 0.48, 'size': 25}, {'price': 0.47, 'size': 12},
                 {'price': 0.45, 'size': 40}],
        'asks': [{'price': 0.49, 'size': 25}, {'price': 0.50, 'size': 8},
                 {'price': 0.52, 'size': 30}]}
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin .ld-grid tbody tr')
        page.wait_for_timeout(600)
        sizes = page.evaluate("""() => {
          const out = {};
          document.querySelectorAll('.ladderwin .ld-grid tbody tr').forEach((tr) => {
            const px = tr.querySelector('td.price').textContent;
            const b = tr.querySelector('td.bid').textContent;
            const a = tr.querySelector('td.ask').textContent;
            if (b || a) out[px] = [b, a];
          });
          return out;
        }""")
        assert sizes == {'0.4800': ['25', ''], '0.4700': ['12', ''],
                         '0.4500': ['40', ''], '0.4900': ['', '25'],
                         '0.5000': ['', '8'], '0.5200': ['', '30']}
        btn = page.locator('.ladderwin .ld-depth')
        assert btn.inner_text() == 'Depth: FULL'
        btn.click()
        page.wait_for_timeout(400)
        sent = json.loads((tmp / 'commands.jsonl').read_text().strip().splitlines()[-1])
        assert sent['action'] == 'depth' and sent['args'] == {'on': False}
        # the control: no depth from the engine — the touch alone
        snap['contracts'][0]['depth'] = None
        (tmp / 'status.json').write_text(json.dumps(snap))
        page.wait_for_timeout(900)
        filled = page.locator('.ladderwin td.bid.has-qty, .ladderwin td.ask.has-qty').count()
        assert filled == 2
        assert page.locator('.ladderwin .ld-depth').is_disabled()
        browser.close()
    assert errors == []


def test_the_fills_tab_prices_a_manual_close_and_counts_lots_and_contracts(server):
    """Found on TT UAT: manual closing fills showed no P&L (the total was
    the Algo's alone), and "26 contracts" was the lots traded. A manual close
    is priced against the ticket it closed, with the instrument's tick value."""
    url, tmp = server
    from fixtrader.config import ContractConfig, TraderConfig
    from fixtrader.database import Database
    cfg = (TraderConfig.from_file(str(tmp / 'config.json')) if (tmp / 'config.json').exists()
           else TraderConfig(path=str(tmp / 'config.json')))
    cfg.contracts['gc'] = ContractConfig(key='gc', name='GC Dec26', symbol='GC',
                                         tick_size=0.1, tick_value=10.0, security_id='GC1')
    cfg.save()                                         # the desk contract, as the trader set it
    db = Database(str(tmp / 'fixtrader.db'))          # beside the status file
    base = {'orig_clordid': '', 'account': 'ACC', 'symbol': 'GC', 'contract_key': '',
            'qty': 1.0, 'cum_qty': 1.0, 'leaves_qty': 0.0, 'exec_type': 'F',
            'ord_status': '2', 'text': '', 'ours': 'MANUAL'}
    rows = [
        dict(base, exec_id='X1', clordid='FTM-open1', order_id='T1', tt_time='20260109-18:46:33.000',
             security_id='GC1', side='BUY', open_close='OPEN', price=4220.1, received='2026-10-09T18:46:33+00:00'),
        dict(base, exec_id='X2', clordid='FTM-close1', order_id='T2', tt_time='20260109-18:46:34.000',
             security_id='GC1', side='SELL', open_close='CLOSE', price=4218.0, received='2026-10-09T18:46:34+00:00'),
        dict(base, exec_id='X3', clordid='FTM-open2', order_id='T3', tt_time='20260109-09:39:55.000',
             security_id='ES1', symbol='ES', side='SELL', open_close='OPEN', price=7853.0,
             received='2026-10-09T09:39:55+00:00'),
        dict(base, exec_id='X4', clordid='FTM-close2', order_id='T4', tt_time='20260109-11:05:21.000',
             security_id='ES1', symbol='ES', side='BUY', open_close='CLOSE', price=7879.0,
             received='2026-10-09T11:05:21+00:00'),
    ]
    db.save_tt_fills(rows)
    es = {'security_id': 'ES1', 'tick_size': '0.25', 'tick_value': '12.50'}
    snap = json.loads((tmp / 'status.json').read_text())
    # The desk's own GC contract carries the figures the trader set; the
    # watchlist has no GC at all, and ES there still reads TT's old 25.
    snap['contracts'] = list(snap['contracts']) + [dict(
        snap['contracts'][0], key='gc', name='GC Dec26', security_id='GC1',
        tick_size=0.1, tick_value=10.0)]
    es = dict(es, tick_size='0.25')
    snap['engine']['manual_terminal'] = {
        'watchlist': [{'instrument': es, 'quote': {}}],
        'orders': [
            {'id': 'FTM-open1', 'ids': ['FTM-open1'], 'avg_price': 4220.1, 'status': 'FILLED',
             'ticket': {'security_id': 'GC1', 'side': 'BUY',
                        'instrument': {'tick_size': '1', 'tick_value': '10'}}},   # read before the units fix
            {'id': 'FTM-close1', 'ids': ['FTM-close1'], 'close_of': 'FTM-open1', 'status': 'FILLED',
             'ticket': {'security_id': 'GC1', 'side': 'SELL'}},
            {'id': 'FTM-open2', 'ids': ['FTM-open2'], 'avg_price': 7853.0, 'status': 'FILLED',
             'ticket': {'security_id': 'ES1', 'side': 'SELL',
                        'instrument': {'tick_size': '25', 'tick_value': '12.5'}}},
            {'id': 'FTM-close2', 'ids': ['FTM-close2'], 'close_of': 'FTM-open2', 'status': 'FILLED',
             'ticket': {'security_id': 'ES1', 'side': 'BUY'}},
        ], 'pnl': {'positions': []}}
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1600, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url + 'account', wait_until='domcontentloaded')
        page.locator('.mon-tabs button[data-tab="fills"]').click()
        page.wait_for_selector('.mon-pane table.fills tbody tr')
        page.wait_for_timeout(600)
        summary = page.locator('.mon-sum').inner_text()
        assert '4 lots' in summary and '2 contracts' in summary, summary
        # GC: (4218.0 - 4220.1) x $100 = -$210; ES short: (7853 - 7879) x $50 = -$1,300.
        assert '-$1,510.00' in summary, summary
        text = page.locator('.mon-pane table.fills').inner_text()
        assert '-$210.00' in text and '-$1,300.00' in text
        browser.close()
    assert errors == []


def test_the_order_review_shows_the_spread_red_when_wide(server):
    """The shared modal carries the spread line an order is reviewed against:
    plain inside the contract's limit, red over it, absent with none."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        # Opened, not awaited: ask() resolves only when it is answered.
        page.evaluate("() => { ask('Buy?', 'body', 'Send', {text: 'Spread 4218 / 4230: 12 wide', wide: true}); }")
        warn = page.locator('#modal-warn')
        assert warn.is_visible() and '12 wide' in warn.inner_text()
        assert 'wide' in (warn.get_attribute('class') or '')
        page.click('#modal-cancel')
        page.evaluate("() => { ask('Buy?', 'body', 'Send', {text: 'Spread 4219.5 / 4219.7: 0.2 wide', wide: false}); }")
        assert 'wide' not in (page.locator('#modal-warn').get_attribute('class') or '').split()
        page.click('#modal-cancel')
        page.evaluate("() => { ask('Cancel?', 'body', 'OK'); }")
        assert not page.locator('#modal-warn').is_visible()
        page.click('#modal-cancel')
        browser.close()
    assert errors == []


def test_the_feed_line_never_changes_height_and_never_wraps(server):
    """Found on TT UAT: the price stream and the page refresh each wrote the
    feed line, in two wordings of different lengths — it wrapped differently
    every half second and shifted the whole Instruments page."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1100, 'height': 800})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url + 'instruments', wait_until='domcontentloaded')
        page.wait_for_function("document.getElementById('feed-status').textContent.startsWith('Source:')")
        heights = set()
        for _ in range(10):
            heights.add(page.evaluate("document.getElementById('feed-status').offsetHeight"))
            page.wait_for_timeout(200)
        assert len(heights) == 1, heights
        assert page.evaluate("getComputedStyle(document.getElementById('feed-status')).whiteSpace") == 'nowrap'
        text = page.locator('#feed-status').inner_text()
        assert 'FIX-to-screen' in text and 'quote stream' in text
        browser.close()
    assert errors == []
