"""The Analysis window's arithmetic.

Every test here is about the same thing: a figure that was never measured
must not be reported as zero, because the two read identically on a screen
and only one of them is true.
"""
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import analysis
from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.models import ExitReason, Position, Side, TouchEvent, TouchState

T0 = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def a_trade(net=100.0, gross=None, fees=10.0, side=Side.SELL, qty=5.0,
            opened=0, closed=60, margin=1300.0, reason=ExitReason.TARGET,
            simulated=False, entry_z=2.1):
    return Position(
        contract_key='fef', side=side, qty=qty, avg_price=0.69,
        opened_at=at(opened), closed_at=at(closed),
        entry_z=entry_z, exit_z=0.3, exit_price=0.64,
        margin_locked=margin, exit_reason=reason,
        gross_pnl=(net + fees) if gross is None and net is not None else gross,
        fees_paid=fees, net_pnl=net,
        pnl_pct_on_margin=(100.0 * net / margin) if net and margin else None,
        is_simulated=simulated)


# -- the tiles -------------------------------------------------------------

def test_the_headline_figures():
    trades = [a_trade(net=200), a_trade(net=100), a_trade(net=-90)]
    s = analysis.summarise(trades)
    assert s['trades'] == 3 and s['won'] == 2 and s['lost'] == 1
    assert s['win_rate'] == pytest.approx(66.7, abs=0.1)
    assert s['net'] == 210
    assert s['avg'] == 70
    assert s['avg_win'] == 150 and s['avg_loss'] == -90
    assert s['expectancy'] == pytest.approx(2 / 3 * 150 + 1 / 3 * -90, abs=0.1)


def test_a_trade_with_no_net_is_counted_as_unmeasured_not_as_a_loss():
    """Fees the venue never reported leave the net unknown. Calling it zero
    makes it a scratch trade, which it might not have been."""
    trades = [a_trade(net=100), a_trade(net=None, fees=None)]
    s = analysis.summarise(trades)
    assert s['trades'] == 2
    assert s['measured'] == 1 and s['unmeasured'] == 1
    assert s['net'] == 100                 # the unmeasured one is not a zero
    assert s['won'] == 1 and s['lost'] == 0


def test_return_on_margin_needs_every_trade_to_have_reported_its_margin():
    """A total over some of the rows is not a return on the whole book."""
    both = analysis.summarise([a_trade(net=100, margin=1000),
                               a_trade(net=100, margin=1000)])
    assert both['on_margin'] == pytest.approx(10.0)
    partial = analysis.summarise([a_trade(net=100, margin=1000),
                                  a_trade(net=100, margin=None)])
    assert partial['on_margin'] is None


def test_cost_drag_is_fees_over_gross():
    s = analysis.summarise([a_trade(net=90, fees=10)])   # gross 100
    assert s['cost_drag'] == pytest.approx(10.0)


def test_the_worst_run_is_the_deepest_streak_not_the_worst_trade():
    s = analysis.summarise([a_trade(net=-100), a_trade(net=-200),
                            a_trade(net=500), a_trade(net=-150)])
    assert s['worst_run'] == -300


def test_under_ten_trades_is_not_enough_to_judge():
    """Six losing trades is not evidence."""
    assert analysis.summarise([a_trade() for _ in range(9)])['enough_to_judge'] is False
    assert analysis.summarise([a_trade() for _ in range(10)])['enough_to_judge'] is True


def test_no_trades_reports_nothing_rather_than_zeros():
    s = analysis.summarise([])
    assert s['trades'] == 0
    for field in ('win_rate', 'net', 'avg', 'expectancy', 'on_margin',
                  'avg_hold_min'):
        assert s[field] is None, f"{field} came back as a number"


# -- the touch study -------------------------------------------------------

def a_touch(level=2.0, state='REVERTED', seconds=120.0, adverse=0.4,
            traded=False):
    return {'level': level, 'state': state, 'seconds_to_revert': seconds,
            'adverse_sigma': adverse, 'became_trade': traded}


def test_the_study_reports_what_happened_after_each_touch():
    rows = [a_touch(2.0, 'REVERTED', 60, 0.3, True),
            a_touch(2.0, 'REVERTED', 180, 0.5),
            a_touch(2.0, 'TIMED_OUT', None, 1.4),
            a_touch(-1.0, 'REVERTED', 900, 1.2)]
    study = analysis.touch_study(rows)
    two = [l for l in study['levels'] if l['level'] == 2.0][0]
    assert two['touches'] == 3 and two['resolved'] == 3
    assert two['reverted_pct'] == pytest.approx(66.7, abs=0.1)
    assert two['median_seconds'] == 120.0
    assert two['traded'] == 1


def test_an_unresolved_touch_is_excluded_from_the_percentage():
    """Counting it as a miss understates every level, and understates the
    widest levels most, because those are the ones still open."""
    rows = [a_touch(2.0, 'REVERTED'), a_touch(2.0, 'UNRESOLVED')]
    study = analysis.touch_study(rows)
    two = [l for l in study['levels'] if l['level'] == 2.0][0]
    assert two['reverted_pct'] == 100.0         # one of one RESOLVED
    assert two['unresolved'] == 1
    assert study['unresolved'] == 1

    # control: resolve it as a miss and the percentage moves
    rows[1]['state'] = 'TIMED_OUT'
    two2 = [l for l in analysis.touch_study(rows)['levels']
            if l['level'] == 2.0][0]
    assert two2['reverted_pct'] == 50.0


def test_a_level_with_nothing_resolved_reports_no_percentage():
    """None, not 0% — 'nothing has come back yet' is a different statement
    from 'none of them came back'."""
    study = analysis.touch_study([a_touch(3.0, 'UNRESOLVED')])
    three = [l for l in study['levels'] if l['level'] == 3.0][0]
    assert three['reverted_pct'] is None


def test_the_best_level_needs_enough_resolved_touches_to_mean_anything():
    def rows(level, n):
        return [dict(a_touch(level, 'REVERTED'), std=0.08) for _ in range(n)]

    thin = analysis.touch_study(rows(3.0, 1), round_trip_points=0.02)
    assert thin['best_level'] is None
    thick = analysis.touch_study(rows(2.0, 6), round_trip_points=0.02)
    assert thick['best_level'] == 2.0 and thick['best_reverted_pct'] == 100.0


def test_a_median_of_nothing_is_none_not_zero():
    assert analysis._median([]) is None
    assert analysis._median([None, None]) is None
    assert analysis._median([1.0, 3.0]) == 2.0


# -- costs -----------------------------------------------------------------

SETTINGS = {'commission_per_contract': 1.0, 'exchange_fee_per_contract': 0.5,
            'clearing_fee_per_contract': 0.0, 'slippage_budget_ticks': 0.5}


def a_fill(ticks=0.3, qty=5.0):
    return {'slippage_ticks': ticks, 'qty': qty}


def test_the_budget_is_reported_beside_the_measurement():
    trades = [a_trade(qty=5)]
    fills = [a_fill(0.3, 5), a_fill(0.3, 5)]
    report = analysis.cost_report(trades, fills, SETTINGS, 0.01, 1.0)
    assert report['budget_ticks'] == 0.5
    assert report['measured_ticks'] == pytest.approx(0.3)
    slip = [l for l in report['lines'] if l['item'] == 'slippage'][0]
    assert slip['budgeted'] == pytest.approx(0.5 * 2 * 1.0 * 5)
    assert slip['actual'] == pytest.approx(0.3 * 1.0 * 5 * 2)


def test_a_generous_budget_is_reported_as_a_finding_with_a_number():
    """The edge filter refuses entries using the BUDGET, so one that is too
    generous silently refuses trades that would have paid."""
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(0.3, 5)],
                                  SETTINGS, 0.01, 1.0)
    finding = report['finding']
    assert finding is not None and finding['generous'] is True
    assert finding['suggest_ticks'] == pytest.approx(0.3)
    assert 'refusing entries' in finding['text']


def test_a_mean_budget_is_reported_the_other_way_round():
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(1.4, 5)],
                                  SETTINGS, 0.01, 1.0)
    assert report['finding']['generous'] is False
    assert 'do not cover their costs' in report['finding']['text']


def test_a_budget_that_matches_the_measurement_raises_no_finding():
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(0.5, 5)],
                                  SETTINGS, 0.01, 1.0)
    assert report['finding'] is None


def test_a_fill_that_could_not_be_priced_is_unmeasured_not_zero():
    """Averaging it in as zero understates the cost of every trade."""
    fills = [a_fill(0.4, 5), {'slippage_ticks': None, 'qty': 5}]
    report = analysis.cost_report([a_trade(qty=5)], fills, SETTINGS, 0.01, 1.0)
    assert report['fills'] == 2 and report['unmeasured_fills'] == 1
    assert report['measured_ticks'] == pytest.approx(0.4)   # not 0.2


def test_with_no_measured_fills_there_is_no_measurement_and_no_finding():
    report = analysis.cost_report([a_trade(qty=5)],
                                  [{'slippage_ticks': None, 'qty': 5}],
                                  SETTINGS, 0.01, 1.0)
    assert report['measured_ticks'] is None
    assert report['finding'] is None


# -- exits and the journal -------------------------------------------------

def test_exits_are_grouped_by_the_reason_that_was_stored():
    trades = [a_trade(reason=ExitReason.TARGET, net=100),
              a_trade(reason=ExitReason.TARGET, net=120),
              a_trade(reason=ExitReason.STOP_LOSS, net=-200)]
    rows = analysis.exit_reasons(trades)
    assert rows[0]['reason'] == 'TARGET' and rows[0]['count'] == 2
    assert rows[0]['net'] == 220
    assert rows[1]['reason'] == 'STOP_LOSS' and rows[1]['net'] == -200


def test_the_journal_carries_the_z_each_decision_fired_at():
    rows = analysis.journal([a_trade(entry_z=2.14)])
    assert rows[0]['entry_z'] == 2.14
    assert rows[0]['exit_reason'] == 'TARGET'
    assert rows[0]['held_min'] == 60.0


# -- the whole report, against a real database -----------------------------

@pytest.fixture
def desk(tmp_path):
    db = Database(str(tmp_path / 'a.db'))
    config = TraderConfig(path=str(tmp_path / 'config.json'))
    config.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', tick_size=0.01, tick_value=1.0,
        commission_per_contract=1.0, slippage_budget_ticks=0.5)
    config.contracts['cl'] = ContractConfig(key='cl', name='WTI Dec/Jan',
                                            tick_size=0.01, tick_value=10.0)
    return db, config


def test_a_contract_report_pulls_it_all_together(desk):
    db, config = desk
    for i in range(3):
        db.save_position(a_trade(net=100 + i, closed=60 + i))
    db.save_touch(TouchEvent(contract_key='fef', ts=at(1), level=2.0,
                             direction='UP', state=TouchState.REVERTED,
                             seconds_to_revert=90.0, adverse_sigma=0.3))
    report = analysis.contract_report(db, config, 'fef')
    assert report['summary']['trades'] == 3
    assert report['touches']['levels'][0]['level'] == 2.0
    assert len(report['journal']) == 3
    assert report['name'] == 'Iron ore Oct/Nov'


def test_an_open_position_is_named_and_excluded(desk):
    """A system that counts an open winner is a system that flatters itself."""
    db, config = desk
    db.save_position(a_trade(net=100))
    open_one = a_trade(net=None)
    open_one.closed_at = None
    open_one.net_pnl = None
    db.save_position(open_one)
    report = analysis.contract_report(db, config, 'fef')
    assert report['summary']['trades'] == 1
    assert report['open_positions'] == 1


def test_simulated_trades_are_not_blended_into_a_live_figure(desk):
    db, config = desk
    db.save_position(a_trade(net=100, simulated=False))
    db.save_position(a_trade(net=999, simulated=True))
    live = analysis.contract_report(db, config, 'fef', mode='live')
    assert live['summary']['trades'] == 1 and live['summary']['net'] == 100
    both = analysis.contract_report(db, config, 'fef', mode='both')
    assert both['summary']['trades'] == 2
    sim = analysis.contract_report(db, config, 'fef', mode='sim')
    assert sim['summary']['net'] == 999


def test_the_period_filter_cuts_by_when_the_trade_CLOSED(desk):
    db, config = desk
    old = a_trade(net=100)
    old.closed_at = datetime.now(timezone.utc) - timedelta(days=40)
    db.save_position(old)
    recent = a_trade(net=50)
    recent.closed_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.save_position(recent)
    assert analysis.contract_report(db, config, 'fef', period='all')['summary']['trades'] == 2
    assert analysis.contract_report(db, config, 'fef', period='30d')['summary']['trades'] == 1


def test_the_desk_report_ranks_and_withholds_a_verdict_where_it_should(desk):
    db, config = desk
    for _ in range(12):
        db.save_position(a_trade(net=100))
    thin = a_trade(net=-50)
    thin.contract_key = 'cl'
    db.save_position(thin)

    report = analysis.desk_report(db, config)
    rows = {r['key']: r for r in report['rows']}
    assert rows['fef']['verdict'] == 'earning'
    assert rows['cl']['verdict'] == 'too few to judge'
    assert rows['cl']['enough_to_judge'] is False
    # the total row is the only blended figure, and it is labelled
    assert report['total']['trades'] == 13


def test_a_contract_with_no_trades_says_no_data_rather_than_losing(desk):
    db, config = desk
    report = analysis.desk_report(db, config)
    rows = {r['key']: r for r in report['rows']}
    assert rows['fef']['verdict'] == 'no data yet'
    assert rows['fef']['net'] is None


def test_a_budget_of_zero_with_real_slippage_is_the_loudest_finding():
    """Zero is the DEFAULT budget, so this is the most likely case of all:
    charging nothing for slippage that is measurably costing money, and an
    edge filter that therefore passes trades which do not cover their costs.
    Testing the budget for truthiness silenced exactly this case."""
    settings = dict(SETTINGS, slippage_budget_ticks=0.0)
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(0.35, 5)],
                                  settings, 0.01, 1.0)
    finding = report['finding']
    assert finding is not None
    assert finding['generous'] is False
    assert finding['suggest_ticks'] == pytest.approx(0.35)
    assert 'budget is zero' in finding['text']
    assert 'do not cover their costs' in finding['text']


def test_a_budget_of_zero_with_no_slippage_raises_nothing():
    """The control: nothing measured, nothing to say."""
    settings = dict(SETTINGS, slippage_budget_ticks=0.0)
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(0.0, 5)],
                                  settings, 0.01, 1.0)
    assert report['finding'] is None


# -- a level that reverts is not the same as a level that pays -------------

def a_touch_with_sigma(level, state='REVERTED', std=0.08):
    return dict(a_touch(level, state), std=std)


def test_the_best_level_must_clear_the_round_trip_not_merely_revert():
    """Without this the answer is almost always the innermost band: a spread
    one sigma from its mean comes back more reliably than one at three, and it
    is also the one that cannot pay for the trade. Naming it as the best level
    invites lowering the threshold onto something that reverts beautifully and
    loses money every time."""
    rows = ([a_touch_with_sigma(1.0) for _ in range(9)] +          # 90% back
            [a_touch_with_sigma(1.0, 'TIMED_OUT')] +
            [a_touch_with_sigma(2.0) for _ in range(6)] +
            [a_touch_with_sigma(2.0, 'TIMED_OUT') for _ in range(4)])

    # sigma 0.08, round trip 0.12: +1 captures 0.08 and cannot pay; +2
    # captures 0.16 and can.
    study = analysis.touch_study(rows, round_trip_points=0.12)
    assert study['best_level'] == 2.0
    one = [l for l in study['levels'] if l['level'] == 1.0][0]
    two = [l for l in study['levels'] if l['level'] == 2.0][0]
    assert one['reverted_pct'] > two['reverted_pct']    # it does revert more
    assert one['pays'] is False and two['pays'] is True

    # control: a round trip small enough for +1 to pay, and it wins
    cheap = analysis.touch_study(rows, round_trip_points=0.02)
    assert cheap['best_level'] == 1.0


def test_when_no_level_covers_its_costs_it_says_so(): 
    """Rather than falling back to the innermost band and calling it the
    answer."""
    rows = [a_touch_with_sigma(1.0) for _ in range(8)]
    study = analysis.touch_study(rows, round_trip_points=99.0)
    assert study['best_level'] is None
    assert study['nothing_pays'] is True


def test_without_a_cost_figure_no_level_is_claimed_to_pay():
    """Unknown is not yes."""
    study = analysis.touch_study([a_touch_with_sigma(2.0) for _ in range(6)])
    assert all(l['pays'] is None for l in study['levels'])
    assert study['best_level'] is None


def test_slippage_can_be_negative_and_is_never_proposed_as_a_budget():
    """The market can move in our favour between the price an order is aimed
    at and the fill. That is a price improvement, not a cost — and a budget is
    never negative: proposing one would have the edge filter pay the desk to
    trade."""
    settings = dict(SETTINGS, slippage_budget_ticks=0.5)
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(-1.5, 5)],
                                  settings, 0.01, 1.0)
    finding = report['finding']
    assert finding is not None
    assert finding['improving'] is True
    assert finding['suggest_ticks'] == 0.0          # never negative
    assert 'BETTER than the price' in finding['text']
    assert 'not real' in finding['text']


def test_price_improvement_against_a_zero_budget_proposes_nothing():
    """There is nothing to correct: the budget is already where it should be,
    and a finding with no action is noise on a screen that is watched."""
    settings = dict(SETTINGS, slippage_budget_ticks=0.0)
    report = analysis.cost_report([a_trade(qty=5)], [a_fill(-1.5, 5)],
                                  settings, 0.01, 1.0)
    assert report['finding'] is None
    assert report['measured_ticks'] == pytest.approx(-1.5)   # still reported


def test_the_costs_card_measures_from_the_decision_like_the_report():
    """One slippage figure, not two: a trade that kept its decision price is
    measured from it at both ends, and its fills' send-touch figure is not
    counted a second time. A paper trade has nothing to measure."""
    from types import SimpleNamespace
    from fixtrader.models import Position, Side
    contract = SimpleNamespace(tick_size=0.01)
    decided = Position(contract_key='fef', side=Side.SELL, qty=0.0,
                       opened_qty=5.0, avg_price=0.6, tickets=['E1', 'E2'],
                       entry_slippage=0.02, exit_slippage=-0.01,
                       entry_order_type='MARKET', exit_order_type='MARKET')
    paper = Position(contract_key='fef', side=Side.BUY, qty=0.0,
                     opened_qty=5.0, avg_price=0.6, tickets=['PAPER-1'],
                     entry_order_type='PAPER')
    older = Position(contract_key='fef', side=Side.BUY, qty=0.0,
                     opened_qty=5.0, avg_price=0.6, tickets=['E9'])
    fills = [{'exec_id': 'E1', 'slippage_ticks': 7.0, 'qty': 5},
             {'exec_id': 'E9', 'slippage_ticks': 0.4, 'qty': 5}]
    sides = analysis._measured_sides([decided, paper, older], fills, contract)
    ticks = sorted(s['slippage_ticks'] for s in sides)
    assert ticks == pytest.approx([-1.0, 0.4, 2.0])     # E1's 7.0 not counted
