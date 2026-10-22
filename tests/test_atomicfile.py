"""Atomic saves, and the Windows failure that stopped the engine once."""
import json
import os

import pytest

from fixtrader import atomicfile


def test_a_save_lands_whole(tmp_path):
    path = str(tmp_path / 'x.json')
    atomicfile.write_json(path, {'a': 1})
    assert json.load(open(path)) == {'a': 1}


def test_disposable_save_can_skip_physical_disk_flush(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, 'fsync', lambda fd: calls.append(fd))
    atomicfile.write_json(str(tmp_path / 'quote.json'), {'bid': 1}, durable=False)
    assert calls == []
    atomicfile.write_json(str(tmp_path / 'order.json'), {'id': 1})
    assert len(calls) == 1


def test_no_temp_files_are_left_behind(tmp_path):
    path = str(tmp_path / 'x.json')
    for _ in range(5):
        atomicfile.write_json(path, {'a': 1})
    assert [p.name for p in tmp_path.iterdir()] == ['x.json']


def test_a_locked_destination_is_retried_rather_than_raising(tmp_path):
    """Windows refuses a rename over a file another process has open —
    `PermissionError: [WinError 5]`. The reader holds it for microseconds, so
    waiting clears it. On POSIX this never happens, which is why it has to be
    injected to be tested at all."""
    path = str(tmp_path / 'status.json')
    attempts = {'n': 0}

    def flaky_replace(src, dst):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise PermissionError(5, "Access is denied")
        os.replace(src, dst)

    atomicfile.write_text(path, 'hello', replace=flaky_replace,
                          sleep=lambda s: None)
    assert open(path).read() == 'hello'
    assert attempts['n'] == 3


def test_a_destination_locked_for_ever_eventually_raises(tmp_path):
    """It must not spin silently: the caller decides what to do about it."""
    path = str(tmp_path / 'status.json')

    def always_locked(src, dst):
        raise PermissionError(5, "Access is denied")

    with pytest.raises(PermissionError):
        atomicfile.write_text(path, 'hello', attempts=3,
                              replace=always_locked, sleep=lambda s: None)
    # ...and it does not leave its temp file behind when it gives up
    assert list(tmp_path.iterdir()) == []


def test_a_failure_that_is_not_a_lock_is_not_retried(tmp_path):
    """Waiting will not fix a missing directory, and retrying twelve times
    only delays the error."""
    path = str(tmp_path / 'status.json')
    attempts = {'n': 0}

    def broken(src, dst):
        attempts['n'] += 1
        raise OSError(22, "Invalid argument")

    with pytest.raises(OSError):
        atomicfile.write_text(path, 'hello', replace=broken,
                              sleep=lambda s: None)
    assert attempts['n'] == 1
