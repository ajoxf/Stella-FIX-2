"""The launcher's guard rails.

Both tests here exist because of one incident: the launcher was run on
Anaconda's `base` environment (Python 3.7, old Flask), the children died on an
ImportError, and it restarted them in a loop — printing the same traceback six
times until the one line that explained it had scrolled away.
"""
import sys

import pytest

sys.path.insert(0, __import__('os').path.dirname(
    __import__('os').path.dirname(__import__('os').path.abspath(__file__))))

import start  # noqa: E402


def test_this_interpreter_is_acceptable():
    assert start.check_the_interpreter() is None


def test_an_old_python_is_refused_with_the_command_that_fixes_it(monkeypatch):
    """Naming the interpreter matters: the whole confusion was not knowing
    WHICH python was running."""
    monkeypatch.setattr(start.sys, 'version_info', (3, 7, 9))
    problem = start.check_the_interpreter()
    assert problem is not None
    assert '3.7.9' in problem
    assert sys.executable in problem          # which python, exactly
    assert 'conda activate fixtrader' in problem
    assert 'base' in problem                  # names the likely cause


def test_an_old_flask_is_refused_by_name():
    """Flask 1.x has no @app.get, so every route raises AttributeError at
    import — a failure that reads as a code bug, not an environment one."""
    problem = start.check_the_interpreter(version_of=lambda: '1.1.2')
    assert problem is not None
    assert '1.1.2' in problem
    assert 'requirements.txt' in problem


def test_a_version_that_cannot_be_read_is_allowed_not_refused():
    """`flask.__version__` is deprecated and goes away in Flask 3.2. Reading
    it wrongly and refusing would block a perfectly good environment — the
    failure this whole function exists to prevent, pointed the other way."""
    assert start.check_the_interpreter(version_of=lambda: None) is None
    assert start.check_the_interpreter(version_of=lambda: 'weird') is None


def test_the_real_version_reader_answers_something_usable():
    version = start.flask_version()
    assert version is None or version[0].isdigit()


def test_the_version_check_runs_before_anything_is_written(tmp_path, monkeypatch,
                                                           capsys):
    """It must refuse before it creates config.json and .env, or a bad run
    leaves files behind that look like a successful first run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(start.sys, 'version_info', (3, 7, 9))
    code = start.main(['--no-browser'])
    assert code == 2
    assert not (tmp_path / 'config.json').exists()
    assert not (tmp_path / '.env').exists()
    assert 'cannot run here' in capsys.readouterr().out


def test_the_windows_launcher_does_not_die_on_the_first_log_line():
    """Python's logging writes to stderr. Under Windows PowerShell 5.1 a
    native command's stderr merged with 2>&1 is an ErrorRecord, and with
    $ErrorActionPreference = 'Stop' the first one — Flask's development-server
    warning — killed the launcher before it started the engine, leaving a web
    page with no engine behind it."""
    import os
    import re
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'run_fix.ps1')
    script = open(path, encoding='utf-8').read()
    launch = script.index('& $python start.py')
    settings = re.findall(r"\$ErrorActionPreference\s*=\s*'(\w+)'", script[:launch])
    assert settings and settings[-1] == 'Continue'
    assert 'ForEach-Object' in script[launch:]


def test_stopping_lets_the_engine_log_out_before_any_force():
    """TerminateProcess on Windows ran no handler, so TT kept the sessions
    and refused the next start: "Session is already connected on host". A
    child that exits on its own inside the grace period is never forced."""
    import subprocess as sp
    import start

    class Child:
        def __init__(self, exits_after):
            self.exits_after, self.waited, self.forced = exits_after, 0.0, []
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def send_signal(self, sig):
            self.forced.append('signal')

        def wait(self, timeout=None):
            if self.alive and self.exits_after <= timeout:
                self.alive = False
                return 0
            if self.alive:
                raise sp.TimeoutExpired('engine', timeout)
            return 0

        def terminate(self):
            self.forced.append('terminate')
            self.alive = False

        def kill(self):
            self.forced.append('kill')

    polite = Child(exits_after=3.0)
    start.stop_children({'engine': polite}, graceful=10.0, windows=True)
    assert polite.forced == []                   # waited for, never forced

    # the control: one that never exits is forced once the grace runs out
    stuck = Child(exits_after=1e9)
    start.stop_children({'engine': stuck}, graceful=0.2, windows=True)
    assert stuck.forced == ['terminate']

    # off Windows it is ASKED first, with the signal the runner handles
    asked = Child(exits_after=3.0)
    start.stop_children({'engine': asked}, graceful=10.0, windows=False)
    assert asked.forced == ['signal']
