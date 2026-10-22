"""The feedback loop: what each contract actually did, and what to change.

Scoped to ONE contract at a time, deliberately. A win rate blended across
eight contracts cannot answer the only question this window exists for —
which contract to turn off — and the All-contracts view is a comparison, not
an average.

Three rules run through every figure in here:

- **Closed trades only.** An open position is named and excluded. A system
  that counts an open winner is a system that flatters itself.
- **Unmeasured is not zero.** A trade whose net could not be computed, a fill
  whose slippage could not be priced, a touch still running — each is counted
  separately and left out of the average, never folded in as a zero. Averaging
  in zeros understates costs and overstates reversion, which are the two
  numbers a desk would act on.
- **Under ten closed trades gets no verdict.** Six losing trades is not
  evidence, and a page that says so is worth more than one that ranks noise.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import costs as costs_mod
from . import sizing

#: Below this, a contract is described but never judged.
MIN_TRADES_FOR_A_VERDICT = 10

PERIODS = {'all': None, '30d': 30, '7d': 7, 'today': 0}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def period_start(period: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or _now()
    days = PERIODS.get(period or 'all')
    if days is None:
        return None
    if days == 0:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=days)


def _median(values: List[float]) -> Optional[float]:
    """The median, or None for an empty set. **Not 0.0** — no measurement is
    a different statement from a measurement of nothing."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _in_period(when, start: Optional[datetime]) -> bool:
    if start is None:
        return True
    moment = _dt(when)
    return moment is not None and moment >= start


def _wants(is_simulated: bool, mode: str) -> bool:
    """`mode` is 'live', 'sim' or 'both'. Simulated fills are never blended
    into a live P&L figure without being asked for."""
    if mode == 'both':
        return True
    if mode == 'sim':
        return bool(is_simulated)
    return not is_simulated


# ---------------------------------------------------------------------------
# the tiles
# ---------------------------------------------------------------------------

def _worst_run(nets: List[float]) -> Optional[float]:
    """The deepest run of consecutive losses, in money."""
    if not nets:
        return None
    worst = 0.0
    run = 0.0
    for net in nets:
        if net < 0:
            run += net
            worst = min(worst, run)
        else:
            run = 0.0
    return worst if worst < 0 else 0.0


def summarise(trades: List[Any]) -> Dict[str, Any]:
    """The headline figures for a set of closed positions."""
    measured = [t for t in trades if t.net_pnl is not None]
    unmeasured = len(trades) - len(measured)
    nets = [t.net_pnl for t in measured]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]

    holds = []
    for t in trades:
        opened, closed = _dt(t.opened_at), _dt(t.closed_at)
        if opened and closed:
            holds.append((closed - opened).total_seconds() / 60.0)

    gross = [t.gross_pnl for t in trades if t.gross_pnl is not None]
    fees = [t.fees_paid for t in trades if t.fees_paid is not None]
    margins = [t.margin_locked for t in trades if t.margin_locked]

    win_rate = (100.0 * len(wins) / len(nets)) if nets else None
    # Return on margin only where EVERY trade reported the margin it tied up.
    on_margin = (100.0 * sum(nets) / sum(margins)
                 if margins and len(margins) == len(measured) and sum(margins)
                 else None)
    gross_total = sum(gross) if gross else None
    cost_drag = (100.0 * sum(fees) / gross_total
                 if fees and gross_total and gross_total > 0 else None)

    expectancy = None
    if nets:
        p = len(wins) / len(nets)
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        expectancy = p * avg_win + (1 - p) * avg_loss

    return {
        'trades': len(trades),
        'measured': len(measured),
        'unmeasured': unmeasured,
        'won': len(wins),
        'lost': len(losses),
        'win_rate': round(win_rate, 1) if win_rate is not None else None,
        'net': round(sum(nets), 2) if nets else None,
        'gross': round(gross_total, 2) if gross_total is not None else None,
        'fees': round(sum(fees), 2) if fees else None,
        'avg': round(sum(nets) / len(nets), 2) if nets else None,
        'avg_win': round(sum(wins) / len(wins), 2) if wins else None,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else None,
        'expectancy': round(expectancy, 2) if expectancy is not None else None,
        'on_margin': round(on_margin, 2) if on_margin is not None else None,
        'cost_drag': round(cost_drag, 1) if cost_drag is not None else None,
        'avg_hold_min': round(sum(holds) / len(holds), 1) if holds else None,
        'worst_run': round(_worst_run(nets), 2) if nets else None,
        'enough_to_judge': len(trades) >= MIN_TRADES_FOR_A_VERDICT,
    }


# ---------------------------------------------------------------------------
# the standard-deviation touch study
# ---------------------------------------------------------------------------

def touch_study(rows: List[Dict[str, Any]],
                half_life: Optional[float] = None,
                round_trip_points: Optional[float] = None) -> Dict[str, Any]:
    """What happened AFTER each touch, per level.

    The original system recorded touches and counted them. A count alone is
    not something to act on: what says whether ±2.00 is right for a contract
    is how many of its touches came back, how long they took, and how far they
    went against you first.

    A touch still running is `unresolved` — counted, reported, and **excluded
    from the percentage**. Folding it in as a miss understates every level,
    and understates the widest levels most, because those are the ones still
    open.
    """
    levels: Dict[float, Dict[str, Any]] = {}
    unresolved_total = 0
    #: Sigma as it was AT EACH TOUCH, taken from the rows themselves. The
    #: expected capture at a level is |level| x sigma — the distance back to
    #: the mean — and it is the only way to tell a level that reverts often
    #: from a level worth trading.
    sigmas = [r.get('std') for r in rows if r.get('std')]
    sigma = _median(sigmas)

    for row in rows:
        level = float(row.get('level') or 0)
        if not level:
            continue
        bucket = levels.setdefault(level, {
            'level': level, 'touches': 0, 'reverted': 0, 'timed_out': 0,
            'unresolved': 0, 'traded': 0, '_times': [], '_adverse': [],
        })
        bucket['touches'] += 1
        if row.get('became_trade'):
            bucket['traded'] += 1
        state = row.get('state') or 'UNRESOLVED'
        if state == 'REVERTED':
            bucket['reverted'] += 1
            bucket['_times'].append(row.get('seconds_to_revert'))
            bucket['_adverse'].append(row.get('adverse_sigma'))
        elif state == 'TIMED_OUT':
            bucket['timed_out'] += 1
            bucket['_adverse'].append(row.get('adverse_sigma'))
        else:
            bucket['unresolved'] += 1
            unresolved_total += 1

    out = []
    for level in sorted(levels, key=lambda x: -x):
        b = levels[level]
        resolved = b['reverted'] + b['timed_out']
        capture = abs(level) * sigma if sigma else None
        # A level only PAYS if the move back to the mean clears the round
        # trip. Unknown where either figure is missing — and unknown is not
        # "yes".
        pays = (None if capture is None or round_trip_points is None
                else capture > round_trip_points)
        out.append({
            'level': level,
            'expected_capture': round(capture, 6) if capture is not None else None,
            'pays': pays,
            'touches': b['touches'],
            'resolved': resolved,
            'unresolved': b['unresolved'],
            # None, not 0%: nothing has resolved yet, which is not "none of
            # them came back".
            'reverted_pct': (round(100.0 * b['reverted'] / resolved, 1)
                             if resolved else None),
            'median_seconds': _median(b['_times']),
            'median_adverse_sigma': (round(_median(b['_adverse']), 2)
                                     if _median(b['_adverse']) is not None
                                     else None),
            'traded': b['traded'],
        })

    # The level that came back most often — among those with enough resolved
    # touches to mean anything, AND whose move covers the round trip.
    #
    # Without that second condition the answer is almost always the innermost
    # band: a spread one sigma from its mean comes back more reliably than one
    # at three, and it is also the one that cannot pay for the trade. Naming
    # it "the level that comes back" invites lowering the threshold onto a
    # level that reverts beautifully and loses money every time.
    resolved_enough = [r for r in out
                       if r['resolved'] >= 5 and r['reverted_pct'] is not None]
    paying = [r for r in resolved_enough if r['pays']]
    candidates = paying or []
    best = max(candidates, key=lambda r: r['reverted_pct']) if candidates else None

    return {
        'levels': out,
        'unresolved': unresolved_total,
        'sigma': round(sigma, 6) if sigma else None,
        'round_trip_points': round_trip_points,
        'best_level': abs(best['level']) if best else None,
        'best_reverted_pct': best['reverted_pct'] if best else None,
        # Said plainly when nothing clears its costs, rather than falling back
        # to the innermost band and calling it the answer.
        'nothing_pays': bool(resolved_enough and not paying),
        'half_life': half_life,
    }


# ---------------------------------------------------------------------------
# costs: what was budgeted against what was charged
# ---------------------------------------------------------------------------

def cost_report(trades: List[Any], fills: List[Dict[str, Any]],
                settings: Dict[str, Any], tick_size, tick_value
                ) -> Dict[str, Any]:
    """The round trip as budgeted, beside the round trip as measured.

    This closes the loop the settings page opens. The edge filter refuses
    entries using the BUDGETED slippage, so a budget that is too generous
    silently refuses trades that would have paid — and one that is too mean
    lets through trades that do not.

    A fill whose slippage could not be priced is **unmeasured**, counted
    separately and never averaged in as zero.
    """
    qty_total = sum((t.opened_qty or t.qty or 0) for t in trades) or 0.0
    per_side = {
        'commission': float(settings.get('commission_per_contract', 0) or 0),
        'exchange': float(settings.get('exchange_fee_per_contract', 0) or 0),
        'clearing': float(settings.get('clearing_fee_per_contract', 0) or 0),
    }
    budget_ticks = float(settings.get('slippage_budget_ticks', 0) or 0)

    lines = []
    for name, rate in per_side.items():
        money = rate * 2.0 * qty_total if qty_total else None
        lines.append({'item': name, 'budgeted': round(money, 2) if money is not None else None,
                      'actual': round(money, 2) if money is not None else None,
                      'diff': 0.0 if money is not None else None})

    measured = [f for f in fills if f.get('slippage_ticks') is not None]
    unmeasured = len(fills) - len(measured)
    slip_budget = (budget_ticks * 2.0 * (tick_value or 0) * qty_total
                   if tick_value and qty_total else None)

    slip_actual = None
    slip_ticks_actual = None
    if measured and tick_value:
        # Slippage is per FILL, in ticks, priced at that fill's own quantity.
        slip_actual = sum(f['slippage_ticks'] * tick_value * (f.get('qty') or 0)
                          for f in measured)
        weighted_qty = sum(f.get('qty') or 0 for f in measured)
        if weighted_qty:
            slip_ticks_actual = sum(
                f['slippage_ticks'] * (f.get('qty') or 0)
                for f in measured) / weighted_qty

    lines.append({
        'item': 'slippage',
        'budgeted': round(slip_budget, 2) if slip_budget is not None else None,
        'actual': round(slip_actual, 2) if slip_actual is not None else None,
        'diff': (round(slip_actual - slip_budget, 2)
                 if slip_actual is not None and slip_budget is not None else None),
    })

    totals = {
        'budgeted': sum(l['budgeted'] for l in lines if l['budgeted'] is not None) or None,
        'actual': sum(l['actual'] for l in lines if l['actual'] is not None) or None,
    }
    totals['diff'] = (round(totals['actual'] - totals['budgeted'], 2)
                      if totals['budgeted'] is not None
                      and totals['actual'] is not None else None)

    finding = None
    # A budget of ZERO is not a reason to stay quiet — it is the default, so
    # it is the most likely case of all: charging nothing for slippage that is
    # measurably costing money, and an edge filter that therefore passes
    # trades which do not cover their costs.
    if (slip_ticks_actual is not None
            and abs(budget_ticks - slip_ticks_actual) >= 0.05):
        generous = budget_ticks > slip_ticks_actual
        # Slippage can be NEGATIVE: the market moved in our favour between
        # the price the order was aimed at and the fill. That is a price
        # improvement, not a cost, and a budget is never negative — proposing
        # one would have the edge filter pay the desk to trade.
        improving = slip_ticks_actual < 0
        suggest = max(0.0, slip_ticks_actual)
        if improving:
            text = (f"Fills are coming in {abs(slip_ticks_actual):.2f} ticks "
                    f"BETTER than the price they were aimed at" +
                    (f", and the budget charges {budget_ticks:.2f} ticks a "
                     f"side for slippage that is not being paid — so the edge "
                     f"filter is refusing entries on a cost that is not real."
                     if budget_ticks else
                     ". Nothing to correct; the budget is already zero."))
        elif not budget_ticks:
            text = (f"The slippage budget is zero, and the fills are costing "
                    f"{slip_ticks_actual:.2f} ticks a side — so the edge "
                    f"filter is letting through entries that do not cover "
                    f"their costs.")
        else:
            text = (f"The slippage budget is "
                    f"{abs(budget_ticks - slip_ticks_actual):.2f} ticks "
                    f"{'generous' if generous else 'mean'}. The edge filter "
                    f"charges {budget_ticks:.2f} ticks a side and the fills "
                    f"cost {slip_ticks_actual:.2f}" +
                    (" — so it is refusing entries it would have paid for."
                     if generous else
                     " — so it is letting through entries that do not cover "
                     "their costs."))

        # Nothing to propose when the budget is already where it should be.
        if not (improving and not budget_ticks):
            finding = {
                'kind': 'slippage_budget',
                'budget_ticks': round(budget_ticks, 3),
                'measured_ticks': round(slip_ticks_actual, 3),
                'suggest_ticks': round(suggest, 3),
                'generous': generous,
                'improving': improving,
                'text': text,
            }

    return {
        'lines': lines,
        'total': totals,
        'fills': len(fills),
        'unmeasured_fills': unmeasured,
        'budget_ticks': budget_ticks,
        'measured_ticks': (round(slip_ticks_actual, 3)
                           if slip_ticks_actual is not None else None),
        'finding': finding,
    }


# ---------------------------------------------------------------------------
# the reports
# ---------------------------------------------------------------------------

def exit_reasons(trades: List[Any]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        name = t.exit_reason.value if t.exit_reason else 'UNKNOWN'
        g = groups.setdefault(name, {'reason': name, 'count': 0, 'net': 0.0,
                                     'unmeasured': 0})
        g['count'] += 1
        if t.net_pnl is None:
            g['unmeasured'] += 1
        else:
            g['net'] += t.net_pnl
    rows = sorted(groups.values(), key=lambda g: -g['count'])
    for g in rows:
        g['net'] = round(g['net'], 2)
    return rows


def journal(trades: List[Any]) -> List[Dict[str, Any]]:
    """Every column is what was RECORDED AT THE TIME, never recomputed now.
    The z on a row is the z that decision actually fired at."""
    out = []
    for t in trades:
        opened, closed = _dt(t.opened_at), _dt(t.closed_at)
        held = ((closed - opened).total_seconds() / 60.0
                if opened and closed else None)
        out.append({
            'opened_at': t.opened_at.isoformat() if opened else None,
            'closed_at': t.closed_at.isoformat() if closed else None,
            'side': t.side.value, 'qty': t.opened_qty or t.qty,
            'entry_z': t.entry_z, 'exit_z': t.exit_z,
            'entry_price': t.avg_price, 'exit_price': t.exit_price,
            'gross': t.gross_pnl, 'fees': t.fees_paid, 'net': t.net_pnl,
            'on_margin': t.pnl_pct_on_margin,
            'held_min': round(held, 1) if held is not None else None,
            'exit_reason': t.exit_reason.value if t.exit_reason else None,
            'simulated': t.is_simulated,
            'tickets': list(t.tickets or []),
        })
    return out


def _measured_sides(trades, fills, contract) -> List[Dict[str, Any]]:
    """The slippage the Costs card measures, ONE figure with the slippage
    report: each trade's entry and exit against the price its DECISION was
    made at. A trade recorded before decisions were kept falls back to its
    fills' own figure against the touch they were sent at; a PAPER trade is
    filled at the decision price by construction and is left out — it has
    nothing to measure."""
    tick_size = getattr(contract, 'tick_size', None)
    out, covered = [], set()
    for t in trades:
        tickets = [str(x) for x in (t.tickets or ())]
        if any(x.startswith('PAPER-') for x in tickets):
            covered.update(tickets)
            continue
        if t.entry_order_type is None and t.exit_order_type is None:
            continue                       # an older trade: its fills speak
        covered.update(tickets)
        qty = t.opened_qty or t.qty
        for points in (t.entry_slippage, t.exit_slippage):
            out.append({'slippage_ticks': (sizing.to_ticks(points, tick_size)
                                           if points is not None else None),
                        'qty': qty})
    out.extend(f for f in fills if str(f.get('exec_id')) not in covered)
    return out


def contract_report(db, config, key: str, period: str = 'all',
                    mode: str = 'live', now: Optional[datetime] = None
                    ) -> Dict[str, Any]:
    """Everything the Analysis window shows for one contract."""
    now = now or _now()
    start = period_start(period, now)
    contract = config.contract(key)
    settings = config.effective(key) if contract else {}

    trades = [t for t in db.closed_positions(key, limit=5000)
              if _in_period(t.closed_at, start) and _wants(t.is_simulated, mode)]
    fills = _measured_sides(trades, [
        f for f in db.fills(key, limit=20000)
        if _in_period(f.get('our_ts'), start)], contract)
    touches = [t for t in db.touches(key, limit=20000)
               if _in_period(t.get('ts'), start)]

    open_now = [p for p in db.open_positions() if p.contract_key == key]
    tick_size = getattr(contract, 'tick_size', None)
    tick_value = getattr(contract, 'tick_value', None)

    return {
        'key': key,
        'name': getattr(contract, 'name', key),
        'symbol': getattr(contract, 'symbol', ''),
        'decimals': getattr(contract, 'decimals', 4),
        'period': period,
        'mode': mode,
        'summary': summarise(trades),
        'touches': touch_study(
            touches,
            round_trip_points=costs_mod.cost_breakdown(
                float(settings.get('quantity', 1) or 1), tick_size, tick_value,
                settings).get('round_trip_points')),
        'exits': exit_reasons(trades),
        'costs': cost_report(trades, fills, settings, tick_size, tick_value),
        'journal': journal(trades),
        # Named, and excluded from every figure above.
        'open_positions': len(open_now),
        'entry_threshold': settings.get('entry_threshold'),
    }


def desk_report(db, config, period: str = 'all', mode: str = 'live',
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """One row per contract, and a labelled total.

    Nothing here is averaged across contracts except the last row, which says
    so. A contract with too few closed trades is described and given no
    verdict.
    """
    now = now or _now()
    rows = []
    everything: List[Any] = []
    for key, contract in config.contracts.items():
        report = contract_report(db, config, key, period, mode, now)
        s = report['summary']
        everything.extend(
            [t for t in db.closed_positions(key, limit=5000)
             if _in_period(t.closed_at, period_start(period, now))
             and _wants(t.is_simulated, mode)])
        rows.append({
            'key': key,
            'name': contract.name,
            'trades': s['trades'],
            'win_rate': s['win_rate'],
            'net': s['net'],
            'avg': s['avg'],
            'on_margin': s['on_margin'],
            'cost_drag': s['cost_drag'],
            'avg_hold_min': s['avg_hold_min'],
            'best_level': report['touches']['best_level'],
            'enough_to_judge': s['enough_to_judge'],
            'verdict': _verdict(s),
        })

    rows.sort(key=lambda r: (r['net'] is None, -(r['net'] or 0)))
    return {'rows': rows, 'total': summarise(everything),
            'period': period, 'mode': mode,
            'min_trades_for_a_verdict': MIN_TRADES_FOR_A_VERDICT}


def _verdict(s: Dict[str, Any]) -> str:
    """In words, and only where there is enough to say anything."""
    if not s['trades']:
        return 'no data yet'
    if not s['enough_to_judge']:
        return 'too few to judge'
    if s['net'] is None:
        return 'not measured'
    if s['net'] < 0:
        return 'losing'
    if s['cost_drag'] is not None and s['cost_drag'] > 25:
        return 'costs eat it'
    return 'earning'
