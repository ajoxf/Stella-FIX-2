# FIX-Trader

## Live FIX market data into the algorithm

The TT Market Data FIX session and `ManualTerminal` parser supply both the
instrument screen and the algorithm. Configure each algorithm contract with
its exact TT `security_id` and `symbol` in the selected config file. Set
`security_exchange` when known. The security ID is the routing key; symbols
are display metadata.
The gateway normalizes each parsed book into `MarketDataEvent` and publishes it
to a bounded, thread-safe `AlgoDataFeed`. The engine drains the feed on its own
loop, samples the bid/ask midpoint only for new valid updates, and continues to
apply its existing stale-data, signal, and risk logic. Last trades are delivered
as event metadata and never replace the midpoint used by the strategy.
Bursts coalesce by contract; a disconnected Market Data session clears its
books and pending events. The feed reports its coalesced count as `dropped`.

Use `run_fix.ps1` or `run_fix.bat` with your existing UAT configuration and
credentials in `.env`. The FIX audit records incoming messages, while DEBUG
logging for `fixtrader.gateway` and `fixtrader.engine` records normalized
events and strategy consumption. The Algo's orders go to TT over the Order
Routing session (TT UAT, FIX 4.2) — but only once a person arms **LIVE**.

Each contract's **Algo button** (on its ladder and its Algo window) is Off,
Signals or Trades; on a live venue every armed Algo comes back in Signals
after a restart. **Master** on the taskbar stands every Algo down at
once. **Execution** is PAPER or LIVE, on the taskbar — where a trading Algo's
orders go. Every restart comes back on
PAPER: fills simulated at the live bid/offer, nothing sent. LIVE sends the
Algo's orders to TT and is armed by hand, confirmed every time. TT is asked
for the account's positions at each logon; if it does not answer, arming
LIVE is your confirmation that this book's own fills are the record, and a
banner says so for as long as it holds.

For this UAT setup, TT's security-definition search returned a current ES Dec
2026 instrument; `config.tt-uat.json` maps it as `esz6` with automatic trading
off. The expired `esu6` contract is disabled. Raw FIX price and raw FIX tick
size are kept together in the algorithm model.

**Instruments & manual UAT orders:** open `/instruments` to search TT futures,
options and listed spreads, save a watchlist, receive bid/ask prices, and
review/send manual orders with cancel/replace and execution reports.
See [symbol and order instructions](docs/INSTRUMENTS_AND_ORDERS.md).
This manual workflow was added explicitly at the operator's request. Each
contract has ONE Algo switch, on its ladder and its Algo window — **Off**,
**Signals** (the Algo alerts its entries and watches your position's TP/SL;
you trade on the ladder) or **Trades** (PAPER or LIVE) — so hand trades and
algo trades are never on the same contract: while its Algo trades, hand orders
on it are refused, and the Algo never trades a contract a hand is holding.
Closing and cancelling always work. Algorithmic execution and complete account-position
recovery remain unfinished.

**Local TT UAT connection:** the native connection code from
`backup_v1fixapp.py` is now integrated. Run `run_fix.ps1` for the configured
Order Routing and Market Data sessions. See [TT connection](docs/TT_CONNECTION.md)
for setup and scope. This adds session connectivity; the execution and market
data translation work described below remains unfinished.

A **statistical-arbitrage monitor and execution terminal for exchange-listed
spread contracts**, traded over **FIX** through Orient Futures.

One screen, many contracts. Each contract gets a small window carrying the
numbers that decide the trade — rolling mean, standard deviation, the z of the
bid and of the offer, the profit target, position and P&L — an **ALGO ON/OFF** switch, and a gear for that
contract's own settings. Prices refresh twice a second.

This screen runs the algo and shows why it is or is not trading. Manual
trading lives on the Instruments & orders page, and only while the desk is in
MANUAL mode.

Beside the live windows there is an **Analysis** window, one contract at a
time: what the standard-deviation touches at each level actually did next, how
the trades ended, and what the costs really were against what was budgeted for
them. That is the loop that tells you whether a contract's entry threshold is
right, or whether it should be switched off.

## The Algo

The MT5 desk's Algo, on one contract — the venue lists the spread itself,
so there are no legs:

- **Band**: candles of 15 minutes, built from the mids this system records
  (FIX has no history to backfill from), the forming one included; middle =
  EMA(20), sigma = population sigma of the last 20. A restart rebuilds the
  band from the recording.
- **Entry**: H to L (sell the bid) when the BID's z reaches +2.5, L to H (buy
  the offer) when the OFFER's z reaches −2.5 — ARMED at the band and entered
  on the way back in (re-entry, 2.0 → 1.0 by default), confirmed over 3 fresh
  quotes. Held back, with the reason on screen, by: collecting candles, a
  90-minute live warm-up, the 5-minute cooldown, |z| past 3.5, the session
  cutoff, the day's limits (10 trades, 3 losses in a row, a money limit), and
  the filters — edge (capture ≥ 1.5× the round trip), regime (no entry while
  TRENDING) and trend (no entry against a drifting EMA).
- **Exit**, on the closing side, from break-even: the target (2% of the
  margin you enter per contract, or an ATR multiple) and the stop loss (2%,
  or an ATR multiple). Optional: z stop, back at the mean in profit, time
  stop.
- **Modes**, per contract on its Algo button: OFF (traded by hand), SIGNALS
  (alerts on entries; your position watched with its TP/SL; you trade), and
  Trades — PAPER (filled at the live bid/offer, nothing
  sent — every restart comes back here) or LIVE (sent to TT; armed by hand
  and confirmed).

The top bar puts trading first — **Algo desk**, **Instruments & orders**,
**Account** — then the connection pages. **Account** is the Trading
Monitor for FIX: positions with TT's view beside them, working orders, the
TT fills tape (every fill TT reported, in its own tags), closed trades,
slippage and the reconciler.

Each contract on the Algo desk has a **ladder** — the MT5 desk's ladder on
one contract: the book, our working orders and the Algo's levels on it,
CLOSE ALL and Close @ LMT — and an **Algo window** — Signal & Position, Statistics,
Filters, the last signal held back, the last order, and a **Backtest** of
its settings over the recording. Both minimise to the taskbar.

## Why it is simpler than a two-leg spread system

Orient lists the spread itself as a single tradeable contract. There is no leg
A and leg B, no hedge ratio, no matched clip, no naked-leg window and no
orphan-leg recovery — a spread is bought and sold like an outright.

## Contributing

**[`CONTRIBUTING.md`](CONTRIBUTING.md)** is the brief for anyone joining, and
**[`docs/WORK_FIX_GATEWAY.md`](docs/WORK_FIX_GATEWAY.md)** breaks the FIX and
order-execution work into six packages, one PR each.

The handover is a test file. `tests/test_gateway_contract.py` runs against the
simulator the whole system is built on, and it is the same suite a real
`FixGateway` has to pass against Orient's UAT. A gateway that passes it drops
in with no change above `fixtrader/gateway.py`.

## The build specification

**[`BUILD_PROMPT.md`](BUILD_PROMPT.md)** is the brief: the screen, the
per-contract and desk-wide settings, the Exchanges page and its FIX session
fields, the algo, the guards, the hard rules, the tests and the build order.
Read it before writing any code.

`gateway.py` is the only module that speaks FIX: market data, manual
tickets and the Algo's orders (`AlgoOrderRouter`) all go through it.
`FakeGateway` drives the tests and the pre-credentials desk behind the same
interface. See `docs/FIX_NOTES.md` for the message set and the open questions
for Orient.

## Running it — one file

```
python start.py
```

**Python 3.9 or newer, and Flask 2.0 or newer.** `start.py` checks both before
it starts anything and names the interpreter it is running on, because the
usual way to get this wrong is a new terminal that has forgotten the
environment:

```
conda activate fixtrader      # a new terminal does not keep it
python start.py
```

Anaconda's `base` is commonly Python 3.7 with an old Flask, and on it the
imports fail rather than the program misbehaving — `typing.Protocol` is 3.8,
and `@app.get` is Flask 2.0.

On a first run it writes `config.json` and `.env`, brings the **web UI up
first** (the venues are entered on that screen, so it has to be reachable
before there are any), starts the engine, opens the terminal in a window of
its own, and restarts a crashed child with backoff. It ships with three
example contracts against the simulator, so there is something on the screen
before a venue exists — and the taskbar says **SIMULATED** in the place a
**PROD** badge would go.

```
pytest tests/ -q
```

128 tests, everything faked: no venue, no network, no clock. The browser suite
drives the real UI under Playwright and reads `pageerror`, because a
temporal-dead-zone `ReferenceError` that aborts a script block and silently
unregisters a handler is invisible to a Python test — and is exactly what
happened in the system this is ported from. They skip cleanly where no browser
is installed.

## What is built

| Module | What it does |
|---|---|
| `fixtrader/config.py` | Venues, contracts, settings; atomic saves; `.env` keys; blank-versus-zero |
| `fixtrader/sizing.py` | `money = points × tick_value / tick_size × qty` — the one conversion |
| `fixtrader/costs.py` | The round trip, break-even, and the target as a percentage of margin |
| `fixtrader/stats.py` | The touch study's rolling time window (Analysis): mean, sigma, z; the SD touches |
| `fixtrader/signals.py` | Readings off the touch-study window (the Algo's rule is `algo.py`) |
| `fixtrader/algo.py` | The Algo: re-entry signal, gates, exits, levels, filters |
| `fixtrader/bands.py` | Candles from the recorded mids; EMA middle and population sigma |
| `fixtrader/algofilters.py` | Round trip, edge, regime, half-life, ATR |
| `fixtrader/algodesk.py` | One contract's Algo run: candles, warm-up, the day, last blocked / last order |
| `fixtrader/backtest.py` | The Algo over recorded candles — the window's Backtest and the Analysis replay |
| `fixtrader/gateway.py` | The venue seam — **the only module that may import FIX** |
| `fixtrader/fake_gateway.py` | A real book, a real fill model, a real reject |
| `fixtrader/marketdata.py` | The staleness and jump guards, and the session clock |
| `fixtrader/executor.py` | One order path for entries and exits, market or limit |
| `fixtrader/engine.py` | The loop, the book, recovery, and one snapshot per pass |
| `fixtrader/database.py` | SQLite (WAL): positions, orders, fills, touches, events |
| `fixtrader/analysis.py` | The feedback loop: the touch study, the costs, the journal |
| `fixtrader/webapp.py` | The Flask process: it renders and it asks; it never trades |
| `fixtrader/static/`, `templates/` | The terminal — self-hosted, no CDN, no framework |

**Positions** is its own window on the desk: every open position across every
contract in one list — side, quantity, the price it filled at, the z the entry
fired at, break-even, target, stop, margin, open P&L — with **what the venue
says beside what this book holds**, and the venue's own tickets for the fills
that built each one. A row where the two disagree is tinted; a venue showing
long AND short at once is called out in red and never netted to zero, because
that is a close that went out as an open.

**Closes go out by reference, not as opposite orders.** Every closing order
carries an explicit close flag, the position it closes and that position's
tickets, capped at what is open on that side — see `docs/FIX_NOTES.md`.

**Settings** is the desk-wide page: the loop, the master switches, the guards,
the desk limits, and the defaults every blank box on a contract falls back to.
It shows what the engine **achieved** beside what was asked of it, and names
the two settings that need a restart rather than warning about it on every
save.

**Exchanges** is where the system is connected: venues with their FIX session
fields, UAT and PROD as separate rows that never look alike, **Connect**,
**Test** and **Diagnose** answering in the venue's own words with the step
that fixes each failure, and the contracts table with **Read specifications
from the venue** — which reports rather than applies, because a specification
changed under a running desk is every money figure on that window changing
without anybody being told. Against the simulator it says so: a gateway built
from your own configuration cannot confirm it.

**Analysis** is the feedback loop, one contract at a time — because a win rate
blended across eight of them cannot answer the only question it exists for,
which is which contract to turn off. It reports what happened **after** each
standard-deviation touch: how many came back, how long they took, how far they
went against you first, and how many became a trade. A level is only named as
the one to trade if the move back to the mean **covers the round trip** — the
inner bands always revert more often, and they are also the ones that cannot
pay for the trade. Costs are shown as budget against measurement, which closes
the loop the settings page opens: the edge filter refuses entries using the
*budgeted* slippage, so a budget that is wrong silently refuses trades that
would have paid, or passes trades that do not. The correction is offered as a
button; nothing here applies its own findings.

A contract with fewer than ten closed trades is marked **too few to judge** and
given no verdict. An open position is named and excluded from every figure. A
touch still running is counted separately and left out of the percentages —
folding it in as a miss understates every level, and understates the widest
levels most, because those are the ones still open.

## Status

The main terminal runs end to end against the simulator: contracts warm,
arm, enter, manage their positions and close on their targets, with the
notifications, the guards and restart recovery working. On TT UAT the Algo
trades PAPER until LIVE is armed — see `docs/FIX_NOTES.md`.
