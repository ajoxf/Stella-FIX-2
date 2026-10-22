"""A venue that is not a venue: a real book, a real fill model, a real reject.

Every test in the suite runs against this, and so does the desk before UAT
credentials exist. It is deliberately not a stub that answers "filled" to
everything — the failures worth testing are the awkward ones:

- a LIMIT rests and fills only when the book actually trades through it,
  partially where the size on the touch is smaller than the order;
- a close that would exceed the open position is REFUSED, not silently
  reduced, because that is the failure that turns an exit into a reversal;
- a contract outside its trading hours REJECTS with text, the way a venue
  does, so the screen can be built against a real reject path.

The clock is the caller's. Nothing in here reads the wall clock, so a test
can run a day of touches in a millisecond and get the same answer every time.
"""

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import (BookTop, Fill, GatewayEvent, Intent, OrderRequest,
                     OrderState, OrderType, PositionEffect, SecurityDef,
                     SessionState, Side, VenueOrder, VenuePosition)
from .gateway import CLORDID_PREFIX


class SimContract:
    """One contract's simulated book, walking an OU process.

    Mean-reverting on purpose: this system trades mean reversion, so a
    simulator that produced a random walk would show a screen on which the
    algo correctly never trades, and nobody would be able to tell whether the
    algo or the simulator was at fault.
    """

    def __init__(self, key: str, mid: float = 0.5, tick_size: float = 0.01,
                 tick_value: float = 1.0, multiplier: float = 100.0,
                 sigma: float = 0.09, theta: float = 0.02,
                 spread_ticks: int = 1, size: float = 25.0,
                 margin_per_contract: float = 260.0, currency: str = "USD"):
        self.key = key
        self.anchor = mid
        self.mid = mid
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.multiplier = multiplier
        self.sigma = sigma
        self.theta = theta
        self.spread_ticks = spread_ticks
        self.size = size
        self.margin_per_contract = margin_per_contract
        #: Margin on a SPREAD, not on an outright. An exchange charges a
        #: fraction of the outright margin for a calendar because the two
        #: legs offset — and it matters here beyond realism: the profit
        #: target is a percentage OF this, so an outright-sized margin
        #: produces a target several sigma away that the spread will never
        #: reach, and every position runs to its stop or its session cutoff.
        #: theta is per SAMPLE, so the half-life is ln2/theta samples — about
        #: 35 at the default. A window of 120 samples therefore covers three
        #: or four reversions, which is what makes a rolling mean mean
        #: anything. Raise theta and the series reverts faster than the stats
        #: interval can follow it, and the bands go stale between recomputes.
        self.currency = currency
        self.open = True

    def step(self, rng: random.Random) -> None:
        """One observation of an Ornstein-Uhlenbeck process.

        The per-step noise is derived from the STATIONARY variance the caller
        asked for — `sigma` is the spread of the SERIES, not of one step —
        because sizing it by eye produced steps of about one tick, and a
        series quantised to the tick grid is a staircase whose runs of
        identical prices read as strongly TRENDING however mean-reverting the
        process underneath actually is.
        """
        pull = self.theta * (self.anchor - self.mid)
        step_sigma = self.sigma * math.sqrt(2.0 * self.theta)
        self.mid += pull + rng.gauss(0.0, step_sigma)

    def book(self, ts: datetime) -> BookTop:
        half = self.spread_ticks * self.tick_size / 2.0
        bid = round((self.mid - half) / self.tick_size) * self.tick_size
        ask = bid + self.spread_ticks * self.tick_size
        return BookTop(bid=round(bid, 10), ask=round(ask, 10),
                       bid_size=self.size, ask_size=self.size, ts=ts)


class FakeGateway:
    """Implements the `Gateway` protocol without a network."""

    def __init__(self, contracts: Optional[List[SimContract]] = None,
                 seed: int = 7, name: str = "SIM"):
        self.name = name
        self.rng = random.Random(seed)
        self.sim: Dict[str, SimContract] = {c.key: c for c in (contracts or [])}
        self._books: Dict[str, BookTop] = {}
        self._orders: Dict[str, VenueOrder] = {}
        #: LONG and SHORT kept apart, the way an offset-flag venue keeps
        #: them. Netting here would hide the failure this simulator exists to
        #: catch: an opposite order sent WITHOUT a close flag opens a second
        #: position, and a netting book would quietly show it as a close.
        self._long: Dict[str, Dict[str, float]] = {}
        self._short: Dict[str, Dict[str, float]] = {}
        self._events: List[GatewayEvent] = []
        #: The offset flag each order was sent with. The venue obeys it; it
        #: does not guess what the sender meant.
        self._effects: Dict[str, PositionEffect] = {}
        self._state = SessionState.DOWN
        self._seq = 0
        self._exec_seq = 0
        self.now: datetime = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

        #: Test hooks. `readable` False makes orders() and positions() return
        #: None — "could not read" — which is a state the real venue has and
        #: which the rest of the system must handle without treating it as
        #: "flat".
        self.readable = True
        self.reject_next: Optional[str] = None

    # -- session ---------------------------------------------------------

    def start(self) -> None:
        self._state = SessionState.LOGGED_ON

    def stop(self) -> None:
        self._state = SessionState.DOWN

    def state(self) -> SessionState:
        return self._state

    def state_text(self) -> str:
        if self._state is SessionState.LOGGED_ON:
            return "simulator — no venue is connected"
        return "simulator stopped"

    # -- the clock and the walk ------------------------------------------

    def advance(self, seconds: float = 0.5, steps: int = 1) -> None:
        """Move the simulated clock and walk every book. The caller's clock:
        nothing in this module reads the wall clock."""
        for _ in range(max(1, steps)):
            self.now = self.now + timedelta(seconds=seconds)
            for c in self.sim.values():
                c.step(self.rng)
                self._books[c.key] = c.book(self.now)
            self._match_resting()

    def set_book(self, key: str, bid: Optional[float], ask: Optional[float],
                 bid_size: Optional[float] = 10.0,
                 ask_size: Optional[float] = 10.0) -> None:
        """Put the book exactly where a test needs it."""
        self._books[key] = BookTop(bid=bid, ask=ask, bid_size=bid_size,
                                   ask_size=ask_size, ts=self.now)
        self._match_resting()

    # -- market data -----------------------------------------------------

    def subscribe(self, contract) -> None:
        key = getattr(contract, 'key', contract)
        if key not in self.sim:
            self.sim[key] = SimContract(key)
        self._books.setdefault(key, self.sim[key].book(self.now))

    def top_of_book(self, key: str) -> Optional[BookTop]:
        return self._books.get(key)

    def security_definition(self, contract) -> Optional[SecurityDef]:
        key = getattr(contract, 'key', contract)
        c = self.sim.get(key)
        if c is None:
            return None
        return SecurityDef(symbol=getattr(contract, 'symbol', key),
                           tick_size=c.tick_size, tick_value=c.tick_value,
                           contract_multiplier=c.multiplier,
                           currency=c.currency, min_qty=1.0, qty_step=1.0,
                           max_qty=50.0,
                           trading_status="OPEN" if c.open else "CLOSED")

    def margin_for(self, key: str, qty: float) -> Optional[float]:
        c = self.sim.get(key)
        if c is None or not qty:
            return None
        return c.margin_per_contract * abs(qty)

    # -- orders ----------------------------------------------------------

    def _next_clordid(self) -> str:
        self._seq += 1
        return f"{CLORDID_PREFIX}-{self._seq:06d}"

    def _emit(self, kind: str, order: VenueOrder, text: str = "",
              fill: Optional[Fill] = None) -> None:
        """Emit a COPY of the order, as a venue emits a distinct message.

        Handing out the live object made every queued event report the
        order's CURRENT state rather than its state at the time: an ACK
        drained after the order had already filled arrived saying FILLED, the
        reader marked it done, and the FILL that followed had no order to
        belong to — and was applied as an OPEN, doubling the position.
        """
        self._events.append(GatewayEvent(
            kind=kind, contract_key=order.contract_key, clordid=order.clordid,
            text=text, order=replace(order), fill=fill, ts=self.now))

    def send(self, order: OrderRequest) -> str:
        clordid = self._next_clordid()
        vo = VenueOrder(clordid=clordid, venue_id=f"SIM-{self._seq:06d}",
                        contract_key=order.contract_key, side=order.side,
                        qty=order.qty, price=order.price,
                        order_type=order.order_type,
                        state=OrderState.PENDING, ts=self.now)
        self._orders[clordid] = vo

        reject = self._reject_reason(order)
        if reject is not None:
            vo.state = OrderState.REJECTED
            vo.text = reject
            self._emit("REJECTED", vo, reject)
            return clordid

        vo.state = OrderState.WORKING
        self._emit("ACK", vo, "accepted")

        self._effects[clordid] = order.position_effect
        if order.order_type is OrderType.MARKET:
            self._cross(vo)
        else:
            self._match_resting()
        return clordid

    def _reject_reason(self, order: OrderRequest) -> Optional[str]:
        """The venue's own words, in the venue's own voice."""
        if self.reject_next is not None:
            text, self.reject_next = self.reject_next, None
            return text
        if self._state is not SessionState.LOGGED_ON:
            return "Session not logged on"
        c = self.sim.get(order.contract_key)
        if c is None:
            return f"Unknown symbol {order.contract_key}"
        if not c.open:
            return "Instrument not open for trading"
        if order.qty <= 0:
            return "Order quantity must be positive"

        if order.position_effect.is_close:
            # The rule that matters most in here. A close that exceeds what is
            # open on THAT SIDE would reverse it, and a simulator that quietly
            # allowed it would let the bug through to a live account.
            book = self._short if order.side is Side.BUY else self._long
            open_qty = (book.get(order.contract_key) or {}).get('qty', 0.0)
            if open_qty <= 0:
                return ("Close order with no position to close on that side")
            if order.qty > open_qty + 1e-9:
                return (f"Order would not reduce position size "
                        f"({order.qty:g} against {open_qty:g} open)")
        return None

    def _cross(self, vo: VenueOrder) -> None:
        book = self._books.get(vo.contract_key)
        px = book.executable(vo.side) if book else None
        if px is None:
            vo.state = OrderState.REJECTED
            vo.text = "No market to cross"
            self._emit("REJECTED", vo, vo.text)
            return
        available = (book.ask_size if vo.side is Side.BUY else book.bid_size)
        take = vo.remaining if available is None else min(vo.remaining, available)
        if take <= 0:
            return
        self._fill(vo, take, px)

    def _match_resting(self) -> None:
        """A resting limit fills when the book trades through it."""
        for vo in list(self._orders.values()):
            if vo.state not in (OrderState.WORKING, OrderState.PARTIAL):
                continue
            if vo.order_type is not OrderType.LIMIT or vo.price is None:
                continue
            book = self._books.get(vo.contract_key)
            if book is None or not book.usable:
                continue
            if vo.side is Side.BUY and book.ask is not None and book.ask <= vo.price:
                size = book.ask_size if book.ask_size is not None else vo.remaining
                self._fill(vo, min(vo.remaining, size), book.ask)
            elif vo.side is Side.SELL and book.bid is not None and book.bid >= vo.price:
                size = book.bid_size if book.bid_size is not None else vo.remaining
                self._fill(vo, min(vo.remaining, size), book.bid)

    def _fill(self, vo: VenueOrder, qty: float, price: float) -> None:
        if qty <= 0:
            return
        vo.filled_qty += qty
        self._exec_seq += 1
        fill = Fill(venue=self.name, exec_id=f"E{self._exec_seq:06d}",
                    clordid=vo.clordid, contract_key=vo.contract_key,
                    side=vo.side, qty=qty, price=price,
                    fees=None, venue_ts=self.now, our_ts=self.now)
        self._apply_to_position(vo.contract_key, vo.side, qty, price,
                                self._effects.get(vo.clordid,
                                                  PositionEffect.OPEN))

        if vo.remaining <= 1e-9:
            vo.state = OrderState.FILLED
            self._emit("FILL", vo, f"filled {qty:g} @ {price:g}", fill)
        else:
            vo.state = OrderState.PARTIAL
            self._emit("PARTIAL", vo,
                       f"filled {qty:g} of {vo.qty:g} @ {price:g}", fill)

    def _apply_to_position(self, key: str, side: Side, qty: float,
                           price: float, effect: PositionEffect) -> None:
        """The venue obeys the flag it was given.

        An order flagged OPEN adds to its own side WHATEVER is open on the
        other one — which is the whole point: a desk that closes by sending a
        bare opposite order ends up long AND short, both live, both posting
        margin. An order flagged as a close reduces the OTHER side.
        """
        if effect.is_close:
            book = self._short if side is Side.BUY else self._long
            row = book.get(key)
            if row:
                row['qty'] = round(row['qty'] - qty, 10)
                if row['qty'] <= 1e-9:
                    book.pop(key, None)
            return

        book = self._long if side is Side.BUY else self._short
        row = book.setdefault(key, {'qty': 0.0, 'avg': price})
        total = row['qty'] + qty
        row['avg'] = ((row['avg'] * row['qty']) + price * qty) / total
        row['qty'] = round(total, 10)

    def _position_of(self, key: str) -> Optional[VenuePosition]:
        long_row = self._long.get(key) or {}
        short_row = self._short.get(key) or {}
        long_qty = long_row.get('qty', 0.0)
        short_qty = short_row.get('qty', 0.0)
        if not long_qty and not short_qty:
            return None
        net = round(long_qty - short_qty, 10)
        avg = (long_row.get('avg') if long_qty >= short_qty
               else short_row.get('avg'))
        c = self.sim.get(key)
        gross = long_qty + short_qty
        return VenuePosition(contract_key=key, qty=net, avg_price=avg,
                             margin=(c.margin_per_contract * gross
                                     if c and gross else None),
                             long_qty=long_qty or None,
                             short_qty=short_qty or None)

    def cancel(self, clordid: str) -> None:
        vo = self._orders.get(clordid)
        if vo is None:
            return
        if vo.state.is_done:
            return
        vo.state = OrderState.CANCELLED
        self._emit("CANCELLED", vo, "cancelled")

    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None:
        """Cancel-replace, as the venue does it: the order keeps its id and
        loses its queue position. It never becomes a new order with a new
        lineage, which is what makes a partial fill traceable across an
        amend."""
        vo = self._orders.get(clordid)
        if vo is None or vo.state.is_done:
            return
        if price is not None:
            vo.price = price
        if qty is not None:
            vo.qty = qty
        vo.ts = self.now
        self._emit("REPLACED", vo, "replaced")
        self._match_resting()

    def orders(self) -> Optional[List[VenueOrder]]:
        if not self.readable:
            return None                   # could not read — NOT "no orders"
        return [o for o in self._orders.values() if not o.state.is_done]

    def positions(self) -> Optional[List[VenuePosition]]:
        if not self.readable:
            return None                   # could not read — NOT "flat"
        out = []
        for key in set(self._long) | set(self._short):
            pos = self._position_of(key)
            if pos is not None:
                out.append(pos)
        return out

    def drain_events(self) -> List[GatewayEvent]:
        out, self._events = self._events, []
        return out

    def diagnose(self) -> List[Dict[str, Any]]:
        rows = [{'check': 'Simulator', 'ok': True,
                 'detail': f"{len(self.sim)} contracts walking an OU process",
                 'fix': ''}]
        for key, c in self.sim.items():
            rows.append({'check': key, 'ok': c.open,
                         'detail': (f"tick {c.tick_size} value {c.tick_value} "
                                    f"mult {c.multiplier:g} {c.currency} "
                                    f"{'OPEN' if c.open else 'CLOSED'}"),
                         'fix': '' if c.open else 'Reopen it in the simulator.'})
        return rows
