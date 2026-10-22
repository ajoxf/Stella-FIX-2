"""Dataclasses and enums. No logic, no I/O, nothing that can fail.

Two conventions in here are load-bearing everywhere else:

- **None means "unknown", and it is not "flat" or "zero".** `BookTop.bid` of
  None is "we could not read the bid"; a `positions()` of None is "we could not
  read the account", which is a completely different statement from "there are
  no positions". Code that treats the first as the second will report a clean
  account while the money sits at the venue.
- **Every figure that a decision was made on is stored on the record**, not
  recomputed later. The z on a trade is the z the entry actually fired at; a
  later change to the lookback must not be able to rewrite history.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 long, -1 short. The one place the direction becomes a number."""
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    GTC = "GTC"


class PositionEffect(str, Enum):
    """What an order does to the POSITION, said explicitly to the venue.

    This is the FIX equivalent of closing an MT5 ticket, and it is not
    optional. An opposite order that does not say it is closing is an order to
    OPEN the other way: on a venue that keeps long and short separately — and
    on every Chinese exchange Orient routes to, which take an offset flag on
    every order — the desk ends up long and short at once, both live, both
    paying margin, with a position count nobody expected. `reduce_only` alone
    does not say it either: it is a cap, not an instruction.

    CLOSE_TODAY and CLOSE_YESTERDAY exist because SHFE and INE charge them
    differently, and closing today's lot against yesterday's fee schedule is
    a real cost, not a formality.
    """
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"

    @property
    def is_close(self) -> bool:
        return self is not PositionEffect.OPEN


class Intent(str, Enum):
    """What an order is FOR. A close is capped at the open position and may
    never reverse it; an open has no such cap. The executor needs to know
    which without inferring it from the side."""
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class OrderState(str, Enum):
    PENDING = "PENDING"        # sent, no acknowledgement yet
    WORKING = "WORKING"        # live at the venue
    PARTIAL = "PARTIAL"        # partially filled, remainder still working
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_done(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED,
                        OrderState.REJECTED, OrderState.EXPIRED)


class ContractState(str, Enum):
    """What the badge on a window says. Exactly one at a time."""
    IDLE = "IDLE"            # algo off
    WARMING = "WARMING"      # algo on, rolling window not full
    ARMED = "ARMED"          # algo on, watching
    BLOCKED = "BLOCKED"      # algo on, a filter is withholding
    WORKING = "WORKING"      # an order of ours is live
    IN = "IN"                # a position is open
    HALTED = "HALTED"        # a guard, the kill switch, or a limit


class SessionState(str, Enum):
    DOWN = "DOWN"
    CONNECTING = "CONNECTING"
    LOGGED_ON = "LOGGED_ON"
    ERROR = "ERROR"


class ExitReason(str, Enum):
    """Stored on the position by whatever closed it. Never inferred from
    prices afterwards — the Analysis window reports on this field."""
    TARGET = "TARGET"
    STOP_LOSS = "STOP_LOSS"
    ZSCORE = "ZSCORE"
    TIME_STOP = "TIME_STOP"
    SESSION_FLAT = "SESSION_FLAT"
    CLOSE_NOW = "CLOSE_NOW"
    KILL_ALL = "KILL_ALL"
    LIMIT_BREACH = "LIMIT_BREACH"
    #: The money stop: net P&L at or below -stop_loss_money per contract.
    MONEY_STOP = "MONEY_STOP"
    #: Back at the rolling mean while already net positive — short of the
    #: profit target, but paid.
    MEAN = "MEAN"
    #: Closed by the trader's resting Close @ LMT on the ladder.
    CLOSE_LIMIT = "CLOSE_LIMIT"


class TargetBasis(str, Enum):
    """What the profit-target percentage is a percentage OF. MARGIN is the
    desk's answer; the other two exist for a venue that will not report it."""
    MARGIN = "MARGIN"
    NOTIONAL = "NOTIONAL"
    ENTRY_SIGMA = "ENTRY_SIGMA"


class TouchState(str, Enum):
    """A touch still running is UNRESOLVED — not a failure. Counting it as a
    miss understates every level, and the widest levels most, because those
    are the ones still open."""
    UNRESOLVED = "UNRESOLVED"
    REVERTED = "REVERTED"
    TIMED_OUT = "TIMED_OUT"


# --------------------------------------------------------------------------
# market data
# --------------------------------------------------------------------------

@dataclass
class BookTop:
    """Top of book for one spread contract.

    Any field may be None, and None is not zero. A one-sided book is a real
    thing that a venue publishes, and it must not become a mid of half the
    offer.
    """
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    ts: Optional[datetime] = None

    @property
    def mid(self) -> Optional[float]:
        """The mid of the BOOK. Never the last trade — that is the rule the
        whole statistics layer rests on."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def width(self) -> Optional[float]:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def crossed(self) -> bool:
        """A crossed book is bad data, not an arbitrage. Reject the update."""
        return (self.bid is not None and self.ask is not None
                and self.bid > self.ask)

    @property
    def usable(self) -> bool:
        return (self.bid is not None and self.ask is not None
                and not self.crossed)

    def executable(self, side: Side) -> Optional[float]:
        """The side this direction actually trades against: a BUY lifts the
        ask, a SELL hits the bid."""
        return self.ask if side is Side.BUY else self.bid

    def to_dict(self) -> Dict[str, Any]:
        return {
            'bid': self.bid, 'ask': self.ask,
            'bid_size': self.bid_size, 'ask_size': self.ask_size,
            'mid': self.mid, 'width': self.width,
            'ts': self.ts.isoformat() if self.ts else None,
        }


@dataclass
class SecurityDef:
    """What the venue says a contract is. Read, never typed, wherever the
    venue publishes it — every money figure on the screen runs through
    tick_size and tick_value."""
    symbol: str = ""
    tick_size: Optional[float] = None
    tick_value: Optional[float] = None
    contract_multiplier: Optional[float] = None
    currency: Optional[str] = None
    min_qty: Optional[float] = None
    qty_step: Optional[float] = None
    max_qty: Optional[float] = None
    trading_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------
# orders and fills
# --------------------------------------------------------------------------

@dataclass
class OrderRequest:
    contract_key: str = ""
    side: Side = Side.BUY
    qty: float = 0.0
    order_type: OrderType = OrderType.MARKET
    intent: Intent = Intent.OPEN
    price: Optional[float] = None          # None for MARKET
    tif: TimeInForce = TimeInForce.DAY
    reduce_only: bool = False
    reason: str = ""                       # why the algo sent it
    #: Said to the VENUE. Never left at OPEN on a closing order — that is the
    #: order that opens an opposite position instead of closing one.
    position_effect: PositionEffect = PositionEffect.OPEN
    #: Our own position, and the venue's own ids for the fills that built it.
    #: Carried so a close is traceable to what it closes, and so a venue that
    #: wants a position or ticket reference has one to be given.
    position_id: Optional[int] = None
    close_tickets: List[str] = field(default_factory=list)


@dataclass
class VenueOrder:
    """An order as the VENUE sees it. Ours carry our ClOrdID; anything else
    at the venue is somebody's hand order and is never touched."""
    clordid: str = ""
    venue_id: Optional[str] = None
    contract_key: str = ""
    side: Side = Side.BUY
    qty: float = 0.0
    filled_qty: float = 0.0
    price: Optional[float] = None
    order_type: OrderType = OrderType.MARKET
    state: OrderState = OrderState.PENDING
    text: str = ""                         # the venue's own words
    ts: Optional[datetime] = None

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)


@dataclass
class Fill:
    """Keyed (venue, exec_id): two venues' identical execution ids must not
    overwrite each other."""
    venue: str = ""
    exec_id: str = ""
    clordid: str = ""
    contract_key: str = ""
    side: Side = Side.BUY
    qty: float = 0.0
    price: float = 0.0
    fees: Optional[float] = None
    venue_ts: Optional[datetime] = None
    our_ts: Optional[datetime] = None
    #: Measured against the touch the order was sent at. None where it could
    #: not be priced — unmeasured is not zero, and never averaged in as zero.
    slippage_ticks: Optional[float] = None


@dataclass
class VenuePosition:
    contract_key: str = ""
    qty: float = 0.0                       # signed NET: + long, - short
    avg_price: Optional[float] = None
    margin: Optional[float] = None
    #: Gross, where the venue keeps the two sides separately — every Chinese
    #: exchange does. `qty` of 0 with a long of 3 and a short of 3 is not
    #: flat: it is two live positions, both posting margin, and the screen
    #: must be able to tell the difference. None where the venue nets.
    long_qty: Optional[float] = None
    short_qty: Optional[float] = None

    @property
    def is_gross_hedged(self) -> bool:
        """Both sides open at once — almost always a close sent as an open."""
        return bool(self.long_qty and self.short_qty)


@dataclass
class GatewayEvent:
    kind: str = ""          # ACK | FILL | PARTIAL | CANCELLED | REJECTED | ...
    contract_key: str = ""
    clordid: str = ""
    text: str = ""
    order: Optional[VenueOrder] = None
    fill: Optional[Fill] = None
    ts: Optional[datetime] = None


# --------------------------------------------------------------------------
# our own book
# --------------------------------------------------------------------------

@dataclass
class Position:
    """One open position on one contract, with everything the Analysis window
    will need recorded AT THE TIME. None of it can be reconstructed later."""
    id: Optional[int] = None
    contract_key: str = ""
    side: Side = Side.BUY
    qty: float = 0.0
    avg_price: float = 0.0

    opened_at: Optional[datetime] = None
    #: The size this position was OPENED at. `qty` is what remains and is
    #: decremented as it closes, so by the time a trade reaches the journal it
    #: is zero — and a trade journal reporting every trade as size 0 makes
    #: every cost figure it feeds read as unmeasured.
    opened_qty: float = 0.0
    entry_z: Optional[float] = None
    entry_mean: Optional[float] = None
    entry_std: Optional[float] = None
    entry_half_life: Optional[float] = None
    #: Margin the venue said this position ties up. None where it would not
    #: say — and then there is no target, rather than a target on a guess.
    margin_locked: Optional[float] = None

    break_even: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None

    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_z: Optional[float] = None
    exit_reason: Optional[ExitReason] = None

    gross_pnl: Optional[float] = None
    fees_paid: Optional[float] = None
    net_pnl: Optional[float] = None
    #: Return on the margin actually locked — the same base the target uses.
    pnl_pct_on_margin: Optional[float] = None

    is_simulated: bool = False
    #: The venue's execution ids for every fill that built this position —
    #: its tickets. A close carries them.
    tickets: List[str] = field(default_factory=list)
    #: The venue trading DAY this was opened on, for venues that price a
    #: close-today differently from a close-yesterday.
    opened_session: Optional[str] = None
    #: SLIPPAGE, in price points, against the price the decision was made at
    #: — positive a cost, negative an improvement, None unmeasured (and None
    #: for a PAPER fill, which has nothing to measure). See `slippage`.
    entry_slippage: Optional[float] = None
    exit_slippage: Optional[float] = None
    #: How each end was sent — MARKET, LIMIT, LIMIT escalated — so a limit
    #: that is not beating a market fill can be seen not to.
    entry_order_type: Optional[str] = None
    exit_order_type: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def signed_qty(self) -> float:
        return self.qty * self.side.sign


@dataclass
class TouchEvent:
    """A crossing of a standard-deviation level, with the state at the moment
    it happened. Resolved later by a second write.

    Counted once per CROSSING, not once per tick: a z that sits at 2.1 for
    four minutes is one touch, and counting it per update would make the
    busiest level look like the most significant one.
    """
    id: Optional[int] = None
    contract_key: str = ""
    ts: Optional[datetime] = None
    level: float = 0.0                     # -3 -2 -1 +1 +2 +3
    direction: str = ""                    # UP | DOWN — which way it crossed
    price: float = 0.0                     # the mid at the touch
    z: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    half_life: Optional[float] = None
    algo_armed: bool = False
    became_trade: bool = False

    state: TouchState = TouchState.UNRESOLVED
    resolved_at: Optional[datetime] = None
    seconds_to_revert: Optional[float] = None
    #: How much further it went against the reversion first, in sigma.
    adverse_sigma: Optional[float] = None


@dataclass
class Signal:
    """What the algo would do, and — when it would do nothing — why."""
    contract_key: str = ""
    action: str = "NONE"                   # NONE | OPEN | CLOSE
    side: Optional[Side] = None
    qty: float = 0.0
    reason: str = ""
    exit_reason: Optional[ExitReason] = None
    blocked_by: Optional[str] = None
    ts: Optional[datetime] = None
