"""The engine process: poll, drain commands, publish the snapshot.

It writes `status.json` atomically on every pass. The web process reads that
file and nothing else, so a slow browser cannot slow the loop and a dead
engine shows up as a snapshot that has stopped moving — which the banner says
out loud rather than leaving as a quiet market.
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import atomicfile
from .commands import CommandBridge, apply_command
from .config import TraderConfig
from .database import Database
from .engine import Engine

logger = logging.getLogger("fixtrader.runner")


def build_gateway(config, simulated: bool, runtime_dir: str = ""):
    """The simulator, or the real session once it is wired.

    Until a venue is configured this runs against `FakeGateway` and the
    screen says SIMULATED — which is a true statement about what is on it,
    and a good deal safer than a screen that looks live and is not.
    """
    # No venue, or a venue with no endpoint, means the simulator — and the
    # screen says SIMULATED where a PROD badge would go. The alternative is a
    # FIX session pointed at nothing, which looks like a connection failure
    # somebody could waste an afternoon retyping a port to fix.
    reachable = [v for v in config.venues.values() if v.host and v.enabled]
    require_live = config.settings.get('REQUIRE_LIVE_FIX') is True
    if require_live and simulated:
        raise RuntimeError('This configuration requires live FIX; simulated mode is refused')
    if require_live and not reachable:
        raise RuntimeError('This configuration requires live FIX but has no enabled FIX venue')
    if not simulated and not reachable:
        raise RuntimeError('Live FIX mode has no enabled FIX venue; simulated fallback is refused')
    if simulated:
        from .fake_gateway import FakeGateway, SimContract
        sims = []
        for c in config.contracts.values():
            sims.append(SimContract(
                c.key, mid=0.5, tick_size=c.tick_size or 0.01,
                tick_value=c.tick_value or 1.0,
                multiplier=c.contract_multiplier or 100.0,
                currency=c.currency or "USD"))
        return FakeGateway(sims), True
    from .gateway import FixGateway
    venue = reachable[0]
    state_dir = runtime_dir or os.path.dirname(os.path.abspath(config.path))
    manual_name = os.path.splitext(os.path.basename(config.path))[0] + '.manual.db'
    return FixGateway(venue, list(config.contracts.values()),
                      manual_path=os.path.join(state_dir, manual_name)), False


#: How fresh a snapshot has to be for us to conclude another engine is alive
#: and publishing. Generous: a paused debugger or a slow disk must not look
#: like a dead engine, because the cost of being wrong here is refusing to
#: start when nothing is running.
ANOTHER_ENGINE_WITHIN_SEC = 5.0


def another_engine_is_running(status_path: str,
                              within: float = ANOTHER_ENGINE_WITHIN_SEC
                              ) -> Optional[float]:
    """Seconds since the last snapshot, if one is being published right now.

    Two engines against one book is a genuinely bad accident: both trade the
    same signals on the same account, each sees the other's fills as positions
    it cannot explain, and the reconciler is handed a book that changes under
    it. An operator double-clicking the launcher is all it takes.

    The snapshot is the heartbeat — it is written every pass and nothing else
    writes it. A file being updated now means somebody is alive behind it.
    """
    try:
        raw = atomicfile.read_json(status_path, default=None)
        if not raw or not raw.get('ts'):
            return None
        from datetime import datetime as _dt
        age = (datetime.now(timezone.utc)
               - _dt.fromisoformat(raw['ts'])).total_seconds()
    except Exception:                                    # noqa: BLE001
        return None
    # An engine that stopped CLEANLY says so in its last snapshot. A fresh
    # file from an engine that has shut down is not a live engine, and
    # treating it as one refused every restart made within seconds — the
    # restart button's, and the launcher's after a clean exit.
    if (raw.get('engine') or {}).get('stopped'):
        return None
    return age if 0 <= age <= within else None


def run(config_path: str = "config.json", status_path: str = "status.json",
        command_path: str = "commands.jsonl",
        result_path: str = "results.json", simulated: bool = False,
        once: bool = False,
        should_stop: Optional[Callable[[], bool]] = None) -> None:
    """Run the engine loop until a signal, or until `should_stop` says so.

    `should_stop` is how a caller that is not the main thread stops us:
    signal handlers can only be installed on the main thread, and a test or
    a launcher that runs the engine beside something else is not it.
    """
    age = another_engine_is_running(status_path)
    if age is not None and not once:
        raise SystemExit(
            f"Another engine is already publishing {status_path} "
            f"({age:.1f}s ago). Two engines against one book both trade the "
            f"same signals and each sees the other's fills as positions it "
            f"cannot explain. Stop that one first, or point this at a "
            f"different --status and --config.")
    config = TraderConfig.from_file(config_path)
    runtime_dir = os.path.dirname(os.path.abspath(status_path))
    os.makedirs(runtime_dir, exist_ok=True)
    from .config import database_path as _database_path
    db = Database(_database_path(config, status_path))
    gateway, is_sim = build_gateway(config, simulated, runtime_dir)
    engine = Engine(config, gateway, db=db, simulated=is_sim,
                    mode_path=str(status_path) + '.mode.json')
    bridge = CommandBridge(command_path, result_path)
    bridge.prime()                    # a restart never replays a KILL ALL
    engine.start()
    quote_publisher = None
    if not is_sim and getattr(gateway, 'terminal', None) is not None:
        from .quote_stream import QuotePublisher
        quote_publisher = QuotePublisher(gateway.terminal, status_path)
        quote_publisher.start()

    stopping = {'now': False}

    def _stop(signum, frame):
        stopping['now'] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass                      # not the main thread; the caller stops us

    def _config_stamp() -> Optional[tuple]:
        """What makes an edited config.json distinguishable from the one we
        already read. Size as well as mtime: an editor that writes twice in
        the same clock tick is not a hypothetical on a fast disk."""
        try:
            st = os.stat(config_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    config_stamp = _config_stamp()

    interval = float(config.settings.get('ENGINE_POLL_SEC', 0.1) or 0.1)
    #: The snapshot is published at the SCREEN's rate, not the engine's. The
    #: screen refreshes twice a second, so writing it ten times a second was
    #: nine wasted writes and — on Windows, where a rename over an open file
    #: is refused — five times the chance of colliding with the reader.
    publish_every = float(config.settings.get('PRICE_REFRESH_SEC', 0.5) or 0.5)
    #: A switch must not wait for a price. Commands are drained on their own
    #: cadence THROUGH the wait between engine passes, so pressing an algo
    #: toggle or CLOSE NOW is answered in tens of milliseconds rather than
    #: whenever the next pass comes round. Until this existed the setting was
    #: on the Settings page doing nothing, which is the same class of lie as
    #: a saved setting that is never read back.
    command_every = float(config.settings.get('COMMAND_POLL_SEC', 0.02) or 0.02)
    published = {'at': 0.0, 'failures': 0}

    def publish() -> None:
        """Write the snapshot. NOTHING in here may stop the loop.

        The snapshot is what the screen reads; the loop is what manages live
        positions. An engine that died because a browser had the display file
        open would have stopped trading for a reason that has nothing to do
        with trading — which is exactly what happened the first time this ran
        on Windows.
        """
        published['at'] = time.monotonic()
        try:
            atomicfile.write_json(status_path, engine.snapshot())
            if published['failures']:
                logger.info("snapshot published again after %d failed "
                            "attempts", published['failures'])
                published['failures'] = 0
        except Exception as e:                               # noqa: BLE001
            published['failures'] += 1
            n = published['failures']
            if n in (1, 10) or n % 100 == 0:
                logger.warning(
                    "could not publish the snapshot (%s) — the engine is "
                    "still running; the screen will say its prices are not "
                    "live until this clears [%d in a row]", e, n)

    def drain_commands() -> bool:
        """Apply what the web process asked for. True if anything was."""
        acted = False
        for command in bridge.drain():
            bridge.record(command['id'], apply_command(engine, command))
            acted = True
        return acted

    logger.info("engine up — %d contracts, %s", len(engine.runtimes),
                "simulated" if is_sim else "live gateway")
    try:
        while not stopping['now'] and not (should_stop and should_stop()) \
                and not engine.restart_requested:
            started = time.monotonic()
            if is_sim:
                gateway.advance(seconds=interval)
            acted = drain_commands()

            # Settings are edited in the WEB process, which writes config.json
            # and nothing else. This is the engine reading it back — without
            # it, a saved setting sits on disk looking applied while the loop
            # goes on trading the old one.
            stamp = _config_stamp()
            if stamp is not None and stamp != config_stamp:
                config_stamp = stamp
                try:
                    engine.apply_config(TraderConfig.from_file(config_path))
                except Exception as e:                       # noqa: BLE001
                    # A half-written or unparseable file is not a reason to
                    # stop managing live positions. We keep the settings we
                    # have and try again when the file changes next.
                    logger.warning("could not read %s (%s) — the engine is "
                                   "still running on the settings it has",
                                   config_path, e)

            engine.poll()

            # A command changed something the operator is looking at, so the
            # snapshot goes out NOW rather than at the next screen tick. A
            # switch that has already been obeyed but still reads the old way
            # is a switch the operator presses again.
            if once or acted or started - published['at'] >= publish_every:
                publish()

            if once:
                break
            # Wait out the rest of the pass in slices, draining commands in
            # each one. The engine's own work stays on its interval.
            while not stopping['now'] and not (should_stop and should_stop()) \
                    and not engine.restart_requested:
                left = interval - (time.monotonic() - started)
                if left <= 0:
                    break
                time.sleep(min(command_every, left))
                if drain_commands():
                    publish()
    finally:
        if quote_publisher:
            quote_publisher.stop()
        # Sweep our own working orders at shutdown, scoped to our own ids.
        engine.stop()
        # The last word: nobody is behind this file any more. Written only
        # after the sweep, so a snapshot that says "stopped" never has a
        # working order of ours still being managed behind it.
        try:
            last = engine.snapshot()
            last.setdefault('engine', {})['stopped'] = True
            atomicfile.write_json(status_path, last)
        except Exception:                                    # noqa: BLE001
            pass
        logger.info("engine down%s", " — restarting to take on saved changes"
                    if engine.restart_requested else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FIX-Trader engine")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--simulated', action='store_true',
                        help="run against the simulator even if a venue exists")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [engine] %(message)s")
    run(args.config, args.status, args.commands, args.results, args.simulated)
    return 0


if __name__ == '__main__':
    sys.exit(main())
