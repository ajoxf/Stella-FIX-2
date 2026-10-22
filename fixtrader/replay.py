"""Replay recorded prices through the SAME signal code the desk runs.

The Analysis window says what happened. This says what a different setting
would have done to the same market — which is the other half of the loop, and
the half that stops "±1.0 reverts more often" from being acted on before
anybody checks whether it pays.

**What this is, exactly.** A SIGNAL replay. It builds the contract's candles
from the mids this engine recorded and runs them through `backtest.run` —
the live Algo's own `algo.AlgoSignal`, filters and levels — charging the
contract's configured round trip against every trade. It is not a fill
simulator and it never pretends to be one:

- **The recorded series is mids.** The book either side of the mid was not
  stored, so it is ASSUMED — one tick wide by default — and every report says
  so in `assumptions`. A replay whose spread assumption is wrong is wrong
  about its exits, because an exit reads the executable side.
- **Costs are BUDGETED, never measured.** The slippage in here is the number
  from the settings, not a fill that happened. `slippage_measured` is None and
  stays None: the measured figure lives in the Analysis window beside the
  budget, and blending the two would let a backtest report a cost it never
  paid.
- **Queue position does not exist here.** A limit entry is treated as filled
  at the price the signal fired on. That flatters a limit strategy and the
  report says which order types it assumed.
- **A position still open at the end is excluded** from every P&L figure and
  counted separately — the same rule the Analysis window follows for a live
  position, and for the same reason.

Nothing in this module re-implements a rule. It calls the Algo's own
functions, so a rule changed in `algo.py` changes what the replay says on
the same commit.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import algo as algo_mod
from . import backtest
from . import bands
from . import costs as costs_mod
from .models import BookTop

#: How wide the book is assumed to be, in ticks, when it was not recorded.
#: One tick is the usual quote on a listed spread. It is a stated assumption
#: and not a measurement, which is why it is reported back on every result.
DEFAULT_ASSUMED_SPREAD_TICKS = 1.0

#: Below this many closed trades a replay reports its figures and refuses to
#: draw a conclusion from them. Same threshold the Analysis window uses.
MIN_TRADES_FOR_A_VERDICT = 10


def assumptions(settings: Dict[str, Any], tick_size: float,
                tick_value: float,
                assumed_spread_ticks: float = DEFAULT_ASSUMED_SPREAD_TICKS
                ) -> Dict[str, Any]:
    """What the replay had to assume, in words, for the page it prints on.

    Attached to every result. A figure whose assumptions are not beside it is
    a figure somebody will quote without them.
    """
    qty = float(settings.get('quantity', 1.0) or 1.0)
    return {
        'book': (f'assumed {assumed_spread_ticks:g} tick wide around the '
                 f'recorded mid — the book itself was not recorded'),
        'fills': ('every order is treated as filled at the price its signal '
                  'fired on; there is no queue here, which flatters a limit '
                  'entry'),
        'costs': ("the contract's configured round trip, including the "
                  "slippage BUDGET. Nothing here is a measured cost"),
        'assumed_spread_ticks': assumed_spread_ticks,
        'round_trip_money': costs_mod.cost_breakdown(
            qty, tick_size, tick_value, settings)['round_trip_money'],
    }


def _margin_note(margin_per_contract: Optional[float],
                 entered: bool = False) -> str:
    """Which margin the target and the stop were priced off, in words."""
    if margin_per_contract and margin_per_contract > 0:
        return (f'{margin_per_contract:,.2f} per contract, ' +
                ('as entered for the contract' if entered else
                 'as the venue charged it on positions this contract '
                 'recorded'))
    return ('not entered for this contract and not recorded — a target '
            'that is a percentage of margin cannot be priced')


def _margin(settings, recorded):
    """The entered margin wins; the recorded one stands in. (margin, entered)"""
    entered = costs_mod.configured_margin(settings, 1.0)
    if entered:
        return entered, True
    return (recorded if recorded and recorded > 0 else None), False


def _book(mid: float, tick_size: float, spread_ticks: float,
          ts: datetime) -> BookTop:
    """A book around a recorded mid. The half-spread is the assumption."""
    half = (spread_ticks * tick_size) / 2.0
    return BookTop(bid=mid - half, ask=mid + half,
                   bid_size=None, ask_size=None, ts=ts)


def replay(samples: Sequence[Tuple[datetime, float]],
           settings: Dict[str, Any],
           tick_size: float, tick_value: float,
           contract_multiplier: Optional[float] = None,
           contract_key: str = "",
           assumed_spread_ticks: float = DEFAULT_ASSUMED_SPREAD_TICKS,
           margin_per_contract: Optional[float] = None
           ) -> Dict[str, Any]:
    """Run one set of settings over one recorded series, through the Algo.

    The recorded mids become the contract's candles and go through
    `backtest.run` — the live Algo's own `AlgoSignal`, filters and levels.
    The margin the target and stop are a percentage of: the one entered for
    the contract, else what the venue charged on recorded positions.
    """
    params = algo_mod.params_from_settings(settings)
    tf = params['timeframe_min'] * 60.0
    candles = bands.candles_from_samples(samples, tf)
    qty = params['algo_qty']
    breakdown = costs_mod.cost_breakdown(qty, tick_size, tick_value, settings)
    fees = sum(breakdown[x] or 0.0
               for x in ('commission', 'exchange', 'clearing'))
    k = (tick_value / tick_size) if tick_value and tick_size else None
    margin, entered = _margin(settings, margin_per_contract)
    result: Dict[str, Any] = {'trades': [], 'summary': _empty_summary(),
                              'still_open': 0,
                              'warm': len(candles) > params['length'],
                              'blocked_by': None, 'withheld': {},
                              'target_missing': None,
                              'assumptions': assumptions(
                                  settings, tick_size, tick_value,
                                  assumed_spread_ticks)}
    result['assumptions']['margin'] = _margin_note(margin, entered)
    if margin is None:
        result['target_missing'] = ('no margin entered for this contract, '
                                    'and none recorded')
    if not samples:
        result['blocked_by'] = 'nothing recorded for this period'
        return result
    if not result['warm']:
        result['blocked_by'] = (
            f"{len(candles)} candle(s) of {params['timeframe_min']} min "
            f"recorded, and the band needs {params['length']} before its "
            f"first entry. Record more.")
        return result
    run = backtest.run(candles, params,
                       (assumed_spread_ticks or 0.0) * (tick_size or 0.0), k,
                       breakdown['round_trip_points'], fees, margin,
                       breakdown['slippage_budget'] or 0.0)
    round_trip = breakdown['round_trip_money']
    trades = []
    for t in run['trades']:
        if t['closed_at'] is None:
            result['still_open'] += 1      # excluded from every figure
            continue
        gross = (None if t['pnl'] is None or round_trip is None
                 else t['pnl'] + round_trip)
        trades.append({'side': t['side'], 'entry': t['entry'],
                       'exit': t['exit'], 'entry_z': t['entry_z'],
                       'reason': t['reason'], 'net': t['pnl'],
                       'gross': gross, 'costs': round_trip,
                       'exit_price': t['exit'],
                       'opened_at': t['opened_at'],
                       'closed_at': t['closed_at'],
                       'held_sec': (t['closed_at'] - t['opened_at'])})
    result['trades'] = trades
    result['summary'] = summarise(trades, round_trip)
    result['withheld'] = dict(run['held'])
    if not trades:
        # The signal's own words: "nothing crossed" when a filter was the
        # blocker sends the desk to change the number that was never it.
        if run['held']:
            result['blocked_by'] = max(run['held'].items(),
                                       key=lambda kv: kv[1])[0]
        else:
            result['blocked_by'] = ('the band filled but nothing crossed '
                                    'the entry threshold over this period')
    return result


def _empty_summary() -> Dict[str, Any]:
    return {'trades': 0, 'won': 0, 'lost': 0, 'win_rate': None,
            'gross': None, 'costs': None, 'net': None, 'per_trade': None,
            'worst': None, 'best': None, 'median_hold_sec': None,
            'enough_to_judge': False, 'slippage_measured': None}


def summarise(trades: List[Dict[str, Any]],
              round_trip: Optional[float] = None) -> Dict[str, Any]:
    """The figures, and whether there are enough of them to mean anything."""
    out = _empty_summary()
    if not trades:
        return out
    nets = [t['net'] for t in trades if t['net'] is not None]
    out['trades'] = len(trades)
    if not nets:
        # Costs unmeasurable means net unmeasurable. Not zero.
        return out
    won = [n for n in nets if n > 0]
    out['won'] = len(won)
    out['lost'] = len(nets) - len(won)
    out['win_rate'] = round(100.0 * len(won) / len(nets), 1)
    out['gross'] = round(sum(t['gross'] for t in trades
                             if t['gross'] is not None), 2)
    out['costs'] = round(sum(t['costs'] for t in trades
                             if t['costs'] is not None), 2)
    out['net'] = round(sum(nets), 2)
    out['per_trade'] = round(out['net'] / len(nets), 2)
    out['worst'] = round(min(nets), 2)
    out['best'] = round(max(nets), 2)
    holds = sorted(t['held_sec'] for t in trades)
    out['median_hold_sec'] = round(holds[len(holds) // 2], 1)
    out['enough_to_judge'] = len(nets) >= MIN_TRADES_FOR_A_VERDICT
    return out


def sweep(samples: Sequence[Tuple[datetime, float]],
          settings: Dict[str, Any], tick_size: float, tick_value: float,
          thresholds: Sequence[float],
          contract_multiplier: Optional[float] = None,
          contract_key: str = "",
          assumed_spread_ticks: float = DEFAULT_ASSUMED_SPREAD_TICKS,
          margin_per_contract: Optional[float] = None
          ) -> Dict[str, Any]:
    """The same series at several entry thresholds.

    This is the answer to the question the touch table invites. The inner
    bands revert more often — they always do — and this says what each one
    would have MADE after its round trip, which is a different question with
    a different answer.
    """
    rows = []
    for threshold in thresholds:
        run = replay(samples, dict(settings, entry_threshold=float(threshold)),
                     tick_size, tick_value, contract_multiplier,
                     contract_key, assumed_spread_ticks,
                     margin_per_contract=margin_per_contract)
        rows.append({
            'entry_threshold': float(threshold),
            'trades': run['summary']['trades'],
            'win_rate': run['summary']['win_rate'],
            'net': run['summary']['net'],
            'per_trade': run['summary']['per_trade'],
            'worst': run['summary']['worst'],
            'median_hold_sec': run['summary']['median_hold_sec'],
            'enough_to_judge': run['summary']['enough_to_judge'],
            'blocked_by': run['blocked_by'],
            'withheld': run.get('withheld', {}),
            'target_missing': run.get('target_missing'),
        })
    # Every threshold blocked for the same reason is a statement about the
    # RECORDING, not about the thresholds. Said once, at the top, rather
    # than as a table of dashes that reads like "nothing paid".
    reasons = {r['blocked_by'] for r in rows}
    blocked = (rows[0]['blocked_by']
               if rows and len(reasons) == 1 and rows[0]['blocked_by']
               and not any(r['trades'] for r in rows) else None)
    stated = assumptions(settings, tick_size, tick_value, assumed_spread_ticks)
    stated['margin'] = _margin_note(*_margin(settings, margin_per_contract))
    return {'rows': rows, 'best': best_of(rows), 'blocked_by': blocked,
            'assumptions': stated}


def best_of(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The threshold that MADE the most, not the one that reverted most.

    A level that reverts is not a level that pays, and a row with too few
    trades behind it is not a finding — it is one lucky trade with a
    percentage sign after it.
    """
    judged = [r for r in rows
              if r['enough_to_judge'] and r['net'] is not None and r['net'] > 0]
    if not judged:
        return None
    return max(judged, key=lambda r: r['net'])
