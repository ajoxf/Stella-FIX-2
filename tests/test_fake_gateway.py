"""The simulator has to be honest about the awkward cases, or the bugs it
hides go straight to a live account."""
import pytest

from fixtrader.fake_gateway import FakeGateway, SimContract
from fixtrader.models import (Intent, OrderRequest, OrderState, OrderType,
                              PositionEffect, Side)


def gw(**kw):
    g = FakeGateway([SimContract('fef', mid=0.50, tick_size=0.01,
                                 tick_value=1.0, size=10.0, **kw)])
    g.start()
    g.set_book('fef', 0.49, 0.51, 10.0, 10.0)
    return g


def market(key='fef', side=Side.BUY, qty=5.0, intent=Intent.OPEN,
           effect=None):
    """A market order. The effect follows the intent unless a test is
    deliberately sending the WRONG one — which is the failure worth having a
    simulator for."""
    if effect is None:
        effect = (PositionEffect.CLOSE if intent is Intent.CLOSE
                  else PositionEffect.OPEN)
    return OrderRequest(contract_key=key, side=side, qty=qty,
                        order_type=OrderType.MARKET, intent=intent,
                        position_effect=effect)


def test_a_market_order_crosses_the_executable_side():
    g = gw()
    g.send(market(side=Side.BUY, qty=5))
    fills = [e for e in g.drain_events() if e.kind == 'FILL']
    assert fills and fills[0].fill.price == 0.51        # lifted the offer
    g.send(market(side=Side.SELL, qty=5, intent=Intent.CLOSE))
    fills = [e for e in g.drain_events() if e.kind == 'FILL']
    assert fills[0].fill.price == 0.49                  # hit the bid


def test_a_limit_rests_until_the_book_trades_through_it():
    g = gw()
    g.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=5,
                        order_type=OrderType.LIMIT, price=0.45))
    assert [o.state for o in g.orders()] == [OrderState.WORKING]
    g.set_book('fef', 0.43, 0.45, 10.0, 10.0)           # offer comes to us
    assert [e.kind for e in g.drain_events()][-1] == 'FILL'


def test_a_partial_fill_leaves_the_remainder_working():
    g = gw()
    g.set_book('fef', 0.49, 0.51, 3.0, 3.0)             # only 3 on the touch
    g.send(market(qty=5))
    events = g.drain_events()
    assert any(e.kind == 'PARTIAL' for e in events)
    order = g.orders()[0]
    assert order.filled_qty == 3.0 and order.remaining == 2.0


def test_a_close_larger_than_the_position_is_refused():
    """The failure this simulator exists to catch: an exit that reverses."""
    g = gw()
    g.send(market(side=Side.BUY, qty=3))
    g.drain_events()
    g.send(market(side=Side.SELL, qty=10, intent=Intent.CLOSE))
    rejects = [e for e in g.drain_events() if e.kind == 'REJECTED']
    assert rejects and 'would not reduce' in rejects[0].text
    assert g.positions()[0].qty == 3.0                  # untouched


def test_a_close_with_no_position_is_refused():
    g = gw()
    g.send(market(side=Side.SELL, qty=1, intent=Intent.CLOSE))
    rejects = [e for e in g.drain_events() if e.kind == 'REJECTED']
    assert rejects and 'no position to close' in rejects[0].text


def test_a_closed_instrument_rejects_in_the_venues_own_words():
    g = gw()
    g.sim['fef'].open = False
    g.send(market())
    rejects = [e for e in g.drain_events() if e.kind == 'REJECTED']
    assert rejects[0].text == 'Instrument not open for trading'


def test_unreadable_is_none_and_is_not_flat():
    """The state the real venue has, and the one that makes a system report a
    clean account while the money sits at the venue."""
    g = gw()
    g.send(market(qty=2))
    assert g.positions() and g.orders() is not None
    g.readable = False
    assert g.positions() is None
    assert g.orders() is None


def test_an_amend_keeps_the_order_id_and_its_lineage():
    """Cancel-then-new would give up the queue and lose the partial fill's
    history."""
    g = gw()
    cl = g.send(OrderRequest(contract_key='fef', side=Side.BUY, qty=5,
                             order_type=OrderType.LIMIT, price=0.45))
    g.drain_events()
    g.amend(cl, price=0.46)
    order = [o for o in g.orders() if o.clordid == cl][0]
    assert order.price == 0.46 and order.clordid == cl


def test_averaging_and_flattening_keep_the_position_honest():
    g = gw()
    g.send(market(side=Side.BUY, qty=2))
    g.set_book('fef', 0.59, 0.61, 10.0, 10.0)
    g.send(market(side=Side.BUY, qty=2))
    pos = g.positions()[0]
    assert pos.qty == 4.0 and pos.avg_price == pytest.approx(0.56)
    g.send(market(side=Side.SELL, qty=4, intent=Intent.CLOSE))
    assert g.positions() == []


def test_an_opposite_order_sent_as_an_OPEN_does_not_close_anything():
    """THE failure. On a venue that keeps the two sides apart — every Chinese
    exchange Orient routes to — an opposite order that does not carry a close
    flag opens the other side. The desk is then long AND short, both live,
    both posting margin, and a netting screen would call it flat."""
    g = gw()
    g.send(market(side=Side.BUY, qty=3))
    g.drain_events()
    g.send(market(side=Side.SELL, qty=3, effect=PositionEffect.OPEN))
    g.drain_events()

    pos = g.positions()[0]
    assert pos.qty == 0.0                       # NET reads flat...
    assert pos.long_qty == 3.0 and pos.short_qty == 3.0   # ...it is not
    assert pos.is_gross_hedged
    assert pos.margin == pytest.approx(260.0 * 6)         # margin on BOTH


def test_the_same_order_with_a_close_flag_actually_closes():
    """The control for the test above: one flag is the whole difference."""
    g = gw()
    g.send(market(side=Side.BUY, qty=3))
    g.drain_events()
    g.send(market(side=Side.SELL, qty=3, effect=PositionEffect.CLOSE))
    g.drain_events()
    assert g.positions() == []


def test_a_close_reduces_the_other_side_not_its_own():
    g = gw()
    g.send(market(side=Side.SELL, qty=5))        # open a short
    g.drain_events()
    g.send(market(side=Side.BUY, qty=2, effect=PositionEffect.CLOSE))
    g.drain_events()
    pos = g.positions()[0]
    assert pos.short_qty == 3.0 and pos.long_qty is None


def test_margin_is_reported_so_the_profit_target_can_be_priced():
    g = gw()
    assert g.margin_for('fef', 5) == pytest.approx(260.0 * 5)
    assert g.margin_for('unknown', 5) is None


def test_the_walk_is_deterministic_for_a_given_seed():
    a = FakeGateway([SimContract('fef')], seed=42)
    b = FakeGateway([SimContract('fef')], seed=42)
    for _ in range(50):
        a.advance(); b.advance()
    assert a.top_of_book('fef').mid == b.top_of_book('fef').mid


def test_the_simulated_series_mean_reverts():
    """A random walk would show a screen on which the algo correctly never
    trades, and nobody could tell which half was at fault."""
    g = FakeGateway([SimContract('fef', mid=0.5, sigma=0.06, theta=0.05)],
                    seed=3)
    mids = []
    for _ in range(600):
        g.advance()
        mids.append(g.top_of_book('fef').mid)
    assert 0.3 < sum(mids) / len(mids) < 0.7        # it stays near its anchor
