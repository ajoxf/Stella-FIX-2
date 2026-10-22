# Build Prompt: FIX-Trader — a multi-contract stat-arb monitor and execution terminal

> **Paste this whole file as the first message into a fresh session on the
> `ajoxf/FIX-Trader` repository.** It is self-contained: assume the model
> reading it has never seen `Stat_Arb_W3_Wsckt` or `MT5-Trader`. Where those
> two are named below it is to say what was learned there, not to send you
> looking.

---

## 0. Mission

Build a **screen that watches several exchange-listed spread contracts at once,
runs one mean-reversion algo per contract, and executes over FIX**.

It is one page of small windows. One window per contract. Each window is
numbers only — mean, standard deviation, z-score, the edge filter, position,
P&L — with an **ALGO ON/OFF** switch and a **gear** that opens that contract's
own settings. No ladders. No charts. No manual click-to-trade.

Three things make this different from a general trading terminal, and every
design decision below follows from them:

1. **One contract, not two legs.** Orient Futures lists the spread itself as a
   tradeable instrument. There is no leg A and leg B, no hedge ratio, no
   matched-clip arithmetic, no naked-leg window, no orphan-leg recovery. A
   spread is bought and sold like an outright. This deletes the hardest half of
   a spread trading system — do not reintroduce it.
2. **The trader does not trade on this screen.** Discretionary orders go
   through TT's own front end. This screen exists to run the algo and to show
   why it is or is not trading. The only buttons that send anything are the
   algo switch, a per-contract **CLOSE NOW**, and the global **KILL ALL** — the
   last two are safety, not trading.
3. **Density over decoration.** Old-school: grey window chrome, a bottom
   taskbar, mono figures, blue bid / red ask, no web font, no CDN, no
   animation. A trader watching eight of these at once reads them in a glance
   or the screen has failed.

**This phase builds the main page, the Settings, and the Exchanges page.**
FIX connectivity is **stubbed behind an interface** — see §9. Build the seam
now so that dropping the real session in later touches exactly one module.

---

## 0.1 The screens, and what the operator has already settled

**[`docs/screens.html`](docs/screens.html) is the design reference.** All six
screens are drawn there at desk scale, in the terminal's real visual language,
with every window state the system can be in. Open it before writing any
markup: where this document and that file disagree about layout, the file wins.

Four decisions were put to the operator against those screens and answered.
They are settled — build to them, do not re-open them:

1. **The profit target is a percentage of INITIAL MARGIN.** Notional and
   sigma-at-entry remain in the code as selectable bases (§6.4) because a venue
   that cannot report margin has to fall back to something, but **margin is the
   default and the one the desk trades on**. Where margin cannot be read, the
   window shows an em dash and names what is missing — it does not quietly
   switch basis.
2. **Entry and exit are each independently MARKET or LIMIT, chosen per
   contract.** This is a widening of what was first proposed: the limit
   machinery is not an entry-only path. It must serve a **closing** order too —
   see §6.5, which is the part of this build with the most room to go wrong.
3. **CLOSE NOW and KILL ALL stay on the screen.** Per-contract close, and a
   global switch that stands every algo down and cancels our working orders.
   Both ask once. Nothing else on the screen sends an order.
4. **Six to ten contracts on screen at once.** Windows are sized so eight fit a
   1920×1080 desk with room — roughly 270×415 px. Every field in §2.2 is
   always on the window at that size; nothing hides behind a hover.

**Still open, and to be asked rather than assumed** (they are listed on the
last panel of `docs/screens.html`): whether the z-strip stays; how Orient
identifies a spread contract (`Symbol(55)` alone, `SecurityID` +
`SecurityExchange`, or a multi-leg definition); whether market data is a
separate FIX session; whether a separate account/margin window is wanted
beyond the per-contract position line; and whether Telegram is worth wiring.
None of them blocks this phase. Where one is reached, build the version drawn
in `docs/screens.html` and leave the seam obvious.

---

## 1. What a "contract" is here

One row in configuration, one window on the screen:

| Field | Meaning | Example |
|---|---|---|
| `key` | stable id, derived from the symbol | `sgx_fef_q1q2` |
| `name` | what the trader calls it | `FEF Jan/Feb` |
| `symbol` | the FIX `Symbol` (55) as the venue names it | `FEFF6-FEFG6` |
| `security_id` / `security_exchange` | (48) / (207), where the venue needs them | |
| `exchange` | which configured venue/session routes it | `SGX-UAT` |
| `tick_size` | minimum price increment of the spread | `0.01` |
| `tick_value` | money per tick per contract, in `currency` | `1.00` |
| `contract_multiplier` | (231) where required | `100` |
| `currency` | the contract's money | `USD` |
| `min_qty` / `qty_step` / `max_qty` | order size bounds | `1 / 1 / 50` |
| `session_open` / `session_close` | venue trading hours, venue clock | `09:00` / `17:15` |

**Everything above is read from the venue where the venue publishes it**
(security definition), cached into config so the screen can render before the
session is up, and shown with its derivation. A number the operator typed and a
number the venue reported must be visually distinguishable — the typed one is
an override and says so.

**Price is the spread price directly.** No `Leg B − beta × Leg A` anywhere in
this codebase. If you find yourself writing a hedge ratio, stop: you are
building the wrong system.

**The mid is the mid of the book** — `(bid + ask) / 2` from the top of book —
never the last trade. Levels and triggers read the **executable side for their
own direction**: a long entry is priced off the ask, a short off the bid, and
an open position reads the **opposite** side to close. Money from spread points
is always `points × tick_value / tick_size × quantity`, and that conversion
lives in exactly one function.

---

## 2. The screen

### 2.1 Layout

```
┌ NEXUS FIX  ·  spread terminal ───────────────────────────────────────────┐
│ [banner: session down / unclaimed position / warm-up]                    │
│                                                                          │
│  ┌ FEF Jan/Feb          ⚙ × ┐  ┌ SGXTF Q1/Q2         ⚙ × ┐              │
│  │ ALGO  [ ON  ]   ARMED     │  │ ALGO  [ OFF ]   IDLE     │              │
│  │ Bid    12.25  Ask  12.30  │  │ Bid   −3.10  Ask  −3.05  │              │
│  │ Mid    12.275             │  │ Mid   −3.075             │              │
│  │ Mean   11.980  σ  0.412   │  │ Mean  −3.220  σ  0.180   │              │
│  │ Z      +0.72              │  │ Z     +0.81              │              │
│  │ Bands  ±2.00 → 11.16 / 12.80  │ ...                     │              │
│  │ Edge   1.8×  PASS         │  │ Edge  0.7×  BLOCK        │              │
│  │ H 0.41 MEAN-REV  HL 22    │  │ ...                      │              │
│  │ Warm   100%  (400/400)    │  │ Warm   62%  (248/400)    │              │
│  │ Pos    SHORT 5 @ 12.640   │  │ Pos    flat              │              │
│  │ Target 12.310  (+0.35%)   │  │ Target —                 │              │
│  │ P&L    +$412  open        │  │ P&L    —                 │              │
│  │ [CLOSE NOW]               │  │ [CLOSE NOW]              │              │
│  └───────────────────────────┘  └──────────────────────────┘              │
│                                                                          │
├ [+] [FEF Jan/Feb] [SGXTF Q1/Q2] [Blotter]   LINK: ok  Settings  KILL ALL ─┤
└──────────────────────────────────────────────────────────────────────────┘
```

- Windows **move by their title bar**, resize from a corner grip, and where the
  trader puts them is where they are after a reload (persist per contract key
  in `localStorage`). **Tidy** in the taskbar puts them back in a row.
- A contract saved on the Exchanges page **gets its window by itself**, beside
  the ones already open, as soon as the engine picks it up — no reload. A
  window that is closed stays closed; the **+** menu lists every contract, the
  Blotter and the Positions monitor, to open one again.
- **Eight windows must fit a 1920×1080 screen without scrolling.** That is the
  size budget: roughly 300×360 px each. If a field does not earn its row,
  delete the row.

### 2.2 Every field in a contract window

Grouped as the mock above. Each is a number or an em dash — **never a zero
standing in for something unmeasured**.

**Header** — name, an ⚙ (this contract's settings), an × (close the window),
and a state badge: `IDLE` (algo off) / `WARMING` / `ARMED` (algo on, watching) /
`WORKING` (an order is live) / `IN` (position open) / `BLOCKED` (algo on, a
filter is holding it back) / `HALTED` (a guard or the kill switch).

**Market** — `Bid`, `Ask`, `Mid`, and the bid/ask **sizes**. Age of the last
update in the footer; a quote older than `MAX_QUOTE_AGE_SEC` turns the whole
market block grey and the badge to `HALTED`.

**Statistics** — `Mean`, `σ`, `Z`, and the two **band prices** the z thresholds
correspond to, in the contract's own price, because a trader checks a level
against the market and not against a z-score.

**Filters** — `Edge` (σ over round-trip cost, as a multiple, with PASS/BLOCK),
`H` (Hurst, with the regime word), `HL` (half-life in periods). Each shows the
threshold it is being judged against on hover.

**Warm-up** — percentage and `have/need` samples. Until the full lookback is
collected the algo **must not enter**, and the badge says `WARMING`.

**Position** — side, quantity, average price, and the ticket/order ids behind a
hover. `flat` when flat.

**Target and exit** — break-even (entry ± all costs) and the take-profit price,
which is break-even plus `PROFIT_TARGET_PCT` (§6.4). Both in the contract's own
price. Also the stop-loss z level and the price it currently sits at.

**P&L** — open P&L marked at the **closing side** of the book, net of costs
still outstanding; and today's realised beside it.

**Actions** — the algo toggle, and `CLOSE NOW` (asks once).

**Footer** — quote age, last event, and any refusal in the venue's own words.

### 2.3 The other windows

- **Blotter** — every order and every fill, newest first: time, contract, side,
  qty, price, state, venue order id, and the venue's own text on a reject.
  CSV export.
- **Positions** — one row per contract plus a total: position, average price,
  open P&L, realised today, and the account's margin where the venue reports it.
- **Events** — the log the banners are drawn from, filterable by contract.
- **Analysis** — §2.6. Its own window, one contract at a time.

### 2.3a The window marks its own position

A trader watching eight windows should not have to read eight of them to find
the one that just did something.

- **In a position → the window is BLUE.** A state, not an event: it is read
  straight off the snapshot, so it is still right after a page reload and it
  can never stick on a contract that is flat.
- **Closed in profit → a GREEN flash. Closed at a loss → a RED flash.** Both
  fade after a couple of seconds. A window left red says "this is losing",
  which is a different statement from "the last trade lost".
- **Break-even, or a net nothing could measure → NEITHER.** A P&L of zero is
  not a win, and an unmeasured one is not a loss; colouring it red would
  state a loss the system never measured. Both get a neutral grey mark.

The close arrives in the snapshot as `last_close = {seq, net, side, qty,
price, reason, ts}`, and the screen marks off `seq` changing — never off the
wording of `last_event`. A highlight driven by a regex over a sentence stops
working, silently, the day somebody rewords the sentence.

The colour tints the window's frame, titlebar and border. It is never a layer
over the content: every price, z and P&L keeps the panel and the contrast it
had, because a tint that makes a number harder to read has cost more than it
gave. First sight of a contract never flashes, or reloading after a session
would flash every window at once for trades nobody was watching.

### 2.4 Notifications

Every one of these fires a **toast** and a **sound**, and lands in the events
feed and the blotter:

| Event | Toast | Sound |
|---|---|---|
| Order sent | `FEF Jan/Feb · SELL 5 @ 12.64 sent` | one blip |
| Order acknowledged | quiet (badge → `WORKING`) | — |
| **Filled** (fully) | `FILLED SELL 5 @ 12.641` | two rising notes |
| Partially filled | `PART 3/5 @ 12.641` | one blip |
| **Position opened** | `OPEN SHORT 5 @ 12.641 · z +2.14` | two rising notes |
| **Position closed** | `CLOSED SHORT 5 @ 12.310 · +$412 net` | two rising notes |
| Cancelled / expired | `CANCELLED` | low note |
| **Rejected** | the venue's own text, verbatim | low note |
| Guard withheld an order | the guard and its threshold | low note |
| Session down / restored | banner, not toast | low note / — |

Sound is generated **in the page with WebAudio** — nothing that a blocked
network can silence — and is one click off in the taskbar. Errors and rejects
**stay until dismissed**; a failure that vanishes in three seconds is one the
operator misses.

### 2.5 Visual rules

- `--bid: #4a9ede`, `--ask: #b83232`, `--traded: #4a9c5d`, frame `#cfcfcf`,
  text `#1c1c1c`. Bid is blue and ask is red **everywhere** — a price must not
  change colour depending on which panel it sits in.
- Figures are monospaced and right-aligned, fixed to the contract's own
  decimals so the digits do not jump.
- Positive P&L green, negative the red above, flat plain.
- **Self-hosted only.** No CDN, no web font, no framework. Vanilla JS. A
  dialog that reports "could not save" must work when the network is what
  failed.
- **No native `alert()` / `confirm()` / `prompt()`, ever.** One shared modal.
  Write a test that fails the build if they come back.

### 2.6 Analysis — the feedback loop, per contract

Ported from the Stat-Arb system's **Analysis** tab, and scoped where that one
was not: **one contract at a time**. A single blended win rate across eight
contracts tells you nothing about which one to turn off, which is the only
question this window exists to answer. It opens from the window's own menu or
the taskbar **+**, floats like everything else, and has two sub-tabs — the
named contract, and **All contracts**.

Filters across the top, applying to everything below: **period** (since
inception / 30d / 7d / today) and **live, simulated, or both** — simulated
fills from `FakeGateway` must never be blended into a live P&L figure without
being asked for.

**Closed trades only.** An open position is named in the footer and excluded
from every statistic; a system that counts an open winner is a system that
flatters itself.

**1. The tiles.** Trades (won / lost), win rate, net P&L after costs, average
per trade with the average win and average loss beside it, expectancy, **return
on margin** (the same base the profit target uses, §6.4), cost drag as a
percentage of gross, average hold with the half-life beside it, and the worst
losing run.

**2. The standard-deviation touch study.** The original recorded touches and
counted them. That is not enough to act on, so this one records what happened
*after* each touch, per level (±1, ±2, ±3):

| Column | Meaning |
|---|---|
| Touches | crossings, **counted once per crossing**, not once per tick |
| Reverted | share that reached the rolling mean within **2× the half-life** |
| Median | median time from the touch to the mean |
| Adverse | median further adverse move before it turned, in σ |
| Traded | how many became an entry (the rest were blocked, or the algo was off) |

That table is what says whether ±2.00 is the right threshold **for this
contract** — a level that reverts 86% of the time in 24 minutes is a different
instrument from one that reverts 57% of the time in 44 minutes having gone 1.2σ
further against you first.

**A touch still open at the end of the window is `unresolved`, not a failure.**
Count it separately, exclude it from the percentage, and say how many there
were. Rolling an unfinished touch into the denominator as a miss understates
every level, and understates the widest levels most, because those are the ones
still running.

**3. How they ended.** Count and net money by exit reason: profit target,
stop-loss, session flat, time stop, CLOSE NOW, KILL ALL.

**4. Costs — budget against measurement.** Commission, exchange and clearing
fees as charged, and **the slippage budget beside the slippage actually
measured**, per side and per round trip, in money and in ticks. This closes the
loop that the settings page opens: the edge filter (§6.3) refuses entries using
the *budget*, so a budget that is too generous silently refuses trades that
would have paid. Where the two differ, say so in words and offer the measured
figure as a one-click correction — **never apply it silently**.

**5. The trade journal**, this contract only: entry and exit time, side,
quantity, **z at entry and at exit**, price in and out, gross, costs, net,
return on margin, hold time, exit reason. Every column is **what was recorded at
the time**, never recomputed now — the z at entry is the z the decision was
actually made on, and a later change to the lookback must not rewrite history.
Empty cells where nothing was measured, never zeros. CSV export.

**6. All contracts.** One row per contract — trades, win rate, net, average,
return on margin, cost drag, average hold, the level that reverts best — plus a
labelled total row. **A contract with fewer than ten closed trades is marked
`too few to judge` and given no verdict.** Six losing trades is not evidence,
and a page that says so is worth more than one that ranks noise.

**What this requires of the rest of the build**, and it is the reason this
section sits in phase 1 rather than being bolted on later:

- `stats.py` must **emit a touch event on every crossing** of ±1/±2/±3 and
  record the state at that instant (level, direction, mid, z, mean, σ,
  half-life, whether the algo was armed). Resolution — reverted, unresolved, or
  timed out — is written later against the same row.
- Every trade row must carry **entry z, exit z, entry mean and σ, margin locked,
  fees actually charged and slippage actually measured**. None of these can be
  reconstructed after the fact, so they are written when the fill arrives or
  they do not exist.
- The **exit reason is a stored field on the position**, set by whatever closed
  it, not inferred from prices afterwards.

---

### 2.7 The replay — what a different threshold would have done

The touch table in §2.6 says which level REVERTS most. It is always an inner
band, and that is the finding that loses money if it is acted on directly.
The replay answers the question that follows: what did each level MAKE, after
this contract's own round trip.

It re-runs `stats.StatsWindow`, `signals.entry_signal` and
`signals.exit_signal` — the desk's own code, never a second implementation —
over the mids the engine recorded, and charges the configured round trip
against every trade. A threshold changed in `signals.py` changes what the
replay says on the same commit.

**What it is honest about, on the page, under every table:**

- **It is a SIGNAL replay, not a fill simulator.** The card says so.
- **The book was not recorded** — only the mid. The spread is an ASSUMPTION,
  one tick by default, and it is not cosmetic: an exit reads the executable
  side, so a replay run on the mid would flatter every exit by half a spread.
- **Costs are BUDGETED.** `slippage_measured` is None and stays None. The
  measured figure lives in §2.6 beside the budget, and a backtest reporting a
  cost it never paid is how a budget stops being corrected from data.
- **There is no queue.** A limit entry is treated as filled where its signal
  fired, which flatters a limit strategy.
- **A position open at the end is excluded** from every P&L figure and
  counted separately.
- **Fewer than ten closed trades is not a finding**, and such a row can never
  be named best however much money is against it.
- **Where every entry was withheld, the SIGNAL'S OWN WORDS are reported** —
  "edge 1.1x, sigma against a round trip of…", not "nothing crossed the
  threshold". The second is a different answer, and it sends the desk to
  change the number that was never the problem.

`GET /api/replay/<key>?period=&thresholds=` returns the sweep. The best row
is the one that PAID with enough trades behind it, and where nothing cleared
its costs the window says that outright rather than leaving a blank.

---

## 3. The Exchanges page

This is where the system is connected, and in this phase it is the page that
must be genuinely finished. It is a normal page in the browser (not a floating
window), reachable from the taskbar.

### 3.1 Venues and FIX sessions

One card per venue. Each carries a **session** — because FIX is what connects
here, the credential fields are session fields:

| Field | Notes |
|---|---|
| Name | `Orient SGX UAT` |
| Broker / venue | `Orient Futures` |
| **Environment** | `UAT` or `PROD` — a **radio, not a checkbox**, and PROD is never the default |
| Host / Port | order-entry endpoint |
| Market-data host / port | where a venue separates them; blank = same session |
| `SenderCompID` / `TargetCompID` | |
| `SenderSubID` / `TargetSubID` / `OnBehalfOfCompID` | optional |
| FIX version | `FIX.4.2` / `FIX.4.4` / `FIXT.1.1 + FIX50SP2` |
| Username / Password | logon (553/554) |
| Account | (1) stamped on every order |
| Heartbeat interval | default 30 |
| Reset sequence on logon | tick |
| TLS | on/off, plus a CA bundle path |
| Data dictionary | path to the venue's custom XML |
| Store / log paths | where sequence files live |

**Credentials live only in `.env`.** The saved venue holds the **name of the
environment variable**, never the value. Nothing secret is ever written to
`config.json`, returned by an API, printed in a log line, or put in a toast. A
password field returns `***` on read and is only written when non-empty.

**UAT and PROD are never the same row.** They are two venues with two sets of
credentials, and the screen says which one it is at all times: a **PROD**
badge in the taskbar, red, and every window's title bar carries it. A UAT
screen must never be mistakable for a live one, and neither must the reverse.

### 3.2 The three buttons

Ported from what worked before, one line each and every failure carrying the
step that fixes it:

- **Connect** — is the gateway process there, and is the session logged on?
  Reports the FIX state machine's own words (`Logon sent`, `Logout: MsgSeqNum
  too low, expecting 42 but received 7`).
- **Test** — can it actually trade? Session up, entitlements present, the
  account accepted, the clock inside the venue's tolerance.
- **Diagnose** — everything: sequence numbers both ways, last heartbeat, the
  data dictionary loaded, every configured contract resolved against the
  venue's security definitions (tick size, multiplier, currency, trading
  status) with any disagreement named and offered as a one-click correction —
  **never applied silently**.

At the top of the page, one line says whether the system is **CONNECTED** or
names the single thing standing in the way.

### 3.3 Contracts

A table under the venue cards: add, edit, delete. **New contract** takes the
venue, a symbol (with a **Find** that searches the venue's security list once
FIX is live, and accepts a typed symbol before then), and a **Read from venue**
button that fills tick size, tick value, multiplier, currency and size bounds
from the security definition — each shown with where it came from. Save writes
the contract, and the window appears on the main page within a few seconds.

Deleting a contract that has an open position is **refused**, naming the
position.

---

## 4. Settings

Two levels, and the distinction is load-bearing:

- **Per contract** (the ⚙ on each window, and an editor on the Exchanges
  page). Everything about how *this* instrument is traded.
- **Desk-wide** (the Settings page). Everything about how the *system* runs.

A per-contract field **left blank means "use the desk default"**, and `0` is a
real number, not a blank. Render the effective value beside the box in grey.

### 4.1 Per-contract settings

Port the full set, because the comprehensiveness is the point:

**Signal**
- `lookback_period` (samples in the rolling window; default 400)
- `stats_update_interval_sec` (how often mean/σ are recomputed; `0` = every
  update. Default 300 — stable bands are easier to trade against)
- `entry_threshold` (|z| to enter; default 2.0)
- `exit_signal_mode`: `profit` (default — §6.4) / `zscore` / `hybrid`
- `exit_threshold` (|z| to exit, used by `zscore` and `hybrid`; default 0.5)
- `stop_loss_z` (|z| emergency exit; default 4.0)
- `max_hold_minutes` (0 = no time stop)

**Filters**
- `hurst_enabled`, `hurst_threshold` (default 0.5 — H below it is
  mean-reverting and tradeable)
- `edge_filter_enabled`, `min_std_multiple` (σ must be at least this many times
  the round-trip cost; default 1.5)
- `half_life_enabled`, `max_half_life` (reject a window whose reversion is
  slower than the intended hold)
- `min_book_size` (contracts on the touch, both sides, before an order is sent)
- `max_book_spread_ticks` (refuse to cross a book wider than this)

**Sizing and risk**
- `quantity` (contracts per entry)
- `max_position` (contracts; the hard ceiling this contract may reach)
- `max_trades_per_day`
- `daily_max_loss` (money; hitting it turns this contract's algo OFF and says so)
- `entry_cooldown_seconds` (default 60)

**Execution** — entry and exit are set **independently**, and each may be
either type. Every combination must work, including limit-in / limit-out:
- `entry_order_type`: `LIMIT` (default) / `MARKET`
- `exit_order_type`: `MARKET` (default) / `LIMIT`
- `entry_limit_offset_ticks` / `exit_limit_offset_ticks` (how far behind its own
  touch each is priced; separate, because patience going in and patience coming
  out are different decisions)
- `entry_limit_timeout_sec` / `exit_limit_timeout_sec` (unfilled after this →)
- `entry_on_timeout` / `exit_on_timeout`: `CANCEL` / `CROSS_AT_MARKET`
  (default `CANCEL` for an entry, `CROSS_AT_MARKET` for an exit — a missed
  entry is a trade not taken, a missed exit is a position you still hold)
- `repeg_dead_band_ticks` (how far the touch must move before an amend; every
  amend costs queue position)
- `time_in_force`: `DAY` / `IOC` / `GTC`
- `session_flat_at` (venue clock; blank = hold)

**Costs** — every one of these feeds the edge filter and the profit target:
- `commission_per_contract` (per side)
- `exchange_fee_per_contract` (per side)
- `clearing_fee_per_contract` (per side)
- `slippage_budget_ticks` (per side — a **budget**, shown beside the
  **measured** realised slippage so it gets corrected from data)
- `profit_target_pct` (§6.4)

**Display**
- decimals, window title, and whether this contract is enabled at all.

### 4.2 Desk-wide settings

- `PRICE_REFRESH_SEC` = **0.5** — the UI snapshot interval, and the headline
  requirement. The engine may compute faster; the screen refreshes twice a
  second. It is a setting, not a constant, and it is on the page.
- `ENGINE_POLL_SEC` = 0.1, `COMMAND_POLL_SEC` = 0.02 (a switch flipped in the
  UI must not wait for a price poll)
- `MAX_QUOTE_AGE_SEC` = 15 (0 = off) and `MAX_PRICE_JUMP_SIGMA` = 5,
  `JUMP_SETTLE_SEC` = 2
- `ALGO_MASTER_ENABLED` — the one switch that stands every contract's algo down
  at once. Deliberately not per contract: a switch you have to find eight times
  is a switch that gets missed once.
- `DEFAULT_*` for every per-contract field above
- `DAILY_MAX_LOSS_TOTAL`, `MAX_OPEN_CONTRACTS`
- `SHUTDOWN_CLOSE_POSITIONS`: `ask` / `always` / `never` — **an unanswered
  prompt means NO**
- `SOUND_ENABLED`, `ROW_DENSITY`, `CONFIRM_CLOSE`
- Notifications: Telegram bot token / chat id (token from `.env`), and which
  events go out (fills, opens/closes, rejects, errors)

Saving is **atomic** — write a temp file and `os.replace`. A plain
`open(path, 'w')` truncates, and a reader in that window has seen half a config
and written an empty one back. Name the settings that need an engine restart
and hot-apply the rest; crying "restart" on every save teaches the operator to
ignore the line that matters.

---

## 5. Architecture

```
FIX-Trader/
├── README.md
├── BUILD_PROMPT.md              # this file
├── CLAUDE.md                    # the hard rules, for future sessions
├── start.py                     # one command: config, gateway, engine, web, browser
├── config.example.json
├── .env.example
├── requirements.txt
├── fixtrader/
│   ├── __init__.py
│   ├── config.py                # venues, contracts, settings; atomic save; .env keys
│   ├── models.py                # dataclasses and enums, no logic
│   ├── gateway.py               # THE ONLY MODULE THAT MAY IMPORT quickfix  (§9)
│   ├── fake_gateway.py          # a real book and a real fill model, for tests and UAT-less dev
│   ├── marketdata.py            # top of book per contract, staleness and jump guards
│   ├── stats.py                 # rolling mean/σ/z, Hurst, half-life
│   ├── signals.py               # the algo: entry, exit, filters — pure, no I/O
│   ├── costs.py                 # round-trip cost, break-even, profit target
│   ├── sizing.py                # ticks ↔ money, the ONE conversion
│   ├── executor.py              # order lifecycle: send, amend, cancel, escalate
│   ├── book.py                  # our orders and positions, persisted
│   ├── reconcile.py             # what the venue says vs what we think
│   ├── engine.py                # the loop: one pass per contract, one snapshot out
│   ├── commands.py              # web → engine bridge, primed at startup
│   ├── database.py              # SQLite (WAL, 30s busy timeout)
│   ├── session.py               # venue clock, trading hours, the flat-at cutoff
│   ├── notify.py                # events → toasts, sound, Telegram
│   ├── webapp.py                # Flask: it renders and it asks; it never trades
│   ├── static/  (app.js, settings.js, terminal.css, favicon.svg)
│   └── templates/ (index.html, settings.html, exchanges.html)
└── tests/
```

**Process shape.** The web process and the engine are separate. The web process
renders a snapshot and writes commands; the engine reads commands and trades.
A crashed browser must never be able to stop an exit, and a crashed engine must
be *visible* — a dead engine is never to be mistaken for a quiet market. That is
what the banner is for.

**The snapshot** is one JSON document per pass, written atomically, containing
everything every window needs. Shape it once and hold to it:

```jsonc
{
  "ts": "2026-09-07T13:22:41.500Z",
  "engine": {"alive": true, "loop_ms": 96, "master_algo": true,
             "environment": "UAT", "venues": [{"name": "...", "state": "LOGGED_ON",
             "seq_out": 812, "seq_in": 1904, "last_heartbeat_sec": 3.1}]},
  "contracts": [{
    "key": "sgx_fef_q1q2", "name": "FEF Jan/Feb", "state": "IN",
    "algo_on": true,
    "market": {"bid": 12.25, "ask": 12.30, "bid_size": 40, "ask_size": 25,
               "mid": 12.275, "age_sec": 0.4, "stale": false},
    "stats": {"mean": 11.98, "std": 0.412, "z": 0.72,
              "upper_band": 12.804, "lower_band": 11.156,
              "hurst": 0.41, "regime": "MEAN_REVERTING", "half_life": 22.0,
              "samples": 400, "need": 400, "warm_pct": 100,
              "next_stats_update_sec": 143},
    "filters": {"edge_ratio": 1.8, "edge_ok": true, "hurst_ok": true,
                "book_ok": true, "blocked_reason": null},
    "costs": {"round_trip_money": 18.40, "round_trip_ticks": 1.84},
    "position": {"side": "SHORT", "qty": 5, "avg_price": 12.641,
                 "break_even": 12.585, "target": 12.310, "stop_price": 13.628,
                 "open_pnl": 412.0, "realised_today": 130.0,
                 "opened_at": "..."},
    "orders": [{"id": "...", "side": "BUY", "qty": 5, "price": 12.31,
                "state": "WORKING", "venue_id": "..."}],
    "last_event": {"kind": "FILL", "text": "SELL 5 @ 12.641"}
  }]
}
```

**Unknown is not empty.** A read that failed returns `None` and renders `—`.
Code that treats "could not read the position" as "flat" will report a clean
account while the money sits at the venue. Every function that can be
uncertain must be able to say so, and every test that asserts a value must have
a sibling asserting the em dash.

---

## 6. The algo

One instance per contract, entirely independent of every other. Pure functions
in `signals.py`, given a state and returning an intent; the executor is the
only thing that sends.

### 6.1 The series
Every market-data update appends the **mid of the book** to the contract's
rolling window (`maxlen = lookback_period`). Reject an update with a crossed or
one-sided book, and say so rather than dropping it silently.

### 6.2 Statistics
`mean` and `std` (sample, `ddof=1`) are recomputed every
`stats_update_interval_sec` — **not every tick** — so the bands stay still
enough to trade against. **`z` is recomputed on every update** against the
current mid and the cached mean/σ. Hurst (R/S over the window) and half-life
(OLS on the OU process, `ln2/θ`, `inf` when θ ≤ 0) recompute with the stats.

### 6.3 Entry
With **no position**, the algo is armed, the window is **fully warm**, and the
master switch is on:

```
z >= +entry_threshold   → SELL the spread   (short: expect reversion down)
z <= -entry_threshold   → BUY  the spread   (long:  expect reversion up)
```

Blocked, and the reason shown on the window rather than in a log, when:
Hurst above threshold · edge ratio below `min_std_multiple` · half-life above
`max_half_life` · |z| already past `stop_loss_z` (too late, not early) · the
book too thin or too wide · inside `entry_cooldown_seconds` of the last trade ·
`max_position`, `max_trades_per_day` or a daily loss limit reached · outside
session hours · a stale or jumping quote.

**Filters gate entries only. A filter must never prevent an exit.**

### 6.4 Exit — the fixed percentage after costs
This is the rule the operator asked for, and it is the default (`exit_signal_mode
= profit`):

```
round_trip_cost = (commission + exchange_fee + clearing_fee) × 2 × qty
                + slippage_budget_ticks × 2 × tick_value × qty

break_even  = entry_price ± round_trip_cost / (tick_value/tick_size × qty)
                   (+ for a long, − for a short: the level that returns nothing)

target      = break_even ± (profit_target_pct / 100) × basis / (…same k…)
```

`basis` is what `profit_target_pct` is a percentage **of**. **The operator has
settled this: it is `MARGIN`** — the initial margin the position ties up, read
from the venue, because that is what the trade actually costs to hold. Two
other bases stay selectable per contract for the case where a venue will not
report margin: `NOTIONAL` (price × multiplier × qty) and `ENTRY_SIGMA` (σ at
entry — the target expressed in the move being caught).

**Where the basis cannot be measured there is no target: show `—` and name
what is missing.** Do not silently fall back to notional — a target that
changes meaning without saying so is worse than no target. A target of 0.00
would read as "get out at break-even", which is a different instruction again.

The exit fires when the **executable closing side** of the book reaches the
target: a short covers on the **ask**, a long leaves on the **bid**. Never the
mid, never the last.

`zscore` mode exits at |z| ≤ `exit_threshold` instead; `hybrid` exits on
whichever comes first. **The stop-loss is always the z-score one** — it is a
safety net, not a profit-take — plus the time stop and the session cutoff.

### 6.5 The order lifecycle

**Entry and exit each have their own order type, set per contract, and either
may be MARKET or LIMIT.** One code path serves both — write it once, with the
side and the intent (`OPEN` / `CLOSE`) as arguments — because the operator can
and will run limit-in / limit-out on one contract and market-in / market-out on
the next.

A **MARKET** order crosses the executable side for its direction and is done.

A **LIMIT** order is the part of this build with the most room to go wrong:

- priced `limit_offset_ticks` from the touch on its own side, never through it;
- **re-priced only when the touch moves further than the dead band.** Every
  amend loses queue position, so re-pricing on every pass guarantees you are
  never at the front of a queue, which defeats quoting entirely;
- amended by `OrderCancelReplaceRequest`, never cancel-then-new — the second
  form gives up the queue and opens a window where the order does not exist;
- after `limit_timeout_sec` it is **cancelled or crossed at market**, per the
  contract's own setting, and the escalation is **announced** — a toast and an
  events row, never a silent change of order type;
- partial fills are the normal case, not an error. The position is whatever has
  filled; the remainder stays working under the same `ClOrdID` lineage, and the
  window shows `3/5` rather than rounding to one or the other.

**A closing limit has two rules a working entry does not:**

1. **It must be able to reduce and never to reverse.** Send it `reduce-only`
   where the venue supports the flag, and cap the quantity at the open position
   in any case. An exit that overfills opens the opposite side of a spread the
   desk thought it had left.
2. **It is not a broker-side stop, and no stop is ever attached.** It is a
   resting order this process owns, cancelled at shutdown with everything else.
   The stop-loss (§6.4) stays a level this system watches and acts on — nothing
   protective is left sitting at the venue.

**A guard may withhold a working limit's re-price; nothing may withhold the
escalation to market on a CLOSE.** If the exit limit times out and the contract
is set to escalate, it escalates — a stale quote, a jump settle, a filter, none
of them stand in the way of getting out.

Every order carries a `ClOrdID` unique across restarts, and the account and
environment it was sent under.

### 6.6 Restarts and reconciliation
Positions and working orders are written to SQLite on **every change** and
recovered at startup. Until recovery has completed against the venue the book
is marked **incomplete and the reconciler closes nothing** — a position is only
an orphan if we are sure it is not ours. Anything at the venue that recovery
cannot explain is listed **UNCLAIMED** on a red banner, never closed
automatically, with *adopt* and *close it* offered to a person.

At startup **and** at shutdown, **cancel our own working orders**, scoped to
our `ClOrdID` prefix / account so a hand order placed in TT is never touched.

---

## 7. Guards

- **Staleness** — a quote unchanged for `MAX_QUOTE_AGE_SEC` withholds orders
  and greys the window. It never blocks a close.
- **Jump** — a move greater than `MAX_PRICE_JUMP_SIGMA` σ withholds entries for
  `JUMP_SETTLE_SEC` and says so.
- **Session** — no entries outside the venue's hours, on the **venue's clock**,
  measured rather than assumed. With no measurement the cutoff **does not fire**
  and the screen says the clock is unmeasured. Unmeasured is not zero.
- **Limits** — position, daily trade count, daily loss, per contract and desk
  wide. Hitting one turns the algo off and announces which.
- **Kill** — `KILL ALL` stands every algo down and cancels every working order
  of ours. It asks once. It does **not** flatten positions unless the operator
  chooses that in the dialog.
- A refusal always carries **the venue's own words** — the FIX reject reason,
  tag and text — never "check the log".

---

## 8. Data

SQLite, WAL, 30 s busy timeout:

- `contracts`, `venues` — mirrors of config, so the UI renders with the engine
  down
- `orders` — every order, every state transition, `ClOrdID` and venue id
- `fills` — keyed `(venue, exec_id)`; two venues' identical ids must not
  overwrite each other; price, qty, fees, and the venue's own timestamp beside
  the offset from ours
- `positions` — crash-safe, with open/close prices and net P&L
- `stats_samples` — enough of the rolling series to warm up again after a
  restart without waiting for a fresh window
- `sd_touches` — one row per crossing of ±1/±2/±3 (§2.6): contract, time, level,
  direction, mid, z, mean, σ, half-life, and whether the algo was armed at the
  time. A second write resolves it — `REVERTED` with the time and the peak
  adverse excursion, `TIMED_OUT`, or left `UNRESOLVED` — and the row records
  which. **Never delete an unresolved touch to tidy the table**; it is the
  honest denominator
- `events` — the audit trail behind the banners and the events window

CSV export for fills, orders and positions, with **empty cells rather than
zeros** where nothing was measured.

---

## 9. FIX — the provision, not the implementation

**Build the seam this phase; leave the wire for the next.**

`gateway.py` is the **only** module allowed to import `quickfix` (or whatever
engine is chosen) — the same rule that kept every MT5 quirk in one file. It
exposes exactly this, and nothing above it knows what FIX is:

```python
class Gateway(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> SessionState: ...      # DOWN | CONNECTING | LOGGED_ON | ERROR (+ text)
    def subscribe(self, contract) -> None: ...
    def top_of_book(self, key) -> BookTop | None: ...   # None = unknown, NOT empty
    def security_definition(self, contract) -> SecurityDef | None: ...
    def send(self, order: OrderRequest) -> str: ...     # returns ClOrdID
    def cancel(self, clordid: str) -> None: ...
    def amend(self, clordid: str, price=None, qty=None) -> None: ...
    def orders(self) -> list[VenueOrder] | None: ...    # None = could not read
    def positions(self) -> list[VenuePosition] | None: ...
    def events(self) -> Iterator[GatewayEvent]: ...     # ack, fill, reject, logout…
```

Ship two implementations now:

- **`FakeGateway`** — a real order book with a real fill model, a real reject
  path, and a clock the tests control. Every test in the suite runs against it.
  It is also what the operator runs before UAT credentials exist, and the
  screen says `SIMULATED` in the taskbar in the same place `PROD` would go.
- **`FixGateway`** — the class, its config plumbing, its data-dictionary
  loading and its session-state reporting, with the message handlers **stubbed
  and clearly marked `TODO(fix-wire)`**. It must import cleanly with no venue
  reachable, report `DOWN` with a readable reason, and never crash the engine.

Write down, in `docs/FIX_NOTES.md`, the message set the wiring will need —
Logon(A)/Logout(5)/Heartbeat(0)/TestRequest(1)/ResendRequest(2), MarketDataRequest(V)
and its snapshot/incremental refreshes (W/X), SecurityDefinitionRequest(c)/(d),
NewOrderSingle(D), OrderCancelRequest(F), OrderCancelReplaceRequest(G),
ExecutionReport(8), OrderCancelReject(9), BusinessMessageReject(j) — and the
open questions for Orient: the exact FIX version and dictionary, whether market
data is a separate session, how spread instruments are identified (55 alone, or
48/207/167), sequence-reset policy, and their UAT endpoints and hours. **Do not
guess these into code.** A stub that says "not wired" is honest; a stub that
invents a tag is a bug with a long fuse.

---

## 10. API surface

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | the terminal |
| GET | `/api/snapshot` | the whole screen, polled every `PRICE_REFRESH_SEC` |
| POST | `/api/command` | `{action, contract, args}` → the engine; returns a command id |
| GET | `/api/result/<id>` | what the engine did with it |
| GET/POST | `/api/settings` | desk-wide |
| GET/POST | `/api/contracts` , `/api/contracts/<key>` | per contract (POST hot-applies) |
| DELETE | `/api/contracts/<key>` | refused with an open position |
| POST | `/api/contracts/<key>/read-from-venue` | fill specs from the security definition |
| GET/POST | `/api/venues` , `/api/venues/<name>` | FIX sessions (secrets write-only) |
| GET | `/api/venues/<name>/connect` , `/test` , `/diagnose` | the three buttons |
| GET | `/api/orders` , `/api/fills` , `/api/positions` (+`.csv`) | the blotter |
| GET | `/api/events` | since a cursor |
| GET | `/api/analysis/<key>` | one contract: tiles, touch study, exit reasons, costs |
| GET | `/api/analysis` | the All-contracts roll-up |
| GET | `/api/analysis/<key>/trades` (+`.csv`) | that contract's trade journal |
| GET | `/api/analysis/<key>/touches` (+`.csv`) | the raw touch rows behind the study |

Every analysis route takes `period`, `mode` (`live` / `sim` / `both`) and
honours them identically; a route that silently blends simulated fills into a
live figure is a bug, not a convenience.

Commands: `algo_on` / `algo_off` / `close_now` / `cancel_all` / `kill_all` /
`master_algo`. Every command is idempotent by id and **primed at startup** so a
restart never replays yesterday's kill.

---

## 11. Hard rules

Put these in `CLAUDE.md` as the first thing the next session reads:

- **`pytest tests/ -q` must pass before any commit**, and PROD must never be
  run without it.
- **Price is the spread contract's own price, from the MID OF THE BOOK.**
  Never the last trade. Triggers read the executable side for their own
  direction; a position reads the **opposite** side to close.
- **One conversion, in `sizing.py`**: `money = points × tick_value / tick_size ×
  qty`. Every money figure on the screen goes through it.
- **`gateway.py` is the only module that may import the FIX library.**
- **Credentials live only in `.env`** — never in code, config, an API response,
  a toast or a log line.
- **Cancel our own working orders at startup AND at shutdown**, scoped to our
  own id prefix. Never touch an order we did not send.
- **The book is persisted and recovered.** The reconciler auto-closes nothing
  until recovery says the book is complete, and never touches a position it
  cannot explain.
- **`positions()` and `orders()` return `None` for "unknown"**, which is not
  "flat" / "no orders". Unmeasured is not zero: return `None`, render `—`.
- **A guard may withhold an ORDER. A guard must never prevent a close.**
- **A refusal carries the venue's own words.**
- **No manual order entry. This program is the ALGO.** The only controls that
  send are the algo toggle, `CLOSE NOW` and `KILL ALL`. Manual trading is a
  SEPARATE program and is out of scope here (§14): an algo and a hand on the
  same contract fight over the same position, the same limits and the same
  journal.
- **UAT and PROD are separate venues** and the screen always says which.
- **Every test that asserts a guard withholds something needs a control** that
  turns the guard off and asserts the opposite.

---

## 12. Tests

`pytest`, everything faked, no network and no clock. At minimum:

- **stats** — mean/σ/z against hand-computed series; the update interval
  actually holding the bands still while z keeps moving; Hurst on synthetic
  mean-reverting and trending series; half-life returning `inf` on a random walk
- **warm-up** — no entry before the window is full, and a control that fills it
  and asserts the entry
- **signals** — entry at each threshold and each blocked reason, **each with
  its control**; exits in all three modes; the stop-loss firing regardless of
  every filter
- **costs** — the round trip, break-even and the target across all three bases,
  and `—` when the basis is unmeasured
- **sizing** — ticks to money on contracts with different tick values; a
  regression test that a wrong multiplier is caught
- **executor** — **all four combinations** of entry/exit order type
  (market-in/market-out, market-in/limit-out, limit-in/market-out,
  limit-in/limit-out), each opening and closing a position end to end
- **executor, the limit path** — offset pricing on the correct side; the dead
  band suppressing an amend inside it and allowing one outside it; amend by
  cancel-replace rather than cancel-then-new; timeout cancelling on an entry and
  crossing on an exit; a partial fill leaving the remainder working under the
  same lineage; a closing limit capped at the open position so it can never
  reverse; and the escalation on a CLOSE firing **through** a stale quote, a
  jump settle and a filter, each with its control
- **executor** — cancel and reject; a reject surfacing the venue's text verbatim
- **book / recovery** — restart with an open position; the incomplete-book rule
  closing nothing; UNCLAIMED never auto-closed
- **guards** — stale, jump, session, limits; each with a control; and the rule
  that none of them blocks a close
- **analysis** — a touch counted once per crossing and not once per tick; an
  unresolved touch excluded from the reverted percentage rather than counted as
  a miss, **with a control** that resolves it and asserts the percentage moves;
  an open position excluded from every statistic; simulated fills excluded from
  a `live` request and present in a `both` one; a contract under ten closed
  trades getting no verdict; the costs panel reporting budget and measurement
  separately and never overwriting the budget on its own
- **config** — atomic save under a concurrent read; blank vs `0`; secrets never
  round-tripping through an API response
- **webapp** — every route; the snapshot shape; `None` rendering as `—`
- **UI, under Playwright, reading `pageerror`** — eight windows laid out
  without overflow, the snapshot refreshing at 0.5 s, the algo toggle
  round-tripping, a fill raising the toast, no native dialogs anywhere. Skip
  cleanly where no browser is installed; nothing else may skip with them.

---

## 13. Build order

**Phase 1 — this phase.** Read `docs/screens.html` first; it is the layout
this order is building toward.
1. `models.py`, `config.py` (atomic save, `.env` keys, venue/contract schema), tests
2. `sizing.py`, `costs.py`, `stats.py`, `signals.py` — all pure, all tested
3. `fake_gateway.py` and the `Gateway` protocol; `FixGateway` stubbed
4. `book.py`, `database.py`, `engine.py`, `commands.py` — the loop and the snapshot
5. `webapp.py` + the **main page**: windows, the fields of §2.2, the 0.5 s
   refresh, toasts and sound, the taskbar
5a. `analysis.py` + the **Analysis window** (§2.6). It is phase 1 because what
   it reads — touch rows, entry and exit z, margin locked, measured slippage,
   the stored exit reason — can only be written as it happens. Build the
   recording with the engine, or the window has nothing to show for weeks
6. The **Exchanges page**: venues with FIX session fields, UAT/PROD, the three
   buttons, contracts with **Read from venue**
7. The **Settings page**: desk-wide, and the per-contract ⚙ — plus the
   engine READING AN EDITED CONFIGURATION BACK. A settings page that writes
   a file the loop never re-reads is a settings page that does nothing until
   the next restart, and looks like it worked
8. `start.py` — one command brings up config, gateway, engine and web, and
   opens the terminal in its own app window
9. `README.md`, `CLAUDE.md`, `docs/FIX_NOTES.md`

**Phase 2.** Wire `FixGateway` against Orient's UAT; measured slippage
against the budget; margin from the venue; Telegram. The **backtest replay**
(`replay.py`, §2.7) is built.

---

## 14. Not in scope

No ladders. No depth grid. No charts of any kind. No manual order entry, no
click-to-trade, no keyboard order keys — **manual trading is a separate
program**, not a mode of this one. No two-leg execution, no hedge ratio,
no synthetic spread built from outrights. No portfolio optimiser, no pair
scanner, and **no auto-tuning**: the Analysis window proposes a corrected
number and a person presses the button. Nothing applies its own findings. No cloud, no login, no multi-user — one
trader, one desktop, one screen.

---

## 15. Acceptance

**Run on 2026-09-08 against `FakeGateway`, eight contracts, 1920×1080.** All
eight criteria pass. Four things it found, all now fixed and covered:

1. The environment badge read a plain **UAT** while the simulator drove — a
   configured venue nothing is connected to is not that venue. It reads
   `UAT · SIM`.
2. The algo switch took **773 ms** to move on the screen. `COMMAND_POLL_SEC`
   was on the Settings page doing nothing: commands were drained once an
   engine pass, and the snapshot then waited for the next screen tick.
   Commands are drained through the wait, the snapshot is published on the
   command, and the screen posts and waits for the engine's own answer.
   Round trip is now ~100 ms, and a refusal raises a toast instead of being
   swallowed.
3. The **Analysis window opened on "Live only" while the engine was the
   simulator**, showing an empty window for the session just watched. It
   opens on what the engine is, and an empty window says which filter has
   the rows.
4. The venue refused a close with **"Close order with no position to close on
   that side"** — the escalation from an unfilled limit sent its market
   replacement alongside the cancel REQUEST, so a limit that filled in
   between left two orders out for one position. Armed on the timeout, sent
   on the CANCELLED event.


The phase is done when, with `FixGateway` still stubbed and `FakeGateway`
driving:

1. `python start.py` brings the terminal up in its own window with no arguments
   and nothing to edit by hand.
2. Eight contracts are configured on the Exchanges page, and eight windows fit
   a 1920×1080 screen and refresh twice a second.
3. Each window shows mean, σ, z, both band prices, the edge filter with its
   verdict, Hurst, half-life, warm-up, position, break-even, target and P&L —
   or an em dash and a reason.
4. The algo toggle on a window turns that contract's algo on and off, alone,
   within 100 ms, and survives a page reload and an engine restart.
5. A simulated fill raises the toast, the sound, the blotter row and the
   position, and a close raises the closing notification with the net money.
6. A UAT venue and a PROD venue coexist with separate credentials, the screen
   says which is which at all times, and no secret appears in any API response,
   log line or config file.
7. The Analysis window opens per contract, and after a session against
   `FakeGateway` it shows real touch counts with their resolutions, a trade
   journal carrying the z each decision was made on, and a slippage measurement
   beside the budget.
8. `pytest tests/ -q` passes, including the Playwright suite where a browser is
   installed, and every guard test has its control.
