# Working on FIX-Trader

Read **`BUILD_PROMPT.md`** first — it is the specification. **`docs/screens.html`**
is the layout reference: where the two disagree about what a screen looks like,
the drawing wins.

## Hard rules

- **`pytest tests/ -q` must pass before any commit**, and a PROD venue must
  never be run without it.
- **One contract, not two legs.** The venue lists the spread itself. If you
  find yourself writing a hedge ratio, stop — you are building the wrong
  system.
- **Price is the spread's own price, from the MID OF THE BOOK.** Never the last
  trade. A trigger reads the executable side for its own direction; an open
  position reads the **opposite** side to close.
- **Prices are the prices a trader knows, never TT's FIX units.** TT sends
  Crude as 9057; its DisplayFactor (9787) makes it 90.57. The conversion is
  made ONCE, at the FIX boundary (`ManualTerminal.to_display` / `to_fix`):
  270/31/6/44 in, 44/99 out, manual and Algo alike. A contract whose factor
  is unknown records nothing and enters nothing; a recording in the old
  units is rescaled once (`price_units`), never mixed with the new.
- **One conversion, in `sizing.py`**: `money = points × tick_value / tick_size
  × qty`. Every money figure on the screen goes through it.
- **`gateway.py` is the only module that may import a FIX library.**
- **Credentials live only in `.env`** — never in code, config, an API response,
  a toast or a log line. A password is reported as set or not set, never
  returned, not even masked.
- **Cancel our own working orders at startup AND at shutdown**, scoped to our
  `ClOrdID` prefix. Never touch an order this system did not send.
- **The book is persisted and recovered.** The reconciler auto-closes nothing
  until recovery says the book is complete, and never touches a position it
  cannot explain.
- **A close is never a bare opposite order.** It carries an explicit
  `PositionEffect`, the position it closes and that position's venue tickets,
  and it is capped at what is open on that side. `reduce_only` is a CAP, not
  an instruction — sent as well as the flag, never instead of it. On a venue
  that keeps long and short apart, an opposite order flagged OPEN opens the
  other side: the desk is long AND short, both posting margin, and a netting
  screen calls it flat. An unknown flag degrades to CLOSE, never to OPEN.
- **What comes BACK is applied as a close, never as a new position.** An
  order's purpose survives a restart: `Executor.intent_of` reads it from the
  orders table it was written to when sent — a close still working when the
  engine stopped was otherwise read as an OPEN. And the book guards itself
  whatever the record says: a fill on the OPPOSITE side of an open position
  only reduces it (never averaged in, never opens the other side); a
  "close" on the SAME side is reported, not applied; a close larger than
  what is open closes it and the excess is reported, never booked; a close
  with nothing open opens nothing. `tests/test_close_safety.py` pins each,
  for a Take Profit and a Stop Loss alike.
- **Manual AND algo, but never both on the same contract.** This program
  trades by hand (Instruments & orders: tickets; and the desk ladder) and by
  algo (the Algo desk). Each contract has ONE Algo switch — the MT5 desk's,
  on its ladder AND its Algo window, always reading the same word —
  `Engine.set_algo_state(key, OFF | DRY | TRADE)` — DRY is shown as
  **SIGNALS** — read as ALGO OFF / ALGO SIGNALS / ALGO PAPER / ALGO LIVE
  (`algo_state`). **On a UAT venue the sending state is SHOWN as UAT**
  (ALGO UAT, Orders: UAT — `execution.send_word`): "LIVE" on the screen is
  only ever a live market. The switch's menu is Off / Signals / Paper / UAT
  (LIVE on a live venue) and sets where the Algo's orders go itself
  (`set_execution`, with the engine's own confirmation) — no second control
  to find. An algo and a hand on the
  same book fight — the trader puts a position on and the algo closes it at
  its own target, or the trader gets flat and the algo re-enters on the next
  pass. So:
  - While a contract's Algo TRADES it (PAPER or LIVE), a NEW manual order on
    THAT contract is refused by `ManualTerminal` itself (`mode_block(ticket)`
    → `Engine._manual_block`), at the review AND at the send, so no page or
    command can go round it; it is also refused while the Algo still holds a
    position or order there. Off and Signals leave the contract to the hand,
    as on the MT5 desk. Other instruments are never refused.
  - **SIGNALS: the Algo signals, the trader trades.** An entry signal is an
    ALERT (`signal_alert`, keyed by `seq` — toast + chime, said once, never
    replayed on a reload). The trader's own position on that contract is
    WATCHED (`_manual_position`): the same `levels` (per-direction ones
    included), frozen when first seen, on the ladder and in the Algo window
    marked as theirs, and its exits (TP, SL, z/mean/time stops if on) are
    alerts too. Nothing in SIGNALS ever sends or closes an order.
  - A contract a hand is holding (a working manual order or an unclosed
    manual fill on its Security ID) gets no Algo entry, and cannot be set to
    TRADE; a contract whose Algo holds a position cannot be set Off or
    Signals (nothing would manage the exit) — CLOSE ALL closes it and stands the
    Algo down. Each refusal names what is open.
  - Setting ONE contract to TRADE turns automatic trading on without setting
    any other armed contract trading: they stay in Signals. Signals choices
    are kept beside the status file; a live venue still comes back with
    automatic trading off, i.e. every armed Algo in Signals.
  - Closes and cancels are never refused. A manual order recovered as
    UNKNOWN after a restart is named, not counted as open.
- **The Algo desk is a ladder and an Algo window per contract**, each with
  a taskbar button that minimises and restores it. The desk ladder is the
  MT5 desk's ladder on ONE contract (Work / Bids / Price / Asks / LTQ, the
  rail, the B/S/W bar, the footer book): it shows the book, our working
  orders and the Algo's levels against it. With its Algo Off or in Signals
  it trades by hand THROUGH `ManualTerminal` — BUY / SELL, or a click in Bids (buys at that
  price) / Asks (sells), each a `terminal_preview` reviewed in the shared
  modal before `terminal_submit`, an `FTM-` ticket flagged 77=O with the
  manual safety limits; never an order path of its own. While its Algo
  trades, its order controls are off and the lock banner says so
  (ManualTerminal refuses regardless). CLOSE ALL and Close @ LMT always close — a manual position by
  `preview_close` (77=C, capped at the ticket's open fills; a price makes it
  a LIMIT).
- **A contract without prices says WHY** (`ManualTerminal.feed_status`,
  `Engine.md_status`, on the ladder footer, the Algo window and the
  Instruments watchlist): REFUSED by TT (35=Y with 281 in words, a 35=3
  naming V — market-data permission: Orient / TT), NO ANSWER after
  `FEED_ANSWER_SEC` (a Security ID TT does not know), EMPTY (TT answered,
  nobody quotes it — a quiet UAT market, not a fault), or LIVE. An empty
  snapshot is an answer.
- **The ladder shows the order book TT sends**: Depth TOP (264=1, the
  touch) or FULL (264=0, every level, `FixGateway.depth`, best first) —
  switched per contract from the ladder (`Engine.set_depth`), each size at
  its own price, the touch in bold. No book to read (the simulator, Market
  Data down) is None, never an empty book.
- **Close @ LMT rests ONE closing limit at the trader's price**, by the
  position's tickets (77=C), PINNED: never re-pegged, never timed out. It
  stands the Algo down (its exits would be a second close). On PAPER it
  fills here when the touch reaches the price. **CLOSE ALL over a working
  close ESCALATES it** (cancel, then market on the CANCELLED event for what
  is left) — never a second close beside the first.
- **The Trading Monitor is the MT5 desk's, for FIX** — Positions, Working
  Orders, Fills, Slippage, Accounts, Reconciler, Analysis — ONE renderer
  (`static/monitor.js`) mounted twice: the desk's Trading Monitor window
  (fed the desk's snapshot) and the Account tab (polling for itself). Its Fills tab is the
  TT FILLS TAPE — every execution report with a fill on the Order Routing
  session, ours or not, in TT's own tags (60, 1, 48, 54, 77, 32, 31, 37, 17,
  11, 58), kept in `tt_fills`. Display only: the book is built from OUR
  fills (`_execution`), never from the tape.
- **CLOSE NOW stands that contract's algo down.** The z that put the position
  on has not moved, so an algo left armed re-enters on the next pass — a
  tenth of a second after the trader pressed the button to get out.
- **Order tests run on TT UAT from the screen** (`/order-tests`,
  `fixtrader/uat.py`): every manual and Algo order path — rest, replace,
  cancel, MARKET, marketable LIMIT, hit at the touch, Close @ LMT, CLOSE ALL
  escalation, a refusal in TT's words — through the SAME commands the ladder
  and the Algo window send, never a side door. Refused on any venue but UAT;
  each test cleans up by ticket, pass or fail. `tests/test_uat_orders.py`
  proves them against `tests/fake_tt.py` (real web app, engine loop and FIX
  sessions). A new order path gets a scenario here.
  The page is three steps — pick a contract, Check automatically (four
  groups: Manual / Algo × Market / Limit, each flow keeping its newest
  result; a quiet, unmoving UAT price never blocks a check), Try it yourself — the last embedding the desk's ladder, Algo
  window and Trading Monitor for the contract: with the Algo switch on UAT
  the ladder makes the Algo send its order now (`Engine.uat_order`) and its
  TP / SL manage the result. Keep it that simple.
- **UAT and PROD are separate venues** and the screen always says which.

## Conventions that are easy to lose in a refactor

- **`orders()` and `positions()` return `None` for "unknown"**, which is NOT
  "no orders" / "flat". Code that treats the first as the second reports a
  clean account while the money sits at the venue.
- **The window marks its own position.** Blue while a position is open — a
  STATE read off the snapshot, so it survives a reload and cannot stick on a
  contract that is flat — and a green or red FLASH on the close, which fades.
  A window left red says "this is losing", which is a different statement
  from "the last trade lost". The close is read from `last_close.seq`, never
  from the wording of `last_event`: a highlight driven by a regex over a
  sentence stops working, silently, the day somebody rewords the sentence.
  A net of exactly 0 is not a win and a net of `None` is not a loss — both
  get the neutral mark. And the colour goes on the FRAME, the titlebar and
  the border, never on a layer over the figures: a tint that makes a price
  harder to read has cost more than it gave.
- **Unmeasured is not zero.** Return `None` and render `—`. A target of 0.00
  reads as "get out at break-even", which is a different instruction; a
  net P&L that quietly means gross makes a losing system look profitable.
- **"At market" goes as a LIMIT through the touch, immediate-or-cancel**
  (`market_limit_ticks`, default 2; 0 = a true market order): a buy at the
  offer + N ticks, a sell at the bid - N, 59=3 — manual tickets
  (`ManualTerminal._marketable`) and the Algo (`Executor.place`,
  `through_price`) alike. CME via TT gives a bare market order its own
  protection price and REJECTS it outside the price band ("Bid of 7941.25
  violates High Band 7872.00") — a close that never happens. With no fresh
  quote or no tick it stays a true market order: a close is never withheld
  for want of a price. The IOC is never re-pegged.
- **A close that did not happen is said loudly** — refused by TT, or an IOC
  close that found nothing — while the position is still open: the Algo's
  in `close_alert` (engine), a manual ticket's in `close_alerts`
  (ManualTerminal, computed from the orders, never stale), in red on the
  ladder and first on the Trading Monitor's Positions tab, in TT's words,
  until a later close fills or the position is flat.
- **A guard may withhold an ORDER. A guard must never prevent a close** — and
  nothing withholds the escalation to market on an exit.
- **A refusal carries the venue's own words** (`tag 58: "Instrument not open
  for trading"`), never "check the log".
- **The figures a decision was made on are recorded at the time**, not read off
  the window when the fill lands. `Executor.decisions` exists for exactly this:
  by fill time the market has moved, and an entry logged at z −0.67 for a trade
  taken at −2.24 makes every Analysis figure the wrong one.
- **`cancel` is a REQUEST, not a fact.** The resting order is still live at
  the venue until the venue says otherwise, and it can fill in between. The
  escalation from an unfilled limit is therefore ARMED on the timeout and
  SENT on the CANCELLED event, for whatever is still outstanding then.
  Sending the replacement alongside the cancel puts two orders out for one
  position: on a close the second finds nothing to close (the simulator says
  so in the venue's own words), and on an open it doubles the position.
- **A gateway event carries a SNAPSHOT of the order, never the live object.**
  Aliasing made a queued ACK report the order's current state, the reader
  marked it done, and the fill that followed was applied as an open — doubling
  the position instead of closing it.
- **The Algo is the MT5 desk's Algo, on ONE contract** (`algo.py`,
  `bands.py`, `algofilters.py`, `algodesk.py`, `backtest.py`). Bollinger
  bands, TradingView's convention: candles of `timeframe_min` closing on the
  last MID in them (the forming candle counts), middle = EMA(N), sigma = the
  POPULATION sigma of the last N. "H to L" sells the contract into its BID,
  "L to H" buys its OFFER — there are no legs and no beta. A side is ARMED
  when its own z reaches the entry z and, with re-entry on, ENTERS on the way
  back in, inside the re-entry window; it must hold for `confirm_samples`
  FRESH QUOTES (a quote read again is not a new one). The gates — health,
  mode, the day's limits, collecting candles, the live warm-up, cooldown,
  session cutoff, the |z| cap, levels, and the filters (edge, regime, trend,
  half-life) — hold ENTRIES only and each says why; a filter it cannot price
  BLOCKS. Those modules DECIDE; the engine acts — `tests/test_algo.py` fails
  the build if one of them imports an order path.
- **History is the recording.** A FIX market-data session has no bars to
  backfill from: the band is rebuilt from the mids this system recorded, on
  the Algo's FIRST pass and on the clock the passes run on (seeding on a
  clock of its own left a band empty over hours of recordings). The live
  warm-up is time WATCHED since the Algo was armed; a quick restart carries
  it, standing the Algo down resets it.
- **H to L and L to H may each size their own target and stop**
  (`*_hl` / `*_lh`: mode, %, ATR multiple — "Exit by side" in the contract's
  settings). Blank is "same as both", never zero; `algo.side_params` lays a
  direction's own over the shared ones and `levels` is still the ONE
  function the engine and the backtest price with. The levels gate is per
  direction (`levels_gate`): a side that cannot price its own levels holds
  only itself.
- **One settings window per contract**, opened from the Algo window's gear
  — the ladder has none, so there is never a second door to the same
  settings. The Algo window's ladder button reopens / restores the ladder.
- **Levels are from BREAK-EVEN and frozen at entry**: the target is
  `profit_target_pct` % of the margin the trader ENTERS (or `atr_target_mult`
  x the ATR at entry), the stop loss `stop_loss_pct` % (or `atr_stop_mult` x
  ATR), ON by default. TT reports no margin: `margin_per_contract` wins over
  the venue's, and with neither the levels cannot be priced and nothing
  enters — the window says so. It is never read as zero. The z stop, the
  mean exit (in profit only) and the time stop are OFF until a contract asks.
- **A checkbox shows what is IN FORCE, and Save writes it only if it was
  changed.** A box drawn unticked for a desk default of ON wrote OFF on the
  next Save — a stop loss switched off by saving a different field.
- **The Algo's orders go to TT over Order Routing** (`AlgoOrderRouter` in
  `gateway.py`): New Order Single / Cancel / Cancel-Replace with an `FT-`
  ClOrdID, the venue's account (tag 1), 77=O or 77=C (an unknown effect is a
  CLOSE), 1028=N (automated), TT cancel-on-disconnect (18=o 2), and a close's
  position and tickets in 58. Execution reports, cancel rejects (9) and
  business rejects (j) for OUR ids become `GatewayEvent`s with a snapshot of
  the order; a repeated ExecID is not a second fill; a manual ticket's
  `FTM-` reports are never read as the Algo's. Only `FTM-` and `FT-` orders
  can leave the session at all.
- **PAPER until a person arms LIVE, every session** (`Engine.set_execution`).
  A live venue comes back on PAPER after every restart; LIVE needs
  `confirm` every time, with the engine's own text (venue, account,
  positions). A switch either way is refused while anything is open or
  working — a position at the venue does not become a paper one by a
  setting. PAPER fills at the live bid/offer with `PAPER-n` tickets and
  sends nothing.
- **The FIX session RECOVERS, as TT's certification expects**
  (`NativeFixSession`): a gap sends a ResendRequest (2) and holds what came
  ahead of it until the gap is filled, so messages are applied in TT's
  order; TT's ResendRequest is answered with a SequenceReset-GapFill — our
  orders are NEVER resent; a SequenceReset moves the expected number; a
  session Reject (3) never stops the session; a resent duplicate (43=Y) is
  not applied twice; TT's News (B) records that its recovery is complete;
  a quiet line gets a TestRequest before it is given up. Only a number
  already used and not marked as a resend stops it.
  TT sends News (B) "Recovery is complete" after every logon and asks
  clients to wait for it: price requests, definition requests, the
  positions request, the startup sweep and NEW orders wait for it
  (`NativeFixSession.ready`, a 5 s fallback if TT never says) — a CLOSE
  (77=C) never waits. ClOrdID (11) is at most 20 characters (TT): FTM- +
  16 hex for manual tickets, FT-<ms hex>-<base 36> for the Algo.
- **What Order Routing missed is RECOVERED from TT** (`FixRecoverySession`,
  `FixGateway.recover_missed`): after each logon — once the previous run's
  orders are adopted — a non-persistent TT FIX Recovery connection on the
  SAME login (its own address: 11508 / 11708 through stunnel, derived from
  the order-entry endpoint or set on the Exchanges page) sends ONE Recovery
  Request (U2): 18002=Y (what TT has not delivered since its weekly reset,
  Sat 22:00 UTC), or 916/917 from when Order Routing was last heard if that
  is before the reset — never both (TT refuses). The 8s and 9s it replays go
  through the Order Routing handlers: a fill already booked is never booked
  again (`Database.has_fill`, the manual fills table, the router's ids), an
  order is never moved back from a final state, and CumQty only grows. TT
  ends it with a Logout "Recovery completed"; a refusal (j naming U2) or no
  answer in `RECOVERY_TIMEOUT_SEC` is said in TT's words on the Reconciler
  tab, never read as done. Algo ENTRIES wait while it runs; exits never do.
  Beyond TT's 720 hours, or a Trade Capture Report (AE), Recovery cannot
  reach: said, with the statement as the remedy.
- **What an Execution Report IS is decided before anything is booked**
  (`tt_exec.kind`, both books): an exchange-listed spread's fill comes as
  the spread (442=3 or 1) AND one report per LEG (442=2) at the leg's price
  — only the spread is booked; a trade bust or correction (20=1/2, 150=H/G)
  is SAID, never booked as a new fill; a status (20=3, 150=I/D) is not a
  fill. A fill is known by TT's UniqueExecID (16612), or 17 with TT's
  TradeDate (75) — never a date that depends on an optional tag. A reject
  says OrdRejReason (103) in words.
- **TT FIX Order Routing does not support Request For Positions (AN)** —
  it is not among its messages, and TT asks clients to send nothing it does
  not list. `AlgoOrderRouter.ASK_POSITIONS` is off: positions are unknown,
  with the reason, until a TT Drop Copy session provides them.
- **TT positions: asked for, never assumed.** A Request For Positions (AN)
  goes at each logon. Answered (AO/AP), `positions()` is the account and the
  book reconciles; refused (j, or a 35=3 naming AN — which must NOT stop the
  session) or unanswered in `POSITIONS_TIMEOUT_SEC`, it is None — unknown,
  never flat. LIVE on unknown positions is the trader's explicit word that
  this book's fills are the record (`positions_waived`), said in a banner
  for as long as it holds; without that word automatic trading on a live
  venue is refused.
- **Startup sweep, once the session is up**: orders of ours (`FT-`) a
  previous run left recorded as working are adopted — so a fill for one is
  applied as what it was — and cancelled. Nothing else at the venue is
  touched. An adopted order the venue no longer knows is let go on its
  cancel reject.
- **A refused entry waits at least `ENTRY_RETRY_SEC`** before it is tried
  again, whatever the contract's cooldown — the same refused order is never
  sent three times a second — and "Last order" shows the refusal in the
  venue's words, giving the trade back to the day's count.
- **The touch study (Analysis) keeps its own rolling time window**
  (`stats.StatsWindow`), resumed across a restart only if the gap is short
  (`RESUME_MAX_GAP_MINUTES`). It is a reading for the Analysis window, not
  the Algo's band.
- **Hurst is computed on the INCREMENTS, not the levels**, and is a reading
  only. R/S over a price series reads ~1.0 for everything, a random walk
  included.
- **Statistics stay live until the window is warm.** The update interval holds
  the bands still for a trader to aim at; applied during warm-up it froze a
  sigma computed from two samples.
- **A level that reverts is not a level that pays.** The inner bands always
  revert more often — a spread one sigma from its mean comes back more
  reliably than one at three — and they are also the ones whose move cannot
  cover the round trip. Any "best level" must clear its costs, or the Analysis
  window invites lowering the threshold onto something that reverts
  beautifully and loses money every time.
- **The replay and the backtest are SIGNAL replays and say so on their own
  face.** They build candles from the recorded mids and run them through
  `backtest.run` — the live `AlgoSignal`, `judge_filters` and `levels`, never
  a second implementation of a rule. The book either side of the mid was never stored, so the
  spread is a stated assumption printed under the table; costs are the
  configured BUDGET and `slippage_measured` is None for ever; there is no
  queue. Where every entry was withheld it reports the signal's own words,
  because "nothing crossed the threshold" when the edge filter was the
  blocker sends the desk to change the number that was never the problem.
- **Slippage is measured from the DECISION price** (`slippage.py`, ported
  from the MT5 desk): each position keeps `entry_slippage` and
  `exit_slippage` in price points against the executable price when the
  Algo decided — or the touch when a close was pressed — with the order type
  each end went as. Positive is a COST at both ends (`slip` takes the side
  of the order that paid). An unfilled limit escalated to market keeps its
  ORIGINAL decision and is labelled `LIMIT escalated`: the wait is part of
  what it cost. A PAPER fill is made at the decision price by construction —
  None, never a perfect 0.00. The Analysis Costs card and the slippage
  report read the SAME measure; a trade recorded before decisions were kept
  falls back to its fills' send-touch figure. A round turn counts only with
  both ends measured, and an open position has no exit, not an unmeasured
  one.
- **A manual ticket's slippage** is its fills against the touch when it was
  SENT — the offer for a buy, the bid for a sell (`ManualTerminal._touch`).
  No fresh quote then means unmeasured, never zero. It is shown on the
  Executions table and joins the Analysis slippage card under live only.
- **Slippage can be NEGATIVE** — the market moving our way between the price an
  order was aimed at and the fill. That is a price improvement, not a cost, and
  a budget is never negative: proposing one would have the edge filter pay the
  desk to trade.
- **`Position.qty` is what REMAINS; `opened_qty` is the size it was opened at.**
  Closing decrements the first, so a journal reading the wrong one reports
  every trade as size 0 — and every cost figure fed from it as unmeasured.
- **The engine reads `config.json` back while it runs.** Settings are edited
  in the WEB process, which writes that file and nothing else; the runner
  watches it and calls `Engine.apply_config`. Three rules hold there: the
  LIVE switch wins over the file (`algo_on` is never adopted, or a reload
  stands down a contract the trader just armed); nothing touches the book, a
  position or an open order; and a change the running engine cannot adopt —
  a contract added or removed, a tick value, `enabled`, `DATABASE_PATH` — is
  REPORTED in `config_restart_needed`, never half-applied. A setting that
  looks saved and is not in force is worse than one that plainly says it
  needs a restart.
- **Only one engine may run against a book.** Two trade the same signals on the
  same account and each sees the other's fills as positions it cannot explain.
  The runner refuses to start when the snapshot is being published already.
- **Every test that asserts a guard withholds something needs a control** that
  turns the guard off and asserts the opposite.
- **No native `alert()` / `confirm()` / `prompt()`.** One shared modal, and a
  test fails the build if they come back.
- **The page polls twice a second, so `networkidle` never fires.** In browser
  tests wait for `domcontentloaded` and then for the thing you care about.
- **Every window is its own stacking context.** A child with a `z-index` — the
  sticky table header in Positions and Analysis — is otherwise placed against
  the whole page and paints straight through any window drawn over it. And a
  desk of draggable windows needs click-to-raise, or one of them can never be
  read.
