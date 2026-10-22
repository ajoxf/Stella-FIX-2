"""Small, read-only quote channel between the engine and Flask processes."""
import copy
import json
import logging
import os
import threading
import time

from . import atomicfile


class QuotePublisher:
    def __init__(self, terminal, status_path):
        self.terminal = terminal
        self.path = str(status_path) + '.quotes.json'
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name='quote-publisher', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.terminal.quote_changed.set()
        self.thread.join(timeout=2)

    def publish(self):
        with self.terminal.lock:
            md = self.terminal.gateway._sessions.get('Market Data')
            payload = {'source': 'TT_FIX_UAT', 'simulated': False,
                       'published_ms': time.time_ns() / 1_000_000,
                       'connected': bool(md and md.state.status == 'CONNECTED' and md.is_running()),
                       'quotes': copy.deepcopy({k: {field: value for field, value in book.items() if field != 'entries'}
                                  for k, book in self.terminal.books.items() if k in self.terminal.watch})}
        # This is a disposable latest-value frame, rebuilt after restart.
        # Orders, fills, configuration and the regular status snapshot remain
        # durable; forcing the physical disk to flush for every market tick
        # only adds latency and write pressure on Windows.
        atomicfile.write_json(self.path, payload, indent=None, durable=False)

    def run(self):
        failures = 0
        while not self.stopped.is_set():
            self.terminal.quote_changed.clear()
            try:
                self.publish()
                failures = 0
            except Exception:
                failures += 1
                if failures == 1:
                    logging.getLogger(__name__).exception('Quote stream publish failed; regular snapshots remain available')
            # Coalesce very small bursts without doing disk IO on the FIX
            # receiver thread. Five milliseconds keeps the receiver free and
            # still puts a normal update on screen within one display frame.
            if self.stopped.wait(0.005):
                break
            self.terminal.quote_changed.wait(1)


def events(path):
    """Latest-state stream: slow clients skip intermediate frames, never queue them."""
    previous = None
    previous_stamp = None
    heartbeat = time.monotonic()
    yield 'retry: 250\n\n'
    while True:
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            stamp = None
        if stamp == previous_stamp:
            payload = None
        else:
            previous_stamp = stamp
            try:
                payload = atomicfile.read_json(path, default=None)
            except (OSError, ValueError):
                payload = None
        if payload and payload.get('published_ms') != previous:
            previous = payload['published_ms']
            payload['server_sent_ms'] = time.time_ns() / 1_000_000
            yield 'data: ' + json.dumps(payload, separators=(',', ':')) + '\n\n'
            heartbeat = time.monotonic()
        elif time.monotonic() - heartbeat > 1:
            yield ': heartbeat\n\n'
            heartbeat = time.monotonic()
        # Five milliseconds is below a normal display frame while avoiding
        # hundreds of redundant JSON reads per second for a quiet market.
        time.sleep(0.005)
