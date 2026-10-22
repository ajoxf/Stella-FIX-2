"""The UAT order tests (`python -m fixtrader.uat`), proven here end to end
before anyone runs them on TT UAT.

Everything between the runner and the wire is the production code: the web
app's /api/command and /api/snapshot, the command bridge, the engine loop
(`runner.run`), the real FIX sessions, the manual terminal and the Algo's
order router. Only TT is a stand-in (`tests/fake_tt.py`), quoting Crude in
TT's FIX units (9050 for 90.50) with its DisplayFactor, so the conversion is
tested on the same pass as the orders."""
import contextlib
import json
import threading
import time
import urllib.parse

import pytest

from fixtrader import uat
from fixtrader.config import ContractConfig, TraderConfig, VenueConfig
from tests.fake_tt import Exchange, install


class AppDriver(uat.HttpDriver):
    """`HttpDriver`, over the Flask test client instead of a socket."""

    def __init__(self, client, timeout=10.0):
        super().__init__('http://test', timeout)
        self.client = client

    def _get(self, path):
        return self.client.get(path).get_json()

    def command(self, action, contract='', args=None):
        queued = self.client.post('/api/command', json={
            'action': action, 'contract': contract, 'args': args or {}}).get_json()
        if not queued.get('ok'):
            return queued
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            res = self._get('/api/result/' + queued['id'])
            if not res.get('pending'):
                return res
            time.sleep(0.02)
        return {'ok': False, 'error': f'the engine did not answer {action}'}

    def sent(self, clordid):
        rows = self._get('/api/fix-logs?limit=500&search=' +
                         urllib.parse.quote(clordid)).get('rows', [])
        out = []
        for row in reversed(rows):
            if row.get('direction') != 'OUT':
                continue
            fields = {}
            for part in str(row.get('raw', '')).split('|'):
                tag, _, value = part.partition('=')
                fields.setdefault(tag, value)
            if clordid in (fields.get('11'), fields.get('41')):
                out.append(fields)
        return out

    def sleep(self, seconds):
        time.sleep(min(seconds, 0.05))


@pytest.fixture
def desk(tmp_path, monkeypatch):
    with open_desk(tmp_path, monkeypatch) as d:
        yield d


@contextlib.contextmanager
def open_desk(tmp_path, monkeypatch, **venue_extra):
    """The real runner and web app against the fake TT; `venue_extra`
    adds to the venue (a FIX Recovery address, say)."""
    monkeypatch.chdir(tmp_path)                       # the FIX log, logs/fix
    monkeypatch.setenv('UAT_OR_PW', 'or-secret')
    monkeypatch.setenv('UAT_MD_PW', 'md-secret')
    tt = Exchange(accounts=('ACC1',))
    # Crude spread: 90.50 / 90.52 on the screen, 9050 / 9052 on the wire.
    tt.list('CL1', 9050, 9052, tick=1, factor=0.01, symbol='CL')
    install(monkeypatch, tt)
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'desk.db')
    cfg.venues['TT-UAT'] = VenueConfig(
        name='TT-UAT', environment='UAT', host='or.example', port=11502,
        fix_version='FIX.4.2', use_tls=False, sender_comp_id='ORDER',
        target_comp_id='TT', password_env='UAT_OR_PW', md_host='md.example',
        md_port=11503, md_sender_comp_id='MARKET', md_password_env='UAT_MD_PW',
        account='ACC1', **venue_extra)
    cfg.contracts['clz6'] = ContractConfig(
        key='clz6', name='Crude Dec/Jan', symbol='CL', venue='TT-UAT',
        security_id='CL1', security_exchange='CME', tick_size=0.01,
        tick_value=10.0, enabled=True, algo_on=False,
        margin_per_contract=1000.0)
    cfg.save()
    paths = {k: str(tmp_path / v) for k, v in (
        ('config', 'config.json'), ('status', 'status.json'),
        ('commands', 'commands.jsonl'), ('results', 'results.json'))}
    from fixtrader import runner
    stop = threading.Event()
    thread = threading.Thread(target=runner.run, kwargs=dict(
        config_path=paths['config'], status_path=paths['status'],
        command_path=paths['commands'], result_path=paths['results'],
        should_stop=stop.is_set), daemon=True)
    thread.start()
    from fixtrader.webapp import create_app
    app = create_app(paths['config'], paths['status'], paths['commands'],
                     paths['results'])
    driver = AppDriver(app.test_client())
    deadline = time.monotonic() + 15
    contract = {}
    while time.monotonic() < deadline:
        snap = driver.snapshot()
        contract = next(iter(snap.get('contracts') or []), {})
        if (contract.get('market') or {}).get('bid') is not None:
            break
        time.sleep(0.1)
    assert (contract.get('market') or {}).get('bid') is not None, contract
    try:
        yield SimpleDesk(tt, driver)
    finally:
        stop.set()
        thread.join(timeout=20)


class SimpleDesk:
    def __init__(self, tt, driver):
        self.tt = tt
        self.d = driver

    def runner(self, **kw):
        return uat.Runner(self.d, 'clz6', wait=10.0, hit_wait=10.0,
                          log=lambda s: None, **kw)


def passed(results):
    bad = [r for r in results if r['status'] != 'PASS']
    assert not bad, json.dumps(results, indent=1)


def test_prices_reach_the_screen_in_trader_units(desk):
    c = desk.runner().contract()
    assert c['market']['bid'] == pytest.approx(90.50)
    assert c['market']['ask'] == pytest.approx(90.52)


def test_the_manual_scenarios_pass(desk):
    passed(desk.runner().run(['M3', 'M7', 'M1', 'M8', 'M2', 'M4', 'M6']))
    # Every order went out in TT's FIX units, never the screen's.
    prices = [float(o['44']) for o in desk.tt.orders_in if o.get('44')]
    assert prices and all(p > 1000 for p in prices), prices


def test_the_algo_scenarios_need_live_and_say_so(desk):
    results = desk.runner().run(['A1'])
    assert results[0]['status'] == 'FAIL' and 'TT UAT' in results[0]['detail']
    assert not desk.tt.orders_in                    # nothing sent


def test_the_algo_scenarios_pass_on_live(desk):
    armed = desk.d.command('execution', '', {'mode': 'LIVE', 'confirm': True})
    assert armed.get('ok'), armed
    passed(desk.runner().run(['A1', 'A5', 'A2', 'A6', 'A3']))
    algo = [o for o in desk.tt.orders_in if o['11'].startswith('FT-')]
    assert algo and all(o.get('1028') == 'N' for o in algo if o['35'] == 'D')


def test_a_resting_limit_fills_when_the_market_reaches_it(desk):
    """M5 / A4: a LIMIT at the bid, filled when the market trades down to it."""
    r = desk.runner()

    def trade_down():
        time.sleep(1.5)
        desk.tt.move('CL1', 9049, 9050)             # the offer reaches our bid
        time.sleep(1.5)
        desk.tt.move('CL1', 9050, 9052)
    threading.Thread(target=trade_down, daemon=True).start()
    results = r.run(['M5'])
    passed(results)
    assert results[0]['detail'].startswith('hit at')


def test_an_algo_resting_limit_fills_when_the_market_reaches_it(desk):
    assert desk.d.command('execution', '', {'mode': 'LIVE', 'confirm': True})['ok']

    def trade_down():
        time.sleep(1.5)
        desk.tt.move('CL1', 9049, 9050)
        time.sleep(1.5)
        desk.tt.move('CL1', 9050, 9052)
    threading.Thread(target=trade_down, daemon=True).start()
    results = desk.runner().run(['A4'])
    passed(results)
    assert results[0]['detail'].startswith('hit at')


def test_a_market_that_never_trades_there_is_a_skip_not_a_pass(desk):
    """The control: nobody trades at our bid — M5 says so and cleans up."""
    results = desk.runner().run(['M5'])
    assert results[0]['status'] == 'SKIP'
    assert not desk.tt.resting                      # cancelled, nothing left


# -- from the screen: the Order tests page --------------------------------------

def wait_run(client, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get('/api/order-tests').get_json()
        if not body['run'].get('running'):
            return body
        time.sleep(0.1)
    raise AssertionError('the run did not finish')


def test_the_page_runs_the_tests_and_keeps_the_run(desk):
    c = desk.d.client
    page = c.get('/order-tests').data
    assert b'Order tests' in page and b'order_tests.js' in page
    body = c.get('/api/order-tests').get_json()
    assert body['environment'] == 'UAT'
    groups = {(s['kind'], s['order_type']) for s in body['scenarios']}
    assert groups == {('manual', 'market'), ('manual', 'limit'),
                      ('algo', 'market'), ('algo', 'limit')}
    assert all(s['steps'] and s['expect'] for s in body['scenarios'])  # what to do, what to see
    # Not without the trader's word.
    refused = c.post('/api/order-tests/run', json={'ids': ['M1'], 'contract': 'clz6'})
    assert refused.status_code == 400 and not desk.tt.orders_in
    # M5 waits (3 s here) for a market that never trades at our bid: the run
    # is still going when the second one is asked for.
    ok = c.post('/api/order-tests/run', json={'ids': ['M5', 'M1', 'M3'], 'contract': 'clz6',
                                              'qty': 1, 'hit_wait': 3, 'confirm': True}).get_json()
    assert ok['ok'], ok
    again = c.post('/api/order-tests/run', json={'ids': ['M1'], 'contract': 'clz6',
                                                 'confirm': True})
    assert again.status_code == 409                          # one run at a time
    body = wait_run(c)
    assert [r['status'] for r in body['last']['results']] == ['SKIP', 'PASS', 'PASS'], body['last']
    assert body['last']['log']


def test_the_page_refuses_a_live_venue(desk, monkeypatch):
    """On PROD nothing is sent: the steps are for doing it by hand."""
    import fixtrader.webapp as webapp
    from fixtrader import atomicfile
    real = atomicfile.read_json

    def as_prod(path, default=None):
        snap = real(path, default)
        if snap and str(path).endswith('status.json'):
            snap = dict(snap, engine=dict(snap.get('engine') or {}, environment='PROD'))
        return snap
    monkeypatch.setattr(webapp.atomicfile, 'read_json', as_prod)
    r = desk.d.client.post('/api/order-tests/run', json={
        'ids': ['M1'], 'contract': 'clz6', 'confirm': True})
    assert r.status_code == 409 and 'UAT only' in r.get_json()['error']
    assert not desk.tt.orders_in


# -- hands-on: the Algo's own order path from the ladder, as it trades live ----

def test_an_algo_test_order_while_the_algo_trades_is_managed_like_live(desk):
    """The Order tests page's hands-on ladder, Algo on TRADE: a test order goes
    through the Algo's path, and the Algo's own take-profit closes it."""
    d = desk.d
    assert d.command('execution', '', {'mode': 'LIVE', 'confirm': True})['ok']
    # One tick wide: the spread alone ($10) does not reach the 2% stop ($20).
    desk.tt.move('CL1', 9051, 9052)
    run = desk.runner()
    run.until('the one-tick book', lambda s: run.contract(s)['market'].get('bid') == 90.51)
    r = d.command('algo_state', 'clz6', {'state': 'TRADE'})
    assert r.get('ok'), r
    sent = d.command('uat_order', 'clz6', {'side': 'BUY', 'order_type': 'MARKET', 'qty': 1})
    assert sent.get('ok'), sent
    run = desk.runner()
    pos = run.until('the Algo position', lambda s: run.algo_position(s))
    assert pos['side'] == 'BUY'
    again = d.command('uat_order', 'clz6', {'side': 'BUY', 'order_type': 'MARKET'})
    assert not again['ok'] and 'open' in again['error']       # one at a time
    desk.tt.move('CL1', 9150, 9152)                           # +1.00: past the TP
    run.until('closed by the Algo at its target', lambda s: run.algo_position(s) is None)
    closes = [o for o in desk.tt.orders_in if o.get('77') == 'C']
    assert closes and closes[-1]['11'].startswith('FT-') and closes[-1]['54'] == '2'


def test_a_test_done_by_hand_is_recorded_and_cleared(desk):
    c = desk.d.client
    page = c.get('/order-tests').data
    assert b'ot-desk' in page                                 # the hands-on desk
    assert b'id="desktop"' in c.get('/desk?embed=uat&contract=clz6').data
    r = c.post('/api/order-tests/check', json={'id': 'a1', 'result': 'pass',
                                                'contract': 'clz6'}).get_json()
    assert r['checks']['A1']['result'] == 'PASS'
    assert r['checks']['A1']['environment'] == 'UAT'
    assert c.get('/api/order-tests').get_json()['checks']['A1']['contract'] == 'clz6'
    cleared = c.post('/api/order-tests/check', json={'id': 'A1', 'result': ''}).get_json()
    assert 'A1' not in cleared['checks']
    bad = c.post('/api/order-tests/check', json={'id': 'Z9', 'result': 'PASS'})
    assert bad.status_code == 400
    assert not desk.tt.orders_in                              # a record, never an order


def test_a_quiet_uat_price_does_not_stop_the_checks():
    """Found on UAT: preflight refused because the price had not MOVED —
    'stale' — which on a quiet test market is normal. A bid and an offer is
    what the checks need; with neither, they still refuse (the control)."""
    def snap(market):
        return {'engine': {'environment': 'UAT', 'session': {'state': 'LOGGED_ON'},
                           'execution': {'mode': 'PAPER'},
                           'manual_terminal': {'orders': [], 'pnl': {'positions': []}}},
                'contracts': [{'key': 'esz6', 'security_id': 'ES1', 'algo_state': 'OFF',
                               'feed': {'stale': True}, 'market': market,
                               'tick_size': 0.25, 'decimals': 2}]}

    class Driver:
        def __init__(self, s):
            self.s = s

        def snapshot(self):
            return self.s
    ok = uat.Runner(Driver(snap({'bid': 6800.0, 'ask': 6800.25})), 'esz6', log=lambda s: None)
    assert ok.preflight(need_live=False)['key'] == 'esz6'
    none = uat.Runner(Driver(snap({'bid': None, 'ask': None})), 'esz6', log=lambda s: None)
    with pytest.raises(uat.Failed, match='no bid/offer'):
        none.preflight(need_live=False)


def test_each_flow_keeps_its_newest_result_across_runs(desk):
    c = desk.d.client
    for ids in (['M3'], ['M1']):
        assert c.post('/api/order-tests/run', json={'ids': ids, 'contract': 'clz6',
                                                    'confirm': True}).get_json()['ok']
        body = wait_run(c)
    latest = body['last']['latest']
    assert latest['M3']['status'] == 'PASS' and latest['M1']['status'] == 'PASS'
    assert [r['id'] for r in body['last']['results']] == ['M1']     # the run itself


def test_an_at_market_order_tt_cancels_unfilled_fails_at_once_and_says_why(desk):
    """A thin UAT market: the price is shown, nothing at it trades, and TT
    cancels the immediate-or-cancel order. The check says so straight away
    — not "filled — not seen in 20s"."""
    desk.tt.no_liquidity = True
    started = time.monotonic()
    results = desk.runner().run(['M3'])
    assert results[0]['status'] == 'FAIL'
    assert 'cancelled the at-market order unfilled' in results[0]['detail']
    assert 'not seen in' not in results[0]['detail']
    assert time.monotonic() - started < 8


def test_an_at_market_order_that_trades_still_passes(desk):
    """The control: the same check with liquidity at the touch."""
    passed(desk.runner().run(['M3']))


def test_clear_results_gives_a_fresh_page_and_sends_nothing(desk):
    client = desk.d.client
    assert client.post('/api/order-tests/run', json={
        'ids': ['M1'], 'contract': 'clz6', 'confirm': True}).get_json()['ok']
    wait_run(client)
    client.post('/api/order-tests/check', json={'id': 'M3', 'result': 'PASS'})
    sent = len(desk.tt.orders_in)
    body = client.get('/api/order-tests').get_json()
    assert body['last'] and body['checks']
    assert client.post('/api/order-tests/clear').get_json()['ok']
    body = client.get('/api/order-tests').get_json()
    assert not body['last'] and not body['checks']
    assert len(desk.tt.orders_in) == sent                    # nothing sent


def test_an_algo_close_written_late_to_the_fix_log_is_still_found(desk, monkeypatch):
    """The FIX log is written in the background; on a busy feed the close's
    line lands after the position is already flat. The check waits for it
    instead of failing "no closing order (77=C) found"."""
    from fixtrader.fix_audit import FixAuditLog
    append = FixAuditLog._append

    def late(self, row):
        if '77=C' in str(row.get('raw', '')) and row.get('direction') == 'OUT':
            time.sleep(1.5)
        append(self, row)
    monkeypatch.setattr(FixAuditLog, '_append', late)
    assert desk.d.command('execution', '', {'mode': 'LIVE', 'confirm': True})['ok']
    passed(desk.runner().run(['A1']))


def test_the_screen_reads_the_database_the_engine_writes(tmp_path, monkeypatch):
    """Found on TT UAT: DATABASE_PATH is relative ('fixtrader.db'). The engine
    put it beside its status file (runtime/), the screen read it from the
    program folder — an empty file: no fills on the Fills tab, and the Algo
    checks found "no closing order". Both now resolve it the same way."""
    import os
    from fixtrader.config import TraderConfig, database_path
    from fixtrader.webapp import create_app
    program = tmp_path / 'program'
    runtime = program / 'runtime'
    runtime.mkdir(parents=True)
    monkeypatch.chdir(program)
    cfg = TraderConfig(path=str(program / 'config.json'))
    cfg.settings['DATABASE_PATH'] = 'fixtrader.db'               # relative, as shipped
    cfg.save()
    status = str(runtime / 'status.json')
    assert database_path(cfg, status) == os.path.join(str(runtime), 'fixtrader.db')
    from fixtrader.database import Database
    Database(database_path(cfg, status)).save_order({
        'clordid': 'FT-a-1', 'contract_key': 'gc', 'side': 'SELL', 'qty': 1,
        'filled_qty': 1, 'order_type': 'LIMIT', 'intent': 'CLOSE',
        'state': 'FILLED', 'sent_at': '2026-10-09T20:00:00+00:00'})
    app = create_app(str(program / 'config.json'), status,
                     str(runtime / 'c.jsonl'), str(runtime / 'r.json'))
    orders = app.test_client().get('/api/journal').get_json()['orders']
    assert [o['clordid'] for o in orders] == ['FT-a-1']
    assert not (program / 'fixtrader.db').exists() or not Database(
        str(program / 'fixtrader.db')).orders()
