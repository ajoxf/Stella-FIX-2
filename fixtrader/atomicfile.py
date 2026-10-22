"""Write a file so a reader never sees half of it.

`open(path, 'w')` truncates first. A reader in that window sees an empty or
partial file — and in front of a read-modify-write save, that read an EMPTY
config and wrote it back, deleting every account. This is one small module so
that no caller has to remember the sequence.
"""

import json
import os
import tempfile
import time
from typing import Any

#: How many times to retry the final rename, and how long to wait between.
#:
#: **This is Windows, and it is not optional there.** POSIX lets you rename
#: over a file another process has open; Windows refuses with
#: `PermissionError: [WinError 5] Access is denied` until every handle on the
#: destination is closed. The web process reads `status.json` twice a second
#: and the engine publishes it several times a second, so on a Windows desk
#: the two collide within a minute — and the first time it happened the
#: exception came out of the engine's own loop and STOPPED IT. An engine that
#: dies because a browser had a display file open is a system that stops
#: managing live positions for a reason that has nothing to do with trading.
#:
#: The reader holds the handle for microseconds, so a handful of short
#: retries clears it. Antivirus scanners on the temp file behave the same way.
REPLACE_ATTEMPTS = 12
REPLACE_BACKOFF_SEC = 0.01


def write_text(path: str, text: str, encoding: str = "utf-8",
               attempts: int = REPLACE_ATTEMPTS,
               replace=os.replace, sleep=time.sleep,
               durable: bool = True) -> None:
    """Replace `path` with `text` atomically, retrying a locked destination.

    `replace` and `sleep` are injectable so the Windows failure can be tested
    on a machine that does not have it.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            if durable:
                os.fsync(fh.fileno())

        last = None
        for attempt in range(max(1, attempts)):
            try:
                replace(tmp, path)
                return
            except PermissionError as e:
                # Windows: somebody has the destination open. Wait for them.
                last = e
                sleep(REPLACE_BACKOFF_SEC * (attempt + 1))
            except OSError as e:
                # Anything else is not a lock and will not clear by waiting.
                last = e
                break
        raise last
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: str, data: Any, indent: int = 2,
               durable: bool = True) -> None:
    write_text(path, json.dumps(data, indent=indent, sort_keys=False,
                                default=str) + "\n", durable=durable)


def read_json(path: str, default: Any = None) -> Any:
    """Read, returning `default` when the file is missing.

    A file that exists but will not parse RAISES: a corrupt config that reads
    as an empty one is how a system starts up flat and reports everything at
    the venue as an orphan.
    """
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.strip():
        return default
    return json.loads(text)
