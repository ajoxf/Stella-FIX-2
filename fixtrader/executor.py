"""Sending, re-pricing, escalating and closing. One path for entries and exits.

The operator chose MARKET or LIMIT **independently for entries and exits**, so
every combination has to work — limit-in / limit-out included. That is why
there is one `place` here taking an `Intent` rather than a `send_entry` and a
`send_exit` that would drift apart.

The rules that make the limit path safe, and the reasons they exist:

- **Amend by cancel-replace, never cancel-then-new.** The second gives up the
  queue AND opens a window in which the order does not exist at all — a window
  a fast market walks straight through.
- **Re-price only outside the dead band.** Every amend loses queue position,
  so re-pricing on every pass guarantees you are never at the front of a
  queue, which defeats quoting entirely.
- **A closing order is capped at the open position and carries reduce-only.**
  An exit that overfills opens the opposite side of a spread the desk thought
  it had left.
- **Nothing withholds the escalation on a CLOSE.** If an exit limit times out
  and the contract escalates, it escalates — through a stale quote, through a
  settling jump, through any filter.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import sizing
from .models import (Intent, OrderRequest, OrderState, OrderType,
                     PositionEffect, Side, TimeInForce)

logger = logging.getLogger(__name__)


class WorkingOrder:
    """One of our orders, and everything needed to manage it over its life."""

    def __init__(self, clordid: str, contract_key: str, side: Side, qty: float,
                 order_type: OrderType, intent: Intent, price: Optional[float],
                 sent_at: datetime, sent_at_touch: Optional[float],
                 reason: str = "", position_id: Optional[int] = None):
        self.clordid = clordid
        self.contract_key = contract_key
        self.side = side
        self.qty = qty
        self.filled_qty = 0.0
        self.order_type = order_type
        self.intent = intent
        self.price = price
        self.sent_at = sent_at
        #: The touch the order was sent at — the anchor slippage is measured
        #: against. Without it a fill cannot be priced and is reported as
        #: UNMEASURED rather than as zero slippage.
        self.sent_at_touch = sent_at_touch
        self.reason = reason
        self.position_id = position_id
        self.state = OrderState.PENDING
        self.text = ""
        self.escalated = False
        self.position_effect = PositionEffect.OPEN
        #: A price the TRADER named (Close @ LMT on the ladder): never
        #: re-pegged and never timed out — it waits there until it fills,
        #: is cancelled, or CLOSE ALL escalates it to market.
        self.pinned = False
        #: A MARKET order sent as a LIMIT through the touch, IOC.
        self.marketable = False

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_close(self) -> bool:
        return self.intent is Intent.CLOSE

    def to_dict(self) -> Dict[str, Any]:
        return {
            'clordid': self.clordid, 'contract_key': self.contract_key,
            'side': self.side.value, 'qty': self.qty,
            'filled_qty': self.filled_qty, 'price': self.price,
            'order_type': self.order_type.value, 'intent': self.intent.value,
            'state': self.state.value, 'text': self.text,
            'reason': self.reason, 'position_id': self.position_id,
            'sent_at': self.sent_at.isoformat() if self.sent_at else None,
            'sent_at_touch': self.sent_at_touch, 'escalated': self.escalated,
            'position_effect': self.position_effect.value,
            'pinned': self.pinned,
        }


def close_effect(settings: Dict[str, Any], position,
                 now: datetime) -> PositionEffect:
    """Which close flag this contract's venue wants.

    `CLOSE` is the plain offset flag and suits most venues. SHFE and INE
    (and anything else that prices a close-today differently) want the split,
    and `AUTO` picks it from the trading day the position was opened on.

    **Unknown resolves to CLOSE, never to OPEN.** A close whose flag could not
    be determined is still a close; degrading it to an open would turn an exit
    into a second position, which is the failure this whole function exists to
    prevent.
    """
    mode = str(settings.get('close_offset_mode', 'CLOSE') or 'CLOSE').upper()
    if mode in ('CLOSE', 'CLOSE_TODAY', 'CLOSE_YESTERDAY'):
        return PositionEffect(mode)
    if mode != 'AUTO':
        return PositionEffect.CLOSE
    opened = getattr(position, 'opened_session', None)
    if not opened:
        return PositionEffect.CLOSE          # not measured: the safe reading
    return (PositionEffect.CLOSE_TODAY
            if opened == now.date().isoformat()
            else PositionEffect.CLOSE_YESTERDAY)


def limit_price(book, side: Side, offset_ticks: float,
                tick_size: Optional[float]) -> Optional[float]:
    """Where a resting order goes: behind its own touch, by the offset.

    A BUY rests at or below the bid and a SELL at or above the ask — pricing a
    limit through the touch makes it a market order wearing a limit's name.
    Rounded AWAY from the market so the rounding cannot cross it either.
    """
    if book is None or tick_size is None:
        return None
    anchor = book.bid if side is Side.BUY else book.ask
    if anchor is None:
        return None
    raw = anchor - offset_ticks * tick_size if side is Side.BUY \
        else anchor + offset_ticks * tick_size
    return sizing.round_to_tick(raw, tick_size,
                                'down' if side is Side.BUY else 'up')


def through_price(book, side: Side, ticks: float,
                  tick_size: Optional[float]) -> Optional[float]:
    """A MARKET order's price as a LIMIT: `ticks` THROUGH its own touch — a
    BUY at the offer + N ticks, a SELL at the bid - N. Rounded through the
    market, so it still crosses. None where there is no touch or no tick (a
    true market order then goes: a close is never withheld for want of a
    price), or where ticks is 0 (asked for a true market order)."""
    if book is None or not tick_size or not ticks or ticks <= 0:
        return None
    touch = book.executable(side)
    if touch is None:
        return None
    raw = touch + ticks * tick_size if side is Side.BUY else touch - ticks * tick_size
    return sizing.round_to_tick(raw, tick_size, 'up' if side is Side.BUY else 'down')


class Executor:
    """Owns every order this system has live at a venue."""

    def __init__(self, gateway, db=None, notify=None, simulated: bool = False):
        self.gateway = gateway
        self.db = db
        self.notify = notify
        self.simulated = simulated
        self.working: Dict[str, WorkingOrder] = {}
        #: What each order was FOR, kept after the order itself is done. A
        #: fill can arrive after the state that completed the order — out of
        #: order, or resent — and an exit fill mistaken for an entry doubles
        #: the position instead of closing it. This is the one lookup that
        #: must not disappear when `working` is pruned.
        self.intents: Dict[str, Intent] = {}
        #: Orders whose limit has timed out and whose cancel has been
        #: REQUESTED — the market replacement is sent when the venue
        #: confirms the cancel, never before. Keyed by ClOrdID, holding the
        #: settings the escalation should use.
        self.escalating: Dict[str, Dict[str, Any]] = {}
        #: The last contract and book seen for each key, so an escalation
        #: that fires on a venue event — not inside a pass — still has the
        #: instrument it is about.
        self.contracts: Dict[str, Any] = {}
        self.books: Dict[str, Any] = {}
        #: The state the DECISION was made on — z, mean, sigma, half-life at
        #: the moment the signal fired. Kept per order and stamped onto the
        #: position when the fill lands, because by then the window has moved
        #: on: reading it at fill time recorded an entry at z -0.67 for a
        #: trade the algo took at -2.24, and the whole touch study and trade
        #: journal are built on the z each decision was actually made at.
        self.decisions: Dict[str, Dict[str, Any]] = {}
        #: Orders taken back from a previous run at startup.
        self.adopted: set = set()
        #: The position each closing order is closing. Kept so an escalation
        #: sends the same close against the same position rather than a fresh
        #: opposite order with no effect flag on it.
        self.positions: Dict[str, Any] = {}

    # -- sending ----------------------------------------------------------

    def place(self, contract, settings: Dict[str, Any], side: Side, qty: float,
              intent: Intent, book, now: datetime, reason: str = "",
              open_qty: float = 0.0, position_id: Optional[int] = None,
              decision: Optional[Dict[str, Any]] = None,
              position=None,
              limit_price_at: Optional[float] = None) -> Optional[WorkingOrder]:
        """Send one order. Returns None when there is nothing safe to send.

        A CLOSE is never a bare opposite order. It carries an explicit
        `PositionEffect`, the position it is closing, and that position's
        venue tickets — because an opposite order that does not SAY it is
        closing is an order to open the other way.
        """
        self.note(contract, book)
        prefix = 'exit' if intent is Intent.CLOSE else 'entry'
        order_type = OrderType(settings.get(f'{prefix}_order_type',
                                            OrderType.MARKET.value))

        qty = sizing.clamp_qty(qty, contract.min_qty, contract.qty_step,
                               contract.max_qty)
        if intent is Intent.CLOSE:
            # Cap at what is actually open. A close can only ever reduce.
            qty = min(qty, abs(open_qty)) if open_qty else 0.0
        if qty <= 0:
            return None

        price = None
        tif = TimeInForce(settings.get('time_in_force', 'DAY'))
        marketable = False
        if limit_price_at is None and order_type is OrderType.MARKET:
            # "Market" as an exchange takes it: a LIMIT N ticks through the
            # touch, fill-or-cancel. A bare market order is priced by the
            # exchange itself and REJECTED outside its price band — on a
            # close, that is a position that stays open.
            through = through_price(book, side,
                                    float(settings.get('market_limit_ticks') or 0),
                                    contract.tick_size)
            if through is not None:
                order_type, price, tif, marketable = (OrderType.LIMIT, through,
                                                      TimeInForce.IOC, True)
        if limit_price_at is not None:
            # The trader's own price: a LIMIT at exactly that level.
            order_type, price = OrderType.LIMIT, float(limit_price_at)
        elif order_type is OrderType.LIMIT and not marketable:
            price = limit_price(book, side,
                                float(settings.get(f'{prefix}_limit_offset_ticks',
                                                   1.0) or 0.0),
                                contract.tick_size)
            if price is None:
                # No book to price a limit against. Crossing instead would
                # send a market order the operator did not ask for.
                return None

        effect = (close_effect(settings, position, now)
                  if intent is Intent.CLOSE else PositionEffect.OPEN)
        req = OrderRequest(
            contract_key=contract.key, side=side, qty=qty,
            order_type=order_type, intent=intent, price=price,
            tif=tif,
            # reduce_only is a CAP, not an instruction. It is sent as well as
            # the effect, never instead of it.
            reduce_only=(intent is Intent.CLOSE), reason=reason,
            position_effect=effect,
            position_id=position_id or getattr(position, 'id', None),
            close_tickets=list(getattr(position, 'tickets', []) or [])
            if intent is Intent.CLOSE else [])

        touch = book.executable(side) if book is not None else None
        clordid = self.gateway.send(req)
        wo = WorkingOrder(clordid, contract.key, side, qty, order_type, intent,
                          price, now, touch, reason,
                          position_id or getattr(position, 'id', None))
        wo.position_effect = effect
        # A marketable IOC is never re-pegged: it fills or is gone at once.
        wo.pinned = limit_price_at is not None or marketable
        wo.marketable = marketable
        self.working[clordid] = wo
        self.intents[clordid] = intent
        if decision is not None:
            self.decisions[clordid] = dict(decision)
        if position is not None:
            self.positions[clordid] = position
        self._persist(wo, now)
        return wo

    # -- managing a resting limit ------------------------------------------

    def note(self, contract, book) -> None:
        """The contract and its latest book, for an escalation to send on.
        Kept on EVERY pass, whatever Auto trade says: the market close that
        follows a cancelled close limit (CLOSE ALL over Close @ LMT) needs
        them, and a close is never withheld for want of them."""
        self.contracts[contract.key] = contract
        if book is not None:
            self.books[contract.key] = book

    def manage(self, contract, settings: Dict[str, Any], book,
               now: datetime) -> List[str]:
        """Re-price and escalate this contract's working limits.

        Returns a list of things that happened, in words, for the events feed.
        """
        said: List[str] = []
        self.contracts[contract.key] = contract
        self.books[contract.key] = book
        for wo in list(self.working.values()):
            if wo.contract_key != contract.key:
                continue
            if wo.state.is_done or wo.order_type is not OrderType.LIMIT:
                continue
            if wo.pinned and not wo.escalated:
                continue              # the trader's price: it waits there

            prefix = 'exit' if wo.is_close else 'entry'
            timeout = float(settings.get(f'{prefix}_limit_timeout_sec', 0) or 0)
            waited = (now - wo.sent_at).total_seconds()

            if timeout and waited >= timeout and not wo.escalated:
                on_timeout = str(settings.get(f'{prefix}_on_timeout', 'CANCEL'))
                if on_timeout == 'CROSS_AT_MARKET':
                    # Announced, never a silent change of order type. And on a
                    # CLOSE nothing may stand in the way of this.
                    #
                    # The market order is NOT sent here. `cancel` is a
                    # REQUEST: the resting limit is still live at the venue
                    # until the venue says otherwise, and it can fill in
                    # between. Sending the replacement now means two orders
                    # for one position — on a close, the second finds nothing
                    # to close; on an open, it doubles the position. So the
                    # escalation is armed, and it fires on the CANCELLED
                    # event, for whatever is left THEN.
                    wo.escalated = True
                    self.escalating[wo.clordid] = dict(settings)
                    self.gateway.cancel(wo.clordid)
                    said.append(f"{wo.intent.value.lower()} limit unfilled "
                                f"after {timeout:.0f}s — crossing at market")
                else:
                    self.gateway.cancel(wo.clordid)
                    said.append(f"{wo.intent.value.lower()} limit unfilled "
                                f"after {timeout:.0f}s — cancelled")
                continue

            # Re-peg, but only outside the dead band.
            offset = float(settings.get(f'{prefix}_limit_offset_ticks', 1.0) or 0)
            wanted = limit_price(book, wo.side, offset, contract.tick_size)
            if wanted is None or wo.price is None or not contract.tick_size:
                continue
            band = float(settings.get('repeg_dead_band_ticks', 1.0) or 0.0)
            moved_ticks = abs(wanted - wo.price) / contract.tick_size
            if band and moved_ticks < band:
                continue                  # inside the band: keep the queue
            if moved_ticks <= 0:
                continue
            self.gateway.amend(wo.clordid, price=wanted)
            wo.price = wanted
            self._persist(wo, now)
        return said

    def escalate_closes(self, contract_key: str,
                        settings: Dict[str, Any]) -> int:
        """Turn this contract's working CLOSE limits into market closes.

        The same path an unfilled exit limit takes on its timeout: armed
        here, the cancel requested, and the market order SENT on the
        CANCELLED event for whatever is still open then — never a second
        close alongside the first."""
        n = 0
        for wo in list(self.working.values()):
            if (wo.contract_key != contract_key or not wo.is_close
                    or wo.state.is_done or wo.escalated
                    or wo.order_type is not OrderType.LIMIT):
                continue
            wo.escalated = True
            self.escalating[wo.clordid] = dict(settings)
            self.gateway.cancel(wo.clordid)
            n += 1
        return n

    def cancel_all(self, contract_key: Optional[str] = None,
                   side: Optional[str] = None) -> int:
        """Cancel OUR working orders, scoped by ClOrdID. Never touches an
        order this system did not send — a hand order in TT is not ours.
        `side` ('BUY'/'SELL') pulls one side only — the ladder's B and S."""
        n = 0
        for wo in list(self.working.values()):
            if contract_key and wo.contract_key != contract_key:
                continue
            if side and wo.side.value != side:
                continue
            if wo.state.is_done:
                continue
            self.gateway.cancel(wo.clordid)
            n += 1
        return n

    # -- events from the venue ---------------------------------------------

    def apply_event(self, event, tick_size: Optional[float],
                    now: datetime) -> None:
        """Fold one gateway event into our own order record."""
        wo = self.working.get(event.clordid)
        if wo is None:
            return
        vo = event.order
        if vo is not None:
            wo.state = vo.state
            wo.filled_qty = vo.filled_qty
            wo.text = vo.text or wo.text

        if event.fill is not None and tick_size:
            # Slippage against the touch the order was SENT at. None where
            # that anchor is missing: unmeasured, never averaged in as zero.
            if wo.sent_at_touch is not None:
                worse = (event.fill.price - wo.sent_at_touch) * wo.side.sign
                event.fill.slippage_ticks = round(worse / tick_size, 4)
            if self.db is not None:
                self.db.save_fill(event.fill)

        self._persist(wo, now)
        if wo.state.is_done:
            self.working.pop(wo.clordid, None)
            self._escalate_if_armed(wo, now)

    def _escalate_if_armed(self, wo: WorkingOrder, now: datetime) -> None:
        """Send the market replacement for a limit whose cancel just landed.

        Only for what is STILL outstanding. A limit that filled while the
        cancel was in flight leaves nothing to escalate, and sending the
        original quantity anyway is how one exit becomes two orders — the
        second either finds nothing to close, or opens the other side.
        """
        settings = self.escalating.pop(wo.clordid, None)
        if settings is None:
            return
        if wo.state is not OrderState.CANCELLED:
            return                    # it filled, or the venue refused it
        remaining = wo.remaining
        if remaining <= 0:
            return
        contract = self.contracts.get(wo.contract_key)
        if contract is None:
            return
        prefix = 'exit' if wo.is_close else 'entry'
        book = self.books.get(wo.contract_key)
        self.place(contract, dict(settings, **{f'{prefix}_order_type': 'MARKET'}),
                   wo.side, remaining, wo.intent, book, now,
                   reason="escalated from an unfilled limit",
                   open_qty=remaining if wo.is_close else 0.0,
                   position_id=wo.position_id,
                   # the decision was made when the LIMIT was sent;
                   # escalating does not re-decide it — and its slippage is
                   # measured from THAT price, the wait included
                   decision=self._escalated(self.decisions.get(wo.clordid)),
                   position=self.positions.get(wo.clordid))

    def adopt(self, row: Dict[str, Any]) -> bool:
        """An order of ours a previous run left recorded as working, taken
        back under management: so it can be cancelled, and so a fill for it
        is applied as what it was — an OPEN or a CLOSE — not guessed."""
        clordid = str(row.get('clordid') or '')
        if not clordid or clordid in self.working:
            return False
        try:
            wo = WorkingOrder(
                clordid, row.get('contract_key') or '',
                Side(row.get('side') or 'BUY'), float(row.get('qty') or 0),
                OrderType(row.get('order_type') or 'MARKET'),
                Intent(row.get('intent') or 'OPEN'), row.get('price'),
                datetime.now(timezone.utc), row.get('sent_at_touch'),
                row.get('reason') or 'adopted at startup',
                row.get('position_id'))
        except ValueError:
            return False
        wo.filled_qty = float(row.get('filled_qty') or 0)
        wo.state = OrderState.WORKING
        self.working[clordid] = wo
        self.intents[clordid] = wo.intent
        self.adopted.add(clordid)
        return True

    def is_adopted(self, clordid: str) -> bool:
        return clordid in self.adopted

    def release(self, clordid: str) -> None:
        """Stop managing an adopted order the venue no longer knows."""
        self.working.pop(clordid, None)
        self.adopted.discard(clordid)

    @staticmethod
    def _escalated(decision: Optional[Dict[str, Any]]):
        if decision is None:
            return None
        kind = decision.get('order_type') or 'LIMIT'
        return dict(decision, order_type=(kind if kind.endswith('escalated')
                                          else kind + ' escalated'))

    def decision_of(self, clordid: str) -> Dict[str, Any]:
        """The window's state when the signal fired, or an empty dict."""
        return self.decisions.get(clordid, {})

    def intent_of(self, clordid: str) -> Intent:
        """What an order was for: from memory, else from the orders table
        it was written to when it was SENT. A restart forgets the first;
        reading a close that was still working as an OPEN applied its fill
        the wrong way. OPEN only for an order this system never sent."""
        if clordid in self.intents:
            return self.intents[clordid]
        if self.db is not None and hasattr(self.db, 'order_intent'):
            try:
                recorded = self.db.order_intent(clordid)
            except Exception:                            # noqa: BLE001
                recorded = None
            if recorded:
                intent = Intent(str(recorded).split('.')[-1])
                self.intents[clordid] = intent
                return intent
        return Intent.OPEN

    def working_for(self, contract_key: str) -> List[WorkingOrder]:
        return [w for w in self.working.values()
                if w.contract_key == contract_key and not w.state.is_done]

    def _persist(self, wo: WorkingOrder, now: datetime) -> None:
        if self.db is None:
            return
        row = wo.to_dict()
        row['updated_at'] = now.isoformat()
        row['venue_id'] = None
        row['is_simulated'] = int(self.simulated)
        self.db.save_order(row)
