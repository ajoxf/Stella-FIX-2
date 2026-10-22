"""The gateway contract — the definition of "done" for a FIX implementation.

**This suite is the handover.** Every test in here is run against
`FakeGateway`, which the whole system is built and tested on. A `FixGateway`
that passes the same suite against a real UAT session drops straight in: the
engine, the executor, the statistics and the screen cannot tell the two apart,
because everything above `gateway.py` only ever sees this protocol.

So the job is not "make FIX work". The job is **make this file pass against
Orient's UAT**, and nothing above `fixtrader/gateway.py` should need to change
for it.

Running it:

    pytest tests/test_gateway_contract.py -q            # the simulator
    FIXTRADER_CONTRACT_VENUE=1 \\
    FIXTRADER_CONTRACT_CONFIG=config.json \\
    FIXTRADER_CONTRACT_KEY=fef_v6x6 \\
        pytest tests/test_gateway_contract.py -q        # a real UAT session

**The venue run sends real orders into UAT.** Point it at a UAT venue and a
contract you are happy to trade in size 1. It never runs against PROD: the
fixture refuses a venue whose environment is not UAT.

Some behaviour cannot be forced on a live venue — you cannot make a real
exchange go unreadable, and you cannot put its book where you want it. Those
tests declare what they need and skip with a reason rather than passing
vacuously.
"""

import os
import time

import pytest

from fixtrader.config import TraderConfig
from fixtrader.fake_gateway import FakeGateway, SimContract
from fixtrader.gateway import CLORDID_PREFIX
from fixtrader.models import (Intent, OrderRequest, OrderState, OrderType,
                              PositionEffect, SessionState, Side)


class Harness:
    """One gateway under test, plus what a test is allowed to do to it.

    `can` is how a test says what it needs. A capability the venue does not
    offer skips the test with a reason — it never quietly passes.
    """

    def __init__(self, gateway, key, tick, can, settle=lambda: None):
        self.gateway = gateway
        self.key = key
        self.tick = tick
        self.can = can
        self._settle = settle

    def settle(self):
        """Give the venue a moment, then bring events back."""
        self._settle()
        return self.gateway.drain_events()

    def drain_until(self, kind, timeout=5.0):
        """Every event so far, waiting for one of `kind` to show up."""
        seen = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            seen.extend(self.settle())
            if any(e.kind == kind for e in seen):
                return seen
        return seen

    def requires(self, capability):
        if not self.can.get(capability):
            pytest.skip(f"this venue cannot {capability}")

    def market(self, side=Side.BUY, qty=1.0, intent=Intent.OPEN, effect=None):
        if effect is None:
            effect = (PositionEffect.CLOSE if intent is Intent.CLOSE
                      else PositionEffect.OPEN)
        return OrderRequest(contract_key=self.key, side=side, qty=qty,
                            order_type=OrderType.MARKET, intent=intent,
                            position_effect=effect,
                            reduce_only=effect.is_close)

    def limit(self, side, price, qty=1.0):
        return OrderRequest(contract_key=self.key, side=side, qty=qty,
                            order_type=OrderType.LIMIT, price=price,
                            position_effect=PositionEffect.OPEN)

    def flatten(self):
        """Leave the venue as we found it. A contract suite that leaves a
        position behind is a contract suite nobody runs twice."""
        positions = self.gateway.positions()
        if not positions:
            return
        for pos in positions:
            for side, qty in (('long', pos.long_qty), ('short', pos.short_qty)):
                if not qty:
                    continue
                closing = Side.SELL if side == 'long' else Side.BUY
                self.gateway.send(self.market(side=closing, qty=qty,
                                              intent=Intent.CLOSE))
            if pos.long_qty is None and pos.short_qty is None and pos.qty:
                closing = Side.SELL if pos.qty > 0 else Side.BUY
                self.gateway.send(self.market(side=closing, qty=abs(pos.qty),
                                              intent=Intent.CLOSE))
        self.settle()


def _fake_harness():
    sim = SimContract('fef', mid=0.50, tick_size=0.01, tick_value=1.0,
                      size=10.0)
    gw = FakeGateway([sim])
    gw.start()
    gw.set_book('fef', 0.49, 0.51, 10.0, 10.0)
    return Harness(gw, 'fef', 0.01, can={
        'control the book': True,
        'be made unreadable': True,
        'keep the two sides apart': True,
        'reject on demand': True,
        'close an instrument': True,
    })


def _venue_harness():
    """A real session, from the environment. UAT only, and it says why."""
    config_path = os.environ.get('FIXTRADER_CONTRACT_CONFIG', 'config.json')
    key = os.environ.get('FIXTRADER_CONTRACT_KEY')
    if not key:
        pytest.skip("set FIXTRADER_CONTRACT_KEY to the contract to trade")

    config = TraderConfig.from_file(config_path)
    contract = config.contract(key)
    if contract is None:
        pytest.skip(f"no contract {key!r} in {config_path}")
    venue = config.venue(contract.venue)
    if venue is None:
        pytest.skip(f"no venue {contract.venue!r} in {config_path}")
    if venue.environment != 'UAT':
        pytest.fail(
            f"venue {venue.name!r} is {venue.environment}. This suite sends "
            f"real orders and runs against UAT only.")

    from fixtrader.gateway import FixGateway
    gw = FixGateway(venue, [contract])
    gw.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and gw.state() is not SessionState.LOGGED_ON:
        time.sleep(0.25)
    if gw.state() is not SessionState.LOGGED_ON:
        pytest.fail(f"session did not log on: {gw.state_text()}")
    gw.subscribe(contract)

    return Harness(gw, key, contract.tick_size or 0.01, can={
        # A real exchange will not be told where to put its book, will not
        # pretend to be unreadable, and will not close an instrument to
        # order. Those tests skip and say so.
        'control the book': False,
        'be made unreadable': False,
        'keep the two sides apart': os.environ.get(
            'FIXTRADER_CONTRACT_GROSS', '1') == '1',
        'reject on demand': False,
        'close an instrument': False,
    }, settle=lambda: time.sleep(0.3))


PARAMS = ['fake']
if os.environ.get('FIXTRADER_CONTRACT_VENUE') == '1':
    PARAMS.append('venue')


@pytest.fixture(params=PARAMS)
def h(request):
    harness = _fake_harness() if request.param == 'fake' else _venue_harness()
    yield harness
    try:
        harness.flatten()
        harness.gateway.stop()
    except Exception:                                    # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

def test_a_started_session_reports_logged_on_and_says_so_in_words(h):
    assert h.gateway.state() is SessionState.LOGGED_ON
    assert h.gateway.state_text()          # never blank: the banner shows it


def test_a_stopped_session_is_down(h):
    h.gateway.stop()
    assert h.gateway.state() is SessionState.DOWN


# ---------------------------------------------------------------------------
# unknown is not empty, and it is not zero
# ---------------------------------------------------------------------------

def test_an_unknown_contract_has_no_book_rather_than_an_empty_one(h):
    """None means "we do not know". An empty BookTop would mean "there is no
    market", and the algo treats those differently — as it must."""
    assert h.gateway.top_of_book('no-such-contract-at-all') is None


def test_the_book_that_does_exist_is_two_sided_and_priced(h):
    h.settle()
    book = h.gateway.top_of_book(h.key)
    assert book is not None
    assert book.bid is not None and book.ask is not None
    assert not book.crossed
    assert book.mid == pytest.approx((book.bid + book.ask) / 2)


def test_an_unreadable_account_is_None_and_never_an_empty_list(h):
    """The distinction the whole book rests on: "could not read" is not
    "flat". A gateway that returns [] on a failed read will have this system
    report a clean account while the money sits at the venue."""
    h.requires('be made unreadable')
    h.gateway.readable = False
    assert h.gateway.positions() is None
    assert h.gateway.orders() is None
    h.gateway.readable = True
    assert h.gateway.positions() is not None


def test_a_security_definition_is_absent_or_complete_never_zeroed(h):
    """A tick size of 0 would divide by zero in every money figure on the
    screen. Absent is fine; zero is not."""
    from fixtrader.config import ContractConfig
    spec = h.gateway.security_definition(ContractConfig(key=h.key))
    if spec is None:
        pytest.skip("this venue does not publish security definitions here")
    for field in ('tick_size', 'tick_value'):
        value = getattr(spec, field)
        assert value is None or value > 0, f"{field} came back as {value!r}"


# ---------------------------------------------------------------------------
# orders
# ---------------------------------------------------------------------------

def test_every_order_we_send_is_stamped_as_ours(h):
    """Anything at the venue without this prefix belongs to somebody else —
    a hand order in TT — and is never cancelled, amended or counted."""
    clordid = h.gateway.send(h.market(qty=1))
    assert clordid.startswith(CLORDID_PREFIX)
    h.drain_until('FILL')


def test_a_market_order_fills_and_reports_a_price(h):
    h.gateway.send(h.market(side=Side.BUY, qty=1))
    events = h.drain_until('FILL')
    fills = [e for e in events if e.kind in ('FILL', 'PARTIAL')]
    assert fills, f"no fill came back: {[e.kind for e in events]}"
    assert fills[0].fill is not None
    assert fills[0].fill.price > 0
    assert fills[0].fill.exec_id                    # keyed (venue, exec_id)


def test_an_event_carries_a_snapshot_of_the_order_not_a_live_reference(h):
    """A queued acknowledgement must report the state at the time it was
    raised. Handing out the live object made an ACK drained after the fill
    arrive saying FILLED — the reader marked the order done, and the fill that
    followed had no order to belong to and was applied as an OPEN, doubling
    the position instead of closing it."""
    h.gateway.send(h.market(qty=1))
    events = h.drain_until('FILL')
    acks = [e for e in events if e.kind == 'ACK']
    fills = [e for e in events if e.kind in ('FILL', 'PARTIAL')]
    if not acks or not fills:
        pytest.skip("this venue does not separate the acknowledgement")
    assert acks[0].order is not fills[0].order
    assert acks[0].order.filled_qty <= fills[0].order.filled_qty


def test_a_rejected_order_carries_the_venues_own_words(h):
    """Never "check the log". The refusal reaches the screen verbatim."""
    if h.can.get('reject on demand'):
        h.gateway.reject_next = "Instrument not open for trading"
    else:
        # A quantity no venue will take. If yours accepts it, say so here.
        pass
    h.gateway.send(h.market(qty=0 if not h.can.get('reject on demand') else 1))
    events = h.drain_until('REJECTED', timeout=5.0)
    rejects = [e for e in events if e.kind == 'REJECTED']
    if not rejects:
        pytest.skip("could not provoke a reject on this venue")
    assert rejects[0].text.strip(), "a reject with no text is not a reason"


def test_cancelling_is_idempotent_and_never_raises(h):
    """The engine cancels our working orders at startup AND at shutdown, and
    it does not first check whether each one is still alive."""
    h.requires('control the book')
    clordid = h.gateway.send(h.limit(Side.BUY, 0.40))
    h.settle()
    h.gateway.cancel(clordid)
    h.settle()
    h.gateway.cancel(clordid)                       # again: must not raise
    h.gateway.cancel('FT-does-not-exist')           # nor for an unknown id


def test_an_amend_keeps_the_order_id(h):
    """Cancel-then-new gives up the queue and opens a window in which the
    order does not exist. The id and its lineage survive an amend."""
    h.requires('control the book')
    clordid = h.gateway.send(h.limit(Side.BUY, 0.40))
    h.settle()
    h.gateway.amend(clordid, price=0.41)
    h.settle()
    live = [o for o in (h.gateway.orders() or []) if o.clordid == clordid]
    assert live, "the order disappeared across an amend"
    assert live[0].price == pytest.approx(0.41)


def test_a_partial_fill_leaves_the_remainder_working(h):
    """Partial fills are the normal case, not an error."""
    h.requires('control the book')
    h.gateway.set_book(h.key, 0.49, 0.51, 2.0, 2.0)
    h.gateway.send(h.market(side=Side.BUY, qty=5))
    h.drain_until('PARTIAL')
    order = (h.gateway.orders() or [])[0]
    assert order.filled_qty == 2.0
    assert order.remaining == 3.0
    assert order.state is OrderState.PARTIAL


# ---------------------------------------------------------------------------
# closing — the part that costs money if it is wrong
# ---------------------------------------------------------------------------

def test_a_close_flag_actually_closes(h):
    h.gateway.send(h.market(side=Side.BUY, qty=1))
    h.drain_until('FILL')
    h.gateway.send(h.market(side=Side.SELL, qty=1, intent=Intent.CLOSE))
    h.drain_until('FILL')
    positions = h.gateway.positions()
    assert positions is not None
    assert not any(p.contract_key == h.key and (p.qty or p.long_qty or
                                                p.short_qty)
                   for p in positions), "the close left something open"


def test_an_opposite_order_flagged_OPEN_does_not_close_anything(h):
    """The failure this flag exists to prevent. On a venue that keeps the two
    sides apart, an opposite order without a close flag OPENS the other side:
    the desk is long and short at once, both posting margin, and a netting
    screen calls it flat."""
    h.requires('keep the two sides apart')
    h.gateway.send(h.market(side=Side.BUY, qty=1))
    h.drain_until('FILL')
    h.gateway.send(h.market(side=Side.SELL, qty=1,
                            effect=PositionEffect.OPEN))
    h.drain_until('FILL')
    pos = [p for p in (h.gateway.positions() or [])
           if p.contract_key == h.key]
    assert pos, "both sides vanished — this venue nets; set GROSS=0"
    assert pos[0].long_qty and pos[0].short_qty, \
        "the venue netted an order that was flagged OPEN"
    assert pos[0].is_gross_hedged


def test_a_close_bigger_than_the_position_is_refused_not_reversed(h):
    """An exit that overfills opens the opposite side of a spread the desk
    thought it had left."""
    h.gateway.send(h.market(side=Side.BUY, qty=1))
    h.drain_until('FILL')
    h.gateway.send(h.market(side=Side.SELL, qty=9, intent=Intent.CLOSE))
    events = h.drain_until('REJECTED', timeout=5.0)
    rejects = [e for e in events if e.kind == 'REJECTED']
    assert rejects, "an oversized close was accepted"
    assert rejects[0].text.strip()
    pos = [p for p in (h.gateway.positions() or [])
           if p.contract_key == h.key]
    assert pos and (pos[0].long_qty == 1.0 or pos[0].qty == 1.0), \
        "the refused close moved the position anyway"


def test_a_close_with_nothing_open_is_refused(h):
    h.gateway.send(h.market(side=Side.SELL, qty=1, intent=Intent.CLOSE))
    events = h.drain_until('REJECTED', timeout=5.0)
    assert any(e.kind == 'REJECTED' for e in events)
