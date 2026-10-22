#!/usr/bin/env python3
"""One command. It creates what is missing, starts both processes and opens
the terminal in a window of its own.

    python start.py

Nothing to copy, nothing to edit. On a first run it writes `config.json` and
`.env`, brings the **web UI up first** (the venues are entered on that screen,
so it has to be reachable before there are any), starts the engine, opens the
browser and restarts a crashed child with backoff.

The web process comes up before the engine on purpose: a box with no venue
configured must still reach the screen that configures one.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
import webbrowser
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

#: Below this, the imports fail rather than the program misbehaving:
#: `typing.Protocol` is 3.8, and Flask's `@app.get` shortcut is Flask 2.0.
#: Anaconda's `base` environment is commonly 3.7 and carries an old Flask, so
#: the usual way to hit this is opening a new terminal and forgetting to
#: activate the environment.
MIN_PYTHON = (3, 9)
MIN_FLASK = (2, 0)


def flask_version() -> Optional[str]:
    """Flask's version, or None when it cannot be read.

    `flask.__version__` is deprecated and disappears in Flask 3.2, so the
    packaging metadata is asked first. None means "cannot tell", and the
    caller ALLOWS it: refusing to start because a version string could not be
    read would block a perfectly good environment — the failure this check
    exists to prevent, pointed the other way.
    """
    try:
        from importlib.metadata import version as _version
        return _version('flask')
    except Exception:                                    # noqa: BLE001
        pass
    try:
        import flask
        return getattr(flask, '__version__', None)
    except Exception:                                    # noqa: BLE001
        return None


def check_the_interpreter(say=print, version_of=flask_version) -> Optional[str]:
    """The reason this cannot run here, in words, or None.

    Checked BEFORE anything is started. Without it the children die on an
    ImportError, the launcher restarts them, and the real message scrolls past
    six times before anybody reads it — which is exactly what happened.
    """
    if sys.version_info < MIN_PYTHON:
        want = '.'.join(str(n) for n in MIN_PYTHON)
        have = '.'.join(str(n) for n in sys.version_info[:3])
        return (
            f"This needs Python {want} or newer and is running on {have}.\n"
            f"    interpreter: {sys.executable}\n"
            f"    If that path ends in Anaconda3\\python.exe you are in the "
            f"`base` environment.\n"
            f"    A new terminal does not keep the activation:\n"
            f"        conda activate fixtrader")
    try:
        import flask
    except ImportError:
        return (f"Flask is not installed for this interpreter.\n"
                f"    interpreter: {sys.executable}\n"
                f"        pip install -r requirements.txt")
    version = version_of()
    if version is None:
        return None                                      # cannot tell: allow
    try:
        parts = tuple(int(n) for n in version.split('.')[:2])
    except (ValueError, AttributeError):
        return None                                      # cannot tell: allow
    if parts < MIN_FLASK:
        want = '.'.join(str(n) for n in MIN_FLASK)
        return (f"This needs Flask {want} or newer and found {version}.\n"
                f"    interpreter: {sys.executable}\n"
                f"        pip install -r requirements.txt")
    return None

from fixtrader import appwindow                      # noqa: E402
from fixtrader.config import DEFAULT_SETTINGS, TraderConfig  # noqa: E402

#: What a first run gets: three plausible spread contracts against the
#: simulator, so the screen has something on it before a venue exists. They
#: are ordinary config rows and the operator deletes or edits them.
STARTER_CONTRACTS = {
    'fef_v6x6': {
        'name': 'Iron ore Oct/Nov', 'symbol': 'FEFV6-FEFX6', 'venue': '',
        'tick_size': 0.01, 'tick_value': 1.0, 'contract_multiplier': 100.0,
        'currency': 'USD', 'min_qty': 1.0, 'qty_step': 1.0, 'max_qty': 50.0,
        'decimals': 4, 'enabled': True, 'algo_on': True,
        'lookback': 120, 'stats_update_interval_sec': 10, 'quantity': 5.0,
        'commission_per_contract': 1.20, 'exchange_fee_per_contract': 0.55,
        'clearing_fee_per_contract': 0.15,
    },
    'fef_x6z6': {
        'name': 'Iron ore Nov/Dec', 'symbol': 'FEFX6-FEFZ6', 'venue': '',
        'tick_size': 0.01, 'tick_value': 1.0, 'contract_multiplier': 100.0,
        'currency': 'USD', 'min_qty': 1.0, 'qty_step': 1.0, 'max_qty': 50.0,
        'decimals': 4, 'enabled': True, 'algo_on': True,
        'lookback': 120, 'stats_update_interval_sec': 10, 'quantity': 4.0,
        'commission_per_contract': 1.20, 'exchange_fee_per_contract': 0.55,
        'clearing_fee_per_contract': 0.15,
    },
    'cl_z6f7': {
        'name': 'WTI Dec/Jan', 'symbol': 'CLZ6-CLF7', 'venue': '',
        'tick_size': 0.01, 'tick_value': 10.0, 'contract_multiplier': 1000.0,
        'currency': 'USD', 'min_qty': 1.0, 'qty_step': 1.0, 'max_qty': 20.0,
        'decimals': 4, 'enabled': True, 'algo_on': False,
        'lookback': 120, 'stats_update_interval_sec': 10, 'quantity': 2.0,
        'commission_per_contract': 2.50, 'exchange_fee_per_contract': 1.45,
        'clearing_fee_per_contract': 0.20,
    },
}


def first_run(config_path: str, env_path: str) -> None:
    """Write what is missing. The example contracts carry NO venue: until one
    is configured on the Exchanges page the system runs against the simulator
    and says SIMULATED, and a contract pointing at a venue that does not exist
    would read as a broken reference rather than as an honest default."""
    if not os.path.exists(config_path):
        config = TraderConfig(path=config_path)
        config.settings = dict(DEFAULT_SETTINGS)
        from fixtrader.config import ContractConfig
        for key, raw in STARTER_CONTRACTS.items():
            config.contracts[key] = ContractConfig.from_dict(key, raw)
        config.save()
        print(f"[start] wrote {config_path} with {len(config.contracts)} "
              f"example contracts against the simulator")
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as fh:
            fh.write("# Secrets live here and NOWHERE else. Never in\n"
                     "# config.json, never in code, never in a log line.\n"
                     "# The Exchanges page writes venue passwords here.\n")
        print(f"[start] wrote {env_path}")


def spawn(argv, name):
    print(f"[start] {name}: {' '.join(argv)}")
    # Marks the child as having a launcher behind it: an engine asked to
    # restart from the screen exits, and this loop starts it again.
    env = dict(os.environ, FIXTRADER_SUPERVISED='1')
    return subprocess.Popen(argv, cwd=HERE, env=env)


#: How long a child gets to stop on its own. The engine needs it: its
#: shutdown cancels our working orders and sends TT a Logout. Killed before
#: that, TT keeps the sessions logged on — "Session is already connected on
#: host ..." — and refuses the next start until its own timeout drops them.
GRACEFUL_STOP_SEC = 25.0


def stop_children(children, graceful=GRACEFUL_STOP_SEC, windows=None):
    """Stop the children the way they can shut down cleanly, and only then
    by force.

    `Popen.terminate()` on Windows is TerminateProcess: no handler runs, so
    the engine never logged out of TT. There, Ctrl+C has already reached the
    children (they share this console), so they are WAITED for; elsewhere
    they are sent SIGINT, which the runner handles. Whatever is still alive
    after `graceful` seconds is terminated, then killed.
    """
    windows = (os.name == 'nt') if windows is None else windows
    if not windows:
        for proc in children.values():
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGINT)
                except OSError:
                    pass
    deadline = time.monotonic() + graceful
    for proc in children.values():
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for proc in children.values():
        if proc.poll() is None:
            proc.terminate()
    for proc in children.values():
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start FIX-Trader")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--simulated', action='store_true', default=False,
                      help='Use the simulator for development only; never sends FIX orders')
    mode.add_argument('--fix', dest='simulated', action='store_false',
                      help='Use configured TT FIX sessions (default)')
    args = parser.parse_args(argv)

    # Before anything is started, and before any file is written.
    problem = check_the_interpreter()
    if problem:
        print("[start] cannot run here:\n    " + problem)
        return 2
    print(f"[start] python {'.'.join(str(n) for n in sys.version_info[:3])} "
          f"at {sys.executable}")

    first_run(args.config, '.env')
    url = f"http://{args.host}:{args.port}/"
    python = sys.executable

    common = ['--config', args.config, '--status', args.status,
              '--commands', args.commands, '--results', args.results]
    children = {}

    # The WEB first: the venues are entered on that screen, so it has to be
    # reachable before there are any.
    children['web'] = spawn([python, '-m', 'fixtrader.webapp'] + common +
                            ['--host', args.host, '--port', str(args.port)], 'web')
    time.sleep(1.0)
    engine_argv = [python, '-m', 'fixtrader.runner'] + common
    if args.simulated:
        engine_argv.append('--simulated')
    children['engine'] = spawn(engine_argv, 'engine')

    if not args.no_browser:
        appwindow.open_window(url)
    print(f"[start] terminal at {url}")

    backoff = {'web': 1.0, 'engine': 1.0}
    #: A child that dies IMMEDIATELY, over and over, is not going to be fixed
    #: by waiting: it is a bad interpreter, a missing package, a port already
    #: taken. Restarting it forever scrolls the one message that explains it
    #: off the screen, which is worse than stopping.
    instant_failures = {'web': 0, 'engine': 0}
    started_at = {name: time.monotonic() for name in children}
    GIVE_UP_AFTER = 3
    ALIVE_LONG_ENOUGH = 15.0

    try:
        while True:
            time.sleep(1.0)
            for name, proc in list(children.items()):
                if proc.poll() is None:
                    backoff[name] = 1.0
                    if time.monotonic() - started_at[name] > ALIVE_LONG_ENOUGH:
                        instant_failures[name] = 0
                    continue

                lived = time.monotonic() - started_at[name]
                if lived < ALIVE_LONG_ENOUGH:
                    instant_failures[name] += 1
                if instant_failures[name] >= GIVE_UP_AFTER:
                    print(f"\n[start] {name} has died {instant_failures[name]} "
                          f"times in a row within {lived:.0f}s of starting. "
                          f"That is not something restarting will fix — read "
                          f"the error above this line. Stopping.")
                    return 1

                wait = backoff[name]
                print(f"[start] {name} exited ({proc.returncode}) after "
                      f"{lived:.0f}s; restarting in {wait:.0f}s")
                time.sleep(wait)
                backoff[name] = min(30.0, wait * 2)
                started_at[name] = time.monotonic()
                if name == 'web':
                    children[name] = spawn(
                        [python, '-m', 'fixtrader.webapp'] + common +
                        ['--host', args.host, '--port', str(args.port)], 'web')
                else:
                    children[name] = spawn(engine_argv, 'engine')
    except KeyboardInterrupt:
        print("\n[start] stopping")
    finally:
        print("[start] waiting for the engine to log out of TT…")
        stop_children(children)
    return 0


if __name__ == '__main__':
    sys.exit(main())
