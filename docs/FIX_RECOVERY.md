# TT FIX Recovery (Order Routing)

A FIX client that was not listening — the line dropped, the PC restarted, the
program was closed — never receives the Execution Reports TT sent meanwhile.
A fill among them is a position at TT that this screen does not know about.
TT FIX Recovery replays them.

## How the program uses it

1. Order Routing logs on and TT says "Recovery is complete".
2. The previous run's working Algo orders are adopted (so a replayed fill is
   applied as what it was), then the startup sweep cancels them.
3. A second, short connection logs on to **TT FIX Recovery** with the **same
   SenderCompID, TargetCompID and password** as Order Routing, at sequence 1.
4. After TT's "Recovery is complete" it sends ONE **Recovery Request (U2)**:
   - `18002=Y` — reconciliation: everything TT has not already delivered
     since its weekly reset (Saturday 22:00 UTC); or
   - `916` / `917` — a window, from a minute before Order Routing was last
     heard to now, when that is before the weekly reset. TT keeps 720 hours
     (fewer than 250 accounts); older than that is said, with Orient's
     statement as the remedy.
5. TT replays Execution Reports (8) and Cancel Rejects (9), then logs out
   with "Recovery completed for fix-session=…".

Every replayed report goes through the same code as a live one: a fill already
booked is never booked twice, a spread leg is never booked, a bust is said, the
price is converted from TT's FIX units, and an order already FILLED or
CANCELED is never moved back. While it runs, Algo entries wait; exits never do.

## Where it connects

| Order Routing | FIX Recovery (Order Routing) |
|---|---|
| `fixorderrouting-ext-uat-cert.trade.tt:11502` | `fixrecovery-ext-uat-cert.trade.tt:11508` |
| stunnel `127.0.0.1:11702` | stunnel `127.0.0.1:11708` (add a `[recoveryfix-tcp]` section) |

Any other order-entry address needs the Recovery host and port set on the
**Exchanges** page; until then the Reconciler tab says Recovery is not set up.

## On the screen

Trading Monitor → **Reconciler**: the last Recovery (done / running /
REFUSED / FAILED, in TT's words), how many reports TT replayed, when Order
Routing was last heard, and **Recover now**.

## What it cannot do

- Trade Capture Reports (AE: block trades, transfers) are not replayed.
- A position opened before TT's 720-hour window needs a starting figure from
  Orient's statement.
