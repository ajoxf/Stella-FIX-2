"""The guards on the price itself, per contract.

Two of them, and one rule that governs both:

    **A guard may withhold an ORDER. A guard must never prevent a close.**

Nothing in this module knows about positions, and nothing that consults it may
use it to hold back an exit. It answers two questions — is this quote stale,
and has the price just jumped — and the answers gate entries only.

- **Staleness** is measured on the quote CHANGING, not on messages arriving. A
  venue that republishes an unchanged book every second is not a live market,
  and a system that treats it as one keeps trading against a feed that has
  stopped.
- **The jump guard** is not a circuit breaker. It withholds new entries for a
  couple of seconds after a move that does not look like the market, then
  gets out of the way.
"""

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional

from .models import BookTop


class FeedGuard:
    """Staleness and jump detection for one contract."""

    def __init__(self, key: str, max_quote_age_sec: float = 15.0,
                 max_jump_sigma: float = 5.0, jump_settle_sec: float = 2.0,
                 sigma_window: int = 600):
        self.key = key
        self.max_quote_age_sec = float(max_quote_age_sec or 0.0)
        self.max_jump_sigma = float(max_jump_sigma or 0.0)
        self.jump_settle_sec = float(jump_settle_sec or 0.0)

        self.last_book: Optional[BookTop] = None
        #: When the quote last CHANGED. None until we have seen two of them —
        #: and None means "not measured", so the guard does not fire.
        self.last_change: Optional[datetime] = None
        self.last_seen: Optional[datetime] = None
        self._returns: Deque[float] = deque(maxlen=sigma_window)
        self._jumped_at: Optional[datetime] = None
        self.last_jump_size: Optional[float] = None

    def update_config(self, max_quote_age_sec: Optional[float] = None,
                      max_jump_sigma: Optional[float] = None,
                      jump_settle_sec: Optional[float] = None) -> None:
        if max_quote_age_sec is not None:
            self.max_quote_age_sec = float(max_quote_age_sec)
        if max_jump_sigma is not None:
            self.max_jump_sigma = float(max_jump_sigma)
        if jump_settle_sec is not None:
            self.jump_settle_sec = float(jump_settle_sec)

    def observe(self, book: Optional[BookTop], now: datetime) -> None:
        """Take one reading. A None or unusable book is NOT a quote."""
        if book is None or not book.usable:
            return
        self.last_seen = now
        previous = self.last_book
        changed = (previous is None or previous.bid != book.bid
                   or previous.ask != book.ask)
        if changed:
            if previous is not None and previous.mid is not None \
                    and book.mid is not None:
                move = book.mid - previous.mid
                self._returns.append(move)
                sigma = self._sigma()
                if sigma and self.max_jump_sigma and \
                        abs(move) > self.max_jump_sigma * sigma:
                    self._jumped_at = now
                    self.last_jump_size = abs(move) / sigma
            self.last_change = now
        self.last_book = book

    def _sigma(self) -> Optional[float]:
        n = len(self._returns)
        if n < 30:                       # too few to say what "normal" is
            return None
        m = sum(self._returns) / n
        var = sum((r - m) ** 2 for r in self._returns) / (n - 1)
        return var ** 0.5 or None

    def age(self, now: datetime) -> Optional[float]:
        """Seconds since the quote last changed. **None = not measured**, and
        the screen renders an em dash rather than a confident 0.0."""
        if self.last_change is None:
            return None
        return (now - self.last_change).total_seconds()

    def is_stale(self, now: datetime) -> bool:
        if not self.max_quote_age_sec:
            return False                 # the guard is off
        age = self.age(now)
        if age is None:
            return False                 # unmeasured is not stale
        return age > self.max_quote_age_sec

    def is_settling(self, now: datetime) -> bool:
        if self._jumped_at is None or not self.jump_settle_sec:
            return False
        return (now - self._jumped_at).total_seconds() < self.jump_settle_sec

    def status(self, now: datetime) -> Dict[str, Any]:
        age = self.age(now)
        return {
            'age_sec': round(age, 1) if age is not None else None,
            'stale': self.is_stale(now),
            'settling': self.is_settling(now),
            'jump_sigma': (round(self.last_jump_size, 1)
                           if self.last_jump_size else None),
        }


def in_session(now: datetime, open_hhmm: str, close_hhmm: str) -> bool:
    """Whether `now` is inside the contract's hours.

    **An unconfigured session is always open**, not always closed: a contract
    whose hours nobody has typed must not be silently untradeable. Sessions
    that wrap midnight (23:00-22:00, as energy contracts do) are handled by
    reading the window the other way round.
    """
    if not open_hhmm or not close_hhmm:
        return True
    try:
        oh, om = (int(x) for x in open_hhmm.split(':'))
        ch, cm = (int(x) for x in close_hhmm.split(':'))
    except (ValueError, AttributeError):
        return True                      # unparseable is not "closed"
    minute = now.hour * 60 + now.minute
    start, end = oh * 60 + om, ch * 60 + cm
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end        # wraps midnight
