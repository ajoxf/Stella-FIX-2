# Order tests on TT UAT

Every order path the desk uses in a live market, proven on TT UAT first — from
the screen. **Order tests** in the top bar (`/order-tests`).

## Using the page

**Order tests** in the top bar. Three steps:

1. **Pick a contract** — flat (nothing open, nothing working), with a bid and
   an offer. *Options* holds the quantity and the rest; the defaults are fine.
2. **Check automatically** — four groups, each with its own **Check** button:
   **Manual · Market**, **Manual · Limit**, **Algo · Market**, **Algo · Limit**.
   The program places, fills, cancels and closes real orders on TT UAT and
   ticks each flow ✓ or ✗ with what TT answered and when; each flow keeps its
   newest result, whichever group was run. The contract ends flat. An Algo
   group first asks to send the Algo's orders to TT UAT (the engine's own
   confirmation). "Fills when the market reaches it" waits for the market and
   is off unless ticked under *Options*. A price that has not moved does not
   stop a check — a quiet UAT market is normal; a bid and an offer is enough.
3. **Try it yourself** — the desk's own ladder, Algo window and Trading
   Monitor, as you will trade live:
   - **By hand:** Algo switch on **OFF** (or SIGNALS). BUY / SELL, or click a
     price (Bids buys, Asks sells); cancel; CLOSE ALL; Close @ LMT.
   - **As the Algo:** Algo switch on **UAT**. BUY / SELL or a price click makes
     the Algo send its order to TT UAT now, as on a signal; its take-profit
     and stop loss then manage it.
   Tick what you have tried.

## The words

| Algo switch | What happens |
|---|---|
| **OFF** | You trade by hand. |
| **SIGNALS** | The Algo alerts and watches your position's TP/SL; you trade by hand. |
| **PAPER** | The Algo trades; its orders are filled inside the program — nothing goes to TT. |
| **UAT** | The Algo trades; its orders go to TT UAT. (On a live venue this choice reads **LIVE**.) |

"LIVE" on the screen only ever means a live market.

From a terminal instead: `run_uat_tests.bat esz6` or
`python -m fixtrader.uat --contract esz6 [--only M1,M3] [--with-hits]`.

## The tests

| Group | Flow | What you should see |
|---|---|---|
| Manual · Market | Buy at market, then close (M3) | Fills at the offer straight away — you are long 1. CLOSE ALL sells it back: flat. |
| Manual · Market | Sell at market (go short), then close (M7) | Fills at the bid straight away — you are short 1. CLOSE ALL buys it back: flat. |
| Manual · Limit | Buy limit below the market, then cancel (M1) | Waits in the book (Working orders) and does not fill. Cancel removes it. |
| Manual · Limit | Sell limit above the market, then cancel (M8) | Waits in the book above the market and does not fill. Cancel removes it. |
| Manual · Limit | Move a waiting limit to a new price (M2) | TT confirms the new price and the order keeps waiting there. Then cancelled. |
| Manual · Limit | Buy limit at the offer, then a take-profit limit (M4) | Priced at the offer, so it fills at once. A Close @ LMT (take-profit) then waits above the market; it is cancelled and CLOSE ALL closes at market. |
| Manual · Limit | Buy limit at the bid — wait for a seller (M5) | Waits at the bid until someone sells to it, then you are long 1 and it is closed. On a quiet market nobody may — then it is cancelled. |
| Manual · Limit | An order TT rejects (M6) | Sent to an account TT does not know: shows REJECTED, with TT's own reason. |
| Algo · Market | Algo buys at market, then close (A1) | The Algo sends a buy, as on a signal. It fills; the Algo's position shows with its take-profit and stop loss. CLOSE ALL closes it: flat. |
| Algo · Market | Algo sells at market (goes short), then close (A5) | The Algo sends a sell, as on a signal. It fills short; CLOSE ALL buys it back: flat. |
| Algo · Limit | Algo buy limit below the market, then cancel (A2) | The Algo's order waits in the book and does not fill. Cancel all removes it. |
| Algo · Limit | Algo sell limit above the market, then cancel (A6) | The Algo's sell waits above the market and does not fill. Cancel all removes it. |
| Algo · Limit | Algo buy limit fills, then your take-profit limit (A3) | The Algo is long. Your Close @ LMT (take-profit) waits above the market. CLOSE ALL cancels it and closes at market — one close, never two. |
| Algo · Limit | Algo buy limit at the bid — wait for a seller (A4) | The Algo's buy waits at the bid until someone sells to it, then it is closed. On a quiet market nobody may — then it is cancelled. |

On a **live venue** the page runs nothing and keeps the last UAT results.

## How they are proven before UAT

`tests/test_uat_orders.py` runs every test through the real web app, the real
engine loop and the real FIX sessions against a fake TT exchange
(`tests/fake_tt.py`) that quotes in TT's FIX units with a DisplayFactor — so
the price conversion is tested on the same pass. They found one real bug on
the way: with Auto trade off, CLOSE ALL over a resting Close @ LMT cancelled it
and then never sent the market close (the executor only learned the contract
while Auto trade was on). Fixed, with a regression test.

The fake exchange is not TT's matching. A pass here means the program does
what it should with TT's answers; the UAT run proves TT gives those answers.
