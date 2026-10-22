"""The Algo: Bollinger bands on ONE contract, and what it decides.

Ported from the MT5 desk's Algo. The venue lists the spread itself as a
single contract, so everything that desk did with two legs and a beta is
gone: the price is the contract's own BID (what a sale receives), OFFER
(what a purchase pays) and MID.

What follows DECIDES and says what it would do. **It does not place,
modify or cancel an order** — what it decides leaves as an INTENT ("enter,
selling", "exit, profit target"), and the engine is what acts on it: on
paper, or through the executor.

- *the band*: EMA(N) of the contract's candles, plus and minus
  `entry_z` x sigma (population, last N closes, the forming candle
  included). See `bands`.
- *entry*: a side is ARMED when its stretch reaches the band — the BID's
  z at or above `+entry_z` (H to L, sell), the OFFER's at or below
  `-entry_z` (L to H, buy) — and ENTERS on the way back in, between
  `entry_z - reentry_back` and the far edge of the re-entry window. With
  re-entry off it enters on the touch. It must hold for `confirm_ticks`
  fresh quotes in a row, and only with no position open.
- *gates* hold an ENTRY back and say why: no price or a stale/jumping
  one, not enough candles, the warm-up, the cooldown, the day's limits,
  the session cutoff, a |z| past `max_entry_z`, and the filters (edge,
  regime, trend, half-life). **A gate never holds back an exit.**
- *exit*, on the CLOSING side, measured from the position's own fill:
  the STOP LOSS and the PROFIT TARGET (break-even after every cost, minus
  or plus a % of the margin or a multiple of the ATR), and three more,
  each OFF unless the contract turns it on: a z stop, back at the mean
  only in profit, and a time stop.
"""

import math

from . import algofilters
from . import bands

#: The least time a refused entry waits before it is tried again.
ENTRY_RETRY_SEC = 30.0

#: The candle sizes a contract can use, in minutes.
TIMEFRAMES = (1, 5, 15, 30, 60, 240)

#: The directions an entry may take, and the side each one is. The
#: contract settings say SELL_ONLY / BUY_ONLY; the screen says H to L /
#: L to H.
DIRECTIONS = {'BOTH': ('SELL', 'BUY'), 'H_TO_L': ('SELL',),
              'L_TO_H': ('BUY',)}
_FROM_SETTING = {'BOTH': 'BOTH', 'SELL_ONLY': 'H_TO_L', 'BUY_ONLY': 'L_TO_H'}

#: How a stop or a target is sized.
LEVEL_MODES = ('MARGIN', 'ATR')


def _num(settings, key, default):
    value = settings.get(key)
    if value is None or value == '':
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def params_from_settings(s):
    """The Algo's parameters, from a contract's EFFECTIVE settings.

    One place maps the settings page's names onto the rule's, and cleans
    them: blank means the default, never zero — a blank entry z read as 0
    would signal on every quote.
    """
    s = s or {}
    p = {
        'entry_z': _num(s, 'entry_threshold', 2.5),
        'direction': _FROM_SETTING.get(
            str(s.get('trade_direction') or 'BOTH').upper(), 'BOTH'),
        'timeframe_min': int(_num(s, 'timeframe_min', 15)),
        'length': int(_num(s, 'length', 20)),
        'confirm_ticks': int(_num(s, 'confirm_samples', 3)),
        'max_entry_z': _num(s, 'max_entry_z', 3.5),
        'reentry_on': bool(s.get('reentry_on', True)),
        'reentry_back': _num(s, 'reentry_back', 0.5),
        'reentry_window_pct': _num(s, 'reentry_window_pct', 50.0),
        'trend_on': bool(s.get('trend_on', True)),
        'trend_sigma': _num(s, 'trend_sigma', 1.0),
        'trend_lookback_min': _num(s, 'trend_lookback_min', 120.0),
        'cutoff_buffer_min': _num(s, 'cutoff_buffer_min', 20.0),
        'cooldown_min': _num(s, 'entry_cooldown_seconds', 300.0) / 60.0,
        'warmup_min': _num(s, 'warmup_min', 90.0),
        'stop_loss_on': bool(s.get('stop_loss_on', True)),
        'stop_loss_pct': _num(s, 'stop_loss_pct', 2.0),
        'target_pct': _num(s, 'profit_target_pct', 2.0),
        'stop_mode': str(s.get('stop_mode') or 'MARGIN').upper(),
        'target_mode': str(s.get('target_mode') or 'MARGIN').upper(),
        'atr_period': int(_num(s, 'atr_period', 14)),
        'atr_stop_mult': _num(s, 'atr_stop_mult', 2.0),
        'atr_target_mult': _num(s, 'atr_target_mult', 1.5),
        'progress_bar': bool(s.get('progress_bar', True)),
        'algo_qty': _num(s, 'quantity', 1.0),
        'max_trades_day': int(_num(s, 'max_trades_per_day', 10)),
        'daily_loss_limit': _num(s, 'daily_max_loss', 0.0),
        'max_losses_row': int(_num(s, 'max_losses_row', 3)),
        'edge_on': bool(s.get('edge_on', True)),
        'edge_multiple': _num(s, 'edge_multiple', 1.5),
        'edge_capture_frac': _num(s, 'edge_capture_frac', 0.5),
        'regime_on': bool(s.get('regime_on', True)),
        'regime_er_max': _num(s, 'regime_er_max', 0.6),
        'regime_min_crossings': int(_num(s, 'regime_min_crossings', 4)),
        'half_life_min_min': _num(s, 'half_life_min_min', 0.0),
        'half_life_max_min': _num(s, 'half_life_max_min', 0.0),
        'stop_z_on': bool(s.get('stop_z_on', False)),
        'stop_z': _num(s, 'stop_loss_z', 4.0),
        'reversion_on': bool(s.get('exit_at_mean', False)),
        'time_stop_min': _num(s, 'max_hold_minutes', 0.0),
    }
    if p['timeframe_min'] not in TIMEFRAMES:
        p['timeframe_min'] = 15
    p['length'] = max(2, p['length'])
    p['confirm_ticks'] = max(1, p['confirm_ticks'])
    if p['entry_z'] <= 0:
        p['entry_z'] = 2.5
    # Re-entry happens INSIDE the band and above the mean: never at or past
    # the mean, where there is nothing left to revert.
    p['reentry_back'] = min(max(p['reentry_back'], 0.05), p['entry_z'] * 0.9)
    p['reentry_window_pct'] = min(max(p['reentry_window_pct'], 5.0), 100.0)
    if p['trend_lookback_min'] <= 0:
        p['trend_lookback_min'] = 120.0
    if p['algo_qty'] <= 0:
        p['algo_qty'] = 1.0
    p['atr_period'] = max(2, p['atr_period'])
    for key, default in (('atr_stop_mult', 2.0), ('atr_target_mult', 1.5)):
        if p[key] <= 0:
            p[key] = default
    for key in ('stop_mode', 'target_mode'):
        if p[key] not in LEVEL_MODES:
            p[key] = 'MARGIN'
    p['sides'] = {side: _side_levels(s, p, suffix)
                  for side, suffix in (('SELL', 'hl'), ('BUY', 'lh'))}
    return p


#: The level fields a direction may set for itself.
_SIDE_LEVEL_FIELDS = (('target_mode', 'target_mode'),
                      ('target_pct', 'profit_target_pct'),
                      ('atr_target_mult', 'atr_target_mult'),
                      ('stop_mode', 'stop_mode'),
                      ('stop_loss_pct', 'stop_loss_pct'),
                      ('atr_stop_mult', 'atr_stop_mult'))


def _side_levels(s, p, suffix):
    """One direction's OWN levels: H to L (`_hl`, a SELL) or L to H (`_lh`,
    a BUY). Only what that direction set — blank is "same as both", never
    zero — so the shared figures are read at the time they are used."""
    out = {}
    for key, setting in _SIDE_LEVEL_FIELDS:
        raw = s.get(setting + '_' + suffix)
        if key.endswith('_mode'):
            value = str(raw or '').upper()
            if value in LEVEL_MODES:
                out[key] = value
        elif raw not in (None, ''):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            # A non-positive multiple is not a level; the shared one stands.
            if value > 0 or key.endswith('_pct'):
                out[key] = value
    return out


def side_params(p, side):
    """`p` with this direction's own levels laid over the shared ones: what
    `levels` prices a position on that side with."""
    own = (p.get('sides') or {}).get(side)
    if not own:
        return p
    q = dict(p)
    q.update(own)
    return q


def zscore(value, mean, sigma):
    """(value - mean) / sigma, or None when any of them is unmeasured."""
    if value is None or mean is None or not sigma:
        return None
    return (float(value) - float(mean)) / float(sigma)


def reentry_window(p):
    """(enters at, window ends at), as |z|: 2.0 armed, back 0.5, window
    50 % -> (1.50, 0.75)."""
    back_at = p['entry_z'] - p['reentry_back']
    return back_at, back_at * (1.0 - p['reentry_window_pct'] / 100.0)


def entry_z_taken(p):
    """The z an entry is actually taken at: the band, or the way back in."""
    return p['entry_z'] - (p['reentry_back'] if p['reentry_on'] else 0.0)


#: The positional fields the window shows, passed through untouched.
_POSITION_DISPLAY = ('quantity', 'opened_at', 'age_sec', 'entry_atr',
                     'entry_slip_ticks',
                     'stop_mode', 'target_mode', 'tp_money', 'sl_money',
                     'entry_z', 'paper', 'manual', 'levels_why')


class AlgoSignal:
    """One contract's Algo: it watches, decides, and says so.

    `evaluate` is called on every pass with what the engine already has —
    the market, the band, the open position and the gates — and returns
    what the Algo says NOW, plus the INTENTS that became true on this
    call. An intent is reported once, on the edge where it becomes true,
    so the record is one line per signal and not one per pass.
    """

    def __init__(self, params):
        self.params = dict(params)
        self._streak = {'BUY': 0, 'SELL': 0}
        self._last_quote = None
        self._entry_live = None          # the side whose signal is showing
        self._exits_live = {}            # position id -> reason showing
        self._known = set()              # position ids seen last call
        self._cooldown_until = None
        #: Re-entry: which sides have stretched to the band and are waiting
        #: for the price to come back inside it.
        self._armed = {'BUY': False, 'SELL': False}

    def evaluate(self, now, md, stats, positions=(), gates=None):
        """What the Algo says, and what it decided on this call.

        - `now`: seconds, the clock the cooldown and time stop run on.
        - `md`: {'bid', 'ask', 'mid', 'quote_id'}, or None with no price.
        - `stats`: `bands.SpreadCandles.stats()`.
        - `positions`: the OPEN position(s), each {position_id, side,
          entry, opened_at, break_even, tp, sl, net_pnl}. `tp` or `sl`
          None is a level that is not priced (or the stop is off): no exit
          is signalled on a number that does not exist.
        - `gates`: {'health', 'warmup', 'cutoff_min', 'session', 'halt',
          'levels', 'entry_check': f(side, z) -> why not, or None}.
        """
        p = self.params
        gates = gates or {}
        stats = stats or {}
        positions = list(positions or ())
        ready = bool(stats.get('ready'))
        mean = stats.get('mean') if ready else None
        sigma = stats.get('sigma') if ready else None
        band = None if not ready else p['entry_z'] * sigma
        body = {
            'params': dict(p), 'ready': ready, 'count': stats.get('count'),
            'needed': stats.get('needed'), 'note': stats.get('note'),
            'mean': mean, 'sigma': sigma,
            'upper': None if band is None else mean + band,
            'lower': None if band is None else mean - band,
            'z_buy': None, 'z_sell': None, 'z_mid': None,
            'state': 'WATCHING', 'signal': None, 'blocked': None,
            'health': gates.get('health'),
            'cooldown_sec': None, 'positions': [], 'intents': [],
            'warmup': gates.get('warmup')}
        bid = (md or {}).get('bid')
        ask = (md or {}).get('ask')
        if md and ready:
            body['z_sell'] = zscore(bid, mean, sigma)
            body['z_buy'] = zscore(ask, mean, sigma)
            body['z_mid'] = zscore(md.get('mid'), mean, sigma)

        # A position gone since the last call starts the cooldown: whatever
        # closed it, the next entry waits.
        current = {pos['position_id'] for pos in positions}
        if self._known - current:
            self._start_cooldown(now)
        self._known = current
        for gone in set(self._exits_live) - current:
            del self._exits_live[gone]

        fresh = self._fresh_quote(md)
        self._count(body, fresh)

        for pos in positions:
            body['positions'].append(self._judge_exit(now, md, pos, body))

        if self._cooldown_until is not None and now < self._cooldown_until:
            body['cooldown_sec'] = self._cooldown_until - now

        if positions:
            body['state'] = 'IN_POSITION'
            self._entry_live = None
            if any(row['exit'] for row in body['positions']):
                body['state'] = 'EXIT'
            return body

        self._judge_entry(body, md, gates)
        return body

    # -- entry ------------------------------------------------------------

    def _fresh_quote(self, md):
        """A NEW price, or the same one polled again? "Three in a row"
        means three prices: a pass that finds the same quote is not a
        second confirmation of it."""
        if not md:
            self._last_quote = None
            return False
        quote = md.get('quote_id')
        if quote is None:
            return True
        if quote == self._last_quote:
            return False
        self._last_quote = quote
        return True

    def _count(self, body, fresh):
        p = self.params
        entry = p['entry_z']
        z_sell, z_buy = body['z_sell'], body['z_buy']
        if p['reentry_on']:
            back_at, floor = reentry_window(p)
            # Armed by the stretch; disarmed once through the window's far
            # edge — back too far, too fast, to leave anything to trade.
            if z_sell is not None:
                if z_sell >= entry:
                    self._armed['SELL'] = True
                elif z_sell <= floor:
                    self._armed['SELL'] = False
            if z_buy is not None:
                if z_buy <= -entry:
                    self._armed['BUY'] = True
                elif z_buy >= -floor:
                    self._armed['BUY'] = False
            hits = {'SELL': (self._armed['SELL'] and z_sell is not None
                             and floor < z_sell <= back_at),
                    'BUY': (self._armed['BUY'] and z_buy is not None
                            and -back_at <= z_buy < -floor)}
        else:
            self._armed = {'BUY': False, 'SELL': False}
            hits = {'SELL': z_sell is not None and z_sell >= entry,
                    'BUY': z_buy is not None and z_buy <= -entry}
        body['armed'] = dict(self._armed)
        for side, hit in hits.items():
            if not hit:
                self._streak[side] = 0
            elif fresh:
                self._streak[side] += 1
        body['streak'] = dict(self._streak)

    def _judge_entry(self, body, md, gates):
        p = self.params
        side = None
        allowed = DIRECTIONS.get(p['direction'], DIRECTIONS['BOTH'])
        for candidate in ('SELL', 'BUY'):
            if candidate not in allowed:
                continue           # shown, but not an entry on this contract
            if self._streak[candidate] >= p['confirm_ticks']:
                side = candidate
        blocked = self._entry_gate(body, md, gates, side)
        if side is None:
            self._entry_live = None
            if blocked:
                body['state'] = 'BLOCKED'
                body['blocked'] = blocked
            elif any(self._streak.values()):
                body['state'] = 'CONFIRMING'
            return
        if blocked:
            body['state'] = 'BLOCKED'
            body['blocked'] = blocked
            # A signal that WOULD have entered: for "Last signal blocked".
            body['blocked_side'] = side
            body['blocked_z'] = (body['z_sell'] if side == 'SELL'
                                 else body['z_buy'])
            self._entry_live = None
            return
        body['state'] = 'SIGNAL'
        body['signal'] = side
        if self._entry_live != side:
            self._entry_live = side
            self._armed[side] = False          # spent by the entry it gave
            body['armed'] = dict(self._armed)
            z = body['z_sell'] if side == 'SELL' else body['z_buy']
            body['intents'].append({
                'action': 'ENTER', 'side': side, 'z': z,
                'price': md.get('bid' if side == 'SELL' else 'ask'),
                'mid': md.get('mid'), 'mean': body['mean'],
                'sigma': body['sigma'], 'upper': body['upper'],
                'lower': body['lower'], 'entry_z': p['entry_z']})

    def _entry_gate(self, body, md, gates, side):
        """Why an entry is held back now, in words, or None."""
        p = self.params
        if not md:
            return 'no price'
        if gates.get('health'):
            return gates['health']
        if gates.get('mode'):
            return gates['mode']
        if gates.get('halt'):
            return gates['halt']
        if not body['ready']:
            return (body.get('note') or
                    f"collecting candles {body.get('count') or 0}"
                    f"/{body.get('needed')}")
        warmup = gates.get('warmup')
        if warmup and not warmup.get('done'):
            return (f"warming up: {int(warmup['sec'] // 60)} of "
                    f"{round(warmup['need_sec'] / 60.0)} min of live "
                    f"prices watched")
        if body.get('cooldown_sec'):
            return f"cooldown {_mmss(body['cooldown_sec'])}"
        if gates.get('session'):
            return gates['session']
        levels_why = gates.get('levels')
        if isinstance(levels_why, dict):          # one reason per direction
            levels_why = levels_why.get(side)
        if levels_why:
            return levels_why
        buffer_min = p['cutoff_buffer_min']
        cutoff = gates.get('cutoff_min')
        if buffer_min and cutoff is not None and cutoff <= buffer_min:
            return ('past the session cutoff' if cutoff <= 0 else
                    f'{cutoff:.0f} min to the session cutoff')
        if side is not None and p['max_entry_z']:
            z = body['z_sell'] if side == 'SELL' else body['z_buy']
            if z is not None and abs(z) > p['max_entry_z']:
                return (f'z {z:+.2f} is past the {p["max_entry_z"]:g} cap — '
                        f'a blow-out, not a stretch')
        check = gates.get('entry_check')
        if side is not None and check is not None:
            z = body['z_sell'] if side == 'SELL' else body['z_buy']
            return check(side, z)
        return None

    def entry_failed(self, now):
        """An entry that was SENT and did not go on: the cooldown starts,
        so the same refused order is not sent ten times a second — at least
        `ENTRY_RETRY_SEC`, even on a contract whose cooldown is 0."""
        self._entry_live = None
        self._start_cooldown(now)
        floor = now + ENTRY_RETRY_SEC
        if self._cooldown_until is None or self._cooldown_until < floor:
            self._cooldown_until = floor

    def exit_failed(self, position_id):
        """An exit that was sent and did not close: report it again."""
        self._exits_live.pop(position_id, None)

    def _start_cooldown(self, now):
        minutes = self.params['cooldown_min']
        if minutes:
            self._cooldown_until = now + 60.0 * minutes

    # -- exit -------------------------------------------------------------

    def _judge_exit(self, now, md, pos, body):
        """Should THIS position come off, and why. Never gated."""
        p = self.params
        side = pos.get('side')
        # The CLOSING side: a long sells at the bid, a short buys the offer.
        closing = (md or {}).get('bid' if side == 'BUY' else 'ask')
        z_close = zscore(closing, body['mean'], body['sigma'])
        tp, sl, be = pos.get('tp'), pos.get('sl'), pos.get('break_even')
        entry = pos.get('entry')
        row = {'position_id': pos['position_id'], 'side': side,
               'entry': entry, 'closing': closing, 'z_close': z_close,
               'tp': tp, 'sl': sl, 'break_even': be,
               'net_pnl': pos.get('net_pnl'), 'exit': None,
               'progress': progress(side, entry, closing, tp, sl)}
        for name in _POSITION_DISPLAY:
            if name in pos:
                row[name] = pos[name]
        reason = None
        if closing is not None and sl is not None and (
                closing <= sl if side == 'BUY' else closing >= sl):
            reason = 'STOP_LOSS'
        elif closing is not None and tp is not None and (
                closing >= tp if side == 'BUY' else closing <= tp):
            reason = 'PROFIT_TARGET'
        elif p['stop_z_on'] and z_close is not None and (
                z_close <= -p['stop_z'] if side == 'BUY'
                else z_close >= p['stop_z']):
            reason = 'Z_STOP'
        elif p['reversion_on'] and z_close is not None and be is not None \
                and closing is not None and (
                    (z_close >= 0 and closing >= be) if side == 'BUY'
                    else (z_close <= 0 and closing <= be)):
            reason = 'MEAN_REVERSION'
        elif p['time_stop_min'] and pos.get('opened_at') is not None and (
                now - float(pos['opened_at']) >= p['time_stop_min'] * 60.0):
            reason = 'TIME_STOP'
        row['exit'] = reason
        if reason is None:
            self._exits_live.pop(pos['position_id'], None)
            return row
        if self._exits_live.get(pos['position_id']) != reason:
            self._exits_live[pos['position_id']] = reason
            self._start_cooldown(now)
            body['intents'].append({
                'action': 'EXIT', 'position_id': pos['position_id'],
                'side': side, 'reason': reason, 'price': closing,
                'z': z_close, 'entry': entry, 'break_even': be,
                'tp': tp, 'sl': sl, 'net_pnl': pos.get('net_pnl')})
        return row


def progress(side, entry, closing, tp, sl):
    """Where the closing price sits between the stop and the target: +1 AT
    the target, -1 AT the stop, 0 the entry, clamped. Each half on its own
    scale — the stop and the target are rarely the same distance away."""
    if closing is None or entry is None:
        return None
    sign = 1.0 if side == 'BUY' else -1.0
    gained = sign * (float(closing) - float(entry))
    if gained >= 0:
        if tp is None:
            return None
        room = sign * (float(tp) - float(entry))
        return 1.0 if room <= 0 else min(1.0, gained / room)
    if sl is None:
        return None
    room = sign * (float(entry) - float(sl))
    return -1.0 if room <= 0 else max(-1.0, gained / room)


#: The reasons an exit is signalled, in the trader's words.
EXIT_WORDS = {
    'STOP_LOSS': 'stop loss',
    'PROFIT_TARGET': 'profit target (after costs)',
    'Z_STOP': 'z-stop',
    'MEAN_REVERSION': 'back to the mean, in profit',
    'TIME_STOP': 'time stop',
}


def levels(side, entry, fee_points, p, k, margin, atr):
    """(break_even, tp, sl, why_not) for a position entered at `entry`.

    - break-even: the entry moved by the round trip's fees and slippage,
      in points (the bid-ask is already in the entry and the closing price);
    - target: break-even plus `target_pct` % of the margin (MARGIN), or
      `atr_target_mult` x the ATR at entry (ATR);
    - stop: break-even minus `stop_loss_pct` % of the margin, or
      `atr_stop_mult` x ATR; None when the stop is off.

    Each of these is the DIRECTION's own where it set one (`side_params`):
    H to L (a SELL) and L to H (a BUY) can be sized differently.

    `k` is money per 1.00 of price for the WHOLE position (k per contract
    x qty). A level that cannot be priced is None, and `why_not` says why —
    an entry is not taken without its levels.
    """
    if entry is None or fee_points is None:
        return None, None, None, 'levels: the round-trip cost is not priced'
    # H to L and L to H may each have their own target and stop.
    p = side_params(p, side)
    sign = 1.0 if side == 'BUY' else -1.0
    be = float(entry) + sign * float(fee_points)
    tp = sl = None
    why = None
    if p['target_mode'] == 'ATR':
        if not atr:
            why = 'levels: ATR not measured yet'
        else:
            tp = be + sign * p['atr_target_mult'] * atr
    elif not margin or not k:
        why = ('levels: no margin entered for this contract — the target is '
               'a % of it; set Margin per contract')
    else:
        tp = be + sign * (p['target_pct'] / 100.0) * margin / k
    if p['stop_loss_on']:
        if p['stop_mode'] == 'ATR':
            if not atr:
                why = why or 'levels: ATR not measured yet'
            else:
                sl = be - sign * p['atr_stop_mult'] * atr
        elif not margin or not k:
            why = why or ('levels: no margin entered for this contract — the '
                          'stop is a % of it; set Margin per contract')
        else:
            sl = be - sign * (p['stop_loss_pct'] / 100.0) * margin / k
    return be, tp, sl, why


def levels_gate(p, price, fee_points, k, margin, atr, width):
    """Why an entry on each side could not be given its levels, or None:
    {'BUY': why, 'SELL': why}. Per direction, because H to L and L to H may
    size their target and stop differently — one in ATR before the ATR is
    measured, the other in % of margin — and an entry is not taken without
    its own levels. A stop inside the bid-ask is a stop that fires at once."""
    out = {}
    for side in ('BUY', 'SELL'):
        _, _, sl, why = levels(side, price, fee_points, p, k, margin, atr)
        if why is None and sl is not None and price is not None \
                and abs(price - sl) <= (width or 0.0):
            why = 'levels: the stop is inside the bid-ask'
        out[side] = why
    return out


def trend_lookback_candles(p):
    """The trend filter's lookback, in candles of this timeframe."""
    return max(1, int(round(p['trend_lookback_min'] / float(p['timeframe_min']))))


def candles_kept(p):
    """How many closed candles the Algo keeps: enough for the EMA to settle
    (KEEP_MULTIPLE x N) AND for the trend filter to measure its drift (N +
    its lookback). Kept fewer, the trend filter is never measured — on
    1-minute candles a 120-minute lookback needs 140, and 5 x 20 kept it
    blocking every entry for ever."""
    return max(p['length'] * bands.KEEP_MULTIPLE,
               p['length'] + trend_lookback_candles(p) + 1)


def trend_drift(closes, length, lookback, sigma):
    """How far the band's middle (EMA) has moved over `lookback` candles,
    in sigma: + rising, - falling. None until there is enough history —
    and unmeasured is not flat."""
    if not sigma or lookback < 1 or len(closes) < length + lookback:
        return None
    now = bands.ema(closes, length)
    then = bands.ema(closes[:-lookback], length)
    if now is None or then is None:
        return None
    return (now - then) / float(sigma)


def judge_filters(p, md, stats, closes, cost_in):
    """The filters' readings for the window, and the entry check.

    Read at the z an entry would be taken at for the window, so its numbers
    are real before z ever gets there; judged at the actual z when a stretch
    confirms. A filter that cannot be priced BLOCKS. One function for the
    live Algo and the backtest, so the backtest judges what the live one
    would.

    `cost_in`: {'k': money per 1.00 per contract, 'commission': fees for
    the round turn of the Algo qty, 'slippage': the budget for it}.
    """
    cost_in = cost_in or {}
    qty = p['algo_qty']
    k = cost_in.get('k')
    width = None
    if md and md.get('ask') is not None and md.get('bid') is not None:
        width = md['ask'] - md['bid']
    cost = algofilters.round_trip_cost(width, k, qty,
                                       cost_in.get('commission'),
                                       cost_in.get('slippage'))
    sigma = stats.get('sigma') if stats.get('ready') else None
    hl_candles = algofilters.half_life(closes)
    hl_minutes = (None if hl_candles is None
                  else hl_candles * p['timeframe_min'])
    regime = algofilters.regime(closes[-2 * p['length']:],
                                p['regime_er_max'], p['regime_min_crossings'])
    lookback = trend_lookback_candles(p)
    drift = trend_drift(closes, p['length'], lookback, sigma)

    def edge_at(z):
        return algofilters.edge(z, sigma, k, qty, cost['total'],
                                p['edge_capture_frac'], p['edge_multiple'])

    def check(side, z):
        if p['edge_on']:
            verdict = edge_at(z)
            if verdict['ok'] is None:
                return 'edge filter: the round-trip cost is not priced yet'
            if not verdict['ok']:
                return (f"edge filter: capture {verdict['ratio']:.2f}x "
                        f"the cost, under the {p['edge_multiple']:g}x "
                        f"required")
        if p['regime_on'] and regime['state'] == 'TRENDING':
            return (f"regime: TRENDING (efficiency "
                    f"{regime['efficiency_ratio']:.2f}, "
                    f"{regime['crossings']} crossings)")
        if p['trend_on']:
            if drift is None:
                return 'trend filter: not enough candles to measure it yet'
            limit = p['trend_sigma']
            if side == 'SELL' and drift >= limit:
                return (f"trend: the middle ROSE {drift:.1f}σ in the last "
                        f"{p['trend_lookback_min']:g} min — no H to L "
                        f"against it")
            if side == 'BUY' and drift <= -limit:
                return (f"trend: the middle FELL {abs(drift):.1f}σ in the "
                        f"last {p['trend_lookback_min']:g} min — no L to H "
                        f"against it")
        low, high = p['half_life_min_min'], p['half_life_max_min']
        if low or high:
            if hl_minutes is None:
                return 'half-life: not mean-reverting now'
            if low and hl_minutes < low:
                return (f'half-life {hl_minutes:.0f} min under '
                        f'{low:g} — reverts too fast (noise)')
            if high and hl_minutes > high:
                return (f'half-life {hl_minutes:.0f} min over '
                        f'{high:g} — reverts too slowly to hold')
        return None

    preview_edge = edge_at(entry_z_taken(p))
    if drift is None:
        direction = None
    elif drift >= p['trend_sigma']:
        direction = 'UP'
    elif drift <= -p['trend_sigma']:
        direction = 'DOWN'
    else:
        direction = 'FLAT'
    filters = {
        'ready': bool(stats.get('ready')),
        'cost': cost, 'k': k, 'qty': qty,
        'edge': dict(preview_edge, on=p['edge_on']),
        'regime': dict(regime, on=p['regime_on']),
        'trend': {'on': p['trend_on'], 'drift_sigma': drift,
                  'state': direction, 'limit': p['trend_sigma'],
                  'lookback_min': p['trend_lookback_min']},
        'half_life_candles': hl_candles,
        'half_life_minutes': hl_minutes,
        'half_life_band': [p['half_life_min_min'], p['half_life_max_min']],
    }
    return filters, check


def _mmss(seconds):
    seconds = max(0, int(math.ceil(seconds)))
    return f'{seconds // 60}:{seconds % 60:02d}'
