"""The Flask process: it renders and it asks. **It never trades.**

Every route here either reads `status.json` or drops a command on the bridge
for the engine to act on. Nothing in this file touches a venue, and nothing in
it can send an order — that separation is what stops a browser crash from
stopping an exit.

The other rule this file keeps: **no secret ever leaves it.** A venue's
password is reported as set or not set; it is never returned, not even masked,
because a masked value is one that gets echoed back into a form and saved over
the real one.
"""

import os
import io
import csv
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, render_template, request

from . import atomicfile, sizing
from .commands import CommandBridge
from .config import TraderConfig

#: How stale the snapshot may be before the screen calls the engine dead. It
#: is a multiple of the refresh rather than a fixed number of seconds: a desk
#: running at 2s must not be told its engine has died every other pass.
STALE_SNAPSHOT_MULTIPLE = 8.0
ASSET_VERSION = str(int(time.time()))


def create_app(config_path: str = "config.json",
               status_path: str = "status.json",
               command_path: str = "commands.jsonl",
               result_path: str = "results.json") -> Flask:
    app = Flask(__name__)
    bridge = CommandBridge(command_path, result_path)

    def load_config() -> TraderConfig:
        """Read from disk on every request. The engine also writes this file,
        and a cached copy would show the operator a setting they changed
        minutes ago as though it had not taken."""
        return TraderConfig.from_file(config_path)

    def read_status() -> Dict[str, Any]:
        snap = atomicfile.read_json(status_path, default=None)
        if not snap:
            return {
                'ts': None,
                'engine': {'alive': False, 'text': (
                    "the engine has not published a snapshot yet — it may "
                    "still be starting")},
                'contracts': [],
            }
        # Is it moving? A snapshot that has stopped is a dead engine, and it
        # must never be mistaken for a quiet market.
        try:
            ts = datetime.fromisoformat(snap['ts'])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = None
        refresh = float((snap.get('engine') or {}).get('refresh_sec', 0.5) or 0.5)
        snap.setdefault('engine', {})['snapshot_age_sec'] = (
            round(age, 1) if age is not None else None)
        if age is not None and age > refresh * STALE_SNAPSHOT_MULTIPLE:
            snap['engine']['alive'] = False
            snap['engine']['text'] = (
                f"the engine last published {age:.0f}s ago — these prices are "
                f"not live")
        return snap

    # -- pages -------------------------------------------------------------

    @app.get('/')
    def index():
        if any(v.host and v.fix_version == 'FIX.4.2' for v in load_config().venues.values()):
            return render_template('connection.html', asset_version=ASSET_VERSION)
        return render_template('index.html', asset_version=ASSET_VERSION)

    @app.get('/connection')
    def connection():
        return render_template('connection.html', asset_version=ASSET_VERSION)

    @app.get('/desk')
    def desk():
        return render_template('index.html', asset_version=ASSET_VERSION)

    @app.get('/instruments')
    def instruments():
        return render_template('instruments.html', asset_version=ASSET_VERSION)

    @app.get('/account')
    def account_page():
        return render_template('account.html', asset_version=ASSET_VERSION)

    # -- order tests: every order path, run on TT UAT from the screen --------

    from . import uat as uat_mod
    order_tests = uat_mod.OrderTestRun(str(status_path) + '.order-tests.json')
    app.extensions['order_tests'] = order_tests
    hand_checks = uat_mod.HandChecks(str(status_path) + '.order-checks.json')

    @app.get('/order-tests')
    def order_tests_page():
        return render_template('order_tests.html', asset_version=ASSET_VERSION)

    @app.get('/api/order-tests')
    def api_order_tests():
        engine = read_status().get('engine') or {}
        return jsonify({
            'scenarios': [{'id': sid, 'title': title, 'steps': uat_mod.STEPS.get(sid, ''),
                           'short': uat_mod.SHORT.get(sid, title),
                           'expect': uat_mod.EXPECT.get(sid, ''),
                           'kind': uat_mod.GROUPS[sid][0],
                           'order_type': uat_mod.GROUPS[sid][1],
                           'waits': sid in uat_mod.HIT_SCENARIOS}
                          for sid, title in uat_mod.SCENARIOS],
            'environment': engine.get('environment'),
            'run': order_tests.status(), 'last': order_tests.last(),
            'checks': hand_checks.all()})

    @app.post('/api/order-tests/check')
    def api_order_tests_check():
        """The trader's own word on a test done by hand: PASS, FAIL, or
        cleared. A record, not an order — nothing is sent."""
        data = request.get_json(silent=True) or {}
        sid = str(data.get('id') or '').upper()
        result = str(data.get('result') or '').upper()
        if sid not in {s for s, _ in uat_mod.SCENARIOS} or result not in ('PASS', 'FAIL', ''):
            return jsonify({'ok': False, 'error': 'unknown test or result'}), 400
        engine = read_status().get('engine') or {}
        checks = hand_checks.mark(sid, result, str(data.get('note') or ''),
                                  str(data.get('contract') or ''),
                                  str(engine.get('environment') or ''))
        return jsonify({'ok': True, 'checks': checks})

    @app.post('/api/order-tests/run')
    def api_order_tests_run():
        data = request.get_json(silent=True) or {}
        engine = read_status().get('engine') or {}
        env = str(engine.get('environment') or '')
        if env.upper() != 'UAT':
            return jsonify({'ok': False, 'error': (
                f'the venue is {env or "not known"} — order tests send real '
                f'orders and run on TT UAT only. On a live venue, test by hand '
                f'with the steps shown, at the size you mean.')}), 409
        if data.get('confirm') is not True:
            return jsonify({'ok': False, 'error': 'not confirmed'}), 400
        known = {sid for sid, _ in uat_mod.SCENARIOS}
        ids = [str(i).upper() for i in data.get('ids') or [] if str(i).upper() in known]
        if not ids:
            return jsonify({'ok': False, 'error': 'choose at least one test'}), 400
        contract = str(data.get('contract') or '')
        try:
            qty = float(data.get('qty') or 1)
            away = int(data.get('away') or 20)
            hit_wait = float(data.get('hit_wait') or 120)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'quantity, ticks away and wait must be numbers'}), 400
        if qty <= 0 or away < 2 or hit_wait <= 0:
            return jsonify({'ok': False, 'error': 'quantity must be above 0, ticks away at least 2'}), 400
        out = order_tests.start(uat_mod.ClientDriver(app.test_client()), contract,
                                ids, qty=qty, away=away, hit_wait=hit_wait,
                                environment=env)
        return jsonify(out), (200 if out.get('ok') else 409)

    @app.post('/api/order-tests/clear')
    def api_order_tests_clear():
        """A fresh page: the automatic results AND the by-hand ticks."""
        out = order_tests.clear()
        if out.get('ok'):
            for sid in list(hand_checks.all()):
                hand_checks.mark(sid, '')
        return jsonify(out), (200 if out.get('ok') else 409)

    @app.post('/api/order-tests/stop')
    def api_order_tests_stop():
        return jsonify(order_tests.stop())

    @app.get('/logs')
    def logs_page():
        return render_template('logs.html', asset_version=ASSET_VERSION)

    def audit_log():
        from .fix_audit import FixAuditLog
        return FixAuditLog()

    @app.get('/api/fix-logs')
    def api_fix_logs():
        return jsonify({'rows': audit_log().read(request.args.get('limit', 500),
            request.args.get('category', ''), request.args.get('search', ''))})

    @app.post('/api/fix-logs/clear')
    def api_clear_fix_logs():
        audit_log().clear()
        return jsonify({'ok': True})

    @app.get('/api/fix-logs.csv')
    def api_fix_logs_csv():
        rows = audit_log().read(5000, request.args.get('category', ''), request.args.get('search', ''))
        out = io.StringIO(); columns = ['timestamp','level','category','session','direction','event','sequence','details','raw']
        writer = csv.DictWriter(out, fieldnames=columns); writer.writeheader()
        for row in reversed(rows):
            item = {key: row.get(key, '') for key in columns}; item['details'] = str(item['details']); writer.writerow(item)
        return Response(out.getvalue(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=fix-logs.csv'})

    @app.get('/settings')
    def settings_page():
        return render_template('settings.html', asset_version=ASSET_VERSION)

    @app.get('/exchanges')
    def exchanges_page():
        return render_template('exchanges.html', asset_version=ASSET_VERSION)

    # -- the screen --------------------------------------------------------

    @app.get('/api/snapshot')
    def api_snapshot():
        return jsonify(read_status())

    @app.get('/api/quotes/stream')
    def quote_stream():
        from .quote_stream import events
        return Response(events(str(status_path) + '.quotes.json'), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no'})

    @app.post('/api/command')
    def api_command():
        data = request.get_json(silent=True) or {}
        action = data.get('action')
        if not action:
            return jsonify({'ok': False, 'error': 'no action'}), 400
        if action in ('fix_connect', 'fix_reconnect'):
            sessions = (read_status().get('engine', {}).get('fix_connection', {})
                        .get('sessions', []))
            if any('sequence mismatch' in str(session.get('error', '')).lower()
                   for session in sessions):
                return jsonify({'ok': False, 'error':
                    'TT FIX sequence mismatch. Connect/reconnect is blocked until TT '
                    'confirms the sequence reset or recovery procedure and working '
                    'orders and fills have been reconciled.'}), 409
        command_id = bridge.submit(action, data.get('contract', ''),
                                   data.get('args') or {})
        return jsonify({'ok': True, 'id': command_id})

    @app.get('/api/result/<command_id>')
    def api_result(command_id):
        result = bridge.result(command_id)
        if result is None:
            return jsonify({'ok': None, 'pending': True})
        return jsonify(result)

    # -- analysis ----------------------------------------------------------

    def _db(config):
        from .database import Database
        from .config import database_path
        return Database(database_path(config, status_path))

    def _filters():
        """Period and mode, applied identically by every analysis route. A
        route that silently blended simulated fills into a live figure would
        be a bug, not a convenience."""
        period = request.args.get('period', 'all')
        mode = request.args.get('mode', 'live')
        if mode not in ('live', 'sim', 'both'):
            mode = 'live'
        return period, mode

    @app.get('/api/analysis')
    def api_analysis_desk():
        from . import analysis
        config = load_config()
        period, mode = _filters()
        return jsonify(analysis.desk_report(_db(config), config, period, mode))

    @app.get('/api/analysis/<path:key>')
    def api_analysis_contract(key):
        from . import analysis
        config = load_config()
        if key not in config.contracts:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        period, mode = _filters()
        return jsonify(analysis.contract_report(_db(config), config, key,
                                                period, mode))

    def _slippage_report(config):
        """The slippage report over the Analysis window's period and mode.
        Built from the positions the engine recorded; it prices nothing of
        its own."""
        from . import analysis, slippage
        period, mode = _filters()
        since = analysis.period_start(period)
        positions = [p for p in _db(config).positions_opened_since(since)
                     if analysis._wants(p.is_simulated, mode)]
        key = request.args.get('contract') or None
        if key:
            positions = [p for p in positions if p.contract_key == key]
        budgets = {k: float(config.effective(k).get('slippage_budget_ticks')
                            or 0) for k in config.contracts}
        body = slippage.report(positions, config.contracts, budgets)
        body.update(period=period, mode=mode, contract=key)
        # Manual tickets, from the engine's snapshot: each ticket's fills
        # against the touch when it was SENT. Live TT orders only, so they
        # are shown under live, never blended into a simulated figure.
        manual = (((read_status().get('engine') or {}).get('manual_terminal')
                   or {}).get('slippage') or {})
        body['manual'] = (slippage.manual_summary(
            manual.get('rows') or [],
            since.isoformat() if since is not None else None)
            if mode in ('live', 'both') and not key else None)
        return body

    @app.get('/api/journal')
    def api_journal():
        """The Algo's record for the Account page: its recent orders, its
        fills (venue tickets and PAPER ones alike, each marked) and its
        closed trades, newest first. Read from the book; it prices nothing."""
        from . import slippage
        config = load_config()
        db = _db(config)
        limit = max(1, min(int(request.args.get('limit', 200) or 200), 1000))
        closed = []
        for p in db.closed_positions(limit=limit):
            contract = config.contracts.get(p.contract_key)
            row = slippage.row(p, contract) if contract is not None else {}
            closed.append({
                'id': p.id, 'contract_key': p.contract_key,
                'name': contract.name if contract else p.contract_key,
                'decimals': contract.decimals if contract else 4,
                'side': p.side.value, 'qty': p.opened_qty or p.qty,
                'opened_at': p.opened_at.isoformat() if p.opened_at else None,
                'closed_at': p.closed_at.isoformat() if p.closed_at else None,
                'entry_price': p.avg_price, 'exit_price': p.exit_price,
                'entry_z': p.entry_z, 'exit_z': p.exit_z,
                'exit_reason': p.exit_reason.value if p.exit_reason else None,
                'gross_pnl': p.gross_pnl, 'fees': p.fees_paid,
                'net_pnl': p.net_pnl, 'tickets': list(p.tickets or []),
                'entry_order_type': p.entry_order_type,
                'exit_order_type': p.exit_order_type,
                'entry_ticks': row.get('entry_ticks'),
                'exit_ticks': row.get('exit_ticks'),
                'paper': any(str(t).startswith('PAPER-') for t in p.tickets or ()),
                'simulated': bool(p.is_simulated),
            })
        orders = db.orders(limit=2000)
        by_id = {o['clordid']: o for o in orders}
        tt = db.tt_fills(limit=limit)
        _price_fills(config, db, tt, _manual_book())
        timing: Dict[str, List[float]] = {}
        first_fill = set()
        for f in reversed(tt):                 # oldest first: first fill per order
            order = by_id.get(f['clordid']) or by_id.get(f['orig_clordid'])
            if order and order.get('sent_at') and f['clordid'] not in first_fill:
                first_fill.add(f['clordid'])
                try:
                    ms = (datetime.fromisoformat(f['received']) -
                          datetime.fromisoformat(order['sent_at'])).total_seconds() * 1000
                    if ms >= 0:
                        timing.setdefault(order.get('order_type') or '?', []).append(ms)
                except (TypeError, ValueError):
                    pass
        timings = {k: {'n': len(v), 'median': sorted(v)[len(v) // 2],
                       'worst': max(v)} for k, v in timing.items()}
        return jsonify({'ok': True, 'orders': orders[:limit],
                        'fills': db.fills(limit=limit), 'closed': closed,
                        'tt_fills': tt, 'timings': timings,
                        'account': next((v.account for v in config.venues.values()
                                         if getattr(v, 'account', '')), '')})

    def _manual_book():
        """The manual ticket's orders and watchlist, from the engine's
        snapshot — what a manual close is priced against."""
        engine = read_status().get('engine') or {}
        return engine.get('manual_terminal') or {}

    @app.get('/api/tt_fills.csv')
    def api_tt_fills_csv():
        """The TT fills tape as TT sent it, newest first — and beside TT's
        own fields, the P&L this program puts on a closing fill of ours (the
        same figure as the Fills tab), empty where it cannot be priced."""
        from .database import Database
        config = load_config()
        db = _db(config)
        rows = db.tt_fills(limit=100000)
        _price_fills(config, db, rows, _manual_book())
        return _csv('tt_fills.csv', list(Database.TT_FILL_COLUMNS) + ['pnl'], rows)

    @app.get('/api/slippage')
    def api_slippage():
        """Measured slippage: entries, exits and the round turn, by contract
        and by order type, and the worst fills — each against the price its
        decision was made at, positive a cost."""
        return jsonify(dict(_slippage_report(load_config()), ok=True))

    @app.get('/api/slippage.csv')
    def api_slippage_csv():
        """One row per position, both ends — empty cells, not zeros, where
        nothing was measured."""
        body = _slippage_report(load_config())
        columns = ['opened_at', 'closed_at', 'contract_key', 'side', 'qty',
                   'entry_order_type', 'entry_price', 'entry_ticks',
                   'entry_money', 'exit_order_type', 'exit_price',
                   'exit_ticks', 'exit_money', 'round_trip_ticks',
                   'round_trip_money', 'net_pnl', 'paper', 'simulated',
                   'position_id']
        return _csv('slippage.csv', columns, body['rows'])

    @app.get('/api/series/<path:key>')
    def api_series(key):
        """The chart: the recorded mid over the window, and the entries and
        exits inside it. Read from what the engine recorded, so the chart and
        the statistics are drawn from the same prices. Thinned to at most
        `points` so a 150-minute window at one sample a second stays light;
        the thinning keeps each stretch's high and low, so a spike that
        touched a band is not averaged away."""
        from datetime import datetime, timedelta, timezone
        config = load_config()
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        settings = config.effective(key)
        # Twice the band's own span by default: N candles of the timeframe.
        span = (float(settings.get('timeframe_min') or 15)
                * float(settings.get('length') or 20) * 2)
        try:
            minutes = float(request.args.get('minutes') or span)
        except ValueError:
            minutes = span
        points = max(50, min(2000, int(request.args.get('points', 600))))
        since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        db = _db(config)
        rows = db.samples_between(key, since=since)

        series = []
        if rows:
            step = max(1, len(rows) // (points // 2))
            for i in range(0, len(rows), step):
                chunk = rows[i:i + step]
                lo = min(chunk, key=lambda r: r[1])
                hi = max(chunk, key=lambda r: r[1])
                for r in sorted({lo, hi}, key=lambda r: r[0]):
                    series.append([r[0].isoformat(), r[1]])

        marks = []
        def mark(ts, price, kind, side, reason=None):
            if ts is None or price is None:
                return
            stamp = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            if stamp >= since:
                marks.append({'ts': stamp.isoformat(), 'price': price,
                              'kind': kind, 'side': side, 'reason': reason})
        for pos in db.closed_positions(key, limit=200):
            mark(pos.opened_at, pos.avg_price, 'open', pos.side.value)
            mark(pos.closed_at, pos.exit_price, 'close', pos.side.value,
                 pos.exit_reason.value if pos.exit_reason else None)
        for pos in db.open_positions():
            if pos.contract_key == key:
                mark(pos.opened_at, pos.avg_price, 'open', pos.side.value)
        return jsonify({'ok': True, 'key': key, 'minutes': minutes,
                        'series': series, 'marks': marks,
                        'decimals': contract.decimals,
                        'entry_threshold': settings.get('entry_threshold'),
                        'stop_loss_z': settings.get('stop_loss_z')})

    @app.get('/api/backtest/<path:key>')
    def api_backtest(key):
        """What THIS contract's Algo, with its settings as they are now, would
        have done over the last `days` — and, beside it, without re-entry and
        the trend filter. Replayed from the mids this system recorded (a FIX
        session has no history to ask for), through the same decision code
        the live Algo runs. Nothing is sent."""
        from datetime import datetime, timedelta, timezone
        from . import algo as algo_mod, backtest, bands, costs as costs_mod
        config = load_config()
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        try:
            days = max(1.0, min(30.0, float(request.args.get('days', 5))))
        except ValueError:
            days = 5.0
        settings = config.effective(key)
        params = algo_mod.params_from_settings(settings)
        tf = params['timeframe_min'] * 60.0
        # The band needs its N candles BEFORE the first day replayed.
        since = (datetime.now(timezone.utc) - timedelta(days=days)
                 - timedelta(seconds=tf * params['length'] * 2))
        rows = bands.candles_from_samples(
            _db(config).samples_between(key, since=since), tf)
        if len(rows) <= params['length']:
            return jsonify({'ok': False, 'reason': (
                f"{len(rows)} candle(s) of {params['timeframe_min']} min "
                f"recorded over this period — the band needs "
                f"{params['length']} before it can say anything. The engine "
                f"records the mid while it runs.")})
        qty = params['algo_qty']
        k = (contract.tick_value / contract.tick_size
             if contract.tick_value and contract.tick_size else None)
        breakdown = costs_mod.cost_breakdown(qty, contract.tick_size,
                                             contract.tick_value, settings)
        fees = sum(breakdown[x] or 0.0
                   for x in ('commission', 'exchange', 'clearing'))
        # The bid-ask: today's, from the engine's last snapshot; one tick if
        # it has none.
        width = contract.tick_size or 0.0
        for c in read_status().get('contracts') or []:
            market = c.get('market') or {}
            if c.get('key') == key and market.get('bid') is not None \
                    and market.get('ask') is not None:
                width = market['ask'] - market['bid']
        margin = (costs_mod.configured_margin(settings, 1.0)
                  or _db(config).margin_per_contract(key))

        def replay(p):
            return backtest.run(rows, p, width, k,
                                breakdown['round_trip_points'],
                                fees, margin,
                                breakdown['slippage_budget'] or 0.0)
        out = replay(params)
        plain = replay(dict(params, reentry_on=False, trend_on=False))
        out['trades'] = out['trades'][-50:]
        return jsonify({'ok': True, 'key': key, 'days': days,
                        'width': width, 'margin': margin,
                        'without_protections': plain['summary'], **out})

    @app.get('/api/replay/<path:key>')
    def api_replay(key):
        """What a DIFFERENT entry threshold would have done to the same
        recorded market.

        A SIGNAL replay, and the response says so: the book either side of
        the recorded mid was never stored, the costs are the configured
        budget rather than anything measured, and there is no queue. The
        `assumptions` block travels with every figure for exactly that
        reason.
        """
        from . import analysis, replay as replay_mod
        config = load_config()
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        period, _mode = _filters()          # a replay has no live/sim split:
        # it re-runs the SIGNAL over recorded prices, and prices are prices.
        since = analysis.period_start(period)
        settings = config.effective(key)
        rows = _db(config).samples_between(key, since=since)

        thresholds = request.args.get('thresholds', '')
        try:
            levels = [float(x) for x in thresholds.split(',') if x.strip()]
        except ValueError:
            levels = []
        if not levels:
            base = float(settings.get('entry_threshold', 2.0) or 2.0)
            levels = sorted({round(v, 2) for v in
                             (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, base)})

        out = replay_mod.sweep(
            rows, settings, contract.tick_size, contract.tick_value,
            thresholds=levels,
            contract_multiplier=contract.contract_multiplier,
            contract_key=key,
            margin_per_contract=_db(config).margin_per_contract(key))
        out.update({'ok': True, 'key': key, 'symbol': contract.symbol,
                    'period': period, 'samples': len(rows),
                    'decimals': contract.decimals,
                    'entry_threshold': settings.get('entry_threshold'),
                    'recorded_from': rows[0][0].isoformat() if rows else None,
                    'recorded_to': rows[-1][0].isoformat() if rows else None})
        return jsonify(out)

    @app.get('/api/analysis/<path:key>/trades.csv')
    def api_analysis_trades_csv(key):
        from . import analysis
        config = load_config()
        period, mode = _filters()
        rows = analysis.contract_report(_db(config), config, key, period,
                                        mode)['journal']
        columns = ['opened_at', 'closed_at', 'side', 'qty', 'entry_z',
                   'exit_z', 'entry_price', 'exit_price', 'gross', 'fees',
                   'net', 'on_margin', 'held_min', 'exit_reason', 'simulated',
                   'tickets']
        return _csv(f"{key}-trades.csv", columns, rows)

    @app.get('/api/analysis/<path:key>/touches.csv')
    def api_analysis_touches_csv(key):
        config = load_config()
        period, mode = _filters()
        rows = _db(config).touches(key, limit=20000)
        columns = ['ts', 'level', 'direction', 'price', 'z', 'mean', 'std',
                   'half_life', 'algo_armed', 'became_trade', 'state',
                   'resolved_at', 'seconds_to_revert', 'adverse_sigma']
        return _csv(f"{key}-touches.csv", columns, rows)

    # -- configuration -----------------------------------------------------

    @app.get('/api/settings')
    def api_settings():
        return jsonify(load_config().settings)

    @app.post('/api/settings')
    def api_save_settings():
        config = load_config()
        data = request.get_json(silent=True) or {}
        restart = config.structural_changes(data)
        config.settings.update(data)
        config.save()
        return jsonify({'ok': True, 'restart_needed': restart})

    @app.get('/api/contracts')
    def api_contracts():
        config = load_config()
        return jsonify([
            dict(c.to_dict(), key=c.key,
                 effective=c.settings_with_defaults(config.settings))
            for c in config.contracts.values()])

    @app.post('/api/contracts/<path:key>')
    def api_save_contract(key):
        config = load_config()
        data = request.get_json(silent=True) or {}
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        from .config import CONTRACT_DEFAULTS
        for field in ('name', 'symbol', 'venue', 'decimals', 'enabled',
                      'session_open', 'session_close'):
            if field in data:
                setattr(contract, field, data[field])
        for field in ('tick_size', 'tick_value', 'contract_multiplier',
                      'min_qty', 'qty_step', 'max_qty'):
            if field in data and data[field] not in (None, ''):
                setattr(contract, field, float(data[field]))
                # A number the operator typed is an OVERRIDE, and the window
                # says so — it is not the venue's answer any more.
                contract.spec_source[field] = 'operator'
        for field in CONTRACT_DEFAULTS:
            if field in data:
                value = data[field]
                # Blank clears the override; 0 is a real number and sets one.
                contract.overrides[field] = None if value in (None, '') else value
        config.save()
        return jsonify({'ok': True,
                        'effective': contract.settings_with_defaults(config.settings)})

    # -- venues ------------------------------------------------------------

    @app.post('/api/contracts')
    def api_create_contract():
        """Add a contract. The key is derived from the symbol so two rows
        cannot quietly share one."""
        import re
        from .config import ContractConfig
        config = load_config()
        data = dict(request.get_json(silent=True) or {})
        symbol = str(data.get('symbol') or '').strip()
        if not symbol:
            return jsonify({'ok': False, 'error': 'a symbol is required'}), 400
        venue = str(data.get('venue') or '').strip()
        if venue and venue not in config.venues:
            return jsonify({'ok': False,
                            'error': f"no venue {venue!r}"}), 400
        def slug(text):
            return re.sub(r'[^a-z0-9]+', '_', str(text).lower()).strip('_')
        key = data.get('key') or slug(symbol)
        # A TT product symbol is shared by every contract in it — `CL` is the
        # December future AND every CL calendar AND every CL|BZ spread. The
        # TT Security ID is what tells them apart, so a DIFFERENT instrument
        # under a taken key gets its own key; the SAME one is still refused.
        security_id = str(data.get('security_id') or '').strip()
        taken = config.contracts.get(key)
        if (taken is not None and not data.get('key') and security_id
                and str(getattr(taken, 'security_id', '') or '') != security_id):
            key = slug(data.get('name') or symbol) or key
            if key in config.contracts:
                key = f"{key}_{slug(security_id)[-6:]}"
        if key in config.contracts:
            return jsonify({'ok': False,
                            'error': f"{key} already exists"}), 409
        data['symbol'] = symbol
        allowed = {k: v for k, v in data.items() if k != 'key'}
        config.contracts[key] = ContractConfig.from_dict(key, allowed)
        config.save()
        return jsonify({'ok': True, 'key': key})

    @app.delete('/api/contracts/<path:key>')
    def api_delete_contract(key):
        """Refused while anything is open on it — and the refusal says what."""
        config = load_config()
        if key not in config.contracts:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        snap = read_status()
        for c in snap.get('contracts', []):
            if c.get('key') == key and c.get('position'):
                pos = c['position']
                return jsonify({'ok': False, 'error':
                                f"{pos['side']} {pos['qty']:g} is open on "
                                f"this contract. Close it first."}), 409
        config.contracts.pop(key)
        config.save()
        return jsonify({'ok': True})

    @app.get('/api/venues')
    def api_venues():
        """Public form only. The password is reported as set or not set and
        is NEVER returned, masked or otherwise."""
        return jsonify([v.to_public_dict()
                        for v in load_config().venues.values()])

    @app.post('/api/venues/<path:name>')
    def api_save_venue(name):
        """Create or update one venue.

        The password never reaches `config.json`. It goes to `.env` under the
        venue's own key, and only when a non-empty value is sent — an empty
        field means "leave it alone", so re-saving a form that shows no
        password cannot wipe the one that is set.
        """
        from .config import VenueConfig, env_key_for, write_env_value
        config = load_config()
        data = dict(request.get_json(silent=True) or {})
        password = data.pop('password', None)
        data.pop('password_set', None)
        data.pop('name', None)

        existing = config.venues.get(name)
        raw = existing.to_dict() if existing else {}
        raw.update({k: v for k, v in data.items() if k in VENUE_FIELDS})
        raw.setdefault('environment', '')
        raw.setdefault('password_env', env_key_for(name))
        try:
            venue = VenueConfig.from_dict(name, raw)
        except ValueError as e:
            # The environment is the one field with no default: UAT and PROD
            # are separate venues and neither is assumed.
            return jsonify({'ok': False, 'error': str(e)}), 400

        config.venues[name] = venue
        config.save()
        if password:
            write_env_value(venue.password_env, password)
        return jsonify({'ok': True, 'venue': venue.to_public_dict()})

    @app.delete('/api/venues/<path:name>')
    def api_delete_venue(name):
        config = load_config()
        if name not in config.venues:
            return jsonify({'ok': False, 'error': f"no venue {name}"}), 404
        using = [c.key for c in config.contracts.values() if c.venue == name]
        if using:
            # Deleting the venue a contract routes through would leave the
            # contract pointing at nothing, and the refusal names them.
            return jsonify({'ok': False, 'error':
                            f"{len(using)} contract(s) route through it: "
                            f"{', '.join(using)}"}), 409
        config.venues.pop(name)
        config.save()
        return jsonify({'ok': True})

    def _gateway_for(config, venue):
        """A gateway for one venue, for the three buttons. Never the engine's
        — this process renders and asks; it does not trade."""
        contracts = [c for c in config.contracts.values()
                     if c.venue == venue.name]
        if not venue.host:
            from .fake_gateway import FakeGateway, SimContract
            sims = [SimContract(c.key, tick_size=c.tick_size or 0.01,
                                tick_value=c.tick_value or 1.0)
                    for c in contracts]
            return FakeGateway(sims), contracts, True
        from .gateway import FixGateway
        return FixGateway(venue, contracts), contracts, False

    @app.get('/api/venues/<path:name>/<any(connect,test,diagnose):action>')
    def api_venue_action(name, action):
        """Connect, Test and Diagnose.

        Every failure carries the step that fixes it. None of them ever
        returns a credential, and the answer is the venue's own words rather
        than "check the log".
        """
        config = load_config()
        venue = config.venues.get(name)
        if venue is None:
            return jsonify({'ok': False, 'error': f"no venue {name}"}), 404

        if venue.host:
            snap = read_status()
            if not snap.get('engine', {}).get('alive'):
                return jsonify({'ok': False, 'simulated': False, 'rows': [{
                    'check': 'Engine', 'ok': False,
                    'detail': 'The FIX engine is not running.',
                    'fix': 'Start python start.py --fix --no-browser.'}]})
            command_id = bridge.submit('fix_connect' if action == 'connect' else 'fix_status',
                                       args={'venue': name})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = bridge.result(command_id)
                if result is not None:
                    return jsonify(result)
                time.sleep(0.02)
            return jsonify({'ok': False, 'simulated': False, 'rows': [{
                'check': 'Engine', 'ok': False,
                'detail': 'The engine has not answered yet.',
                'fix': 'Wait briefly, then use Test to read the session status.'}]})

        gateway, contracts, simulated = _gateway_for(config, venue)
        rows = []
        try:
            gateway.start()
            state = gateway.state()
            ok = state.value == 'LOGGED_ON'
            rows.append({
                'check': 'Session',
                'ok': ok,
                'detail': f"{state.value} — {gateway.state_text()}",
                'fix': '' if ok else (
                    'Check the host, port and comp ids, and that the '
                    'password is set in .env.'),
            })

            if action in ('test', 'diagnose'):
                rows.append({
                    'check': 'Environment',
                    'ok': True,
                    'detail': (f"{venue.environment}"
                               + (' — SIMULATED, no venue is connected'
                                  if simulated else '')),
                    'fix': '',
                })
                rows.append({
                    'check': 'Password',
                    'ok': venue.has_password,
                    # Set or not set. Never the value, and never a masked
                    # version of it either.
                    'detail': ('set in .env as ' + venue.password_env
                               if venue.has_password else
                               'NOT set — the session cannot log on'),
                    'fix': '' if venue.has_password else
                           f"Type it into the password field and save; it is "
                           f"written to .env as {venue.password_env}.",
                })
                rows.append({
                    'check': 'Account',
                    'ok': bool(venue.account),
                    'detail': venue.account or 'not set',
                    'fix': '' if venue.account else
                           'Every order is stamped with it.',
                })

            if action == 'diagnose':
                for contract in contracts:
                    rows.append(_contract_check(gateway, contract, simulated))
                rows.extend(getattr(gateway, 'diagnose', lambda: [])())
        except Exception as e:                              # noqa: BLE001
            rows.append({'check': 'Session', 'ok': False,
                         'detail': f"{type(e).__name__}: {e}", 'fix': ''})
        finally:
            try:
                gateway.stop()
            except Exception:                               # noqa: BLE001
                pass

        return jsonify({
            'ok': all(r['ok'] for r in rows),
            'rows': rows,
            'simulated': simulated,
        })

    @app.post('/api/contracts/<path:key>/read-from-venue')
    def api_read_specs(key):
        """Fill the specifications from the venue's own security definition.

        It REPORTS rather than applies: each field comes back with what the
        venue says and what the config holds, and the operator presses the
        button. A specification changed under a running desk is every money
        figure on that window changing without anybody being told.
        """
        config = load_config()
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        venue = config.venues.get(contract.venue)
        if venue is None:
            return jsonify({'ok': False,
                            'error': f"contract {key} has no venue"}), 400

        if venue.host:
            return jsonify({'ok': False, 'simulated': False,
                            'error': 'Security-definition translation is not implemented by the connection-only TT adapter.'})
        gateway, _, simulated = _gateway_for(config, venue)
        try:
            gateway.start()
            gateway.subscribe(contract)
            spec = gateway.security_definition(contract)
        finally:
            try:
                gateway.stop()
            except Exception:                               # noqa: BLE001
                pass

        if spec is None:
            return jsonify({'ok': False, 'simulated': simulated,
                            'error': "the venue did not publish a definition "
                                     "for this contract"})
        fields = []
        for field in ('tick_size', 'tick_value', 'contract_multiplier',
                      'currency', 'min_qty', 'qty_step', 'max_qty'):
            theirs = getattr(spec, field, None)
            ours = getattr(contract, field, None)
            fields.append({
                'field': field, 'venue': theirs, 'config': ours,
                'agrees': theirs is None or ours is None or theirs == ours,
                'source': contract.spec_source.get(field, 'unset'),
            })
        return jsonify({'ok': True, 'simulated': simulated, 'fields': fields,
                        'trading_status': spec.trading_status,
                        'note': SIMULATED_SPEC_NOTE if simulated else None})

    return app


def _price_fills(config, db, tt, manual):
    """Name each TT fill and put a P&L on each CLOSING fill of ours, in
    place: the Algo's against the position it closed, a manual ticket's
    against the ticket it closed (`close_of`). (fill - entry) x side, in
    money through `sizing.to_money`. The contract's figures are the desk
    contract's — what the trader set — then the watchlist's as TT defines
    them now, the ticket's own copy last (it may predate a units fix). A
    P&L that cannot be priced is None, never 0 and never a failed page."""
    orders = {o['clordid']: o for o in db.orders(limit=100000)}
    positions = {}
    for p in db.closed_positions(limit=100000):
        positions[p.id] = (p.side.value, p.avg_price)
    for p in db.open_positions():
        positions.setdefault(p.id, (p.side.value, p.avg_price))
    by_sid = {str(getattr(c, 'security_id', '') or ''): c
              for c in config.contracts.values() if getattr(c, 'security_id', '')}
    m_orders = list(manual.get('orders') or [])
    m_by_id = {o.get('id'): o for o in m_orders}
    m_owner = {}
    for o in m_orders:
        for cid in list(o.get('ids') or []) + [o.get('current_id'), o.get('id')]:
            if cid:
                m_owner[cid] = o
    watch = {str((w.get('instrument') or {}).get('security_id') or ''): w.get('instrument') or {}
             for w in manual.get('watchlist') or []}

    def money(points, tick, value, qty):
        try:
            out = sizing.to_money(points, float(tick) if tick else None,
                                  float(value) if value else None, float(qty or 0))
        except (TypeError, ValueError):
            return None
        return round(out, 2) if out is not None else None

    for f in tt:
        order = orders.get(f['clordid']) or orders.get(f.get('orig_clordid') or '')
        contract = config.contracts.get(f.get('contract_key') or
                                        (order or {}).get('contract_key') or '')
        f['name'] = contract.name if contract else (f['symbol'] or f['security_id'])
        f['decimals'] = contract.decimals if contract else None
        f['intent'] = (order or {}).get('intent')
        f['order_type'] = (order or {}).get('order_type')
        f['position_id'] = (order or {}).get('position_id')
        f['pnl'] = None
        if f.get('price') is None:
            continue
        if order is not None:
            pos = positions.get(f['position_id'])
            if order.get('intent') == 'CLOSE' and pos and contract and pos[1] is not None:
                sign = 1 if pos[0] == 'BUY' else -1
                f['pnl'] = money((f['price'] - pos[1]) * sign, contract.tick_size,
                                 contract.tick_value, f['qty'])
            continue
        closing = m_owner.get(f['clordid'])
        entry = m_by_id.get((closing or {}).get('close_of'))
        if not entry or entry.get('avg_price') is None:
            continue
        ticket = entry.get('ticket') or {}
        sid = str(ticket.get('security_id') or f.get('security_id') or '')
        desk = by_sid.get(sid)
        if desk is not None and desk.tick_size and desk.tick_value:
            tick, value = desk.tick_size, desk.tick_value
        else:
            inst = watch.get(sid) or ticket.get('instrument') or {}
            tick, value = inst.get('tick_size'), inst.get('tick_value')
        sign = -1 if ticket.get('side') == 'SELL' else 1
        f['pnl'] = money((f['price'] - float(entry['avg_price'])) * sign, tick, value, f['qty'])
    return tt


def _csv(filename, columns, rows):
    """A CSV with EMPTY cells where nothing was measured, never zeros.

    A spreadsheet full of zeros averages them in, and the figure that comes
    out reads like a measurement.
    """
    import csv
    import io
    from flask import Response
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        out = []
        for column in columns:
            value = row.get(column)
            if value is None:
                out.append('')
            elif isinstance(value, (list, tuple)):
                out.append(' '.join(str(v) for v in value))
            else:
                out.append(value)
        writer.writerow(out)
    return Response(buf.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition': f'attachment; filename="{filename}"'})


#: What may be written to a venue from the UI. `password` is handled on its
#: own and never lands here; anything not on this list is ignored rather than
#: quietly set.
VENUE_FIELDS = (
    'environment', 'broker', 'host', 'port', 'md_host', 'md_port',
    'recovery_host', 'recovery_port',
    'sender_comp_id', 'target_comp_id', 'sender_sub_id', 'target_sub_id',
    'on_behalf_of_comp_id', 'fix_version', 'username', 'account',
    'heartbeat_sec', 'reset_seq_on_logon', 'use_tls', 'data_dictionary',
    'store_path', 'log_path', 'enabled',
)


#: What the simulator can and cannot tell you. It is built from the contract's
#: own configuration, so it reports back exactly what it was given — which
#: means it can never disagree, and a green Diagnose against it confirms
#: NOTHING about the specifications. Saying so is the difference between a
#: check and the appearance of one.
SIMULATED_SPEC_NOTE = (
    "the simulator reports back what you configured, so this confirms "
    "nothing — these figures are checked against the venue when the FIX "
    "session is wired")


def _contract_check(gateway, contract, simulated=False):
    """One contract against the venue's own definition of it."""
    spec = gateway.security_definition(contract)
    if spec is None:
        return {'check': contract.key, 'ok': False,
                'detail': 'the venue published no definition',
                'fix': 'Check the symbol, and how this venue identifies a '
                       'spread contract (docs/FIX_NOTES.md).'}
    for field in ('tick_size', 'tick_value', 'contract_multiplier'):
        theirs = getattr(spec, field, None)
        ours = getattr(contract, field, None)
        if theirs is not None and ours is not None and theirs != ours:
            return {
                'check': contract.key, 'ok': False,
                'detail': (f"venue says {field} is {theirs}, "
                           f"config says {ours}"),
                # Not a warning. Every money figure on that window runs
                # through these two numbers.
                'fix': f"Read the specifications from the venue and accept "
                       f"{theirs}, or correct the config.",
            }
    detail = (f"tick {spec.tick_size} value {spec.tick_value} "
              f"mult {spec.contract_multiplier} {spec.currency} "
              f"{spec.trading_status or ''}").strip()
    if simulated:
        detail += f" — {SIMULATED_SPEC_NOTE}"
    return {'check': contract.key, 'ok': True, 'detail': detail, 'fix': ''}


def main(argv=None) -> int:
    import argparse
    import logging
    parser = argparse.ArgumentParser(description="FIX-Trader web")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [web] %(message)s")
    app = create_app(args.config, args.status, args.commands, args.results)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
