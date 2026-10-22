"""Bounded, thread-safe handoff from FIX market data to the strategy loop."""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketDataEvent:
    contract_key: str
    security_id: str
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    received_at: datetime
    sequence: str


class AlgoDataFeed:
    """Coalesce bursts by contract; FIX callbacks only do bounded memory work."""

    def __init__(self, capacity=1024):
        if capacity < 1:
            raise ValueError('capacity must be positive')
        self.capacity = capacity
        self._lock = Lock()
        self._pending = OrderedDict()
        self._last_sequence = {}
        self.dropped = 0

    def publish(self, event):
        with self._lock:
            identity = (event.security_id, event.sequence)
            if event.sequence and self._last_sequence.get(event.contract_key) == identity:
                return False
            self._last_sequence[event.contract_key] = identity
            if event.contract_key in self._pending:
                self._pending.pop(event.contract_key)
                self.dropped += 1
            elif len(self._pending) >= self.capacity:
                self._pending.popitem(last=False)
                self.dropped += 1
            self._pending[event.contract_key] = event
            return True

    def drain(self):
        with self._lock:
            values = list(self._pending.values())
            self._pending.clear()
        return values

    def clear(self):
        with self._lock:
            self._pending.clear()
            self._last_sequence.clear()
