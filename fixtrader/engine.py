"""The loop. One pass per contract, one snapshot out.

Everything the screen shows comes from `snapshot()`, and everything the screen
can ask for arrives as a command. The engine never talks to the browser and
the browser never talks to a venue.

Order of a pass, and it matters:

1. read the book, and let the guards observe it;
2. drain what the venue said since last time, and fold it into our own book
   FIRST — acting on a stale idea of the position is how a close becomes a
   reversal;
3. add the mid to the statistics, and record any standard-deviation touch;
4. manage working limits (re-price, escalate);
5. ask for an exit, then an entry. **Exits are considered first, always** —
   a pass that runs out of budget must have got the position out, not in.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import algo as algo_mod
from . import config as config_mod
from . import costs as costs_mod
from .algodesk import AlgoRun
from . import slippage as slippage_mod
from . import marketdata, signals as signals_mod, sizing
from .executor import Executor
from .marketdata import FeedGuard
from .models import (ContractState, ExitReason, Fill, Intent, OrderType,
                     Position, Side, TargetBasis, TouchState)
from .stats import StatsWindow

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: One contract's Algo, as its ladder's button names it (the MT5 desk's
#: switch): OFF — traded by hand; DRY — signals only, still by hand; TRADE —
#: the Algo trades it (PAPER or LIVE, by the desk's Execution) and a NEW hand
#: order on that contract is refused. See Engine.set_algo_state.
ALGO_STATES = ('OFF', 'DRY', 'TRADE')


class ContractRuntime:
    """Everything the engine holds for one contract."""

    def __init__(self, contract, desk: Dict[str, Any]):
        self.contract = contract
        s = contract.settings_with_defaults(desk)
        self.window = StatsWindow(
            contract.key, window_minutes=s['window_minutes'],
            min_history_minutes=s['min_history_minutes'],
            sample_interval_sec=s['sample_interval_sec'],
            stats_update_interval_sec=s['stats_update_interval_sec'],
            entry_threshold=s['entry_threshold'])
        #: The Algo: candles, signal, warm-up, the day. Built by the engine,
        #: which has the recording to seed it from.
        self.algo: Optional[AlgoRun] = None
        #: Counts NEW quotes, so "three in a row" means three prices and not
        #: three passes over the same one.
        self.quote_seq: int = 0
        self.last_quote = None
        #: The ATR when the open position went on: its ATR levels are frozen.
        self.entry_atr: Optional[float] = None
        self.guard = FeedGuard(contract.key,
                               max_quote_age_sec=desk.get('MAX_QUOTE_AGE_SEC', 15.0),
                               max_jump_sigma=desk.get('MAX_PRICE_JUMP_SIGMA', 5.0),
                               jump_settle_sec=desk.get('JUMP_SETTLE_SEC', 2.0))
        self.position: Optional[Position] = None
        self.book = None
        self.blocked_by: Optional[str] = None
        self.last_event: str = ""
        #: The most recent close on this contract, and how many there have
        #: been. The screen marks a close off the COUNT changing, not off
        #: the wording of an event line.
        self.closes: int = 0
        self.last_close = None
        #: A Close @ LMT resting on PAPER: filled here when the touch it
        #: would close at reaches the price. Nothing is sent.
        self.paper_close_limit: Optional[Dict[str, Any]] = None
        #: The day's open / high / low of the mid this system watched — the
        #: ladder's quote strip. FIX market data carries no session figures
        #: here, so they are OURS, and the strip says so.
        self.hlo: Optional[Dict[str, Any]] = None
        self.last_trade: Optional[float] = None
        #: SIGNALS mode: the trader's own position on this contract, watched
        #: by the Algo — its levels frozen when it was first seen.
        self.manual_watch: Optional[Dict[str, Any]] = None
        #: The last Signals-mode alert (an entry signal, or the watched
        #: position reaching an exit), with a sequence the screen keys on.
        self.signal_alert: Optional[Dict[str, Any]] = None
        self.last_trade_at: Optional[datetime] = None
        self.trades_today: int = 0
        self.pnl_today: float = 0.0
        self.day: Optional[str] = None
        #: Touches raised but not yet written; the engine drains these.
        self.pending_touches: List[Any] = []
        self.halted_reason: Optional[str] = None
        #: The price factor this contract's prices are read in (1 on Stellar),
        #: as last seen; and why entries wait when it is not known.
        #: A close that did NOT happen — refused by the venue, or a
        #: fill-or-cancel close that found nothing — while the position is
        #: still open. Said loudly on the ladder and the Trading Monitor
        #: until the position is flat or a later close fills.
        self.close_alert: Optional[Dict[str, Any]] = None
        self.units_seen: Optional[float] = None
        self.units_note: Optional[str] = None
        self.proposal: Optional[Dict[str, Any]] = None

    def roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.day != today:
            self.day = today
            self.trades_today = 0
            self.pnl_today = 0.0


    def note_mid(self, mid: float, now: datetime) -> None:
        day = now.date().isoformat()
        if self.hlo is None or self.hlo['day'] != day:
            self.hlo = {'day': day, 'open': mid, 'high': mid, 'low': mid}
            return
        self.hlo['high'] = max(self.hlo['high'], mid)
        self.hlo['low'] = min(self.hlo['low'], mid)


class Engine:
    """One engine per process. Owns the contracts, the venue and the book."""

    def __init__(self, config, gateway, db=None, notify: Optional[Callable] = None,
                 simulated: bool = False, mode_path: Optional[str] = None):
        self.config = config
        self.gateway = gateway
        self.db = db
        self.notify = notify or (lambda *a, **k: None)
        self.simulated = simulated
        self.executor = Executor(gateway, db=db, notify=notify,
                                 simulated=simulated)
        self.runtimes: Dict[str, ContractRuntime] = {}
        self.master_algo: bool = bool(config.settings.get('ALGO_MASTER_ENABLED', True))
        # A live venue starts in proposal mode until execution and account
        # recovery are available. Simulator behavior stays unchanged.
        self.auto_trade_enabled: bool = (
            bool(config.settings.get('AUTO_TRADE_ENABLED', True))
            if not hasattr(gateway, 'venue') else False)
        self.killed: bool = False
        #: PAPER: the gateway can see the market but cannot trade it — Stellar
        #: today. The signal still runs end to end, filled at the live bid
        #: or offer inside this process; no order is built for any venue.
        #: LIVE: the Algo's orders go to the venue. OFF after every restart
        #: and armed only by a person, confirmed (`set_execution`); until
        #: then a live venue's Algo trades on PAPER.
        self.live_armed: bool = False
        #: Armed LIVE with the venue's positions UNREAD, on the trader's
        #: explicit word that this book's own fills are the record.
        self.positions_waived: bool = False
        #: Our orders recorded as working when the engine last ran: cancelled
        #: once the session is up (the startup sweep, scoped to our ids).
        self._swept_previous: bool = False
        #: The Order Router logon the order status sweep was last run for.
        self._recovered_logon: str = ''
        self._paper_seq: int = 0
        #: WHO trades each contract: its Algo or a person, never both on the
        #: same contract. Two hands on one book fight — the algo closes a
        #: hand-placed position at its own target, or re-enters the moment
        #: the trader gets flat. Per contract, as on the MT5 desk: an Algo
        #: that TRADES a contract refuses NEW hand orders on it; Off and Dry
        #: run leave it to the hand. A switch is refused while the side being
        #: left still has something open there. Exits and cancels are never
        #: refused. The Dry-run choices are kept beside the status file.
        self.mode_path = mode_path
        self.dry_run: set = self._load_dry()
        terminal = getattr(gateway, 'terminal', None)
        if terminal is not None:
            terminal.mode_block = self._manual_block
            terminal.market_ticks = self._market_ticks
            terminal.spread_limit = self._spread_limit
        #: Asked for from the screen, after a change only a restart takes on.
        #: The runner stops cleanly and the launcher starts it again.
        self.restart_requested: bool = False
        #: When an edited configuration was last picked up, and anything in
        #: it that is still waiting for a restart. The screen shows both: a
        #: setting that looks saved but is not in force is worse than one
        #: that plainly says it needs the engine bounced.
        self.config_reloaded_at: Optional[datetime] = None
        self.config_restart_needed: List[str] = []
        self.loop_ms: float = 0.0
        self.started_at: Optional[datetime] = None
        #: Why a close was sent, kept until the fill that completes it, so the
        #: exit reason on the position is the one that actually fired rather
        #: than something inferred from prices afterwards.
        self._exit_reasons: Dict[str, ExitReason] = {}
        #: The book is INCOMPLETE until recovery has run. While it is, the
        #: reconciler closes nothing: a position is only an orphan if we are
        #: sure it is not ours.
        self.book_complete: bool = False
        self.unclaimed: List[Dict[str, Any]] = []

    @property
    def paper(self) -> bool:
        """Fill the Algo's orders here, at the live price, and send nothing:
        always on a venue that cannot take an order, and on a live venue
        until LIVE has been armed and confirmed."""
        if getattr(self.gateway, 'connection_only', False):
            return True
        return hasattr(self.gateway, 'venue') and not self.live_armed

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.gateway.start()
        for contract in self.config.enabled_contracts():
            self.add_contract(contract)
        self.recover()
        # Sweep our own working orders at STARTUP as well as at shutdown: a
        # crash leaves orders at the venue that this process no longer knows
        # about, and they are still ours.
        self.executor.cancel_all()
        self.started_at = utcnow()

    def stop(self) -> None:
        self.executor.cancel_all()
        self.gateway.stop()

    def add_contract(self, contract) -> ContractRuntime:
        rt = ContractRuntime(contract, self.config.settings)
        self.runtimes[contract.key] = rt
        self.gateway.subscribe(contract)
        self._fill_specs_from_venue(contract)
        self._resume_window(rt, utcnow())
        rt.algo = AlgoRun(contract.key,
                          algo_mod.params_from_settings(
                              self._settings(contract)),
                          store=self.db)
        return rt

    def _resume_window(self, rt: ContractRuntime, now: datetime) -> None:
        """Reload the recorded window after a restart — if it still reaches
        now.

        Resumed only when the engine was off for less than
        RESUME_MAX_GAP_MINUTES: a quick restart trades again at once, a
        longer gap starts afresh, so the bands never come from a market that
        has since moved on. The rows kept are the last `window_minutes`
        before the NEWEST recorded sample, not before now — anchoring on now
        would let the outage eat the oldest end of the window.
        """
        if self.db is None or not self.config.settings.get(
                'PERSIST_STATS_SAMPLES', True):
            return
        w = rt.window
        max_gap = float(self.config.settings.get('RESUME_MAX_GAP_MINUTES',
                                                 120.0) or 0.0)
        from datetime import timedelta
        since = now - timedelta(minutes=w.window_minutes + max_gap + 1)
        try:
            rows = self.db.samples_between(rt.contract.key, since=since)
        except Exception:                                # noqa: BLE001
            logger.exception("could not read the recorded window for %s",
                             rt.contract.key)
            return
        if not rows:
            return
        newest = rows[-1][0]
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        off = (now - newest).total_seconds() / 60.0
        if off > max_gap:
            logger.info("%s touch study started afresh — the recorded "
                        "window ended %.0f min ago (limit %.0f)",
                        rt.contract.key, off, max_gap)
            return
        start = newest - timedelta(minutes=w.window_minutes)
        kept = [(ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc), px)
                for ts, px, *_ in rows]
        kept = [(ts, px) for ts, px in kept if ts >= start]
        w.prime(kept)
        # The touch study's window, not the Algo's band: said in the log,
        # not on the Algo window, where it read as the band's history.
        logger.info("%s touch study resumed — %.0f min of history, %.0f min "
                    "since the last sample", rt.contract.key,
                    w.history_minutes, off)

    def _fill_specs_from_venue(self, contract) -> None:
        """Take tick size, value, multiplier and bounds from the venue where
        the operator has not overridden them, and record where each came
        from — a typed number and a venue number must be distinguishable."""
        spec = self.gateway.security_definition(contract)
        if spec is None:
            return
        for field in ('tick_size', 'tick_value', 'contract_multiplier',
                      'min_qty', 'qty_step', 'max_qty', 'currency'):
            value = getattr(spec, field, None)
            if value is None:
                continue
            if getattr(contract, field, None) in (None, ''):
                setattr(contract, field, value)
                contract.spec_source[field] = 'venue'
            elif contract.spec_source.get(field) != 'operator':
                contract.spec_source.setdefault(field, 'venue')

    def recover(self) -> None:
        """Rebuild the book from the database, then compare it to the venue.

        Until this has completed, `book_complete` is False and NOTHING is
        auto-closed. Anything at the venue that recovery cannot explain is
        listed as UNCLAIMED for a person to decide about — never closed.
        """
        if self.db is not None:
            for pos in self.db.open_positions():
                rt = self.runtimes.get(pos.contract_key)
                # Only where the book has none in memory: recovery runs again
                # when the venue's positions arrive mid-session, and swapping
                # the live position for a copy would strand every reference
                # the executor holds to it.
                if rt is not None and rt.position is None:
                    rt.position = pos

        venue_positions = self.gateway.positions()
        if venue_positions is None:
            # Could not read. That is NOT "the venue is flat" — leave the book
            # incomplete and say so rather than adopting an empty account.
            self.book_complete = False
            self.unclaimed = []
            return

        self.unclaimed = []
        for vp in venue_positions:
            rt = self.runtimes.get(vp.contract_key)
            ours = rt.position.signed_qty if (rt and rt.position) else 0.0
            if abs(vp.qty - ours) > 1e-9:
                self.unclaimed.append({
                    'contract_key': vp.contract_key,
                    'venue_qty': vp.qty, 'our_qty': ours,
                    'text': (f"the venue reports {vp.qty:+g} on "
                             f"{vp.contract_key}; this book explains "
                             f"{ours:+g}. Nothing has been closed."),
                })
        self.book_complete = True

    # -- one pass ----------------------------------------------------------

    def poll(self, now: Optional[datetime] = None) -> None:
        now = now or utcnow()
        started = now

        events = self.gateway.drain_events()
        for event in events:
            self._handle_event(event, now)
        router = getattr(self.gateway, 'algo', None)
        if router is not None and hasattr(router, 'take_tape'):
            tape = router.take_tape()
            if tape and self.db is not None:
                self.db.save_venue_fills(tape)
        self._venue_housekeeping()

        # The FIX receiver publishes normalized, coalesced updates. Consume
        # them on the engine thread so parsing and strategy work stay apart.
        market_events = (self.gateway.drain_market_data()
                         if hasattr(self.gateway, 'drain_market_data') else None)
        updated_keys = None if market_events is None else set()
        if market_events is not None:
            for event in market_events:
                updated_keys.add(event.contract_key)
                if event.last is not None and event.contract_key in self.runtimes:
                    # The last trade: shown on the ladder, never used as a price.
                    self.runtimes[event.contract_key].last_trade = event.last
                logger.debug('algo consumed quote %s seq=%s bid=%s ask=%s last=%s',
                             event.contract_key, event.sequence, event.bid,
                             event.ask, event.last)

        for key, rt in self.runtimes.items():
            try:
                self._poll_contract(rt, now, market_updated=(
                    updated_keys is None or key in updated_keys))
            except Exception:                    # one contract must never
                logger.exception("contract %s failed its pass", key)
        self.loop_ms = (utcnow() - started).total_seconds() * 1000.0

    def _poll_contract(self, rt: ContractRuntime, now: datetime,
                       market_updated: bool = True) -> None:
        contract = rt.contract
        settings = self._settings(contract)
        rt.roll_day(now)
        rt.proposal = None

        book = self.gateway.top_of_book(contract.key)
        rt.book = book
        rt.guard.observe(book, now)

        if rt.position is None or not rt.position.is_open:
            rt.close_alert = None                # flat: nothing left to close
        record = self._price_units(rt, now)
        armed = bool(contract.algo_on and self.master_algo and not self.killed)
        if record and market_updated and book is not None and book.usable:
            before = rt.window.samples_ts[-1][0] if rt.window.samples_ts else None
            touches = rt.window.add(book.mid, now, algo_armed=armed)
            if touches and self.db is not None:
                for touch in touches:
                    self.db.save_touch(touch)
            sampled = (rt.window.samples_ts
                       and rt.window.samples_ts[-1][0] != before)
            if sampled:
                if self.db is not None and self.config.settings.get(
                        'PERSIST_STATS_SAMPLES', True):
                    self.db.save_samples(contract.key, [(now, book.mid)])

        if book is not None and book.mid is not None:
            rt.note_mid(book.mid, now)
        self._paper_close_limit(rt, book, now)
        self.executor.note(contract, book)
        said = (self.executor.manage(contract, settings, book, now)
                if self.auto_trade_enabled else [])
        for line in said:
            self._say(rt, "ORDER", line)

        self._run_algo(rt, contract, settings, book, now, armed,
                       market_updated)

    def _price_units(self, rt: ContractRuntime, now: datetime) -> bool:
        """Keep this contract's recorded prices in the units the screen
        shows. Stellar's prices ARE trader prices (a factor of 1 for every
        instrument it knows), so on Stellar this records the factor once and
        never rescales; the mechanism is kept so that a recording made in
        other units is rescaled ONCE, the factor recorded beside it, and the
        band, the touch study and the day's strip rebuilt from it — never
        mixed with the new.

        Returns False while the terminal does not know the instrument at
        all: nothing is recorded then, and entries wait (`units_note`)."""
        terminal = getattr(self.gateway, 'terminal', None)
        if terminal is None or not hasattr(terminal, 'price_factor'):
            rt.units_note = None                     # the simulator: one unit
            return True
        key = rt.contract.key
        factor = terminal.price_factor(str(getattr(rt.contract, 'security_id',
                                                   '') or ''))
        if factor is None:
            rt.units_note = ("this contract is not known to the Market Data "
                             "session yet — nothing is recorded and entries wait")
            rt.units_seen = None
            return False
        factor = float(factor)
        rt.units_note = None
        if rt.units_seen == factor:
            return True
        stored = self.db.price_unit(key) if self.db is not None else None
        ratio = factor / (stored if stored else 1.0)
        if self.db is not None and (stored is None or abs(ratio - 1.0) > 1e-12):
            n = self.db.rescale_prices(key, ratio, factor)
            if abs(ratio - 1.0) > 1e-12:
                logger.warning("%s recorded prices rescaled x%g to the "
                               "venue's price factor %g (%d samples)", key, ratio,
                               factor, n)
                self._say(rt, "UNITS", f"recorded prices brought to trader "
                          f"units (x{ratio:g}, price factor {factor:g})")
        pos = rt.position
        if pos is not None and abs(ratio - 1.0) > 1e-12:
            # Opened on FIX-unit fills: its prices and frozen levels too.
            for field in ('avg_price', 'break_even', 'target_price',
                          'stop_price', 'entry_mean', 'entry_std'):
                if getattr(pos, field, None) is not None:
                    setattr(pos, field, getattr(pos, field) * ratio)
            if rt.entry_atr is not None:
                rt.entry_atr *= ratio
            if self.db is not None:
                self.db.save_position(pos)
        # Rebuilt from the recording, now in one unit throughout — the
        # band, the touch study and the day's strip.
        rt.window = ContractRuntime(rt.contract, self.config.settings).window
        self._resume_window(rt, now)
        rt.algo = AlgoRun(key, algo_mod.params_from_settings(
            self._settings(rt.contract)), store=self.db)
        rt.hlo = None
        rt.manual_watch = None
        rt.units_seen = factor
        return True

    #: The Algo's exit reasons, as the journal records them.
    _EXIT_REASONS = {'STOP_LOSS': ExitReason.STOP_LOSS,
                     'PROFIT_TARGET': ExitReason.TARGET,
                     'Z_STOP': ExitReason.ZSCORE,
                     'MEAN_REVERSION': ExitReason.MEAN,
                     'TIME_STOP': ExitReason.TIME_STOP}

    def algo_mode(self, key: Optional[str] = None) -> str:
        """What an Algo intent does now: LIVE sends it, PAPER fills it at
        the live price inside this process, DRY RUN only shows it. A
        contract in a dry run only shows; an Algo that is OFF keeps the
        desk's mode so a position it still holds has its exits acted on."""
        if not self.auto_trade_enabled:
            return 'DRY RUN'
        rt = self.runtimes.get(key) if key is not None else None
        if rt is not None and rt.contract.algo_on and key in self.dry_run:
            return 'DRY RUN'
        return 'PAPER' if self.paper else 'LIVE'

    def _algo_costs(self, contract, settings, qty):
        """k, the fees for the round turn, the slippage budget, and the
        fees + slippage in points — what break-even is moved by."""
        k = (contract.tick_value / contract.tick_size
             if contract.tick_value and contract.tick_size else None)
        breakdown = costs_mod.cost_breakdown(qty, contract.tick_size,
                                             contract.tick_value, settings)
        fees = sum(breakdown[x] or 0.0 for x in
                   ('commission', 'exchange', 'clearing'))
        return {'k': k, 'commission': fees,
                'slippage': breakdown['slippage_budget'],
                'fee_points': breakdown['round_trip_points']}

    def _manual_position(self, rt, contract, settings, book, now, p):
        """SIGNALS mode: the trader's own position on this contract, as the
        Algo reads a position — so it is given the SAME break-even, target
        and stop (the per-direction ones included), frozen when first seen,
        and its exits are SIGNALLED. Nothing here closes it: the trader does.
        None when the hand holds nothing here."""
        terminal = getattr(self.gateway, 'terminal', None)
        sid = str(getattr(contract, 'security_id', '') or '')
        if terminal is None or not sid:
            rt.manual_watch = None
            return None
        with terminal.lock:
            rows = [r for r in terminal._pnl_snapshot([])['positions']
                    if str(r.get('security_id')) == sid]
            opened = [terminal.orders.get(r['entry_order_id'], {}).get('updated')
                      for r in rows]
        net = sum((1 if r['side'] == 'BUY' else -1) * float(r['quantity'])
                  for r in rows)
        if not rows or not net:
            rt.manual_watch = None
            return None
        side = Side.BUY if net > 0 else Side.SELL
        same = [r for r in rows if r['side'] == side.value]
        qty = abs(net)
        held = sum(float(r['quantity']) for r in same)
        entry = (sum(float(r['entry_price']) * float(r['quantity']) for r in same)
                 / held) if held else None
        sig = (side.value, tuple(sorted(r['entry_order_id'] for r in rows)), qty)
        watch = rt.manual_watch
        if watch is None or watch['sig'] != sig:
            money = self._algo_costs(contract, settings, qty)
            atr = rt.algo.atr() if rt.algo is not None else None
            be, tp, sl, why = algo_mod.levels(
                side.value, entry, money['fee_points'], p,
                (money['k'] * qty) if money['k'] else None,
                self._margin(contract, settings, qty), atr)
            first = min((o for o in opened if o), default=None)
            try:
                opened_at = datetime.fromisoformat(first) if first else now
            except ValueError:
                opened_at = now
            watch = rt.manual_watch = {'sig': sig, 'break_even': be, 'tp': tp,
                                       'sl': sl, 'why': why, 'atr': atr,
                                       'opened_at': opened_at}
        close_px = book.executable(side.opposite) if book else None

        def worth(level):
            return costs_mod.open_net(side, qty, entry, level,
                                      contract.tick_size, contract.tick_value,
                                      settings)
        return {
            'position_id': 'manual', 'manual': True,
            'side': side.value, 'entry': entry,
            'opened_at': watch['opened_at'].timestamp(),
            'age_sec': (now - watch['opened_at']).total_seconds(),
            'break_even': watch['break_even'], 'tp': watch['tp'],
            'sl': watch['sl'], 'quantity': qty, 'entry_z': None,
            'entry_atr': watch['atr'], 'paper': False,
            'net_pnl': worth(close_px), 'tp_money': worth(watch['tp']),
            'sl_money': worth(watch['sl']), 'levels_why': watch['why'],
            'tickets': [r['entry_order_id'] for r in rows],
        }

    def _signal_alert(self, rt, kind: str, text: str, now) -> None:
        """Something a trader in SIGNALS mode should act on, said once."""
        seq = (rt.signal_alert or {}).get('seq', 0) + 1
        rt.signal_alert = {'seq': seq, 'kind': kind, 'text': text,
                           'at': now.isoformat()}
        self._say(rt, "SIGNAL", text)

    def _algo_position(self, rt, contract, settings, book, now):
        """The open position as the Algo reads it, or None."""
        pos = rt.position
        if pos is None or not pos.is_open:
            return None
        close_px = book.executable(pos.side.opposite) if book else None

        def worth(level):
            return costs_mod.open_net(pos.side, pos.qty, pos.avg_price,
                                      level, contract.tick_size,
                                      contract.tick_value, settings)
        return {
            'position_id': pos.id if pos.id is not None else 'open',
            'side': pos.side.value, 'entry': pos.avg_price,
            'opened_at': pos.opened_at.timestamp() if pos.opened_at else None,
            'age_sec': ((now - pos.opened_at).total_seconds()
                        if pos.opened_at else None),
            'break_even': pos.break_even, 'tp': pos.target_price,
            'sl': pos.stop_price, 'quantity': pos.qty,
            'entry_z': pos.entry_z, 'entry_atr': rt.entry_atr,
            'entry_slip_ticks': (sizing.to_ticks(pos.entry_slippage,
                                                 contract.tick_size)
                                 if pos.entry_slippage is not None else None),
            # PAPER is a fill made in this process at the live price; the
            # simulator's fills are simulated, which is a different thing.
            'paper': any(str(t).startswith('PAPER-') for t in pos.tickets),
            'net_pnl': worth(close_px),
            'tp_money': worth(pos.target_price),
            'sl_money': worth(pos.stop_price),
        }

    def _run_algo(self, rt, contract, settings, book, now, armed,
                  market_updated) -> None:
        """One pass of the Algo: feed it, read what it decided, act on it.

        Exits are acted on whatever the mode, the master switch or the kill
        switch say — a guard may withhold an order, never a close. Entries
        only with the Algo armed, in ALGO mode, and Auto trade on.
        """
        algo = rt.algo
        algo.mode_changed(self.algo_mode(contract.key))
        p = algo_mod.params_from_settings(settings)
        if p != algo.params:
            algo.reshape(p, now.timestamp())
        p = algo.params
        status = rt.guard.status(now)
        usable = book is not None and book.usable and book.mid is not None
        # A NEW quote, not the same one read again: "three in a row" means
        # three prices, and the loop reads the book ten times a second.
        quote = ((book.bid, book.ask, book.bid_size, book.ask_size, book.ts)
                 if usable else None)
        if market_updated and usable and quote != rt.last_quote:
            rt.quote_seq += 1
        rt.last_quote = quote
        md = ({'bid': book.bid, 'ask': book.ask, 'mid': book.mid,
               'quote_id': rt.quote_seq} if usable else None)
        health = None
        if rt.units_note:
            health = rt.units_note
        elif self.recovery_running():
            health = ('Stellar is answering for the orders of ours that were '
                      'working — entries wait until it finishes')
        elif self._too_wide(contract, settings, book):
            health = self._too_wide(contract, settings, book)
        elif status['stale']:
            health = (f"price unchanged {status['age_sec']:.0f}s — entries "
                      f"wait for a move")
        elif status['settling']:
            health = 'a price jump is settling — entries wait'
        live = md is not None and health is None

        qty = p['algo_qty']
        money = self._algo_costs(contract, settings, qty)
        margin = self._margin(contract, settings, qty)
        atr = algo.atr()
        positions = []
        open_pos = self._algo_position(rt, contract, settings, book, now)
        if open_pos is not None:
            positions.append(open_pos)
        signals_mode = self.algo_state(contract.key) == 'DRY'
        watched = None
        if open_pos is None and signals_mode:
            # The trader trades, the Algo watches: their position gets the
            # Algo's levels and exit SIGNALS — never an order.
            watched = self._manual_position(rt, contract, settings, book,
                                            now, p)
            if watched is not None:
                positions.append(watched)
        else:
            rt.manual_watch = None
        rt.roll_day(now)
        algo.settle_day(now.date().isoformat())

        mode_gate = None
        manual_held = self.manual_business(contract.key)
        if manual_held:
            mode_gate = ('a hand is holding this contract — ' +
                         '; '.join(manual_held))
        elif self.killed:
            mode_gate = 'KILL ALL is on'
        elif not self.master_algo:
            mode_gate = 'the master switch is off'
        elif not contract.algo_on:
            mode_gate = 'ALGO is off on this contract'
        cutoff = None
        if contract.session_close:
            try:
                hh, mm = (int(x) for x in contract.session_close.split(':'))
                cutoff = (hh * 60 + mm) - (now.hour * 60 + now.minute)
            except ValueError:
                cutoff = None
        why_levels = algo_mod.levels_gate(
            p, book.mid if usable else None, money['fee_points'],
            (money['k'] or 0) * qty, margin, atr,
            book.width if usable else None)
        gates = {
            'health': health, 'mode': mode_gate,
            'halt': algo.halt(open_pos['net_pnl'] if open_pos else None),
            'session': (None if self._in_session(contract, settings, now)
                        else 'outside the contract\'s session'),
            'cutoff_min': cutoff, 'levels': why_levels,
        }
        body = algo.observe(now.timestamp(), md, positions, gates,
                            {'k': money['k'], 'commission': money['commission'],
                             'slippage': money['slippage']},
                            armed=armed,
                            live=live)
        # The slippage BUDGET, so the window can put today's measured figure
        # beside it — a budget is corrected from data, or not at all.
        body['slip_budget_ticks'] = float(
            settings.get('slippage_budget_ticks', 0) or 0)
        rt.blocked_by = body.get('blocked') if body.get('state') == 'BLOCKED' \
            else None

        # The session's own flat time closes whatever is open. Never gated.
        if open_pos is not None and self._session_flat_due(contract,
                                                           settings, now):
            if self.auto_trade_enabled and not self._has_working_close(
                    contract.key):
                self._close(rt, settings, rt.position.side.opposite,
                            rt.position.qty, ExitReason.SESSION_FLAT,
                            'session flat time', book, now)
            return

        mode = self.algo_mode(contract.key)
        for intent in body['intents']:
            if intent['action'] == 'EXIT':
                reason = self._EXIT_REASONS.get(intent['reason'],
                                                ExitReason.TARGET)
                words = algo_mod.EXIT_WORDS.get(intent['reason'],
                                                intent['reason'])
                rt.proposal = {'action': 'CLOSE', 'side': (
                    'SELL' if intent['side'] == 'BUY' else 'BUY'),
                    'qty': rt.position.qty if rt.position else None,
                    'reason': words, 'ts': now.isoformat()}
                if mode == 'DRY RUN' or rt.position is None:
                    algo.record(intent, now.timestamp(), mode)
                    if signals_mode and watched is not None:
                        net = intent.get('net_pnl')
                        self._signal_alert(rt, 'EXIT', (
                            f"{words} — close your {watched['side']} "
                            f"{watched['quantity']:g} at "
                            f"{intent.get('price') if intent.get('price') is not None else '—'}"
                            + (f" (net {net:+.2f})" if net is not None else '')),
                            now)
                    continue
                if self._has_working_close(contract.key):
                    continue
                self._close(rt, settings, rt.position.side.opposite,
                            rt.position.qty, reason, words, book, now)
                done = (rt.position is None if self.paper else
                        self._has_working_close(contract.key))
                closing = [w.clordid for w in
                           self.executor.working_for(contract.key)
                           if w.is_close]
                algo.record(intent, now.timestamp(), mode, done=done,
                            result=None if done else rt.last_event,
                            clordid=closing[0] if closing else None)
                continue
            # ENTER
            side = Side(intent['side'])
            rt.proposal = {'action': 'OPEN', 'side': side.value, 'qty': qty,
                           'reason': f"{'H to L' if side is Side.SELL else 'L to H'}"
                                     f" z {intent['z']:+.2f}",
                           'ts': now.isoformat()}
            if mode == 'DRY RUN':
                algo.record(intent, now.timestamp(), mode)
                if signals_mode:
                    self._signal_alert(rt, 'ENTRY', (
                        f"{'H to L' if side is Side.SELL else 'L to H'} — "
                        f"{side.value} {qty:g} @ {intent.get('price')} "
                        f"(z {intent['z']:+.2f})"), now)
                continue
            if self.executor.working_for(contract.key):
                continue
            decision = {'z': intent.get('z'), 'mean': body.get('mean'),
                        'std': body.get('sigma'),
                        # The price the Algo decided at — the anchor the
                        # entry's slippage is measured against.
                        'price': intent.get('price'),
                        'order_type': ('PAPER' if self.paper else str(
                            settings.get('entry_order_type') or 'MARKET')),
                        'half_life': (body['filters'].get('half_life_minutes')
                                      or 0) * 60.0 or None}
            rt.entry_atr = atr
            sent_id = None
            if self.paper:
                done = self._paper_fill(rt, side, qty, Intent.OPEN, book,
                                        now, decision=decision)
            else:
                wo = self.executor.place(contract, settings, side, qty,
                                         Intent.OPEN, book, now,
                                         reason=rt.proposal['reason'],
                                         decision=decision)
                done = wo is not None
                sent_id = wo.clordid if wo is not None else None
                if done:
                    self._say(rt, "ORDER",
                              f"{side.value} {qty:g} — {rt.proposal['reason']}")
            algo.record(intent, now.timestamp(), mode, done=done,
                        result=None if done else rt.last_event,
                        clordid=sent_id)
            if not done:
                algo.signal.entry_failed(now.timestamp())
            else:
                self._mark_touch_traded(rt, side)

    @staticmethod
    def _proposal(sig, now):
        return {'action': sig.action, 'side': sig.side.value,
                'qty': sig.qty, 'reason': sig.reason,
                'ts': now.isoformat()}

    @staticmethod
    def _decision(window, book=None, side: Optional[Side] = None) -> Dict[str, Any]:
        """The window's state at the moment a signal fires.

        Stamped onto the position when the fill lands. Reading these off the
        window at FILL time instead recorded whatever the market had moved to
        by then — an entry logged at z -0.67 for a trade taken at -2.24 — and
        every figure the Analysis window reports about why a trade happened
        would be the wrong one.
        """
        z = window.z
        if book is not None and side is not None:
            # The z the rule actually fired on: the bid's for a sale, the
            # offer's for a purchase. Not the mid's.
            side_z = window.z_of(book.executable(side))
            z = side_z if side_z is not None else z
        return {'z': z, 'mean': window.mean, 'std': window.std,
                'half_life': window.half_life}

    def _mark_touch_traded(self, rt: ContractRuntime, side: Side) -> None:
        """Record that THIS touch is the one an entry acted on.

        The touch study's "traded" column is what says whether the level the
        algo fires at is the level that actually reverts — a level that
        reverts 86% of the time and is never traded is a threshold set wrong.
        The mark has to be made when the order goes, because by the time the
        fill lands the z may have crossed into another band entirely.
        """
        touch = getattr(rt.window, 'last_touch', None)
        if touch is None:
            return
        # A SELL is taken at a high z, a BUY at a low one. A touch of the
        # opposite sign is not the one this entry acted on.
        wanted_positive = side is Side.SELL
        if (touch.level > 0) != wanted_positive:
            return
        if touch.became_trade:
            return
        touch.became_trade = True
        if self.db is not None:
            self.db.save_touch(touch)

    def _exit_decision(self, rt, settings, book, side: Side,
                       order_type: Optional[str] = None) -> Dict[str, Any]:
        """What a close was decided on: the price it could be had at then —
        the side it closes on — and the Algo's z of that price. The anchor
        its slippage is measured against."""
        price = book.executable(side) if book is not None else None
        body = (rt.algo.body or {}) if rt.algo is not None else {}
        return {'z': algo_mod.zscore(price, body.get('mean'), body.get('sigma')),
                'mean': body.get('mean'), 'std': body.get('sigma'),
                'price': price,
                'order_type': ('PAPER' if self.paper else
                               order_type or str(settings.get(
                                   'exit_order_type') or 'MARKET'))}

    def _has_working_close(self, key: str) -> bool:
        return any(w.is_close for w in self.executor.working_for(key))

    def _close(self, rt, settings, side, qty, exit_reason, reason,
               book, now) -> None:
        """The one way out: on paper, a fill at the closing side now; at a
        venue, an order that says it is closing."""
        if self.paper:
            self._paper_fill(rt, side, qty, Intent.CLOSE, book, now,
                             decision=self._exit_decision(rt, settings, book,
                                                          side),
                             exit_reason=exit_reason or ExitReason.TARGET,
                             reason=reason)
            return
        self._send_close(rt, settings, side, qty, exit_reason, reason,
                         book, now)

    def _paper_fill(self, rt, side: Side, qty: float, intent: Intent, book,
                    now: datetime, decision: Optional[Dict[str, Any]] = None,
                    exit_reason: Optional[ExitReason] = None,
                    reason: str = "") -> bool:
        """Fill on paper at the LIVE executable side — the bid for a sale,
        the offer for a purchase, never the mid. Nothing is built for a
        venue and nothing leaves the process: this is the signal measured on
        a real market before the order path to it exists.

        Applied through the SAME code that applies a venue fill, so a paper
        position carries the same break-even, target and journal as a real
        one would. There is no queue and no partial fill, and the round trip
        charged is the configured one — the Analysis window says so.
        """
        price = book.executable(side) if book is not None else None
        if price is None:
            self._say(rt, "PAPER", f"no {('offer' if side is Side.BUY else 'bid')}"
                      f" to paper-fill {side.value} {qty:g} at — waiting")
            return False
        self._paper_seq += 1
        clordid = f"PAPER-{self._paper_seq}"
        fill = Fill(venue='PAPER', exec_id=clordid, clordid=clordid,
                    contract_key=rt.contract.key, side=side, qty=qty,
                    price=price, our_ts=now, venue_ts=now)
        self.executor.decisions[clordid] = dict(decision or {})
        if exit_reason is not None:
            self._exit_reasons[clordid] = exit_reason
        if self.db is not None and hasattr(self.db, 'save_fill'):
            try:
                self.db.save_fill(fill)
            except Exception:                            # noqa: BLE001
                logger.exception("paper fill not journalled")
        from types import SimpleNamespace
        self._apply_fill(rt, SimpleNamespace(fill=fill, clordid=clordid),
                         intent, now, simulated=True)
        if reason and intent is Intent.CLOSE:
            rt.last_event = f"{rt.last_event} · {reason}"
        return True

    def _send_close(self, rt, settings, side, qty, exit_reason, reason,
                    book, now) -> None:
        wo = self.executor.place(
            rt.contract, settings, side, qty, Intent.CLOSE, book, now,
            reason=reason, open_qty=rt.position.qty,
            position_id=rt.position.id,
            decision=self._exit_decision(rt, settings, book, side),
            # The position itself, so the order carries an explicit close
            # flag and the venue tickets it is closing — never a bare
            # opposite order, which opens the other side instead.
            position=rt.position)
        if wo is not None:
            self._exit_reasons[wo.clordid] = exit_reason or ExitReason.TARGET
            self._say(rt, "ORDER", f"closing {qty:g} — {reason}")

    # -- venue events ------------------------------------------------------

    def _handle_event(self, event, now: datetime) -> None:
        rt = self.runtimes.get(event.contract_key)
        contract = rt.contract if rt else None
        intent = self.executor.intent_of(event.clordid)
        # A fill already in the book — resent by Stellar after a logon, or
        # resent — updates the order and is NEVER applied to a position again.
        fill = getattr(event, 'fill', None)
        booked = bool(fill is not None and self.db is not None
                      and hasattr(self.db, 'has_fill')
                      and self.db.has_fill(fill.venue, fill.exec_id))
        wo_before = self.executor.working.get(event.clordid)
        marketable = bool(getattr(wo_before, 'marketable', False))
        self.executor.apply_event(
            event, contract.tick_size if contract else None, now)
        if booked:
            logger.info("fill %s on %s already booked — not applied again",
                        fill.exec_id, event.clordid)
            return
        if rt is not None and intent is Intent.CLOSE:
            self._close_outcome(rt, event, marketable, now)

        if rt is None:
            return
        if event.kind == "REJECTED":
            # The venue's own words, verbatim. Never "check the log".
            self._say(rt, "REJECT", event.text or "rejected")
            self.notify("REJECT", rt.contract.key, event.text or "rejected")
            if rt.algo is not None:
                rt.algo.refused(event.clordid, event.text or "rejected",
                                now.timestamp(), intent is Intent.OPEN)
            return
        if event.kind in ("FILL", "PARTIAL") and event.fill is not None:
            self._apply_fill(rt, event, intent, now)
        elif event.kind == "TRADE_CHANGE":
            # Stellar busted or corrected an earlier fill: never booked as a new
            # one, and never quiet.
            self._say(rt, "REJECT", event.text)
            self.notify("REJECT", rt.contract.key, event.text)
        elif event.kind in ("CANCELLED", "EXPIRED"):
            self._say(rt, "ORDER", event.kind.lower())
        elif event.kind == "CANCEL_REJECTED":
            # The venue's own words. An order adopted from a previous run
            # that the venue no longer knows is let go — it cannot be
            # managed, and holding it would block the contract for ever.
            self._say(rt, "REJECT", f"cancel/replace refused: {event.text}")
            if self.executor.is_adopted(event.clordid):
                self.executor.release(event.clordid)

    def _close_outcome(self, rt: ContractRuntime, event, marketable: bool,
                       now: datetime) -> None:
        """A close that did not happen is said loudly, in the venue's words —
        never left as one line in an orders table while the money sits there."""
        pos = rt.position
        vo = event.order
        filled = float(getattr(vo, 'filled_qty', 0) or 0) if vo is not None else 0.0
        if event.kind in ('FILL', 'PARTIAL'):
            rt.close_alert = None
            return
        if pos is None or not pos.is_open:
            return
        held = (f"still {'long' if pos.side is Side.BUY else 'short'} "
                f"{pos.qty:g} {rt.contract.name}")
        if event.kind == 'REJECTED':
            why = event.text or 'no reason given'
            text = f"Your close was REJECTED — {held}. Stellar: {why}"
        elif event.kind in ('CANCELLED', 'EXPIRED') and marketable and filled <= 0:
            text = (f"Your close did not fill — the price moved before it got "
                    f"there; {held}. Press CLOSE ALL again, or Close @ LMT.")
        else:
            return
        seq = ((rt.close_alert or {}).get('seq') or 0) + 1
        rt.close_alert = {'seq': seq, 'text': text, 'at': now.isoformat(),
                          'clordid': event.clordid}
        if event.kind != 'REJECTED':          # a reject is said by the caller
            self._say(rt, "REJECT", text)
            self.notify("REJECT", rt.contract.key, text)

    def _apply_fill(self, rt: ContractRuntime, event, intent: Intent,
                    now: datetime, simulated: Optional[bool] = None) -> None:
        fill = event.fill
        contract = rt.contract
        settings = self.config.effective(contract.key)

        # The state the DECISION was made on, not the state now.
        decided = self.executor.decision_of(event.clordid)

        # A fill on the OPPOSITE side of an open position can only reduce
        # it. Whatever the order was recorded as, it is never added to the
        # position's size and never opens the other side: that is how an
        # exit becomes a second position, long AND short at once.
        if (intent is Intent.OPEN and rt.position is not None
                and rt.position.is_open and fill.side is not rt.position.side):
            logger.warning("%s: %s fill %s against an open %s was recorded "
                           "as an OPEN — applied as a CLOSE", contract.key,
                           fill.side.value, event.clordid,
                           rt.position.side.value)
            intent = Intent.CLOSE
        # And a "close" on the SAME side as the position is not a close: it
        # would grow the position. It is reported, not applied.
        if (intent is Intent.CLOSE and rt.position is not None
                and rt.position.is_open and fill.side is rt.position.side):
            self._say(rt, "REJECT", f"a {fill.side.value} fill recorded as a "
                      f"close of a {rt.position.side.value} position — not "
                      f"applied; check the venue")
            return

        if intent is Intent.OPEN:
            if rt.position is None or not rt.position.is_open:
                margin = self._margin(contract, settings, fill.qty)
                rt.position = Position(
                    contract_key=contract.key, side=fill.side, qty=fill.qty,
                    opened_qty=fill.qty, avg_price=fill.price, opened_at=now,
                    entry_z=decided.get('z', rt.window.z),
                    entry_mean=decided.get('mean', rt.window.mean),
                    entry_std=decided.get('std', rt.window.std),
                    entry_half_life=decided.get('half_life',
                                                rt.window.half_life),
                    margin_locked=margin,
                    is_simulated=(self.simulated if simulated is None
                                  else bool(simulated)),
                    tickets=[fill.exec_id],
                    opened_session=now.date().isoformat())
            else:
                pos = rt.position
                total = pos.qty + fill.qty
                pos.avg_price = ((pos.avg_price * pos.qty)
                                 + fill.price * fill.qty) / total
                pos.qty = total
                pos.opened_qty = total
                pos.tickets.append(fill.exec_id)
                pos.margin_locked = self._margin(contract, settings, total)

            pos = rt.position
            # Slippage against the price the decision was made at. A PAPER
            # fill is made AT that price: nothing to measure, so None — a
            # perfect 0.00 would be a figure nobody measured.
            pos.entry_order_type = decided.get('order_type') or pos.entry_order_type
            pos.entry_slippage = (None if self._is_paper(pos) else
                                  slippage_mod.slip(pos.side.value,
                                                    decided.get('price'),
                                                    pos.avg_price))
            # The Algo's levels, from break-even: the target and the stop a
            # % of the margin, or a multiple of the ATR frozen at entry.
            params = (rt.algo.params if rt.algo is not None else
                      algo_mod.params_from_settings(settings))
            money = self._algo_costs(contract, settings, pos.qty)
            k = money['k']
            if rt.entry_atr is None and rt.algo is not None:
                rt.entry_atr = rt.algo.atr()
            be, tp, sl, _ = algo_mod.levels(
                pos.side.value, pos.avg_price, money['fee_points'], params,
                (k * pos.qty) if k else None, pos.margin_locked,
                rt.entry_atr)
            pos.break_even, pos.target_price, pos.stop_price = be, tp, sl
            if self.db is not None:
                self.db.save_position(pos)
            rt.last_trade_at = now
            self._say(rt, "OPEN",
                      f"{pos.side.value} {fill.qty:g} @ {fill.price:g}")
            self.notify("OPEN", contract.key,
                        f"{pos.side.value} {fill.qty:g} @ {fill.price:g}")
            return

        # a close
        pos = rt.position
        if pos is None or not pos.is_open:
            # Nothing open to close: the fill is reported, and NEVER becomes
            # a position on the other side.
            self._say(rt, "REJECT", f"close fill {fill.side.value} "
                      f"{fill.qty:g} @ {fill.price:g} with nothing open — "
                      f"not applied; check the venue")
            return
        if fill.qty > pos.qty + 1e-9:
            # More than was open. The position closes; the excess is said,
            # never booked as a position the other way.
            self._say(rt, "REJECT", f"close fill {fill.qty:g} is more than "
                      f"the {pos.qty:g} open — the excess is not booked; "
                      f"check the venue")
        closing_qty = min(fill.qty, pos.qty)
        pos.qty = max(0.0, round(pos.qty - fill.qty, 10))
        pos.tickets.append(fill.exec_id)
        if pos.qty > 1e-9:
            if self.db is not None:
                self.db.save_position(pos)
            return

        pos.closed_at = now
        pos.exit_price = fill.price
        pos.exit_z = decided.get('z', rt.window.z)
        pos.exit_order_type = decided.get('order_type')
        # The CLOSING order's side paid it: a long sells to close.
        pos.exit_slippage = (None if self._is_paper(pos) else
                             slippage_mod.slip(pos.side.opposite.value,
                                               decided.get('price'),
                                               fill.price))
        pos.exit_reason = self._exit_reasons.pop(event.clordid,
                                                 ExitReason.TARGET)
        filled = pos.tickets
        result = costs_mod.net_pnl(pos.side, closing_qty, pos.avg_price,
                                   fill.price, contract.tick_size,
                                   contract.tick_value,
                                   fees_paid=self._fees_for(filled, settings,
                                                            fill.qty))
        pos.gross_pnl = result['gross']
        pos.fees_paid = result['fees']
        pos.net_pnl = result['net']
        if pos.net_pnl is not None and pos.margin_locked:
            pos.pnl_pct_on_margin = 100.0 * pos.net_pnl / pos.margin_locked
        if self.db is not None:
            self.db.save_position(pos)
        rt.trades_today += 1
        rt.last_trade_at = now
        if pos.net_pnl is not None:
            rt.pnl_today += pos.net_pnl
        money = f"{pos.net_pnl:+,.0f}" if pos.net_pnl is not None else "—"
        # Published so the screen can mark the close WITHOUT reading a
        # sentence: a highlight driven by a regex over `last_event` turns a
        # reworded message into a window that quietly stops reporting. `net`
        # may be None, and None is not a loss — it is unmeasured, and the
        # screen colours it neither way.
        rt.closes += 1
        rt.last_close = {
            'seq': rt.closes,
            'net': pos.net_pnl,
            'side': pos.side.value,
            'qty': closing_qty,
            'price': fill.price,
            'reason': pos.exit_reason.value if pos.exit_reason else None,
            'ts': now.isoformat(),
        }
        self._say(rt, "CLOSED",
                  f"{pos.side.value} {fill.qty:g} out at {fill.price:g} · {money}")
        self.notify("CLOSED", contract.key,
                    f"closed {fill.qty:g} @ {fill.price:g} · {money}")
        rt.position = None
        rt.entry_atr = None
        if rt.algo is not None:
            measured = slippage_mod.row(pos, contract)
            rt.algo.settle_day(now.date().isoformat(), pos.net_pnl,
                               closed=True, slips=(
                                   (measured['entry_ticks'],
                                    measured['entry_money']),
                                   (measured['exit_ticks'],
                                    measured['exit_money'])))

    @staticmethod
    def _is_paper(pos) -> bool:
        return any(str(t).startswith('PAPER-') for t in pos.tickets or ())

    def _margin(self, contract, settings, qty) -> Optional[float]:
        """The margin the target is a percentage of: the one the operator
        ENTERED for the contract, and only where none was entered, what the
        venue reports — Stellar reports none. One rule, used by the entry check
        and the position alike, so the target the entry was judged on is the
        target the position gets."""
        entered = costs_mod.configured_margin(settings, qty)
        if entered:
            return entered
        return self.gateway.margin_for(contract.key, qty) or None

    def _settings(self, contract) -> Dict[str, Any]:
        """This contract's effective settings, with the venue's margin
        standing in where the operator entered none."""
        settings = self.config.effective(contract.key)
        if not costs_mod.configured_margin(settings, 1.0):
            venue = self.gateway.margin_for(contract.key, 1.0)
            if venue:
                settings = dict(settings, margin_per_contract=venue)
        return settings

    def _fees_for(self, tickets, settings, qty) -> Optional[float]:
        """What the venue charged, where it says. Falls back to the
        configured schedule, which is what the desk agreed with the broker —
        and is flagged in the Analysis window as budget, not measurement."""
        per_side = (float(settings.get('commission_per_contract', 0) or 0)
                    + float(settings.get('exchange_fee_per_contract', 0) or 0)
                    + float(settings.get('clearing_fee_per_contract', 0) or 0))
        return per_side * 2.0 * qty if per_side else 0.0

    # -- session -----------------------------------------------------------

    def _in_session(self, contract, settings, now: datetime) -> bool:
        if not self.config.settings.get('REFUSE_OUTSIDE_SESSION', True):
            return True
        return marketdata.in_session(now, contract.session_open,
                                     contract.session_close)

    def _session_flat_due(self, contract, settings, now: datetime) -> bool:
        flat_at = settings.get('session_flat_at') or ''
        if not flat_at:
            return False
        try:
            hh, mm = (int(x) for x in str(flat_at).split(':'))
        except ValueError:
            return False
        return (now.hour * 60 + now.minute) >= (hh * 60 + mm)

    # -- commands ----------------------------------------------------------

    def set_algo(self, key: str, on: bool) -> Dict[str, Any]:
        rt = self.runtimes.get(key)
        if rt is None:
            return {'ok': False, 'error': f"no contract {key}"}
        rt.contract.algo_on = bool(on)
        self.config.save()
        self._say(rt, "CONFIG", f"algo {'on' if on else 'off'}")
        return {'ok': True, 'algo_on': rt.contract.algo_on}

    def set_master(self, on: bool) -> Dict[str, Any]:
        self.master_algo = bool(on)
        self.config.settings['ALGO_MASTER_ENABLED'] = self.master_algo
        self.config.save()
        return {'ok': True, 'master_algo': self.master_algo}

    # -- who is trading: the algo or a person ------------------------------

    def _load_dry(self) -> set:
        if self.mode_path:
            try:
                from . import atomicfile
                saved = atomicfile.read_json(self.mode_path, default=None) or {}
                return set(saved.get('dry') or [])
            except Exception:                                # noqa: BLE001
                logger.warning("could not read %s", self.mode_path)
        return set()

    def _save_dry(self) -> None:
        if self.mode_path:
            from . import atomicfile
            atomicfile.write_json(self.mode_path, {
                'dry': sorted(self.dry_run), 'at': utcnow().isoformat()})

    def algo_state(self, key: str) -> str:
        """OFF, DRY, PAPER or LIVE — what this contract's Algo does NOW, the
        one word its ladder and its Algo window both show."""
        rt = self.runtimes.get(key)
        if rt is None or not rt.contract.algo_on:
            return 'OFF'
        if key in self.dry_run or not self.auto_trade_enabled:
            return 'DRY'
        return 'PAPER' if self.paper else 'LIVE'

    def _spread_limit(self, security_id) -> float:
        """The desk contract's "Book no wider than" (ticks, 0 = none)."""
        key = self._contract_for_security(security_id)
        if key is None:
            return 0.0
        try:
            return float(self.config.effective(key).get('max_book_spread_ticks') or 0)
        except (TypeError, ValueError):
            return 0.0

    def _market_ticks(self, security_id) -> float:
        """How many ticks through the touch a manual MARKET ticket goes as a
        limit: the contract's own setting, or the desk's for an instrument
        that is not on the desk."""
        key = self._contract_for_security(security_id)
        if key is not None:
            value = self.config.effective(key).get('market_limit_ticks')
        else:
            value = self.config.settings.get('DEFAULT_MARKET_LIMIT_TICKS',
                                             config_mod.DEFAULT_SETTINGS.get(
                                                 'DEFAULT_MARKET_LIMIT_TICKS', 0))
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    def _contract_for_security(self, security_id) -> Optional[str]:
        sid = str(security_id or '')
        if not sid:
            return None
        return next((k for k, rt in self.runtimes.items()
                     if str(getattr(rt.contract, 'security_id', '') or '') == sid),
                    None)

    def _manual_block(self, ticket=None) -> Optional[str]:
        """Why a NEW manual order is refused right now, or None. Only where
        an Algo TRADES the contract — or still holds something on it."""
        key = self._contract_for_security((ticket or {}).get('security_id'))
        if key is None:
            return None
        name = self.runtimes[key].contract.name or key
        if self.algo_state(key) in ('PAPER', 'LIVE'):
            return (f"the Algo is TRADING {name} — hand orders on it are "
                    f"refused so its book is not mixed with the Algo's. Set "
                    f"its Algo to Off or Dry run (the button on its ladder) "
                    f"to trade it by hand. Closing and cancelling still work.")
        held = self.algo_business(key)
        if held:
            return (f"the Algo still holds {'; '.join(held)} — close it first "
                    f"(CLOSE ALL on the ladder). Closing and cancelling still "
                    f"work.")
        return None

    def algo_business(self, key: Optional[str] = None) -> List[str]:
        """What the algo has open or working, in words. Empty = nothing."""
        out = []
        for k, rt in self.runtimes.items():
            if key is not None and k != key:
                continue
            if rt.position is not None and rt.position.is_open:
                out.append(f"{rt.contract.name or k}: an open algo position "
                           f"({rt.position.side.value} {rt.position.qty:g})")
            if self.executor.working_for(k):
                out.append(f"{rt.contract.name or k}: a working algo order")
        return out

    def manual_business(self, key: Optional[str] = None) -> List[str]:
        terminal = getattr(self.gateway, 'terminal', None)
        if terminal is None:
            return []
        if key is None:
            return terminal.open_business()
        rt = self.runtimes.get(key)
        sid = str(getattr(rt.contract, 'security_id', '') or '') if rt else ''
        return terminal.open_business(sid) if sid else []

    def set_algo_state(self, key: str, state: str) -> Dict[str, Any]:
        """One contract's Algo: OFF, DRY (signals only) or TRADE — the MT5
        desk's one switch per ladder. Refused, in words naming what is open,
        while the side being left still holds something on this contract."""
        rt = self.runtimes.get(key)
        if rt is None:
            return {'ok': False, 'error': f"no contract {key}"}
        state = str(state or '').upper()
        state = {'SIGNALS': 'DRY', 'DRY_RUN': 'DRY'}.get(state, state)
        if state not in ALGO_STATES:
            return {'ok': False, 'error': f"unknown Algo state {state!r} — OFF, DRY or TRADE"}
        name = rt.contract.name or key
        now_state = self.algo_state(key)
        if state != 'TRADE' and now_state in ('PAPER', 'LIVE'):
            held = self.algo_business(key)
            if held:
                # In a dry run nothing would manage the exit any more.
                return {'ok': False, 'error': (
                    f"the Algo still holds {'; '.join(held)}. CLOSE ALL on "
                    f"the ladder closes it and stands the Algo down — or "
                    f"leave it trading until it exits.")}
        if state == 'TRADE':
            held = self.manual_business(key)
            if held:
                return {'ok': False, 'error': (
                    f"{name} has manual business: {'; '.join(held)}. Close or "
                    f"cancel it first — the Algo never trades a book a hand "
                    f"is holding.")}
            if self.killed:
                return {'ok': False, 'error': 'KILL ALL is on'}
            if not self.master_algo:
                return {'ok': False, 'error': 'the Master switch is off'}
            if not self.auto_trade_enabled:
                # Turning automatic trading on must not set every OTHER
                # armed contract trading behind the trader's back: they
                # were dry runs a moment ago, and they stay dry runs.
                others = {k for k, r in self.runtimes.items()
                          if k != key and r.contract.algo_on}
                result = self.set_auto_trade(True)
                if not result.get('ok'):
                    return result
                self.dry_run |= others
            self.dry_run.discard(key)
            if not rt.contract.algo_on:
                self.set_algo(key, True)
        elif state == 'DRY':
            self.dry_run.add(key)
            if not rt.contract.algo_on:
                self.set_algo(key, True)
        else:
            self.dry_run.discard(key)
            if rt.contract.algo_on:
                self.set_algo(key, False)
            rt.proposal = None
        self._save_dry()
        self._say(rt, "CONFIG", f"Algo {self.algo_state(key)}")
        return {'ok': True, 'algo_state': self.algo_state(key)}

    # -- the venue: sweep, reconcile, arm LIVE ------------------------------

    def _venue_housekeeping(self) -> None:
        """Once the venue session is up: cancel the orders of ours a
        previous run left recorded as working (scoped to our ids — nothing
        else at the venue is touched), and reconcile the book the moment the
        venue's positions can be read."""
        if not hasattr(self.gateway, 'venue'):
            return
        try:
            up = self.gateway.state().value == 'LOGGED_ON'
            # Wait for Stellar's resend of what the logon showed was missed
            # before cancelling a previous run's orders.
            ready = getattr(self.gateway, 'order_session_ready', None)
            up = up and (ready() if callable(ready) else True)
        except Exception:                                # noqa: BLE001
            up = False
        if up and not self._swept_previous:
            self._swept_previous = True
            self._sweep_previous_orders()
        if up:
            self._recover_after_logon()
        if not self.book_complete and self.gateway.positions() is not None:
            self.recover()
            if self.book_complete:
                logger.info("venue positions read: the book is complete%s",
                            f" — {len(self.unclaimed)} UNCLAIMED"
                            if self.unclaimed else "")

    def _recover_after_logon(self) -> None:
        """After each Order Router logon — and after the previous run's
        orders are adopted, so a resent fill is applied as what it was —
        ask Stellar for the status of every order of ours still recorded as
        working (the Order Status sweep, `FixGateway.recover_missed`)."""
        logon = getattr(self.gateway, 'order_session_logon', None)
        recover = getattr(self.gateway, 'recover_missed', None)
        if not callable(logon) or not callable(recover):
            return
        stamp = logon()
        if not stamp or stamp == self._recovered_logon:
            return
        self._recovered_logon = stamp
        try:
            recover()
        except Exception:                                # noqa: BLE001
            logger.exception("the order status sweep could not be started")

    @staticmethod
    def _too_wide(contract, settings, book) -> Optional[str]:
        """Why the book is too wide to ENTER on, or None: the contract's
        "Book no wider than" (max_book_spread_ticks, 0 = off). An entry
        crosses the spread twice — on a thin UAT book or a blow-out, 12.0
        points of gold is $1,200 lost the instant it fills. Exits never wait
        for it."""
        try:
            limit = float(settings.get('max_book_spread_ticks') or 0)
            tick = float(contract.tick_size or 0)
        except (TypeError, ValueError):
            return None
        if limit <= 0 or tick <= 0 or book is None or getattr(book, 'width', None) is None:
            return None
        ticks = book.width / tick
        if ticks <= limit + 1e-9:
            return None
        return (f'the book is {ticks:.0f} ticks wide ({book.bid:g} / {book.ask:g}) — '
                f'over the {limit:g}-tick limit; entries wait')

    def recovery_status(self) -> Optional[Dict[str, Any]]:
        status = getattr(self.gateway, 'recovery_status', None)
        return status() if callable(status) else None

    def recovery_running(self) -> bool:
        running = getattr(self.gateway, 'recovery_running', None)
        return bool(running()) if callable(running) else False

    def recover_now(self, window: bool = False) -> Dict[str, Any]:
        recover = getattr(self.gateway, 'recover_missed', None)
        if not callable(recover):
            return {'ok': False, 'error': 'The order status sweep is a Stellar service; this venue has none.'}
        if self.gateway.state().value != 'LOGGED_ON':
            return {'ok': False, 'error': 'Order Routing is not logged on.'}
        return recover('window' if window else 'auto')

    def _sweep_previous_orders(self) -> None:
        if self.db is None or not hasattr(self.gateway, 'adopt'):
            return
        from .gateway import CLORDID_PREFIX
        done = {'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'}
        swept = 0
        for row in self.db.orders(limit=2000):
            clordid = str(row.get('clordid') or '')
            if (not clordid.startswith(CLORDID_PREFIX + '-')
                    or row.get('is_simulated')
                    or str(row.get('state') or '').split('.')[-1] in done):
                continue
            if self.gateway.adopt(row) and self.executor.adopt(row):
                self.gateway.cancel(clordid)
                swept += 1
        if swept:
            logger.info("startup sweep: cancel requested for %d order(s) of "
                        "ours a previous run left working", swept)

    def _open_algo_business(self) -> List[str]:
        out = []
        for key, rt in self.runtimes.items():
            if rt.position is not None and rt.position.is_open:
                kind = 'PAPER' if self._is_paper(rt.position) else 'venue'
                out.append(f"{rt.contract.name}: an open {kind} position")
            if self.executor.working_for(key):
                out.append(f"{rt.contract.name}: a working Algo order")
        return out

    def set_execution(self, mode: str, confirm: bool = False) -> Dict[str, Any]:
        """PAPER or LIVE for the Algo's orders.

        LIVE sends real orders to the venue, so it is never a default and
        never implied: it is OFF after every restart, and arming it needs
        `confirm` every time. When the venue's positions cannot be read, the
        confirmation says so and arming it is the trader's word that this
        book's own fills are the record. A switch either way is refused while
        anything is open or working — a position at the venue does not
        become a paper one by changing a setting."""
        mode = str(mode or '').upper()
        if mode not in ('PAPER', 'LIVE'):
            return {'ok': False, 'error': f'{mode!r} is not PAPER or LIVE'}
        if mode == ('LIVE' if self.live_armed else 'PAPER'):
            return {'ok': True, 'execution': mode}
        open_now = self._open_algo_business()
        if open_now:
            return {'ok': False, 'error': 'Close or cancel first: ' +
                    '; '.join(open_now)}
        if mode == 'PAPER':
            self.live_armed = False
            self.positions_waived = False
            logger.info('Algo execution: PAPER')
            return {'ok': True, 'execution': 'PAPER'}
        if not hasattr(self.gateway, 'venue') or getattr(
                self.gateway, 'connection_only', False):
            return {'ok': False, 'error': 'this engine has no venue to send '
                    'Algo orders to'}
        if self.killed:
            return {'ok': False, 'error': 'KILL ALL is on'}
        if self.gateway.state().value != 'LOGGED_ON':
            return {'ok': False, 'error': 'The venue is not logged on'}
        venue = self.gateway.venue
        status = (self.gateway.positions_status()
                  if hasattr(self.gateway, 'positions_status') else
                  {'status': 'unknown', 'why': None})
        readable = self.gateway.positions() is not None
        if readable:
            self.recover()
        # Said in the venue's own word: on UAT this is "UAT", and "LIVE" is
        # only ever a live market.
        word = 'UAT' if str(venue.environment).upper() == 'UAT' else 'LIVE'
        lines = [f"{word}: the Algo sends REAL orders to {venue.name} "
                 f"({venue.environment}), account {venue.account or '—'} — "
                 f"for every contract whose Algo trades."]
        if readable:
            lines.append('Stellar positions were read: the book is reconciled.' +
                         (f" {len(self.unclaimed)} position(s) at Stellar this book "
                          f"cannot explain are listed as UNCLAIMED and will "
                          f"never be touched." if self.unclaimed else ''))
        else:
            lines.append('Stellar positions could NOT be read (' +
                         (status.get('why') or status.get('status') or
                          'unknown') + f'). Sending to {word} is your word that this '
                         "book's own fills are the record: a position opened "
                         'in Stellar by other means on this account is invisible '
                         'to the Algo.')
        if not confirm:
            return {'ok': False, 'confirm': True, 'text': ' '.join(lines)}
        self.live_armed = True
        self.positions_waived = not readable
        logger.info('Algo execution: LIVE on %s (%s)%s', venue.name,
                    venue.environment,
                    ' — positions unread, confirmed by the trader'
                    if self.positions_waived else '')
        return {'ok': True, 'execution': 'LIVE',
                'positions_waived': self.positions_waived}

    def set_auto_trade(self, on: bool) -> Dict[str, Any]:
        if on and self.paper:
            # Paper trading sends nothing, so there is no venue book to
            # recover and no session it depends on.
            self.auto_trade_enabled = True
            return {'ok': True, 'auto_trade_enabled': True, 'paper': True}
        if on:
            if not self.book_complete and not self.positions_waived:
                return {'ok': False, 'error':
                    'Automatic trading requires a complete recovered account '
                    'book — or LIVE armed with the positions confirmed by you.'}
            if self.gateway.state().value != 'LOGGED_ON':
                return {'ok': False, 'error': 'The venue is not logged on.'}
        was_enabled = self.auto_trade_enabled
        self.auto_trade_enabled = bool(on)
        if not on:
            self.executor.escalating.clear()
            if was_enabled:
                try:
                    self.executor.cancel_all()
                except Exception:
                    logger.exception('automatic trading stopped; cancel outcome unknown')
        self.config.settings['AUTO_TRADE_ENABLED'] = self.auto_trade_enabled
        self.config.save()
        logger.info('automatic trade placement %s', 'enabled' if on else 'disabled')
        return {'ok': True, 'auto_trade_enabled': self.auto_trade_enabled}

    def close_now(self, key: str) -> Dict[str, Any]:
        """Cross out of this contract now, and stand its algo down.

        A guard may withhold an order; it may never withhold this.

        The algo goes off with it, and that is deliberate. The z that put the
        position on is still where it was, so an algo left armed re-enters on
        the very next pass — a tenth of a second after the trader pressed the
        button to get out. Pressing the safety control and watching the
        position come straight back is a control that did nothing. Turning it
        on again is one click, and it is a decision rather than an accident.
        """
        rt = self.runtimes.get(key)
        if rt is None or rt.position is None or not rt.position.is_open:
            return {'ok': False, 'error': "nothing open on that contract"}
        was_armed = rt.contract.algo_on
        if was_armed:
            self.set_algo(key, False)
        settings = dict(self.config.effective(key), exit_order_type='MARKET')
        now = utcnow()
        rt.paper_close_limit = None
        # A close already working is turned into the market close, never
        # joined by a second one: two closes for one position is a close
        # that finds nothing, or opens the other side.
        if not self.paper and self._has_working_close(key):
            n = self.executor.escalate_closes(key, settings)
            self._say(rt, "ORDER", "CLOSE ALL — the working close is being "
                      "cancelled and crossed at market" if n else
                      "CLOSE ALL — a market close is already working")
            return {'ok': True, 'algo_stood_down': was_armed,
                    'escalated': n}
        self._close(rt, settings, rt.position.side.opposite,
                    rt.position.qty, ExitReason.CLOSE_NOW,
                    "closed by hand", rt.book, now)
        return {'ok': True, 'algo_stood_down': was_armed}

    def close_at_limit(self, key: str, price) -> Dict[str, Any]:
        """Close @ LMT on the ladder: rest ONE closing limit for the whole
        open position at the trader's price, carrying its tickets (77=C).

        It waits there — no re-peg, no timeout. The Algo is stood down with
        it, for the reason CLOSE NOW stands it down, and because its own
        exits would otherwise put a second close beside this one. CLOSE ALL
        still crosses at once: it escalates this order rather than adding
        another."""
        rt = self.runtimes.get(key)
        if rt is None or rt.position is None or not rt.position.is_open:
            return {'ok': False, 'error': "nothing open on that contract"}
        try:
            price = float(price)
        except (TypeError, ValueError):
            return {'ok': False, 'error': "Close @ LMT needs a price"}
        tick = rt.contract.tick_size or 0
        if tick:
            price = round(round(price / tick) * tick, 10)
        if self._has_working_close(key) or rt.paper_close_limit:
            return {'ok': False, 'error': "a close is already working on this "
                    "contract — cancel it, or use CLOSE ALL"}
        was_armed = rt.contract.algo_on
        if was_armed:
            self.set_algo(key, False)
        side = rt.position.side.opposite
        now = utcnow()
        if self.paper:
            rt.paper_close_limit = {'side': side.value, 'price': price,
                                    'at': now.isoformat()}
            self._say(rt, "ORDER", f"Close @ LMT {price:g} resting (PAPER) — "
                      f"fills when the {'bid' if side is Side.SELL else 'offer'}"
                      f" reaches it")
            return {'ok': True, 'algo_stood_down': was_armed, 'paper': True}
        settings = dict(self.config.effective(key), exit_order_type='LIMIT')
        wo = self.executor.place(
            rt.contract, settings, side, rt.position.qty, Intent.CLOSE,
            rt.book, now, reason=f"Close @ LMT {price:g}",
            open_qty=rt.position.qty, position_id=rt.position.id,
            decision=self._exit_decision(rt, settings, rt.book, side,
                                         order_type='LIMIT'),
            position=rt.position, limit_price_at=price)
        if wo is None:
            return {'ok': False, 'error': "nothing could be sent"}
        self._exit_reasons[wo.clordid] = ExitReason.CLOSE_LIMIT
        self._say(rt, "ORDER", f"Close @ LMT {price:g} resting at the venue")
        return {'ok': True, 'algo_stood_down': was_armed,
                'clordid': wo.clordid}

    def refresh_feed(self, key: str) -> Dict[str, Any]:
        """↻ Feed on the ladder: ask Stellar for this contract's prices again
        (Market Data Request 263=2 then 263=1). The book is not touched."""
        rt = self.runtimes.get(key)
        terminal = getattr(self.gateway, 'terminal', None)
        if rt is None:
            return {'ok': False, 'error': "no such contract"}
        if terminal is None:
            return {'ok': False, 'error': "the simulator has no Stellar "
                    "subscription to refresh"}
        sid = str(getattr(rt.contract, 'security_id', '') or '')
        try:
            with terminal.lock:
                terminal.session('Market Data')
                if sid not in terminal.watch:
                    return {'ok': False, 'error': f"{sid or 'this contract'} is "
                            "not subscribed on Market Data yet"}
                if sid in terminal.subscriptions:
                    terminal._subscribe(sid, '2')
                    terminal.subscriptions.pop(sid, None)
                terminal._subscribe(sid)
        except ConnectionError as e:
            return {'ok': False, 'error': str(e)}
        self._say(rt, "FEED", "prices re-requested from Stellar")
        return {'ok': True}

    def uat_order(self, key: str, side: str, order_type: str,
                  price=None, qty=None) -> Dict[str, Any]:
        """UAT ONLY: have the Algo send ONE opening order now, through its
        own order path — the executor, an `FT-` ClOrdID, 77=O, the
        venue's account (452=24) — exactly as a signal would, so the path can be
        tested on Stellar UAT without waiting for one. A LIMIT is pinned at the
        price given (never re-pegged or timed out). The fill becomes the
        Algo's position like any other, and closes like any other (CLOSE
        NOW, Close @ LMT).

        With the Algo TRADING the contract (the Order tests page's hands-on
        ladder) its exits then manage the position as they would live: the
        TP / SL priced at the fill. Refused on any venue that is not UAT,
        unless LIVE is armed, and while anything is open or working on the
        contract — algo or hand: one test position at a time."""
        rt = self.runtimes.get(key)
        if rt is None:
            return {'ok': False, 'error': f"no contract {key}"}
        venue = getattr(self.gateway, 'venue', None)
        env = str(getattr(venue, 'environment', '') or '').upper()
        if venue is None or 'UAT' not in env:
            return {'ok': False, 'error': 'test orders are for a UAT venue only '
                    f'— this one is {env or "not a venue"}'}
        if self.paper:
            return {'ok': False, 'error': 'Execution is PAPER — arm LIVE on '
                    'the taskbar first: a test order goes to Stellar UAT'}
        if self.killed:
            return {'ok': False, 'error': 'KILL ALL is on'}
        held = self.algo_business(key) + self.manual_business(key)
        if held:
            return {'ok': False, 'error': 'something is open on this contract: '
                    + '; '.join(held)}
        try:
            side_v = Side(str(side).upper())
        except ValueError:
            return {'ok': False, 'error': f"side must be BUY or SELL, not {side!r}"}
        order_type = str(order_type or '').upper()
        if order_type not in ('MARKET', 'LIMIT'):
            return {'ok': False, 'error': 'order type must be MARKET or LIMIT'}
        book = rt.book
        if book is None or not book.usable:
            return {'ok': False, 'error': 'no usable book for this contract'}
        limit = None
        if order_type == 'LIMIT':
            try:
                limit = float(price)
            except (TypeError, ValueError):
                return {'ok': False, 'error': 'a LIMIT test order needs a price'}
        settings = dict(self.config.effective(key), entry_order_type=order_type)
        size = float(qty or settings.get('quantity') or 1)
        now = utcnow()
        touch = book.executable(side_v)
        wo = self.executor.place(
            rt.contract, settings, side_v, size, Intent.OPEN, book, now,
            reason='UAT test order',
            decision={'z': None, 'price': touch, 'order_type': order_type,
                      'mean': None, 'std': None, 'half_life': None},
            limit_price_at=limit)
        if wo is None:
            return {'ok': False, 'error': 'nothing could be sent'}
        rt.entry_atr = rt.algo.atr() if rt.algo is not None else None
        self._say(rt, "ORDER", f"UAT test: {side_v.value} {size:g} "
                  f"{order_type}{'' if limit is None else f' @ {limit:g}'}")
        return {'ok': True, 'clordid': wo.clordid}

    def set_depth(self, key: str, on: bool) -> Dict[str, Any]:
        """The ladder's Depth button: ask Stellar for the FULL book on this
        contract (Market Data Request 264=0) or only its top (264=1). Prices
        only — nothing about an order or a position changes."""
        rt = self.runtimes.get(key)
        terminal = getattr(self.gateway, 'terminal', None)
        if rt is None:
            return {'ok': False, 'error': "no such contract"}
        if terminal is None:
            return {'ok': False, 'error': "the simulator has no Stellar book to "
                    "ask for depth"}
        sid = str(getattr(rt.contract, 'security_id', '') or '')
        if not sid:
            return {'ok': False, 'error': "this contract has no Stellar Security ID"}
        try:
            terminal.depth({'security_id': sid, 'enabled': bool(on)})
        except (ConnectionError, ValueError) as e:
            return {'ok': False, 'error': str(e)}
        self._say(rt, "FEED", "full depth requested from Stellar" if on
                  else "top of book only")
        return {'ok': True, 'full': bool(on)}

    def cancel_close_limit(self, key: str) -> Dict[str, Any]:
        """Pull a resting Close @ LMT (a cancel REQUEST at a venue)."""
        rt = self.runtimes.get(key)
        if rt is None:
            return {'ok': False, 'error': "no such contract"}
        if rt.paper_close_limit:
            rt.paper_close_limit = None
            self._say(rt, "ORDER", "Close @ LMT pulled (PAPER)")
            return {'ok': True}
        n = 0
        for wo in self.executor.working_for(key):
            if wo.pinned and not wo.state.is_done:
                self.gateway.cancel(wo.clordid)
                n += 1
        return {'ok': bool(n), 'cancelled': n} if n else {
            'ok': False, 'error': "no Close @ LMT working"}

    def _paper_close_limit(self, rt, book, now) -> None:
        lim = rt.paper_close_limit
        if not lim:
            return
        if rt.position is None or not rt.position.is_open:
            rt.paper_close_limit = None
            return
        side = Side(lim['side'])
        touch = book.executable(side) if book is not None else None
        if touch is None:
            return
        reached = (touch >= lim['price'] if side is Side.SELL
                   else touch <= lim['price'])
        if not reached:
            return
        rt.paper_close_limit = None
        settings = self.config.effective(rt.contract.key)
        self._paper_fill(rt, side, rt.position.qty, Intent.CLOSE, book, now,
                         decision=self._exit_decision(rt, settings, book, side),
                         exit_reason=ExitReason.CLOSE_LIMIT,
                         reason=f"Close @ LMT {lim['price']:g}")

    # -- picking up an edited configuration ---------------------------------

    #: Contract fields that cannot be changed under a running engine. Every
    #: one of them re-prices something the book already holds — a tick value
    #: changed while a position is open rewrites what that position made —
    #: or needs the gateway to subscribe again.
    STRUCTURAL_CONTRACT_FIELDS = ('symbol', 'venue', 'security_id',
                                  'security_exchange', 'tick_size',
                                  'tick_value', 'contract_multiplier',
                                  'currency', 'min_qty', 'qty_step',
                                  'max_qty')

    def apply_config(self, new: 'TraderConfig') -> Dict[str, Any]:
        """Adopt an edited configuration without stopping.

        Settings are edited in the web process, which writes `config.json`;
        this is the engine reading it back. It is deliberately narrow:

        - **The live switch wins over the file.** `algo_on` is not adopted.
          The file's copy is whatever was last written by the web process,
          and adopting it would flip a contract the trader had just stood
          down — or arm one they had not.
        - **Nothing here touches the book, a position or an open order.** A
          settings change is a change to what the algo does NEXT.
        - **Structural changes are REPORTED, not applied.** A contract added
          or removed needs the gateway to subscribe; a tick value changed
          under an open position rewrites what that position made. Those want
          a restart, and the screen says so rather than half-applying them.

        Returns what changed, and what is waiting on a restart.
        """
        changed: List[str] = []
        restart: List[str] = []

        for key, value in new.settings.items():
            if key == 'AUTO_TRADE_ENABLED':
                continue  # the live switch wins over an edited settings file
            if self.config.settings.get(key) != value:
                if key in config_mod.STRUCTURAL_SETTINGS:
                    restart.append(key)
                    continue
                self.config.settings[key] = value
                changed.append(key)

        for key in sorted(set(new.contracts) - set(self.config.contracts)):
            restart.append(f'contract {key} added')
        for key in sorted(set(self.config.contracts) - set(new.contracts)):
            restart.append(f'contract {key} removed')

        for key, incoming in new.contracts.items():
            mine = self.config.contracts.get(key)
            if mine is None:
                continue
            for field in self.STRUCTURAL_CONTRACT_FIELDS:
                if getattr(mine, field, None) != getattr(incoming, field, None):
                    restart.append(f'{key}.{field}')
            for field in ('name', 'decimals', 'session_open', 'session_close'):
                if getattr(mine, field) != getattr(incoming, field):
                    setattr(mine, field, getattr(incoming, field))
                    changed.append(f'{key}.{field}')
            # `enabled` is structural: a contract switched off mid-flight has
            # a window, a book and possibly a position that would have nowhere
            # to go.
            if mine.enabled != incoming.enabled:
                restart.append(f'{key}.enabled')
            if mine.overrides != incoming.overrides:
                mine.overrides = dict(incoming.overrides)
                changed.append(f'{key} settings')
            # `algo_on` is NOT adopted. See the docstring.
            mine.spec_source = dict(incoming.spec_source)

        # The statistics window holds its own copy of the three settings that
        # shape it, so it has to be told. A changed lookback resizes in place
        # and keeps the samples; the cached mean and sigma are invalidated,
        # because they were computed over a different window.
        for key, rt in self.runtimes.items():
            settings = self.config.effective(key)
            if rt.window.update_config(
                    window_minutes=settings['window_minutes'],
                    min_history_minutes=settings['min_history_minutes'],
                    sample_interval_sec=settings['sample_interval_sec'],
                    stats_update_interval_sec=settings[
                        'stats_update_interval_sec'],
                    entry_threshold=settings['entry_threshold']):
                changed.append(f'{key} window resized')

        for key, rt in self.runtimes.items():
            if rt.algo is not None:
                fresh = algo_mod.params_from_settings(
                    self._settings(rt.contract))
                if fresh != rt.algo.params:
                    rt.algo.reshape(fresh, None)

        self.config_reloaded_at = utcnow()
        self.config_restart_needed = restart
        if changed or restart:
            logger.info("configuration reloaded — %d changed, %d awaiting a "
                        "restart", len(changed), len(restart))
        return {'changed': changed, 'restart_needed': restart}

    def kill_all(self, close_positions: bool = False) -> Dict[str, Any]:
        """Stand every algo down and cancel our working orders.

        It does NOT flatten unless asked: the button pressed in a hurry must
        not also be the button that crosses eight spreads at market.
        """
        self.killed = True
        self.master_algo = False
        self.auto_trade_enabled = False
        self.executor.escalating.clear()
        cancelled = self.executor.cancel_all()
        closed = 0
        if close_positions:
            for key, rt in self.runtimes.items():
                if rt.position is not None and rt.position.is_open:
                    self.close_now(key)
                    closed += 1
        return {'ok': True, 'cancelled': cancelled, 'closed': closed}

    def resume(self) -> Dict[str, Any]:
        self.killed = False
        return {'ok': True}

    def _say(self, rt: ContractRuntime, kind: str, text: str) -> None:
        rt.last_event = text
        if self.db is not None:
            self.db.log_event(kind, text, rt.contract.key)

    # -- the snapshot ------------------------------------------------------

    def halted_by(self, rt: ContractRuntime, now: datetime) -> Optional[str]:
        """Why this contract is HALTED, in words, or None when it is not.

        The same three causes `state_of` checks, in the same order. A badge
        that says HALTED and nothing else reads as a fault in the program;
        the usual cause on a quiet UAT book is a price that has not moved.
        """
        if self.killed:
            return "KILL ALL is on — new entries are stopped desk-wide"
        if rt.halted_reason:
            return rt.halted_reason
        if rt.units_note:
            return rt.units_note
        if rt.guard.is_stale(now):
            age = rt.guard.age(now) or 0.0
            return (f"price unchanged {age:.0f}s (limit "
                    f"{rt.guard.max_quote_age_sec:g}s, MAX_QUOTE_AGE_SEC). "
                    f"Entries wait for a move; exits and CLOSE NOW work")
        return None

    def md_status(self, rt: ContractRuntime) -> Optional[Dict[str, str]]:
        """Why this contract has prices from Stellar or not (`ManualTerminal.
        feed_status`); None on the simulator."""
        terminal = getattr(self.gateway, 'terminal', None)
        if terminal is None or not hasattr(terminal, 'feed_status'):
            return None
        try:
            return terminal.feed_status(str(getattr(rt.contract, 'security_id', '') or ''))
        except Exception:                                # noqa: BLE001
            return None

    def market_note(self, rt: ContractRuntime) -> Optional[str]:
        """Why the window has no mid, in words, or None when it has one.

        A blank window reads as "not subscribed". On a quiet UAT spread the
        usual truth is that Stellar is quoting one side, or nothing yet — and the
        statistics and the algo need BOTH sides, because the price is the mid.
        """
        book = rt.book
        md = self.md_status(rt)
        if md and md.get('state') not in ('LIVE', 'ONE_SIDED', None):
            return md['text']
        if book is None:
            return ("no quote from the venue for this contract yet — its "
                    "window fills when a bid and an offer arrive")
        if book.crossed:
            return "the book is crossed — not used until it clears"
        if book.bid is None:
            return ("only an offer is being quoted, no bid — no mid, so no "
                    "statistics and no entries until both sides are there")
        if book.ask is None:
            return ("only a bid is being quoted, no offer — no mid, so no "
                    "statistics and no entries until both sides are there")
        return None

    def state_of(self, rt: ContractRuntime, now: datetime) -> ContractState:
        if self.killed or rt.halted_reason or rt.units_note:
            return ContractState.HALTED
        if rt.guard.is_stale(now):
            return ContractState.HALTED
        if rt.position is not None and rt.position.is_open:
            return ContractState.IN
        if self.executor.working_for(rt.contract.key):
            return ContractState.WORKING
        if not rt.contract.algo_on or not self.master_algo:
            return ContractState.IDLE
        body = (rt.algo.body or {}) if rt.algo is not None else {}
        warm = (body.get('filters') or {}).get('warmup') or {}
        if not body.get('ready') or not warm.get('done', True):
            return ContractState.WARMING
        if rt.blocked_by:
            return ContractState.BLOCKED
        return ContractState.ARMED

    def snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        contracts = []
        for key, rt in self.runtimes.items():
            contract = rt.contract
            settings = self.config.effective(key)
            book = rt.book
            status = rt.guard.status(now)
            qty = float(settings.get('quantity', 1.0) or 1.0)

            ratio = signals_mod.edge_ratio(rt.window, qty, contract.tick_size,
                                           contract.tick_value, settings)
            breakdown = costs_mod.cost_breakdown(qty, contract.tick_size,
                                                 contract.tick_value, settings)
            pos = rt.position
            open_pnl = None
            live = None
            if pos is not None and pos.is_open:
                close_px = (book.executable(pos.side.opposite)
                            if book is not None else None)
                if close_px is not None:
                    open_pnl = sizing.to_money(
                        (close_px - pos.avg_price) * pos.side.sign,
                        contract.tick_size, contract.tick_value, pos.qty)
                held_min = ((now - pos.opened_at).total_seconds() / 60.0
                            if pos.opened_at else None)
                max_hold = float(settings.get('max_hold_minutes', 0) or 0)
                live = {
                    #: The price it would CLOSE at: the opposite side's touch.
                    'mark': close_px,
                    # NET: the whole round trip taken off, at the closing side.
                    'net': costs_mod.open_net(pos.side, pos.qty, pos.avg_price,
                                              close_px, contract.tick_size,
                                              contract.tick_value, settings),
                    'held_min': round(held_min, 1) if held_min is not None else None,
                    'time_left_min': (round(max(0.0, max_hold - held_min), 1)
                                      if max_hold and held_min is not None else None),
                    'z_close': rt.window.z_of(close_px),
                }
            z_bid, z_ask = signals_mod.side_z(rt.window, book)
            margin = costs_mod.configured_margin(settings, qty)

            contracts.append({
                'key': key,
                'name': contract.name,
                'symbol': contract.symbol,
                'venue': contract.venue,
                'decimals': contract.decimals,
                'tick_size': contract.tick_size,
                'tick_value': contract.tick_value,
                'state': self.state_of(rt, now).value,
                'halted_by': self.halted_by(rt, now),
                'close_alert': (rt.close_alert if rt.position is not None
                                and rt.position.is_open else None),
                'market_note': self.market_note(rt),
                'algo_on': bool(contract.algo_on),
                'market': (book.to_dict() if book is not None else
                           {'bid': None, 'ask': None, 'mid': None}),
                'feed': status,
                'md': self.md_status(rt),
                'stats': dict(rt.window.to_dict(), z_bid=z_bid, z_ask=z_ask,
                              stop_lo=rt.window.price_at_z(-float(settings.get('stop_loss_z', 4) or 4)),
                              stop_hi=rt.window.price_at_z(float(settings.get('stop_loss_z', 4) or 4))),
                'filters': {
                    'edge_ratio': round(ratio, 2) if ratio is not None else None,
                    'trade_direction': settings.get('trade_direction', 'BOTH'),
                    'margin': margin,
                    'blocked_by': rt.blocked_by,
                },
                'costs': {
                    'round_trip_money': breakdown['round_trip_money'],
                    'round_trip_ticks': (round(breakdown['round_trip_ticks'], 2)
                                         if breakdown['round_trip_ticks'] is not None
                                         else None),
                },
                'settings': {
                    'entry_threshold': settings.get('entry_threshold'),
                    'max_entry_z': settings.get('max_entry_z'),
                    'stop_loss_z': settings.get('stop_loss_z'),
                    'profit_target_pct': settings.get('profit_target_pct'),
                    'margin_per_contract': settings.get('margin_per_contract'),
                    'window_minutes': settings.get('window_minutes'),
                    'quantity': qty,
                    'entry_order_type': settings.get('entry_order_type'),
                    'exit_order_type': settings.get('exit_order_type'),
                },
                'position': (dict(self._position_dict(pos, open_pnl), **live)
                             if live is not None else None),
                'orders': [w.to_dict() for w in
                           self.executor.working_for(key)],
                'proposal': rt.proposal,
                #: The Algo window: Signal & Position, Statistics, Filters,
                #: the last signal held back and the last order.
                #: The ONE word this contract's ladder and Algo window show:
                #: OFF / DRY / PAPER / LIVE, and why a hand order is refused.
                'algo_state': self.algo_state(key),
                'manual_block': self._manual_block(
                    {'security_id': getattr(contract, 'security_id', '')}),
                'algo': (rt.algo.block(self.algo_mode(key))
                         if rt.algo is not None else None),
                'last_close': rt.last_close,
                #: The day's O/H/L of the mid this system watched (ours).
                'hlo': dict(rt.hlo) if rt.hlo else None,
                'last_trade': rt.last_trade,
                'signal_alert': rt.signal_alert,
                #: The book behind the ladder: every level Stellar sends, or None
                #: where there is none to read (the simulator, MD down).
                'depth': (self.gateway.depth(key)
                          if hasattr(self.gateway, 'depth') else None),
                #: A resting Close @ LMT: on PAPER here, or our pinned order.
                'close_limit': (dict(rt.paper_close_limit, paper=True)
                                if rt.paper_close_limit else next(
                                    ({'side': w.side.value, 'price': w.price,
                                      'paper': False, 'clordid': w.clordid,
                                      'state': w.state.value}
                                     for w in self.executor.working_for(key)
                                     if w.pinned and not w.state.is_done),
                                    None)),
                'security_id': getattr(contract, 'security_id', ''),
                'pnl_today': round(rt.pnl_today, 2),
                'trades_today': rt.trades_today,
                'last_event': rt.last_event,
                'target_missing': (
                    costs_mod.missing_for_target(
                        settings, pos.margin_locked if pos else None,
                        contract.contract_multiplier,
                        pos.entry_std if pos else None, contract.tick_value)
                    if pos is not None and pos.is_open
                    and pos.target_price is None else None),
            })

        venue_positions = self.gateway.positions()
        return {
            'ts': now.isoformat(),
            'portfolio': self._portfolio(contracts, venue_positions),
            'engine': {
                'alive': True,
                'loop_ms': round(self.loop_ms, 1),
                'master_algo': self.master_algo,
                'auto_trade_enabled': self.auto_trade_enabled,
                'auto_trade_available': True,
                'paper': self.paper,
                'execution': {
                    #: What SENDING to the venue is called on this screen:
                    #: UAT on a UAT venue — "LIVE" is only a live market.
                    'send_word': ('UAT' if str(getattr(getattr(
                        self.gateway, 'venue', None), 'environment', '')
                        ).upper() == 'UAT' else 'LIVE'),
                    'mode': ('LIVE' if (hasattr(self.gateway, 'venue')
                                        and not self.paper) else
                             'PAPER' if self.paper else 'SIMULATOR'),
                    'can_live': bool(hasattr(self.gateway, 'venue') and not
                                     getattr(self.gateway, 'connection_only',
                                             False)),
                    'positions': (self.gateway.positions_status()
                                  if hasattr(self.gateway, 'positions_status')
                                  else None),
                    'positions_waived': self.positions_waived,
                },
                'killed': self.killed,
                'environment': self.config.environment_label,
                'simulated': self.simulated,
                'book_complete': self.book_complete,
                'unclaimed': self.unclaimed,
                #: The Order Status sweep after the last Order Router logon, in words.
                'fix_recovery': self.recovery_status(),
                #: An edited configuration is in force from the pass that
                #: picked it up. Anything the engine could not adopt while
                #: running is named here, so a setting that looks saved and
                #: is not never passes for one that is.
                'config_reloaded_at': (self.config_reloaded_at.isoformat()
                                       if self.config_reloaded_at else None),
                'config_restart_needed': list(self.config_restart_needed),
                #: Whether a launcher is behind this engine to start it again,
                #: which is what the restart button needs.
                'supervised': bool(os.environ.get('FIXTRADER_SUPERVISED')),
                'session': {
                    'state': self.gateway.state().value,
                    'text': self.gateway.state_text(),
                },
                'connection_only': getattr(self.gateway, 'connection_only', False),
                'fix_connection': (self.gateway.connection_snapshot()
                                   if hasattr(self.gateway, 'connection_snapshot') else None),
                'manual_terminal': (self.gateway.terminal.snapshot()
                                    if hasattr(self.gateway, 'terminal') else None),
                'refresh_sec': self.config.settings.get('PRICE_REFRESH_SEC', 0.5),
                'sound': self.config.settings.get('SOUND_ENABLED', True),
                'confirm_close': self.config.settings.get('CONFIRM_CLOSE', True),
                'notify': {
                    'orders': self.config.settings.get('NOTIFY_ORDERS', False),
                    'fills': self.config.settings.get('NOTIFY_FILLS', True),
                    'positions': self.config.settings.get('NOTIFY_POSITIONS', True),
                    'rejects': self.config.settings.get('NOTIFY_REJECTS', True),
                    'withheld': self.config.settings.get('NOTIFY_WITHHELD', True),
                },
            },
            'contracts': contracts,
        }

    def _portfolio(self, contracts: List[Dict[str, Any]],
                   venue_positions) -> Dict[str, Any]:
        """Every open position across every contract, in one list.

        It carries what THIS BOOK holds and what the VENUE says beside it,
        because the two disagreeing is the thing worth seeing. A venue row
        with both sides open — long and short at once — is a close that went
        out as an open, and it is called out by name rather than netted to
        zero and shown as flat.

        `venue` is None where the account could not be READ. That is not
        "flat", and the screen says so rather than showing an empty table.
        """
        by_key = {}
        if venue_positions is not None:
            for vp in venue_positions:
                by_key[vp.contract_key] = vp

        rows: List[Dict[str, Any]] = []
        for c in contracts:
            pos = c.get('position')
            vp = by_key.get(c['key'])
            if pos is None and vp is None:
                continue
            rows.append({
                'key': c['key'],
                'name': c['name'],
                'symbol': c['symbol'],
                'decimals': c['decimals'],
                'side': pos['side'] if pos else None,
                'qty': pos['qty'] if pos else None,
                'avg_price': pos['avg_price'] if pos else None,
                'entry_z': pos['entry_z'] if pos else None,
                'break_even': pos['break_even'] if pos else None,
                'target': pos['target'] if pos else None,
                'stop': pos['stop'] if pos else None,
                'opened_at': pos['opened_at'] if pos else None,
                'open_pnl': pos['open_pnl'] if pos else None,
                'margin_locked': pos['margin_locked'] if pos else None,
                'tickets': pos.get('tickets') if pos else None,
                'id': pos.get('id') if pos else None,
                'opened_qty': pos.get('opened_qty') if pos else None,
                'mark': pos.get('mark') if pos else None,
                'net': pos.get('net') if pos else None,
                'held_min': pos.get('held_min') if pos else None,
                'z_close': pos.get('z_close') if pos else None,
                'entry_slippage': pos.get('entry_slippage') if pos else None,
                'entry_order_type': pos.get('entry_order_type') if pos else None,
                'paper': bool(pos.get('paper')) if pos else False,
                'simulated': bool(pos.get('simulated')) if pos else False,
                'tick_size': c.get('tick_size'),
                'market': c.get('market'),
                'mid': (c.get('market') or {}).get('mid'),
                'venue_qty': vp.qty if vp is not None else None,
                'venue_long': vp.long_qty if vp is not None else None,
                'venue_short': vp.short_qty if vp is not None else None,
                'venue_readable': venue_positions is not None,
                'both_sides_open': bool(vp is not None and vp.is_gross_hedged),
                'agrees': (vp is not None and pos is not None
                           and abs(vp.qty - (pos['qty'] *
                                             (1 if pos['side'] == 'BUY' else -1)))
                           < 1e-9),
            })

        pnls = [r['open_pnl'] for r in rows if r['open_pnl'] is not None]
        margins = [r['margin_locked'] for r in rows
                   if r['margin_locked'] is not None]
        return {
            'rows': rows,
            'venue_readable': venue_positions is not None,
            'position_scope': 'account_verified' if venue_positions is not None else 'algo_local',
            'account_status': ('verified' if venue_positions is not None else
                               'unavailable' if getattr(self.gateway, 'connection_only', False)
                               else 'recovery_pending'),
            # Unmeasured is not zero: a total is only a total when every row
            # it covers was measured.
            'open_pnl': (round(sum(pnls), 2)
                         if len(pnls) == len(rows) and rows else None),
            'margin': (round(sum(margins), 2)
                       if len(margins) == len(rows) and rows else None),
            'realised_today': round(
                sum(rt.pnl_today for rt in self.runtimes.values()), 2),
            'trades_today': sum(rt.trades_today
                                for rt in self.runtimes.values()),
        }

    @staticmethod
    def _position_dict(pos: Optional[Position],
                       open_pnl: Optional[float]) -> Optional[Dict[str, Any]]:
        if pos is None or not pos.is_open:
            return None
        return {
            'side': pos.side.value, 'qty': pos.qty,
            'avg_price': pos.avg_price,
            'entry_z': pos.entry_z,
            'break_even': pos.break_even,
            'target': pos.target_price,
            'stop': pos.stop_price,
            'margin_locked': pos.margin_locked,
            'opened_at': pos.opened_at.isoformat() if pos.opened_at else None,
            'open_pnl': round(open_pnl, 2) if open_pnl is not None else None,
            'tickets': list(pos.tickets or []),
            'id': pos.id,
            'opened_qty': pos.opened_qty or pos.qty,
            'entry_mean': pos.entry_mean,
            'entry_std': pos.entry_std,
            'entry_slippage': pos.entry_slippage,
            'entry_order_type': pos.entry_order_type,
            'simulated': bool(pos.is_simulated),
            'paper': any(str(t).startswith('PAPER-')
                         for t in pos.tickets or ()),
        }
