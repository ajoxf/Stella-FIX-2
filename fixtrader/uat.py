"""UAT order tests: every order path the desk will use in a live market,
placed for real on TT UAT and checked against what TT answers.

Run it from the screen — the ORDER TESTS page (/order-tests) — or against
the running program from a terminal:

    python -m fixtrader.uat --contract esz6            (or run_uat_tests.bat)

It drives the program the way the screen does — the same commands the ladder
and the Algo window send — so what is tested is the path a trader uses, not a
side door. Each scenario places its orders, waits for TT's answer, checks the
working order, the fill, the position and the close on this program's book,
and reads back the FIX tags it actually sent (the FIX log). It cleans up after
itself: anything of its own still working is cancelled and anything it opened
is closed, by ticket.

Manual (the ladder / manual ticket, FTM- orders):
  M1  LIMIT away from the market rests, shows as working, cancels
  M2  a resting LIMIT is replaced to a new price, then cancelled
  M3  MARKET opens; the position shows; a MARKET close by ticket (77=C) flattens
  M4  a marketable LIMIT fills; Close @ LMT rests (77=C); cancelled; market close
  M5  a LIMIT at the touch fills when the price is hit (waits; may not trade)
  M6  an order TT refuses is shown in TT's own words (tag 58)
  M7  MARKET SELL opens a short; a MARKET close by ticket (77=C) flattens
  M8  SELL LIMIT above the market rests, shows as working, cancels
Algo (its own path, FT- orders, 1028=N):
  A1  MARKET opens; the Algo's position shows with TT's tickets; CLOSE NOW
      closes it by ticket (77=C)
  A2  LIMIT away from the market rests, shows as working, cancels
  A3  a marketable LIMIT fills; Close @ LMT rests pinned (77=C); CLOSE ALL
      escalates it — cancel, then market — never two closes at once
  A4  a LIMIT at the touch fills when the price is hit (waits; may not trade)
  A5  MARKET SELL opens a short; CLOSE NOW closes it by ticket
  A6  SELL LIMIT above the market rests, shows as working, cancels

Refused anywhere but a UAT venue. Quantity 1 unless told otherwise.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

WORKING = ('PENDING', 'NEW', 'PARTIALLY_FILLED', 'REPLACED')
DONE = ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED')

SCENARIOS = [
    ('M3', 'Manual: buy at market, then CLOSE ALL'),
    ('M7', 'Manual: sell at market (short), then CLOSE ALL'),
    ('M1', 'Manual: buy limit below the market waits, then cancel'),
    ('M8', 'Manual: sell limit above the market waits, then cancel'),
    ('M2', 'Manual: change the price of a waiting limit'),
    ('M4', 'Manual: buy limit through the offer fills; take-profit limit waits; CLOSE ALL'),
    ('M5', 'Manual: buy limit at the bid fills when the market comes to it'),
    ('M6', "Manual: an order TT refuses shows TT's reason"),
    ('A1', 'Algo: buys at market, then CLOSE ALL'),
    ('A5', 'Algo: sells at market (short), then CLOSE ALL'),
    ('A2', 'Algo: buy limit below the market waits, then cancel'),
    ('A6', 'Algo: sell limit above the market waits, then cancel'),
    ('A3', 'Algo: buy limit fills; your take-profit limit waits; CLOSE ALL closes once'),
    ('A4', 'Algo: buy limit at the bid fills when the market comes to it'),
]
#: Who sends it and what kind of order: the four groups on the page.
GROUPS = {'M3': ('manual', 'market'), 'M7': ('manual', 'market'),
          'M1': ('manual', 'limit'), 'M8': ('manual', 'limit'),
          'M2': ('manual', 'limit'), 'M4': ('manual', 'limit'),
          'M5': ('manual', 'limit'), 'M6': ('manual', 'limit'),
          'A1': ('algo', 'market'), 'A5': ('algo', 'market'),
          'A2': ('algo', 'limit'), 'A6': ('algo', 'limit'),
          'A3': ('algo', 'limit'), 'A4': ('algo', 'limit')}
#: Each flow as a trader says it: what is done ...
SHORT = {
    'M3': 'Buy at market, then close',
    'M7': 'Sell at market (go short), then close',
    'M1': 'Buy limit below the market, then cancel',
    'M8': 'Sell limit above the market, then cancel',
    'M2': 'Move a waiting limit to a new price',
    'M4': 'Buy limit through the offer, then a take-profit limit',
    'M5': 'Buy limit at the bid — wait for a seller',
    'M6': 'An order TT rejects',
    'A1': 'Algo buys at market, then close',
    'A5': 'Algo sells at market (goes short), then close',
    'A2': 'Algo buy limit below the market, then cancel',
    'A6': 'Algo sell limit above the market, then cancel',
    'A3': 'Algo buy limit fills, then your take-profit limit',
    'A4': 'Algo buy limit at the bid — wait for a seller',
}
#: ... and what the trader should see happen.
EXPECT = {
    'M3': 'Fills at the offer straight away — you are long 1. CLOSE ALL sells it back: flat.',
    'M7': 'Fills at the bid straight away — you are short 1. CLOSE ALL buys it back: flat.',
    'M1': 'Waits in the book (Working orders) and does not fill. Cancel removes it.',
    'M8': 'Waits in the book above the market and does not fill. Cancel removes it.',
    'M2': 'TT confirms the new price and the order keeps waiting there. Then cancelled.',
    'M4': 'Priced 2 ticks above the offer, so it fills at once — at the offer or better. A Close @ '
          'LMT (take-profit) then waits above '
          'the market; it is cancelled and CLOSE ALL closes at market.',
    'M5': 'Waits at the bid until someone sells to it, then you are long 1 and it is closed. '
          'On a quiet market nobody may — then it is cancelled.',
    'M6': 'Sent to an account TT does not know: shows REJECTED, with TT\'s own reason.',
    'A1': 'The Algo sends a buy, as on a signal. It fills; the Algo\'s position shows with its '
          'take-profit and stop loss. CLOSE ALL closes it: flat.',
    'A5': 'The Algo sends a sell, as on a signal. It fills short; CLOSE ALL buys it back: flat.',
    'A2': 'The Algo\'s order waits in the book and does not fill. Cancel all removes it.',
    'A6': 'The Algo\'s sell waits above the market and does not fill. Cancel all removes it.',
    'A3': 'The Algo is long. Your Close @ LMT (take-profit) waits above the market. CLOSE ALL '
          'cancels it and closes at market — one close, never two.',
    'A4': 'The Algo\'s buy waits at the bid until someone sells to it, then it is closed. On a '
          'quiet market nobody may — then it is cancelled.',
}
#: The two that wait on the market to trade at a price: run with --with-hits.
HIT_SCENARIOS = ('M5', 'A4')

#: Each scenario as a trader does it BY HAND on the screen — the same checks,
#: for the live market, where nothing runs them for you.
STEPS = {
    'M1': 'Ladder (Algo Off or Signals): click a Bids price well below the market → review → Send. '
          'It shows in Work and in Trading Monitor › Working Orders (TT order id). Cancel it there; it goes.',
    'M2': 'Instruments & orders: send a LIMIT well below the market; when acknowledged, Modify its price. '
          'TT answers REPLACED at the new price (35=G, tag 44 in FIX logs). Cancel it.',
    'M3': 'Ladder: BUY (market) → review → Send. The fill and the position show (Trading Monitor › Positions). '
          'CLOSE ALL → review: a SELL flagged CLOSE (77=C) for that ticket only. Flat.',
    'M8': 'Ladder (Algo Off): click an Asks price well above the market → review → Send. It waits; cancel it.',
    'A6': 'Algo switch on UAT: click an Asks price well above the market. The Algo\'s sell waits; CXL All pulls it.',
    'M7': 'Ladder (Algo Off): Market type, SELL → review → Send. A short position shows; CLOSE ALL buys it back (77=C).',
    'M4': 'Ladder: BUY LIMIT a tick or two above the offer — fills at once. Close @ LMT at a price above the market: it RESTS (77=C). '
          'Cancel it; then CLOSE ALL closes at market.',
    'M5': 'Ladder: BUY LIMIT at the bid. It rests until the market trades there, then fills and the position shows. '
          'Close it. If the market never trades there, cancel it.',
    'M6': 'Instruments & orders: send to an account TT does not know. The order shows REJECTED in TT\'s own words (tag 58).',
    'A1': 'Hands-on ladder: Execution LIVE, Algo switch on Trades (ALGO LIVE). Market type, BUY: the Algo\'s order '
          '(FT-, 77=O, 1028=N) fills; its position shows with TT tickets (not PAPER-) and its TP / SL on the Algo window. '
          'CLOSE ALL closes it by ticket (77=C, "Close P<id>" in 58).',
    'A5': 'Algo switch on UAT. Market type, SELL: the Algo\'s order fills short; CLOSE ALL closes it by ticket.',
    'A2': 'Hands-on ladder, ALGO LIVE: click a Bids price well below the market — the Algo\'s LIMIT rests (Work column, '
          'Working Orders, FT- id). Cancel all (CXL All) pulls it.',
    'A3': 'Hands-on ladder, ALGO LIVE: BUY; then Close @ LMT above the market — it rests PINNED (77=C). CLOSE ALL '
          'cancels it, and only on TT\'s CANCELLED sends ONE market close — never two closes at once.',
    'A4': 'Hands-on ladder, ALGO LIVE: click the bid in Bids — the Algo\'s LIMIT rests and fills when the market '
          'trades there; then the Algo\'s TP / SL manage it (or CLOSE ALL).',
}


class Failed(Exception):
    """A check that did not hold — said in words, with the evidence."""


# -- talking to the running program ------------------------------------------------

class HttpDriver:
    """The running program's web process, as the screen talks to it."""

    def __init__(self, base='http://127.0.0.1:8000', timeout=25.0):
        self.base = base.rstrip('/')
        self.timeout = timeout

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))

    def command(self, action, contract='', args=None):
        body = json.dumps({'action': action, 'contract': contract,
                           'args': args or {}}).encode('utf-8')
        req = urllib.request.Request(self.base + '/api/command', data=body,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                queued = json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode('utf-8') or '{}') or {
                'ok': False, 'error': f'HTTP {e.code}'}
        if not queued.get('ok'):
            return queued
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            res = self._get('/api/result/' + queued['id'])
            if not res.get('pending'):
                return res
            time.sleep(0.05)
        return {'ok': False, 'error': f'the engine did not answer {action}'}

    def snapshot(self):
        return self._get('/api/snapshot')

    def journal(self):
        return self._get('/api/journal')

    def sent(self, clordid):
        """The messages this program SENT carrying `clordid` (11 or 41),
        oldest first, as tag -> value (the FIX log, passwords redacted)."""
        rows = self._get('/api/fix-logs?limit=200&search=' +
                         urllib.request.quote(clordid)).get('rows', [])
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
        time.sleep(seconds)


class ClientDriver(HttpDriver):
    """`HttpDriver` over a Flask test client — the web process running the
    tests on itself (the Order tests page), through its own /api routes."""

    def __init__(self, client, timeout=25.0, nap=None):
        super().__init__('http://local', timeout)
        self.client = client
        self.nap = nap

    def _get(self, path):
        return self.client.get(path).get_json()

    def command(self, action, contract='', args=None):
        queued = self.client.post('/api/command', json={
            'action': action, 'contract': contract, 'args': args or {}}).get_json() or {}
        if not queued.get('ok'):
            return queued
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            res = self._get('/api/result/' + queued['id'])
            if not res.get('pending'):
                return res
            time.sleep(0.03)
        return {'ok': False, 'error': f'the engine did not answer {action}'}

    def sleep(self, seconds):
        time.sleep(seconds if self.nap is None else min(seconds, self.nap))


class HandChecks:
    """What the trader proved BY HAND on the hands-on ladder, test by test —
    PASS or FAIL with a note, the contract, the venue and when. Kept on disk
    beside the automatic run."""

    def __init__(self, path: str):
        self.path = path
        self.lock = __import__('threading').Lock()

    def all(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def mark(self, sid: str, result: str, note: str = '', contract: str = '',
             environment: str = '') -> Dict[str, Any]:
        with self.lock:
            checks = self.all()
            if result:
                checks[sid] = {'result': result, 'note': note[:300],
                               'contract': contract, 'environment': environment,
                               'at': time.strftime('%Y-%m-%d %H:%M:%S')}
            else:
                checks.pop(sid, None)
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(checks, f, indent=1)
            return checks


class OrderTestRun:
    """One run of the tests from the screen, on a thread of the web
    process: what is running, what has passed, and the last run kept on disk
    so it is still there after a restart — and on a live venue, where these
    tests are refused, as the record of what was proven in UAT."""

    def __init__(self, results_path: str):
        self.results_path = results_path
        self.lock = __import__('threading').Lock()
        self.thread = None
        self.stop_requested = False
        self.state: Dict[str, Any] = {'running': False}

    def last(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.results_path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def status(self) -> Dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def clear(self) -> Dict[str, Any]:
        """Forget every recorded result — a fresh page. Nothing is sent and
        nothing at TT changes; refused while a run is going."""
        with self.lock:
            if self.state.get('running'):
                return {'ok': False, 'error': 'a test run is going — stop it first'}
            try:
                os.remove(self.results_path)
            except FileNotFoundError:
                pass
            self.state = {'running': False}
        return {'ok': True, 'text': 'results cleared'}

    def stop(self) -> Dict[str, Any]:
        with self.lock:
            if not self.state.get('running'):
                return {'ok': False, 'error': 'no test run is going'}
            self.stop_requested = True
        return {'ok': True, 'text': 'stopping after the test that is running — '
                'it cleans up what it sent first'}

    def start(self, driver, contract: str, ids: List[str], qty: float = 1,
              away: int = 20, hit_wait: float = 120.0,
              environment: str = '') -> Dict[str, Any]:
        import threading
        with self.lock:
            if self.state.get('running'):
                return {'ok': False, 'error': 'a test run is already going'}
            self.stop_requested = False
            self.state = {'running': True, 'contract': contract, 'ids': ids,
                          'qty': qty, 'environment': environment,
                          'started': time.strftime('%Y-%m-%d %H:%M:%S'),
                          'current': None, 'results': [], 'log': []}

        def log(line):
            with self.lock:
                self.state['log'] = (self.state['log'] + [line])[-200:]

        def got(row):
            with self.lock:
                self.state['results'].append(row)

        runner = Runner(driver, contract, qty=qty, away_ticks=away,
                        hit_wait=hit_wait, log=log,
                        should_stop=lambda: self.stop_requested, on_result=got)

        def go():
            try:
                runner.run(ids)
            except Exception as e:                   # noqa: BLE001
                got({'id': 'RUN', 'title': 'The run', 'status': 'FAIL',
                     'detail': f'{type(e).__name__}: {e}'})
            finally:
                with self.lock:
                    self.state['running'] = False
                    self.state['current'] = None
                    self.state['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
                    record = dict(self.state)
                # The newest result of EVERY flow, whichever run it came
                # from: checking one group does not wipe the others' ticks.
                latest = dict((self.last() or {}).get('latest') or {})
                for row in record.get('results') or []:
                    if row.get('id') != 'PRE':
                        latest[row['id']] = dict(row, contract=contract,
                                                 at=record['finished'])
                record['latest'] = latest
                try:
                    with open(self.results_path, 'w', encoding='utf-8') as f:
                        json.dump(record, f, indent=1)
                except OSError:
                    pass

        def watch_current():
            while self.thread is not None and self.thread.is_alive():
                with self.lock:
                    self.state['current'] = runner.current
                time.sleep(0.2)

        self.thread = threading.Thread(target=go, name='uat-tests', daemon=True)
        self.thread.start()
        threading.Thread(target=watch_current, daemon=True).start()
        return {'ok': True}


# -- the runner --------------------------------------------------------------------------

class Runner:
    def __init__(self, driver, contract: str, qty: float = 1, away_ticks: int = 20,
                 wait: float = 20.0, hit_wait: float = 120.0,
                 log: Callable[[str], None] = print,
                 should_stop: Callable[[], bool] = lambda: False,
                 on_result: Callable[[Dict[str, Any]], None] = lambda r: None):
        self.d = driver
        self.key = contract
        self.qty = qty
        self.away = away_ticks
        self.wait = wait
        self.hit_wait = hit_wait
        self.log = log
        self.results: List[Dict[str, Any]] = []
        self.mine: List[str] = []          # manual order ids this run sent
        self.should_stop = should_stop
        self.on_result = on_result
        self.current: Optional[str] = None

    # -- reading the program ---------------------------------------------------

    def snap(self):
        return self.d.snapshot()

    def contract(self, snap=None):
        snap = snap or self.snap()
        c = next((x for x in snap.get('contracts', []) if x['key'] == self.key), None)
        if c is None:
            raise Failed(f'no contract {self.key!r} on the desk')
        return c

    def terminal(self, snap=None):
        return ((snap or self.snap()).get('engine') or {}).get('manual_terminal') or {}

    def sid(self):
        return str(self.contract().get('security_id') or '')

    def manual_order(self, oid, snap=None):
        return next((o for o in self.terminal(snap).get('orders', [])
                     if o.get('id') == oid), None)

    def manual_open(self, snap=None):
        sid = self.sid()
        return [p for p in (self.terminal(snap).get('pnl') or {}).get('positions', [])
                if str(p.get('security_id')) == sid]

    def until(self, what: str, test, timeout=None):
        """Poll the program until `test(snapshot)` is truthy; its value, or
        Failed naming what never happened."""
        deadline = time.monotonic() + (timeout or self.wait)
        last = None
        while time.monotonic() < deadline:
            last = self.snap()
            value = test(last)
            if value:
                return value
            self.d.sleep(0.25)
        raise Failed(f'{what} — not seen in {timeout or self.wait:.0f}s')

    def ok(self, result, what):
        if not result or not result.get('ok'):
            raise Failed(f"{what}: {(result or {}).get('error') or 'refused'}")
        return result

    def tags(self, clordid, msg='D', **expect):
        """The FIX message this program sent for `clordid`, checked tag by
        tag. `expect` maps tag ('t77') to value, or to a callable."""
        msgs = [m for m in self.d.sent(clordid) if m.get('35') == msg]
        if not msgs:
            raise Failed(f'no {msg} sent for {clordid} in the FIX log')
        m = msgs[-1]
        for tag, want in expect.items():
            tag = tag.lstrip('t')
            have = m.get(tag)
            good = want(have) if callable(want) else str(have) == str(want)
            if not good:
                said = (getattr(want, 'said', None) or 'something else') if callable(want) else repr(str(want))
                raise Failed(f'{msg} for {clordid}: tag {tag} is {have!r}, '
                             f'expected {said}')
        return m

    # -- prices ---------------------------------------------------------------

    def book(self):
        c = self.contract()
        m = c.get('market') or {}
        if m.get('bid') is None or m.get('ask') is None:
            raise Failed('no bid/offer on the contract — is TT publishing it?')
        tick = float(c.get('tick_size') or 0.01)
        return float(m['bid']), float(m['ask']), tick, int(c.get('decimals') or 4)

    def at_market(self, v):
        """An order "at market": a true market order (40=1), or — as the
        desk sends it, so an exchange's price band accepts it — a limit
        through the touch (40=2) that is immediate-or-cancel."""
        return v in ('1', '2')
    at_market.said = '1 (market) or 2 (a limit through the touch, IOC)'

    def at_market_tif(self, v):
        return v in ('3', '0', None, '')
    at_market_tif.said = '3 (IOC) for a limit sent at market'

    def factor(self):
        """TT's DisplayFactor (9787) for this contract: screen price = FIX
        price x factor. 1 where TT gave none."""
        sid = self.sid()
        for row in self.terminal().get('watchlist') or []:
            inst = row.get('instrument') or {}
            if str(inst.get('security_id')) == sid:
                try:
                    return float(inst.get('display_factor') or 1)
                except (TypeError, ValueError):
                    return 1.0
        return 1.0

    def wire(self, price):
        """Tag 44 must carry `price` in TT's FIX units — a check, named."""
        want = float(price) / self.factor()

        def check(have):
            try:
                return abs(float(have) - want) < 1e-6 * max(1.0, abs(want))
            except (TypeError, ValueError):
                return False
        check.said = f'{want:g} (screen {price} / factor {self.factor():g})'
        return check

    def px(self, price, tick, decimals):
        return f'{round(round(price / tick) * tick, 10):.{decimals}f}'

    # -- the manual ticket, as the ladder sends it --------------------------------

    def manual(self, side, order_type, price=None, account=None, tif='DAY'):
        term = self.terminal()
        args = {'security_id': self.sid(), 'account': account or term.get('account', ''),
                'side': side, 'order_type': order_type, 'quantity': str(self.qty),
                'price': '' if price is None else str(price), 'tif': tif,
                'open_close': 'O'}
        review = self.d.command('terminal_preview', '', args)
        self.ok(review, f'review of {side} {order_type}')
        sent = self.ok(self.d.command('terminal_submit', '', {
            'token': review['token'], 'confirmed': True}), 'send')
        self.mine.append(sent['order_id'])
        return sent['order_id']

    FINAL = ('FILLED', 'CANCELED', 'REJECTED', 'EXPIRED')

    def manual_status(self, oid, statuses, what=None, timeout=None):
        """Wait for the ticket to reach one of `statuses`. One that ended
        somewhere else fails NOW, with what TT said — never 20 seconds of
        waiting for a fill that can no longer come."""
        seen = {}

        def test(s):
            o = self.manual_order(oid, s) or {}
            seen.update(o)
            status = o.get('status')
            if status in statuses:
                return o
            if status in self.FINAL:
                raise Failed(self._ended(oid, o))
            return None
        try:
            return self.until(what or f'{oid} {"/".join(statuses)}', test, timeout)
        except Failed as e:
            if seen.get('status') and seen.get('status') not in self.FINAL:
                raise Failed(f"{e}; it is {seen.get('status')}"
                             + (f" — TT: {seen['text']}" if seen.get('text') else '')) from None
            raise

    def _ended(self, oid, o):
        """Why a ticket ended where it did not have to, in plain words."""
        status, text = o.get('status'), o.get('text') or ''
        ticket = o.get('ticket') or {}
        filled = float(o.get('filled_qty') or 0)
        if status in ('CANCELED', 'EXPIRED') and ticket.get('tif') == 'IOC' and filled <= 0:
            return (f"{oid}: TT cancelled the at-market order unfilled — nobody was at "
                    f"{ticket.get('price')} or better when it arrived (an IOC never waits). "
                    f"On a thin UAT market the shown price may not be tradeable; "
                    f"raise 'market limit ticks' for this contract or retry"
                    + (f". TT: {text}" if text else ''))
        return f"{oid} ended {status}" + (f" — TT: {text}" if text else '')

    def manual_close(self, entry_oid, price=None):
        args = {'order_id': entry_oid}
        if price is not None:
            args['price'] = str(price)
        review = self.ok(self.d.command('terminal_preview_close', '', args),
                         'review of the close')
        sent = self.ok(self.d.command('terminal_submit', '', {
            'token': review['token'], 'confirmed': True}), 'send of the close')
        self.mine.append(sent['order_id'])
        return sent['order_id']

    def manual_cancel(self, oid):
        self.ok(self.d.command('terminal_cancel', '', {'order_id': oid}),
                f'cancel of {oid}')
        return self.manual_status(oid, ('CANCELED',), f'{oid} cancelled by TT')

    # -- the Algo's path --------------------------------------------------------

    def algo_open(self, side, order_type, price=None):
        args = {'side': side, 'order_type': order_type, 'qty': self.qty}
        if price is not None:
            args['price'] = price
        return self.ok(self.d.command('uat_order', self.key, args),
                       f'Algo {side} {order_type}')['clordid']

    def algo_order(self, clordid, snap=None):
        return next((o for o in self.contract(snap).get('orders') or []
                     if o.get('clordid') == clordid), None)

    def algo_position(self, snap=None):
        return self.contract(snap).get('position')

    def algo_flat(self, what):
        try:
            return self.until(what, lambda s: self.contract(s).get('position') is None
                              and not self.contract(s).get('orders'))
        except Failed as e:
            c = self.contract()
            pos = c.get('position') or {}
            orders = [f"{o.get('clordid')} {o.get('side')} {o.get('order_type')} "
                      f"{o.get('state')} {o.get('text') or ''}".strip()
                      for o in c.get('orders') or []]
            raise Failed(f"{e}. Still open: position {pos.get('side')} "
                         f"{pos.get('qty')}; orders {orders or 'none'}; last: "
                         f"{c.get('last_event') or '—'}") from None

    # -- the scenarios ----------------------------------------------------------

    def preflight(self, need_live: bool):
        snap = self.snap()
        engine = snap.get('engine') or {}
        env = str(engine.get('environment') or '')
        if 'UAT' not in env.upper():
            raise Failed(f'the venue is {env or "unknown"} — these tests run on UAT only')
        if (engine.get('session') or {}).get('state') != 'LOGGED_ON':
            raise Failed('the TT sessions are not logged on (Exchanges → Connect)')
        c = self.contract(snap)
        if not c.get('security_id'):
            raise Failed(f'{self.key} has no TT Security ID')
        # A price that has not MOVED is a quiet UAT market, not a missing
        # one: these checks need a bid and an offer (`book()` below), not a
        # market that trades.
        if c.get('algo_state') in ('PAPER', 'LIVE'):
            raise Failed(f'the Algo is trading {self.key} — set its Algo switch to Off '
                         f'or Signals first')
        if c.get('position') or c.get('orders') or self.manual_open(snap) or [
                o for o in self.terminal(snap).get('orders', [])
                if str((o.get('ticket') or {}).get('security_id')) == c['security_id']
                and o.get('status') in WORKING]:
            raise Failed(f'something is already open or working on {self.key} — '
                         f'flatten it first; these tests start from flat')
        if need_live and (engine.get('execution') or {}).get('mode') != 'LIVE':
            # Just armed? The engine publishes its state a moment after it
            # answers: wait for it before calling it PAPER.
            try:
                self.until('orders going to TT', lambda s: ((s.get('engine') or {})
                           .get('execution') or {}).get('mode') == 'LIVE', 3)
                engine = (self.snap().get('engine') or {})
            except Failed:
                pass
        if need_live and (engine.get('execution') or {}).get('mode') != 'LIVE':
            raise Failed("the Algo's orders are on PAPER (filled here) — they must go "
                         "to TT UAT for these checks: press Check Algo orders, which "
                         "asks to send them there")
        self.book()
        return c

    def m1(self, side='BUY'):
        bid, ask, tick, dec = self.book()
        price = (self.px(bid - self.away * tick, tick, dec) if side == 'BUY'
                 else self.px(ask + self.away * tick, tick, dec))
        oid = self.manual(side, 'LIMIT', price)
        o = self.manual_status(oid, ('NEW',), f'{oid} acknowledged by TT')
        if not o.get('venue_order_id'):
            raise Failed('acknowledged without a TT order id (37)')
        self.tags(oid, t40='2', t44=self.wire(price), t54='1' if side == 'BUY' else '2',
                  t77='O', t1=lambda v: bool(v), t48=self.sid())
        self.manual_cancel(oid)
        self.tags(oid, msg='F')
        return f'waited at {price} (TT order {o["venue_order_id"]}), cancelled'

    def m8(self):
        return self.m1(side='SELL')

    def m2(self):
        bid, ask, tick, dec = self.book()
        price = self.px(bid - self.away * tick, tick, dec)
        new_price = self.px(bid - (self.away + 2) * tick, tick, dec)
        oid = self.manual('BUY', 'LIMIT', price)
        self.manual_status(oid, ('NEW',), f'{oid} acknowledged')
        self.ok(self.d.command('terminal_replace', '', {
            'order_id': oid, 'price': new_price, 'quantity': str(self.qty)}),
            'replace')
        self.until(f'{oid} replaced to {new_price}', lambda s: (
            (self.manual_order(oid, s) or {}).get('status') in ('REPLACED', 'NEW')
            and not (self.manual_order(oid, s) or {}).get('pending')
            and abs(float((self.manual_order(oid, s) or {}).get('ticket', {}).get('price') or 0)
                    - float(new_price)) < 1e-9))
        self.tags(oid, msg='G', t44=self.wire(new_price))
        self.manual_cancel(oid)
        return f'{price} → {new_price}, then cancelled'

    def _manual_round_trip(self, oid, close_with_limit=False, side='BUY'):
        """An opening manual order that filled: its position shows, and it
        closes by its own ticket — flagged CLOSE, never a second open."""
        close_side = '2' if side == 'BUY' else '1'
        fill = self.manual_status(oid, ('FILLED',), f'{oid} filled')
        pos = self.until('the manual position on the book', lambda s: self.manual_open(s))
        if abs(sum(p['quantity'] for p in pos) - self.qty) > 1e-9:
            raise Failed(f'position shows {pos}, expected {self.qty}')
        note = ''
        if close_with_limit:
            bid, ask, tick, dec = self.book()
            far = (self.px(ask + self.away * tick, tick, dec) if side == 'BUY'   # a SELL far above
                   else self.px(bid - self.away * tick, tick, dec))         # a BUY far below
            cl = self.manual_close(oid, far)
            self.manual_status(cl, ('NEW',), 'the Close @ LMT resting at TT')
            self.tags(cl, t77='C', t40='2', t44=self.wire(far), t54=close_side)
            self.manual_cancel(cl)
            note = f'take-profit waited at {far}, cancelled; '
        cl = self.manual_close(oid)
        self.manual_status(cl, ('FILLED',), 'the close filled')
        self.tags(cl, t77='C', t54=close_side, t40=self.at_market, t59=self.at_market_tif)
        self.until('flat again — no manual position left', lambda s: not self.manual_open(s))
        verb = 'bought' if side == 'BUY' else 'sold'
        return note + f'{verb} at {fill.get("avg_price")}, closed — flat'

    def m3(self):
        oid = self.manual('BUY', 'MARKET')
        self.tags(oid, t77='O', t54='1', t40=self.at_market, t59=self.at_market_tif)
        return self._manual_round_trip(oid)

    def m7(self):
        oid = self.manual('SELL', 'MARKET')
        self.tags(oid, t77='O', t54='2', t40=self.at_market, t59=self.at_market_tif)
        return self._manual_round_trip(oid, side='SELL')

    #: A marketable limit is priced this many ticks THROUGH the offer: a
    #: quiet UAT offer moves a tick between reading it and the order
    #: arriving, and a limit exactly AT it then rests instead of filling.
    THROUGH = 2

    def m4(self):
        bid, ask, tick, dec = self.book()
        oid = self.manual('BUY', 'LIMIT', self.px(ask + self.THROUGH * tick, tick, dec))
        o = self.manual_status(oid, ('FILLED', 'NEW'), f'{oid} answered')
        if o['status'] == 'NEW':
            self.manual_cancel(oid)
            raise Failed(f'it rested instead of filling — the offer moved more than {self.THROUGH} ticks; rerun M4')
        return self._manual_round_trip(oid, close_with_limit=True)

    def m5(self):
        bid, ask, tick, dec = self.book()
        price = self.px(bid, tick, dec)
        oid = self.manual('BUY', 'LIMIT', price)
        self.manual_status(oid, ('NEW', 'FILLED'), f'{oid} acknowledged')
        try:
            self.manual_status(oid, ('FILLED',), f'the market trading at {price}',
                               timeout=self.hit_wait)
        except Failed:
            self.manual_cancel(oid)
            return (f'SKIP: the market did not trade at {price} in '
                    f'{self.hit_wait:.0f}s — rested and cancelled')
        return f'hit at {price}: ' + self._manual_round_trip(oid)

    def m6(self):
        bid, ask, tick, dec = self.book()
        price = self.px(bid - self.away * tick, tick, dec)
        oid = self.manual('BUY', 'LIMIT', price, account='NO_SUCH_ACCOUNT_UAT')
        o = self.manual_status(oid, ('REJECTED',), f'{oid} rejected by TT')
        text = o.get('text') or ''
        if not text or 'check the log' in text.lower():
            raise Failed(f'rejected without TT\'s own words: {text!r}')
        return f'TT said: {text}'

    def a1(self, side='BUY'):
        cid = self.algo_open(side, 'MARKET')
        self.tags(cid, t11=lambda v: str(v).startswith('FT-'), t77='O', t1028='N',
                  t54='1' if side == 'BUY' else '2', t1=lambda v: bool(v),
                  t40=self.at_market, t59=self.at_market_tif)
        pos = self.until('the Algo position on the book', lambda s: self.algo_position(s))
        tickets = pos.get('tickets') or []
        if not tickets or any(str(t).startswith('PAPER-') for t in tickets):
            raise Failed(f'the position carries {tickets} — not TT tickets')
        self.ok(self.d.command('close_now', self.key), 'CLOSE NOW')
        self.algo_flat('flat again after CLOSE NOW')
        closes = self._closes_after(cid)
        closes.sort(key=lambda m: int(str(m.get('11', '0')).rsplit('-', 1)[-1] or '0', 36))
        close = closes[-1]
        want = '2' if side == 'BUY' else '1'
        if close.get('54') != want or 'Close P' not in close.get('58', ''):
            raise Failed(f'the close went as 54={close.get("54")} 58={close.get("58")!r}')
        verb = 'bought' if side == 'BUY' else 'sold'
        return (f'{verb} at {pos.get("avg_price")} (TT ticket {", ".join(map(str, tickets))}), '
                f'closed — flat')

    def a5(self):
        return self.a1(side='SELL')

    def _closes_after(self, open_cid, count=1):
        """The D messages of the Algo's CLOSING orders on this contract
        sent after `open_cid` — read from the journal, then the FIX log,
        waited for: the log is written in the background, so the newest line
        can land a moment after the position is already flat."""
        def closing_orders():
            journal = self.d.journal() or {}
            if journal.get('ok') is False or 'orders' not in journal:
                raise Failed('the journal could not be read: '
                             + str(journal.get('error') or 'no answer'))
            orders = journal.get('orders') or []
            opened = next((o.get('sent_at') or '' for o in orders
                           if o.get('clordid') == open_cid), '')
            return [o for o in orders if o.get('contract_key') == self.key
                    and o.get('intent') == 'CLOSE'
                    and str(o.get('clordid') or '').startswith('FT-')
                    and (o.get('sent_at') or '') >= opened]

        def found(_snap):
            msgs = []
            orders = closing_orders()
            for o in orders:
                msgs += [m for m in self.d.sent(o['clordid'])
                         if m.get('35') == 'D' and m.get('77') == 'C']
            return msgs if orders and len(msgs) >= max(count, 1) else None
        try:
            return self.until('the closing order (77=C) in the FIX log', found)
        except Failed:
            if not closing_orders():
                raise Failed('flat again, but the Algo recorded no closing order — '
                             'the position closed some other way; see the Fills tab') from None
            raise Failed('the Algo recorded its closing order, but its New Order '
                         '(35=D, 77=C) is not in the FIX log') from None

    def a2(self, side='BUY'):
        bid, ask, tick, dec = self.book()
        price = float(self.px(bid - self.away * tick, tick, dec) if side == 'BUY'
                      else self.px(ask + self.away * tick, tick, dec))
        cid = self.algo_open(side, 'LIMIT', price)
        self.until(f'{cid} working at TT', lambda s: (self.algo_order(cid, s) or {}).get('state') == 'WORKING')
        self.tags(cid, t40='2', t77='O', t1028='N', t44=self.wire(price),
                  t54='1' if side == 'BUY' else '2')
        self.ok(self.d.command('cancel_all', self.key), 'cancel')
        self.algo_flat(f'{cid} cancelled, nothing open')
        return f'waited at {price}, cancelled'

    def a6(self):
        return self.a2(side='SELL')

    def a3(self):
        bid, ask, tick, dec = self.book()
        cid = self.algo_open('BUY', 'LIMIT', float(self.px(ask + self.THROUGH * tick, tick, dec)))
        pos = self.until('the marketable LIMIT filled', lambda s: self.algo_position(s)
                         or ((self.algo_order(cid, s) or {}).get('state') == 'WORKING' and 'rest'))
        if pos == 'rest':
            self.ok(self.d.command('cancel_all', self.key), 'cancel')
            self.algo_flat('cancelled')
            raise Failed(f'it rested instead of filling — the offer moved more than {self.THROUGH} ticks; rerun A3')
        bid, ask, tick, dec = self.book()
        far = float(self.px(ask + self.away * tick, tick, dec))
        self.ok(self.d.command('close_limit', self.key, {'price': far}), 'Close @ LMT')
        pinned = self.until('the Close @ LMT working', lambda s: next(
            (o for o in self.contract(s).get('orders') or []
             if o.get('pinned') and o.get('state') == 'WORKING'), None))
        first = self.tags(pinned['clordid'], t77='C', t40='2',
                  t44=self.wire(far))
        self.ok(self.d.command('close_now', self.key), 'CLOSE ALL')
        self.algo_flat('flat after CLOSE ALL escalated the resting close')
        self.tags(pinned['clordid'], msg='F')
        # The closes of THIS position: they name it in 58 ("Close P<id> ...").
        ref = (first.get('58') or '').split(' ')[:2]
        closes = [m for m in self._closes_after(cid, count=2)
                  if (m.get('58') or '').split(' ')[:2] == ref]
        closes.sort(key=lambda m: int(str(m.get('11', '0')).rsplit('-', 1)[-1] or '0', 36))
        last = closes[-1] if closes else {}
        crossed = last.get('40') == '1' or (last.get('40') == '2' and last.get('59') == '3')
        if len(closes) != 2 or not crossed:
            raise Failed(f'expected the resting close then ONE market close, '
                         f'saw {[(m.get("11"), m.get("40")) for m in closes]}')
        return f'take-profit at {far} cancelled, then closed at market once — flat'

    def a4(self):
        bid, ask, tick, dec = self.book()
        price = float(self.px(bid, tick, dec))
        cid = self.algo_open('BUY', 'LIMIT', price)
        deadline = time.monotonic() + self.hit_wait
        while time.monotonic() < deadline:
            if self.algo_position():
                break
            self.d.sleep(0.5)
        if not self.algo_position():
            self.ok(self.d.command('cancel_all', self.key), 'cancel')
            self.algo_flat('cancelled')
            return f'SKIP: the market did not trade at {price} in {self.hit_wait:.0f}s'
        self.ok(self.d.command('close_now', self.key), 'CLOSE NOW')
        self.algo_flat('flat after CLOSE NOW')
        return f'hit at {price}; closed by ticket'

    # -- running them ------------------------------------------------------------

    def cleanup(self):
        """Leave nothing of this run behind: cancel what is working, close
        what is open — by ticket, flagged CLOSE."""
        try:
            snap = self.snap()
            for oid in self.mine:
                o = self.manual_order(oid, snap)
                if o and o.get('status') in ('NEW', 'PARTIALLY_FILLED', 'REPLACED') \
                        and not o.get('pending'):
                    self.d.command('terminal_cancel', '', {'order_id': oid})
            c = self.contract(snap)
            if c.get('orders'):
                self.d.command('cancel_all', self.key)
            if c.get('position'):
                self.d.command('close_now', self.key)
            for p in self.manual_open(snap):
                review = self.d.command('terminal_preview_close', '',
                                        {'order_id': p['entry_order_id']})
                if review.get('ok'):
                    self.d.command('terminal_submit', '', {
                        'token': review['token'], 'confirmed': True})
        except Exception as e:                              # noqa: BLE001
            self.log(f'  cleanup: {e}')

    def run(self, ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        ids = [i.upper() for i in (ids or [s for s, _ in SCENARIOS
                                            if s not in HIT_SCENARIOS])]
        need_live = any(i.startswith('A') for i in ids)
        try:
            self.preflight(need_live)
        except Failed as e:
            row = {'id': 'PRE', 'title': 'Preflight', 'status': 'FAIL',
                   'detail': str(e)}
            self.results.append(row)
            self.on_result(row)
            self.log(f'PRE   FAIL  {e}')
            return self.results
        titles = dict(SCENARIOS)
        for sid in ids:
            title = titles.get(sid, sid)
            if self.should_stop():
                row = {'id': sid, 'title': title, 'status': 'SKIP',
                       'detail': 'SKIP: stopped by the trader before it ran'}
                self.results.append(row)
                self.on_result(row)
                continue
            self.current = sid
            started = time.monotonic()
            try:
                detail = getattr(self, sid.lower())()
                status = 'SKIP' if str(detail).startswith('SKIP') else 'PASS'
            except Failed as e:
                status, detail = 'FAIL', str(e)
            except Exception as e:                          # noqa: BLE001
                status, detail = 'FAIL', f'{type(e).__name__}: {e}'
            if status == 'FAIL':
                self.cleanup()
            secs = time.monotonic() - started
            row = {'id': sid, 'title': title, 'status': status,
                   'detail': detail, 'seconds': round(secs, 1)}
            self.results.append(row)
            self.on_result(row)
            self.log(f'{sid:<5} {status:<5} {title}\n        {detail}')
        self.current = None
        self.cleanup()
        return self.results


def report_html(results, contract, path):
    rows = ''.join(
        f"<tr class='{r['status']}'><td>{r['id']}</td><td>{r['status']}</td>"
        f"<td>{r['title']}</td><td>{r.get('detail', '')}</td>"
        f"<td>{r.get('seconds', '')}</td></tr>" for r in results)
    html = (f"<!doctype html><meta charset='utf-8'><title>UAT order tests</title>"
            f"<style>body{{font:13px Segoe UI,Arial}}td,th{{padding:4px 8px;border-bottom:1px solid #ddd;text-align:left}}"
            f".PASS td:nth-child(2){{color:#177349;font-weight:700}}.FAIL td:nth-child(2){{color:#b03030;font-weight:700}}"
            f".SKIP td:nth-child(2){{color:#9a6b00}}</style><h2>UAT order tests — {contract} — "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}</h2><table><tr><th>Test</th><th>Result</th>"
            f"<th>What</th><th>Evidence</th><th>s</th></tr>{rows}</table>")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='UAT order tests against the running program')
    ap.add_argument('--contract', required=True, help='the contract key on the desk, e.g. esz6')
    ap.add_argument('--url', default='http://127.0.0.1:8000')
    ap.add_argument('--qty', type=float, default=1)
    ap.add_argument('--away', type=int, default=20,
                    help='ticks away from the market for orders that must NOT fill')
    ap.add_argument('--only', default='', help='comma list, e.g. M1,M3,A1')
    ap.add_argument('--with-hits', action='store_true',
                    help='also run M5 / A4, which wait for the market to trade at the touch')
    ap.add_argument('--hit-wait', type=float, default=120.0)
    ap.add_argument('--yes', action='store_true', help='do not ask before sending')
    args = ap.parse_args(argv)
    ids = [x.strip() for x in args.only.split(',') if x.strip()] or [
        s for s, _ in SCENARIOS if args.with_hits or s not in HIT_SCENARIOS]
    print(f'UAT order tests on {args.contract}: {", ".join(ids)} — qty {args.qty:g}')
    if not args.yes:
        answer = input('These send REAL orders to TT UAT and close them again. '
                       'Type yes to go: ')
        if answer.strip().lower() != 'yes':
            print('Nothing sent.')
            return 1
    runner = Runner(HttpDriver(args.url), args.contract, qty=args.qty,
                    away_ticks=args.away, hit_wait=args.hit_wait)
    results = runner.run(ids)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    with open(f'uat-results-{stamp}.json', 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2)
    report_html(results, args.contract, f'uat-results-{stamp}.html')
    passed = sum(r['status'] == 'PASS' for r in results)
    failed = sum(r['status'] == 'FAIL' for r in results)
    print(f'\n{passed} passed, {failed} failed, '
          f'{sum(r["status"] == "SKIP" for r in results)} skipped — '
          f'uat-results-{stamp}.html')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
