"""Where one contract's Algo runs: its candles, its warm-up, its day.

`algo.AlgoSignal` decides; `bands.SpreadCandles` measures; `AlgoRun` feeds
them on every pass and keeps what the window shows beside the signal —
the last signal held back and why, the last order and what became of it,
the day's count. It never reaches an order: the engine acts on the
intents it returns.

- **History is the recording.** A FIX market-data session has no bars to
  backfill from, so the candles are built from the mids this system
  recorded, and the window says "collecting 7/20" until there are N.
- **The warm-up is live time watched since the Algo was armed.** A band
  built from the recording is not a feed that has been watched. A quick
  restart carries it (`WARMUP_CARRY_SEC`); standing the Algo down resets it.
- **The day's limits** — trades, a losing run, the money lost — stop
  ENTRIES for the rest of the day and say which. Exits carry on.
"""

import logging
import re
from collections import deque

from . import algo as algo_module
from . import algofilters
from . import bands

#: Warm-up progress carries over a restart when the Algo is back on within
#: this long of the last live price it watched.
WARMUP_CARRY_SEC = 900.0
WARMUP_SAVE_SEC = 10.0
#: The longest gap between two live prices that still counts as watched.
WARMUP_GAP_SEC = 10.0
#: A held-back signal is journalled when its reason changes, and again at
#: most this often while it stays the same.
BLOCKED_JOURNAL_SEC = 300.0
#: How long after an exit that did not close it is signalled again.
EXIT_RETRY_SEC = 5.0

logger = logging.getLogger(__name__)


class AlgoRun:
    """One contract's Algo, from its candles to what the window shows."""

    def __init__(self, key, params, store=None, clock_now=None):
        self.key = key
        self.params = params
        self.store = store
        self.signal = algo_module.AlgoSignal(params)
        self.candles = bands.SpreadCandles(params['timeframe_min'] * 60.0,
                                           params['length'],
                                           keep=algo_module.candles_kept(params))
        self.history = {'note': None, 'candles': 0}
        self.day = self._new_day(None)
        self.recent = deque(maxlen=20)
        self.last_blocked = None
        self.blocked_journal = None
        self.body = None
        self.live_sec = 0.0
        self.live_at = None
        self.warmup_saved_at = None
        self.armed_before = False
        #: The clock of the last pass, so a reshape between passes seeds
        #: from the same clock the passes run on.
        self.last_now = clock_now
        #: The mode the last pass ran in; a change re-opens the entry edge.
        self.last_mode = None
        #: position id -> when an exit for it was last sent.
        self.exit_sent = {}
        #: Seeded from the recording on the FIRST pass, on the clock the
        #: passes run on — never on a clock of its own, which is how a band
        #: comes up empty over hours of recorded mids.
        self.seeded = False

    @property
    def signature(self):
        return (self.params['timeframe_min'], self.params['length'],
                algo_module.candles_kept(self.params))

    # -- history --------------------------------------------------------------

    def seed(self, now):
        """Closed candles from the recorded mids — enough for the band and
        the EMA to settle and the trend filter to measure (`candles_kept`)."""
        if self.store is None:
            self.history['note'] = 'collecting candles from the live price'
            return
        tf = self.candles.timeframe_sec
        span = tf * (self.candles.keep + 2)
        from datetime import datetime, timezone
        since = datetime.fromtimestamp(now - span, timezone.utc)
        try:
            rows = self.store.samples_between(self.key, since=since)
        except Exception as e:                               # noqa: BLE001
            logger.error('[ALGO %s] recorded mids unreadable: %s', self.key, e)
            rows = []
        current = bands.bucket_of(now, tf)
        candles = [(b, c) for b, c in bands.candles_from_samples(rows, tf)
                   if b < current]
        self.candles.seed(candles)
        self.history['candles'] = len(candles)
        self.history['note'] = (f'{len(candles)} candles from the recorded '
                                f'mids' if candles else
                                'collecting candles from the live price')
        logger.info('[ALGO %s] %s', self.key, self.history['note'])

    def reshape(self, params, now):
        """New settings. A new timeframe or length is a different series:
        rebuilt from the recording. A new threshold is the same series read
        differently. Either way the day, the warm-up and what was last done
        carry — it is the same Algo."""
        old = self.signature
        now = self.last_now if self.last_now is not None else now
        self.params = params
        self.signal.params = params
        if old != self.signature:
            self.candles = bands.SpreadCandles(params['timeframe_min'] * 60.0,
                                               params['length'],
                                               keep=algo_module.candles_kept(params))
            if now is not None:
                self.seed(now)
            else:
                self.seeded = False         # on the first pass, then

    # -- the warm-up ----------------------------------------------------------

    def _warmup(self, now, live, armed):
        if armed and not self.armed_before:
            self._carry_warmup(now)
        if not armed and self.armed_before:
            # Stood down by the trader: the warm-up starts again next time.
            self.live_sec, self.live_at = 0.0, None
            if self.store is not None:
                try:
                    self.store.clear_warmup(self.key)
                except Exception as e:                       # noqa: BLE001
                    logger.error('could not clear the warm-up: %s', e)
        self.armed_before = armed
        if armed and live:
            if self.live_at is not None:
                self.live_sec += max(0.0, min(now - self.live_at,
                                              WARMUP_GAP_SEC))
            self.live_at = now
            self._save_warmup(now)
        else:
            self.live_at = None
        need = float(self.params['warmup_min']) * 60.0
        return {'sec': min(self.live_sec, need) if need else 0.0,
                'need_sec': need, 'done': self.live_sec >= need}

    def _carry_warmup(self, now):
        if self.store is None:
            return
        try:
            saved = self.store.warmup(self.key)
        except Exception as e:                               # noqa: BLE001
            logger.error('could not read the warm-up: %s', e)
            return
        if saved is None:
            return
        live_sec, at = saved
        if 0 <= now - at <= WARMUP_CARRY_SEC:
            self.live_sec = float(live_sec)
            logger.info('[ALGO %s] warm-up carried over: %.0f min watched',
                        self.key, live_sec / 60.0)

    def _save_warmup(self, now):
        if self.store is None:
            return
        if self.warmup_saved_at is not None \
                and now - self.warmup_saved_at < WARMUP_SAVE_SEC:
            return
        self.warmup_saved_at = now
        try:
            self.store.save_warmup(self.key, self.live_sec, now)
        except Exception as e:                               # noqa: BLE001
            logger.error('could not save the warm-up: %s', e)

    # -- the day ------------------------------------------------------------

    @staticmethod
    def _new_day(date):
        # `slip_*`: today's MEASURED slippage, per fill side, in ticks and
        # money. Unmeasured sides (paper, a missing decision price) are
        # counted apart and never averaged in as zero.
        return {'date': date, 'trades': 0, 'losses_row': 0, 'pnl': 0.0,
                'slip_sides': 0, 'slip_ticks': 0.0, 'slip_money': 0.0,
                'slip_unmeasured': 0}

    def settle_day(self, date, closed_net=None, closed=False, slips=()):
        """A new day clears the counts; a closed trade is scored. A net of
        None is unmeasured — not a loss, and not a win. `slips` are the
        trade's (ticks, money) at each end, None where unmeasured."""
        if date != self.day['date']:
            self.day = self._new_day(date)
        for ticks, cash in slips or ():
            if ticks is None:
                self.day['slip_unmeasured'] += 1
            else:
                self.day['slip_sides'] += 1
                self.day['slip_ticks'] += ticks
                self.day['slip_money'] += cash or 0.0
        if closed and closed_net is not None:
            self.day['pnl'] += closed_net
            self.day['losses_row'] = (self.day['losses_row'] + 1
                                      if closed_net < 0 else 0)

    def halt(self, open_net=None):
        """Which of the day's limits stops entries now, in words, or None."""
        p, day = self.params, self.day
        if p['max_trades_day'] and day['trades'] >= p['max_trades_day']:
            return (f"{day['trades']} Algo trades today — the day's limit "
                    f"is {p['max_trades_day']}")
        if p['max_losses_row'] and day['losses_row'] >= p['max_losses_row']:
            return (f"{day['losses_row']} losing Algo trades in a row — "
                    f"paused for the day")
        if p['daily_loss_limit']:
            total = day['pnl'] + (open_net or 0.0)
            if total <= -abs(p['daily_loss_limit']):
                return (f"Algo P&L today {total:,.2f} — past the "
                        f"-{abs(p['daily_loss_limit']):,.2f} limit")
        return None

    # -- every pass -----------------------------------------------------------

    def atr(self):
        closes = [self.candles.closed[b] for b in sorted(self.candles.closed)]
        return algofilters.atr(closes, self.params['atr_period'])

    def observe(self, now, md, positions, gates, cost_in, armed, live):
        """Feed this pass's market. Returns the body the window shows, with
        the intents that became true on this pass."""
        gates = dict(gates or {})
        if not self.seeded:
            self.seeded = True
            self.seed(now)
        self.last_now = now
        warmup = self._warmup(now, live, armed)
        gates['warmup'] = warmup
        for pid, sent_at in list(self.exit_sent.items()):
            if now - sent_at >= EXIT_RETRY_SEC:
                if any(p['position_id'] == pid for p in positions):
                    # Sent, and still open: say it again so it is retried.
                    self.signal.exit_failed(pid)
                del self.exit_sent[pid]
        if live and md and md.get('mid') is not None:
            # A price the jump guard is holding back is not fed to the band:
            # one bad print in a close moves sigma for N candles.
            closed = self.candles.observe(now, md['mid'])
            if closed is not None:
                self.history['candles'] += 1
        stats = self.candles.stats()
        filters, check = algo_module.judge_filters(
            self.params, md, stats, self.candles.closes(), cost_in)
        filters['ready'] = bool(filters['ready'] and warmup['done'])
        filters['warmup'] = warmup
        gates['entry_check'] = check
        body = self.signal.evaluate(now, md, stats, positions, gates)
        body['filters'] = filters
        body['atr'] = self.atr()
        body['atr_period'] = self.params['atr_period']
        if body.get('blocked_side'):
            self.last_blocked = {'side': body['blocked_side'],
                                 'z': body.get('blocked_z'), 'at': now,
                                 'reason': body.get('blocked')}
            self._journal_blocked(now)
        self.body = body
        return body

    def mode_changed(self, mode):
        """DRY RUN -> PAPER or LIVE with a signal on the screen: the signal
        still stands, and is now acted on — the entry edge opens again."""
        if self.last_mode is not None and mode != self.last_mode:
            self.signal._entry_live = None
        self.last_mode = mode

    def refused(self, clordid, text, now, was_entry):
        """The venue refused an order this Algo sent: its "Last order" says
        so in the venue's own words, an entry gives its trade back to the
        day's count, and the cooldown starts so the same refused order is
        not sent again on the next pass."""
        for row in self.recent:
            if clordid and row.get('clordid') == clordid:
                if row.get('done') and row['action'] == 'ENTER':
                    self.day['trades'] = max(0, self.day['trades'] - 1)
                row['done'] = False
                row['result'] = text
                break
        if was_entry:
            self.signal.entry_failed(now)

    def record(self, intent, now, mode, done=None, result=None,
               clordid=None):
        """What the Algo did, for "Last order": the intent, the mode it was
        in, and — when it was acted on — whether it went."""
        row = dict(intent, at=now, mode=mode, done=done, result=result,
                   clordid=clordid)
        self.recent.appendleft(row)
        if intent['action'] == 'ENTER' and (done or mode == 'DRY RUN'):
            self.day['trades'] += 1            # a dry run counts as one
        if intent['action'] == 'EXIT' and done:
            self.exit_sent[intent['position_id']] = now
        return row

    def _journal_blocked(self, now):
        last = self.last_blocked
        kind = re.sub(r'[-+]?\d[\d.,:]*', '#', last['reason'] or '')
        seen = self.blocked_journal
        if seen and seen[0] == last['side'] and seen[1] == kind \
                and now - seen[2] < BLOCKED_JOURNAL_SEC:
            return
        self.blocked_journal = (last['side'], kind, now)
        logger.info('[ALGO %s] %s signal at z %s held back: %s', self.key,
                    last['side'], last['z'], last['reason'])

    def block(self, mode):
        """What the window shows."""
        body = dict(self.body or {'state': 'STARTING',
                                  'params': dict(self.params)})
        body.pop('intents', None)
        body.update(mode=mode, day=dict(self.day),
                    history=dict(self.history),
                    last_blocked=(dict(self.last_blocked)
                                  if self.last_blocked else None),
                    recent=list(self.recent)[:5],
                    timeframe_min=self.params['timeframe_min'],
                    length=self.params['length'])
        return body
