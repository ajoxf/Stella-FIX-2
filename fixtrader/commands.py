"""The bridge: the web process asks, the engine acts.

They are separate processes on purpose. The browser must not be able to stop
an exit by crashing, and a dead engine must be *visible* rather than looking
like a quiet market — which is what the banner reads this file for.

Commands are appended as JSON lines and results written beside them. The file
is **primed at startup**: everything already in it is marked seen without
being run, so a restart never replays yesterday's KILL ALL.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import atomicfile


class CommandBridge:
    def __init__(self, command_path: str = "commands.jsonl",
                 result_path: str = "results.json"):
        self.command_path = command_path
        self.result_path = result_path
        self._lock = threading.Lock()
        self._offset = 0

    # -- the web side ------------------------------------------------------

    def submit(self, action: str, contract: str = "",
               args: Optional[Dict[str, Any]] = None) -> str:
        command_id = uuid.uuid4().hex[:12]
        line = json.dumps({
            'id': command_id, 'action': action, 'contract': contract,
            'args': args or {},
            'ts': datetime.now(timezone.utc).isoformat(),
        })
        with self._lock:
            with open(self.command_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return command_id

    def result(self, command_id: str) -> Optional[Dict[str, Any]]:
        results = atomicfile.read_json(self.result_path, default={}) or {}
        return results.get(command_id)

    # -- the engine side ---------------------------------------------------

    def prime(self) -> None:
        """Mark everything already in the file as seen, WITHOUT running it.

        A restart that replays the log re-sends every order of the day in the
        first second. This is the line that stops it.
        """
        if os.path.exists(self.command_path):
            self._offset = os.path.getsize(self.command_path)
        else:
            self._offset = 0

    def drain(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.command_path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.command_path, "r", encoding="utf-8") as fh:
            fh.seek(self._offset)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue           # a half-written line; it will be re-read
            self._offset = fh.tell()
        return out

    def record(self, command_id: str, result: Dict[str, Any]) -> None:
        results = atomicfile.read_json(self.result_path, default={}) or {}
        results[command_id] = result
        # Keep the file from growing without bound; the UI only ever asks
        # about a command it has just sent.
        if len(results) > 500:
            for key in list(results)[:-200]:
                results.pop(key, None)
        atomicfile.write_json(self.result_path, results)


#: Set by start.py on the processes it launches and restarts. An engine that
#: has it can be asked to restart; one without it has nobody to start it.
SUPERVISED_ENV = "FIXTRADER_SUPERVISED"


def apply_command(engine, command: Dict[str, Any]) -> Dict[str, Any]:
    """Run one command against the engine. Never raises: a bad command from
    the UI must not take the engine down with it."""
    action = command.get('action', '')
    key = command.get('contract', '')
    args = command.get('args') or {}
    try:
        if action.startswith('terminal_'):
            terminal = getattr(engine.gateway, 'terminal', None)
            if terminal is None:
                raise ValueError('Start the TT FIX engine to use instruments and manual orders')
            operation = action[len('terminal_'):]
            methods = {'search': terminal.lookup, 'add': terminal.add, 'remove': terminal.remove,
                       'depth': terminal.depth,
                       'preview': terminal.preview, 'preview_close': terminal.preview_close, 'submit': terminal.submit,
                       'risk': terminal.set_risk,
                       'cancel': terminal.manage,
                       'replace': lambda data: terminal.manage(data, replace=True)}
            if operation not in methods:
                raise ValueError('Unknown terminal action')
            return methods[operation](args)
        if action in ('fix_connect', 'fix_status', 'fix_disconnect', 'fix_reconnect'):
            gateway = engine.gateway
            venue = getattr(gateway, 'venue', None)
            if venue is None or venue.name != args.get('venue'):
                return {'ok': False, 'rows': [{'check': 'Session', 'ok': False,
                    'detail': 'This venue is not owned by the running FIX engine.',
                    'fix': 'Restart with --fix and the intended venue enabled.'}],
                    'simulated': engine.simulated}
            if action in ('fix_connect', 'fix_reconnect'):
                sessions = getattr(gateway, '_sessions', {}).values()
                sequence_error = any(
                    'sequence mismatch' in str(
                        getattr(getattr(session, 'state', None), 'error', '')
                    ).lower()
                    for session in sessions)
                if sequence_error:
                    raise ValueError(
                        'TT FIX sequence mismatch. Connect/reconnect is blocked until '
                        'TT confirms the sequence reset or recovery procedure and '
                        'working orders and fills have been reconciled.')
            if action == 'fix_connect':
                gateway.start()
            elif action == 'fix_disconnect':
                gateway.stop()
            elif action == 'fix_reconnect':
                gateway.reconnect()
            rows = gateway.diagnose()[:1]
            return {'ok': True if action != 'fix_status' else gateway.state().value == 'LOGGED_ON',
                    'pending': bool(rows and rows[0].get('pending')),
                    'simulated': False, 'rows': rows}
        if action == 'algo_on':
            return engine.set_algo(key, True)
        if action == 'algo_off':
            return engine.set_algo(key, False)
        if action == 'master_algo':
            return engine.set_master(bool(args.get('on', True)))
        if action == 'algo_state':
            return engine.set_algo_state(key, args.get('state', ''))
        if action == 'auto_trade':
            return engine.set_auto_trade(bool(args.get('on', False)))
        if action == 'execution':
            return engine.set_execution(args.get('mode', ''),
                                        confirm=args.get('confirm') is True)
        if action == 'close_limit':
            return engine.close_at_limit(key, args.get('price'))
        if action == 'cancel_close_limit':
            return engine.cancel_close_limit(key)
        if action == 'close_now':
            return engine.close_now(key)
        if action == 'cancel_all':
            return {'ok': True,
                    'cancelled': engine.executor.cancel_all(
                        key or None, side=args.get('side') or None)}
        if action == 'uat_order':
            return engine.uat_order(key, args.get('side', ''),
                                    args.get('order_type', ''),
                                    args.get('price'), args.get('qty'))
        if action == 'depth':
            return engine.set_depth(key, args.get('on') is True)
        if action == 'refresh_feed':
            return engine.refresh_feed(key)
        if action == 'recover_missed':
            return engine.recover_now(bool(args.get('window')))
        if action == 'kill_all':
            return engine.kill_all(bool(args.get('close_positions', False)))
        if action == 'resume':
            return engine.resume()
        if action == 'restart_engine':
            # The engine stops the way it always stops — our own working
            # orders cancelled, the book already persisted — and the launcher
            # that started it starts it again, reading config.json afresh.
            # Without a launcher behind it, it would simply stop: refused.
            if not os.environ.get(SUPERVISED_ENV):
                return {'ok': False, 'error': 'this engine was not started by '
                        'start.py / run_fix.bat, so nothing would start it '
                        'again — stop it and start it by hand'}
            engine.restart_requested = True
            return {'ok': True, 'text': 'the engine is restarting to take on '
                    'the saved changes'}
        return {'ok': False, 'error': f"unknown action {action!r}"}
    except Exception as e:                       # noqa: BLE001
        return {'ok': False, 'error': f"{type(e).__name__}: {e}"}
