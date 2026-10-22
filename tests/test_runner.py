"""The engine loop must outlive anything that goes wrong around it."""
import json
import time

import pytest

from fixtrader import atomicfile, runner
from fixtrader.config import ContractConfig, TraderConfig


def a_config(tmp_path):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'f.db')
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        lookback=20)
    cfg.save()
    return cfg


def test_live_locked_configuration_refuses_every_simulator(tmp_path):
    cfg = a_config(tmp_path)
    cfg.settings['REQUIRE_LIVE_FIX'] = True
    with pytest.raises(RuntimeError, match='simulated mode is refused'):
        runner.build_gateway(cfg, simulated=True)
    with pytest.raises(RuntimeError, match='no enabled FIX venue'):
        runner.build_gateway(cfg, simulated=False)


def test_fix_mode_never_falls_back_to_fake_prices(tmp_path):
    cfg = a_config(tmp_path)
    with pytest.raises(RuntimeError, match='simulated fallback is refused'):
        runner.build_gateway(cfg, simulated=False)


def run_once(tmp_path, **kw):
    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, once=True, **kw)


def _published(tmp_path):
    """The first contract's settings from the snapshot on disk, or {}."""
    try:
        snap = json.loads((tmp_path / 'status.json').read_text())
    except (OSError, ValueError):
        return {}                    # mid-write; the caller tries again
    return (snap.get('contracts') or [{}])[0].get('settings', {})


def test_a_pass_publishes_a_snapshot(tmp_path):
    a_config(tmp_path)
    run_once(tmp_path)
    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['contracts'][0]['key'] == 'fef'
    assert snap['engine']['alive'] is True


def test_relative_databases_follow_the_runtime_status_directory(tmp_path):
    config_dir = tmp_path / 'configuration'
    runtime_dir = tmp_path / 'runtime'
    config_dir.mkdir()
    runtime_dir.mkdir()
    cfg = TraderConfig(path=str(config_dir / 'config.json'))
    cfg.settings['DATABASE_PATH'] = 'fixtrader.db'
    cfg.save()

    runner.run(config_path=str(cfg.path),
               status_path=str(runtime_dir / 'status.json'),
               command_path=str(runtime_dir / 'commands.jsonl'),
               result_path=str(runtime_dir / 'results.json'),
               simulated=True, once=True)

    assert (runtime_dir / 'fixtrader.db').exists()
    assert not (config_dir / 'fixtrader.db').exists()


def test_the_engine_survives_a_snapshot_it_cannot_publish(monkeypatch, tmp_path):
    """The failure this test exists for actually happened, on Windows, on the
    first run: `os.replace` cannot rename over a file another process has
    open, the PermissionError came out of the loop, and the engine STOPPED —
    while positions were live. The snapshot is what the screen reads; the loop
    is what manages the money. A display file must never be able to stop it."""
    a_config(tmp_path)

    def refuses(*a, **k):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(atomicfile, 'write_json', refuses)
    run_once(tmp_path)                     # must return, not raise
    assert not (tmp_path / 'status.json').exists()


def test_a_restart_never_replays_yesterdays_commands(tmp_path):
    """A KILL ALL from last night must not fire again at breakfast."""
    from fixtrader.commands import CommandBridge
    a_config(tmp_path)
    bridge = CommandBridge(str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    old = bridge.submit('kill_all', '')
    run_once(tmp_path)                     # primes past it without running it
    assert bridge.result(old) is None


def test_a_second_engine_refuses_to_start_against_a_live_book(tmp_path):
    """Two engines against one book both trade the same signals on the same
    account, and each sees the other's fills as positions it cannot explain.
    An operator double-clicking the launcher is all it takes — and it happened
    here during development, leaving a database written by two processes."""
    import json
    from datetime import datetime, timezone
    a_config(tmp_path)
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True}, 'contracts': []}))

    with pytest.raises(SystemExit) as e:
        runner.run(config_path=str(tmp_path / 'config.json'),
                   status_path=str(status),
                   command_path=str(tmp_path / 'commands.jsonl'),
                   result_path=str(tmp_path / 'results.json'),
                   simulated=True)
    assert 'Another engine' in str(e.value)
    assert 'cannot explain' in str(e.value)        # it says why, not just no


def test_a_stale_snapshot_is_not_a_running_engine(tmp_path):
    """Refusing to start when nothing is running is the worse failure of the
    two, so the check is deliberately generous."""
    import json
    from datetime import datetime, timedelta, timezone
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'ts': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        'engine': {'alive': True}, 'contracts': []}))
    assert runner.another_engine_is_running(str(status)) is None

    a_config(tmp_path)
    run_once(tmp_path)                             # starts without complaint


def test_no_snapshot_at_all_is_not_a_running_engine(tmp_path):
    assert runner.another_engine_is_running(str(tmp_path / 'nothing.json')) is None


def test_an_edited_config_is_picked_up_while_the_loop_runs(tmp_path):
    """Settings are edited in the WEB process, which writes config.json and
    nothing else. Without the engine reading it back, a saved setting sits on
    disk looking applied while the loop goes on trading the old one."""
    cfg = a_config(tmp_path)
    # Driven by what the loop has actually DONE, not by a stopwatch: a test
    # that waits a fixed 0.15s passes alone and fails on a loaded box, which
    # makes it noise rather than a check.
    started = time.monotonic()
    edited = {'done': False}

    def stop_after_a_few():
        if not edited['done'] and (tmp_path / 'status.json').exists():
            # A snapshot exists, so the loop is running. Edited under it,
            # exactly as the web process would have written it.
            cfg.contracts['fef'].overrides['entry_threshold'] = 3.25
            cfg.save()
            edited['done'] = True
            return False
        if edited['done'] and _published(tmp_path).get('entry_threshold') == 3.25:
            return True
        return time.monotonic() - started > 20.0      # a bounded failure

    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, should_stop=stop_after_a_few)

    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['contracts'][0]['settings']['entry_threshold'] == 3.25
    assert snap['engine']['config_reloaded_at'] is not None
    assert snap['engine']['config_restart_needed'] == []


def test_an_unreadable_config_does_not_stop_the_loop(tmp_path):
    """A half-written file is not a reason to stop managing live positions."""
    a_config(tmp_path)
    started = time.monotonic()
    broken = {'passes': 0}

    def stop_after_a_few():
        if not broken['passes'] and (tmp_path / 'status.json').exists():
            (tmp_path / 'config.json').write_text('{ this is not json')
            broken['passes'] = 1
            return False
        if broken['passes']:
            broken['passes'] += 1
            # Long enough for the watcher to have seen the broken file and
            # for the loop to have published again after it.
            if broken['passes'] > 200 and (tmp_path / 'status.json').exists():
                return True
        return time.monotonic() - started > 20.0      # a bounded failure

    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, should_stop=stop_after_a_few)

    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['engine']['alive'] is True
    # the settings it already had are still the ones in force
    assert snap['contracts'][0]['settings']['entry_threshold'] is not None


def test_a_switch_does_not_wait_for_an_engine_pass(tmp_path):
    """`COMMAND_POLL_SEC` is on the Settings page saying a switch must not
    wait for a price. Until commands were drained through the wait between
    passes it did nothing, and pressing a toggle took a whole engine pass to
    be answered — the same class of lie as a saved setting never read back."""
    from fixtrader.commands import CommandBridge
    cfg = a_config(tmp_path)
    # A deliberately slow engine pass: if commands only drained once a pass,
    # this answer could not arrive inside the window asserted below.
    cfg.settings['ENGINE_POLL_SEC'] = 2.0
    cfg.settings['COMMAND_POLL_SEC'] = 0.02
    cfg.save()

    bridge = CommandBridge(str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    started = time.monotonic()
    sent = {'id': None, 'at': None}

    def stop_when_answered():
        # Sent once the loop is demonstrably up, so the measurement below is
        # of the answer and not of the start-up.
        if sent['id'] is None and (tmp_path / 'status.json').exists():
            sent['at'] = time.monotonic()
            sent['id'] = bridge.submit('algo_off', 'fef', {})
            return False
        if sent['id'] is not None and bridge.result(sent['id']) is not None:
            return True
        return time.monotonic() - started > 20.0     # a bounded failure

    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, should_stop=stop_when_answered)

    answered = bridge.result(sent['id'])
    assert answered is not None and answered.get('ok') is True
    # answered well inside one 2s engine pass, measured from the send
    assert time.monotonic() - sent['at'] < 1.5


def test_a_command_publishes_the_snapshot_at_once(tmp_path):
    """A switch that has already been obeyed but still reads the old way is
    a switch the operator presses again. The snapshot goes out on the
    command, not at the next screen tick."""
    from fixtrader.commands import CommandBridge
    cfg = a_config(tmp_path)
    cfg.settings['ENGINE_POLL_SEC'] = 2.0        # a slow pass
    cfg.settings['PRICE_REFRESH_SEC'] = 5.0      # and a slow screen
    cfg.save()

    bridge = CommandBridge(str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    started = time.monotonic()
    sent = {'id': None, 'at': None}

    def stop_when_the_screen_shows_it():
        if sent['id'] is None and (tmp_path / 'status.json').exists():
            sent['at'] = time.monotonic()
            sent['id'] = bridge.submit('algo_off', 'fef', {})
            return False
        if sent['id'] is not None and (tmp_path / 'status.json').exists():
            try:
                snap = json.loads((tmp_path / 'status.json').read_text())
            except ValueError:
                return False
            if snap['contracts'] and snap['contracts'][0]['algo_on'] is False:
                return True
        return time.monotonic() - started > 20.0      # a bounded failure

    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, should_stop=stop_when_the_screen_shows_it)

    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['contracts'][0]['algo_on'] is False
    # well inside one 5s screen refresh, and one 2s engine pass, measured
    # from the send rather than from the start of the process
    assert time.monotonic() - sent['at'] < 2.0


def test_a_restart_asked_for_from_the_screen_stops_cleanly_and_can_start_again(
        tmp_path, monkeypatch):
    """A new contract is taken on when the engine starts. The screen asks the
    engine to restart; it stops the way it always stops, says so in its last
    snapshot, and the launcher's next start is NOT refused as a second
    engine — which it would be for five seconds on a bare heartbeat."""
    from fixtrader.commands import CommandBridge
    monkeypatch.setenv('FIXTRADER_SUPERVISED', '1')
    a_config(tmp_path)
    bridge = CommandBridge(str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    started = time.monotonic()
    sent = {'id': None}

    def ask_once_then_time_out():
        if sent['id'] is None and (tmp_path / 'status.json').exists():
            sent['id'] = bridge.submit('restart_engine', '', {})
        return time.monotonic() - started > 20.0      # a bounded failure

    paths = dict(config_path=str(tmp_path / 'config.json'),
                 status_path=str(tmp_path / 'status.json'),
                 command_path=str(tmp_path / 'commands.jsonl'),
                 result_path=str(tmp_path / 'results.json'), simulated=True)
    runner.run(should_stop=ask_once_then_time_out, **paths)
    assert time.monotonic() - started < 20.0          # it stopped on the ask
    assert bridge.result(sent['id'])['ok'] is True
    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['engine']['stopped'] is True
    # a start a second later is not refused as a second engine
    assert runner.another_engine_is_running(paths['status_path']) is None
    runner.run(once=True, **paths)


def test_an_engine_nobody_will_start_again_refuses_to_restart(tmp_path,
                                                             monkeypatch):
    """The control: without a launcher behind it, a restart is a stop."""
    from fixtrader.commands import apply_command
    monkeypatch.delenv('FIXTRADER_SUPERVISED', raising=False)
    engine = type('E', (), {'restart_requested': False})()
    answer = apply_command(engine, {'action': 'restart_engine'})
    assert answer['ok'] is False and 'start it by hand' in answer['error']
    assert engine.restart_requested is False


def test_a_live_heartbeat_still_refuses_a_second_engine(tmp_path):
    """The control for the stopped marker: a snapshot that does NOT say it
    stopped, written just now, is still a running engine."""
    status = tmp_path / 'status.json'
    from datetime import datetime, timezone
    atomicfile.write_json(str(status), {
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True}})
    assert runner.another_engine_is_running(str(status)) is not None
