"""Durable, redacted audit trail for TT FIX messages."""
import json
import os
import threading
import queue
from datetime import datetime, timezone

SENSITIVE = {'95', '96', '553', '554'}


def redact_raw(raw):
    sep = '\x01' if '\x01' in (raw or '') else '|'
    out = []
    for field in (raw or '').split(sep):
        tag, mark, value = field.partition('=')
        out.append(tag + mark + ('[redacted]' if mark and tag in SENSITIVE else value))
    return '|'.join(out)


class FixAuditLog:
    def __init__(self, directory='logs/fix', async_write=False):
        self.directory = os.path.abspath(directory)
        self.lock = threading.RLock()
        os.makedirs(self.directory, exist_ok=True)
        self.pending = queue.Queue() if async_write else None
        if self.pending is not None:
            threading.Thread(target=self._writer, name='fix-audit', daemon=True).start()

    @property
    def path(self):
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        return os.path.join(self.directory, f'fix-{day}.jsonl')

    def write(self, **event):
        row = {'timestamp': datetime.now(timezone.utc).isoformat(), **event}
        if 'raw' in row:
            row['raw'] = redact_raw(row['raw'])
        if self.pending is not None:
            self.pending.put(row)
            return
        self._append(row)

    def _append(self, row):
        with self.lock, open(self.path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, separators=(',', ':'), ensure_ascii=True) + '\n')

    def _writer(self):
        while True:
            row = self.pending.get()
            try:
                self._append(row)
            finally:
                self.pending.task_done()

    def read(self, limit=500, category='', search=''):
        rows = []
        if os.path.exists(self.path):
            with self.lock, open(self.path, encoding='utf-8') as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if category and category != 'All' and row.get('category') != category:
                        continue
                    if search and search.lower() not in json.dumps(row).lower():
                        continue
                    rows.append(row)
        return rows[-max(1, min(int(limit), 5000)):][::-1]

    def clear(self):
        with self.lock:
            open(self.path, 'w', encoding='utf-8').close()
