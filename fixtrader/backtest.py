"""Replay recorded prices through the Algo: what it WOULD have done.

The same decision code the live Algo runs — `algo.AlgoSignal` for the
entry and the exits, `algo.judge_filters` for the filters, `algo.levels`
for the stop and the target, the same band — fed one candle at a time.
The candles are built from this system's own recording of the contract's
mid: a FIX market-data session has no history to ask for. Nothing here
can reach an order.

What it cannot see, and says so in its result:

- **Inside a candle.** Each candle is one look at the price, at its close:
  a stop touched and recovered inside the candle is not seen, and an exit
  is taken at the close it was seen at.
- **The book.** The bid-ask is one width, held constant.
- **Confirmation in quotes.** Live, an entry needs N fresh quotes in a
  row; here one candle close is one confirmation.
- **The warm-up and the live feed checks**, which are about the live tape.
"""

import re

from . import algo as algo_module
from . import algofilters
from . import bands

CAVEATS = [
    'one look per candle, at its close: a stop touched inside a candle '
    'is not seen, and exits are taken at the close',
    'the bid-ask is one width, held constant',
    'one candle close counts as one confirmation',
    'no warm-up and no live-feed checks — those are about the live tape',
    'candles are built from the mids this system recorded: time it was not '
    'running is not in them',
]


def run(rows, params, width, k, fee_points, commission, margin,
        slippage=0.0):
    """Replay closed candles `rows` [(bucket, mid close), ...].

    - `params`: `algo.params_from_settings` of the contract;
    - `width`: offer minus bid, held constant;
    - `k`: money per 1.00 of price per contract;
    - `fee_points`: the round trip's fees and slippage, in points, for the
      Algo qty — what break-even is moved by;
    - `commission`: the fees for the round turn, in money, and `slippage`
      the budget for it — both charged on each closed trade;
    - `margin`: the margin of the Algo qty, or None.

    Returns {'trades', 'held', 'summary', 'caveats'}.
    """
    p = dict(params)
    p['confirm_ticks'] = 1                 # one candle is one look
    signal = algo_module.AlgoSignal(p)
    tf = p['timeframe_min'] * 60.0
    rows = sorted((float(b), float(c)) for b, c in rows or () if c is not None)
    half = (width or 0.0) / 2.0
    qty = p['algo_qty']
    cost_in = {'k': k, 'commission': commission, 'slippage': slippage}
    charged = (commission or 0.0) + (slippage or 0.0)
    k_pos = (k * qty) if k else None
    margin_pos = (margin * qty) if margin else None

    trades, open_, held = [], {}, {}
    day = {'date': None, 'trades': 0, 'losses_row': 0}
    last_held = None
    counter = 0
    for i in range(len(rows)):
        bucket, close = rows[i]
        now = bucket + tf
        candles = bands.SpreadCandles(tf, p['length'],
                                      keep=algo_module.candles_kept(p))
        candles.seed(rows[:i + 1])
        closes = candles.closes()
        stats = candles.stats()
        md = {'bid': close - half, 'ask': close + half, 'mid': close,
              'quote_id': i}
        date = int(bucket // 86400)
        if date != day['date']:
            day = {'date': date, 'trades': 0, 'losses_row': 0}
        halt = None
        if p['max_trades_day'] and day['trades'] >= p['max_trades_day']:
            halt = f"{day['trades']} trades today — the day's limit"
        elif p['max_losses_row'] and day['losses_row'] >= p['max_losses_row']:
            halt = f"{day['losses_row']} losing trades in a row"
        positions = []
        for pid, pos in open_.items():
            closing = md['bid'] if pos['side'] == 'BUY' else md['ask']
            sign = 1.0 if pos['side'] == 'BUY' else -1.0
            positions.append(dict(pos, position_id=pid, net_pnl=(
                None if not k_pos else
                sign * (closing - pos['entry']) * k_pos - charged)))
        _, check = algo_module.judge_filters(p, md, stats, closes, cost_in)
        # The levels THIS entry would get, from the ATR on the candles
        # CLOSED before it, as live; none, and no entry, if unpriceable.
        atr = algofilters.atr(closes[:-1], p['atr_period'])
        levels_block = algo_module.levels_gate(p, close, fee_points, k_pos,
                                               margin_pos, atr, width)
        gates = {'health': None, 'halt': halt, 'entry_check': check,
                 'levels': levels_block}
        body = signal.evaluate(now, md, stats, positions, gates)
        if body.get('blocked_side'):
            reason = _reason_kind(body.get('blocked'))
            if reason != last_held:
                held[reason] = held.get(reason, 0) + 1
            last_held = reason
        elif body.get('state') != 'BLOCKED':
            last_held = None
        for intent in body['intents']:
            if intent['action'] == 'ENTER':
                counter += 1
                side = intent['side']
                entry = intent['price']
                be, tp, sl, _ = algo_module.levels(side, entry, fee_points, p,
                                                   k_pos, margin_pos, atr)
                open_[f'bt{counter}'] = {
                    'side': side, 'entry': entry, 'opened_at': now,
                    'break_even': be, 'tp': tp, 'sl': sl,
                    'entry_z': intent.get('z')}
                day['trades'] += 1
            elif intent['position_id'] in open_:
                pos = open_.pop(intent['position_id'])
                sign = 1.0 if pos['side'] == 'BUY' else -1.0
                exit_at = intent['price']
                pnl = (None if not k_pos else
                       sign * (exit_at - pos['entry']) * k_pos - charged)
                if pnl is not None:
                    day['losses_row'] = (day['losses_row'] + 1 if pnl < 0
                                         else 0)
                trades.append({
                    'side': pos['side'], 'opened_at': pos['opened_at'],
                    'entry': pos['entry'], 'entry_z': pos['entry_z'],
                    'closed_at': now, 'exit': exit_at,
                    'reason': intent.get('reason'), 'pnl': pnl})
    for pos in open_.values():
        trades.append({'side': pos['side'], 'opened_at': pos['opened_at'],
                       'entry': pos['entry'], 'entry_z': pos['entry_z'],
                       'closed_at': None, 'exit': None,
                       'reason': 'still open', 'pnl': None})
    return {'trades': trades, 'held': held,
            'summary': summarise(trades, rows, tf), 'caveats': CAVEATS}


def _reason_kind(reason):
    """A held-back reason without its changing numbers, for counting."""
    return re.sub(r'[-+]?\d[\d.,:]*', '#', reason or '?').strip()


def summarise(trades, rows, tf):
    closed = [t for t in trades if t['pnl'] is not None]
    wins = [t for t in closed if t['pnl'] > 0]
    net = sum(t['pnl'] for t in closed) if closed else 0.0
    peak, worst, running = 0.0, 0.0, 0.0
    for t in closed:
        running += t['pnl']
        peak = max(peak, running)
        worst = min(worst, running - peak)
    by_reason = {}
    for t in trades:
        by_reason[t['reason']] = by_reason.get(t['reason'], 0) + 1
    return {'candles': len(rows),
            'from': rows[0][0] if rows else None,
            'to': rows[-1][0] + tf if rows else None,
            'trades': len(trades), 'closed': len(closed),
            'wins': len(wins), 'losses': len(closed) - len(wins),
            'win_rate': (len(wins) / len(closed)) if closed else None,
            'net': net if closed else None,
            'max_drawdown': worst if closed else None,
            'exits': by_reason}
