"""Slippage: what each fill cost against the price the decision was made at.

Ported from the MT5 desk's slippage report, on ONE contract: the venue lists
the spread itself, so there are no legs to split it by. Every number here is
MEASURED — nothing simulates a fill, back-fills a missing one with zero, or
models what the market "should" have done.

The anchor is the DECISION price: the executable side when the Algo decided
— the bid for a sale, the offer for a purchase — or the touch when a close
was pressed. Not the touch when an order happened to be sent: a limit that
sat unfilled and was escalated to market is charged for the wait, because
the wait is what it cost.

Rules, each of which has cost money somewhere:

- **Positive is a COST, at both ends.** A short is bought back to close, so
  the sign turns between entry and exit; `slip` takes the side of the order
  itself, which is the side that paid. A report that reads an exit's cost as
  a gain is worse than no report.
- **Negative is an IMPROVEMENT**, the market moving our way between the
  decision and the fill. It is reported as one, never folded into a budget:
  a budget is never negative.
- **Unmeasured is not zero.** A position whose decision price or fill was not
  kept has no slippage — None, counted in its own column, never averaged in
  as 0.00. A PAPER fill is filled at the decision price by construction: it
  has nothing to measure, and it says so rather than reporting a perfect 0.
- **Money goes through `sizing`** — points x tick_value / tick_size x qty.
"""

from typing import Any, Dict, Iterable, List, Optional

from . import sizing

#: How many of the worst fills to list beside the summary.
WORST_N = 5


def slip(side: str, expected: Optional[float],
         filled: Optional[float]) -> Optional[float]:
    """Price points the fill was WORSE than `expected`, for an order on
    `side` ('BUY' / 'SELL'). Positive a cost, negative an improvement,
    None when either price is unknown."""
    if expected is None or filled is None or side is None:
        return None
    side = getattr(side, 'value', side)
    return ((float(filled) - float(expected)) if side == 'BUY'
            else (float(expected) - float(filled)))


def _mean(values):
    values = [v for v in values if v is not None]
    return (sum(values) / len(values)) if values else None


def _stats(samples: List[tuple]) -> Dict[str, Any]:
    """One end — entries, exits or the round turn — of a set of trades.

    `samples` are (ticks, money) for every trade at that end; a None in
    ticks is an UNMEASURED one, counted apart. With nothing measured every
    figure is None, which the screen renders as a dash.
    """
    measured = [s for s in samples if s[0] is not None]
    out = {'measured': len(measured),
           'unmeasured': len(samples) - len(measured),
           'ticks_mean': None, 'ticks_median': None, 'ticks_worst': None,
           'ticks_best': None, 'money_total': None, 'money_mean': None,
           'paid': 0, 'earned': 0, 'flat': 0}
    if not measured:
        return out
    ticks = sorted(s[0] for s in measured)
    cash = [s[1] for s in measured if s[1] is not None]
    middle = len(ticks) // 2
    out.update(
        ticks_mean=sum(ticks) / len(ticks),
        ticks_median=(ticks[middle] if len(ticks) % 2
                      else (ticks[middle - 1] + ticks[middle]) / 2.0),
        # Worst is the biggest COST, best the biggest improvement.
        ticks_worst=ticks[-1], ticks_best=ticks[0],
        money_total=sum(cash) if cash else None,
        money_mean=(sum(cash) / len(cash)) if cash else None,
        paid=sum(1 for t in ticks if t > 0),
        earned=sum(1 for t in ticks if t < 0),
        flat=sum(1 for t in ticks if t == 0))
    return out


def row(pos, contract) -> Dict[str, Any]:
    """One position, both ends, in ticks and in money."""
    tick_size = getattr(contract, 'tick_size', None)
    tick_value = getattr(contract, 'tick_value', None)
    qty = pos.opened_qty or pos.qty
    paper = any(str(t).startswith('PAPER-') for t in pos.tickets or ())

    def ticks(points):
        return sizing.to_ticks(points, tick_size) if points is not None \
            else None

    def money(points):
        return (sizing.to_money(points, tick_size, tick_value, qty)
                if points is not None else None)

    entry, exit_ = pos.entry_slippage, pos.exit_slippage
    open_ = pos.closed_at is None
    # A round turn counts only when BOTH ends were measured: one end plus a
    # zero is half of one.
    both = None if (entry is None or exit_ is None) else entry + exit_
    return {
        'position_id': pos.id, 'contract_key': pos.contract_key,
        'name': getattr(contract, 'name', pos.contract_key),
        'side': pos.side.value, 'qty': qty,
        'entry_order_type': pos.entry_order_type or ('PAPER' if paper
                                                     else None),
        'exit_order_type': pos.exit_order_type,
        'opened_at': pos.opened_at.isoformat() if pos.opened_at else None,
        'closed_at': pos.closed_at.isoformat() if pos.closed_at else None,
        'open': open_, 'paper': paper, 'simulated': bool(pos.is_simulated),
        'entry_price': pos.avg_price, 'exit_price': pos.exit_price,
        'entry_points': entry, 'entry_ticks': ticks(entry),
        'entry_money': money(entry),
        'exit_points': exit_, 'exit_ticks': ticks(exit_),
        'exit_money': money(exit_),
        'round_trip_ticks': ticks(both), 'round_trip_money': money(both),
        'net_pnl': pos.net_pnl,
    }


def _summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [r for r in rows if not r['open']]
    return {
        'entry': _stats([(r['entry_ticks'], r['entry_money'])
                         for r in rows if not r['paper']]),
        # An open position has no exit yet: that is not an unmeasured exit.
        'exit': _stats([(r['exit_ticks'], r['exit_money'])
                        for r in closed if not r['paper']]),
        'round_trip': _stats([(r['round_trip_ticks'], r['round_trip_money'])
                              for r in closed if not r['paper']]),
        'positions': len(rows),
        'paper': sum(1 for r in rows if r['paper']),
    }


def report(positions: Iterable, contracts: Dict[str, Any],
           budgets: Optional[Dict[str, float]] = None,
           worst_n: int = WORST_N) -> Dict[str, Any]:
    """The whole report: overall, by contract, by ORDER TYPE — a LIMIT that
    is not beating a MARKET on the same contract is costing time for
    nothing, and only the two side by side say so — and the worst fills.

    `budgets`: contract key -> the slippage BUDGET in ticks per side, put
    beside the measured mean so a budget is corrected from data.
    """
    budgets = budgets or {}
    rows = [row(p, contracts.get(p.contract_key)) for p in positions]
    by_contract: Dict[str, List] = {}
    by_type: Dict[str, List] = {}
    for r in rows:
        by_contract.setdefault(r['contract_key'], []).append(r)
        if not r['paper']:
            by_type.setdefault(r['entry_order_type'] or 'UNKNOWN',
                               []).append(r)
    measured = [r for r in rows if r['entry_money'] is not None]
    worst = sorted(measured, key=lambda r: -r['entry_money'])[:worst_n]
    contracts_out = {}
    for key, group in sorted(by_contract.items()):
        body = _summarise(group)
        body['name'] = group[0]['name']
        body['budget_ticks'] = budgets.get(key)
        contracts_out[key] = body
    return {
        'overall': _summarise(rows),
        'by_contract': contracts_out,
        'by_order_type': {k: _summarise(g) for k, g in sorted(by_type.items())},
        'worst': worst,
        'rows': sorted(rows, key=lambda r: r['opened_at'] or '', reverse=True),
        'counts': {'positions': len(rows),
                   'open': sum(1 for r in rows if r['open']),
                   'closed': sum(1 for r in rows if not r['open']),
                   'paper': sum(1 for r in rows if r['paper'])},
    }


def manual_summary(rows: List[Dict[str, Any]],
                   since: Optional[str] = None) -> Dict[str, Any]:
    """Manual tickets, each ticket's average fill against the touch when it
    was SENT (`ManualTerminal.slippage_rows`): entries, exits, by order type,
    and the worst. `since` (ISO time) cuts by the ticket's last update."""
    if since:
        rows = [r for r in rows if (r.get('time') or '') >= since]

    def stats(group):
        return _stats([(r['slippage_ticks'], r['slippage_money'])
                       for r in group])
    entries = [r for r in rows if r['end'] == 'entry']
    exits = [r for r in rows if r['end'] == 'exit']
    by_type: Dict[str, List] = {}
    for r in rows:
        by_type.setdefault(r['order_type'] or 'UNKNOWN', []).append(r)
    measured = [r for r in rows if r['slippage_money'] is not None]
    return {
        'entry': stats(entries), 'exit': stats(exits), 'all': stats(rows),
        'by_order_type': {k: stats(g) for k, g in sorted(by_type.items())},
        'worst': sorted(measured, key=lambda r: -r['slippage_money'])[:WORST_N],
        'rows': sorted(rows, key=lambda r: r.get('time') or '',
                       reverse=True)[:500],
        'counts': {'tickets': len(rows)},
    }
