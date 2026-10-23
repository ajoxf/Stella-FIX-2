# FIX — what the wiring will need, and what is still unknown

`fixtrader/gateway.py` is the only module allowed to import a FIX library.
`FixGateway` there has the shape and the configuration but **no message
handling**: every handler is marked `TODO(fix-wire)`. It imports cleanly with
no venue reachable, reports `DOWN` with a readable reason, and never takes the
engine down.

This file is the brief for finishing it. Nothing here has been guessed into
code: a stub that says "not wired" is honest, and a stub that invents a tag is
a bug with a long fuse.

## Open questions for Stellar

The wire follows Stellar's two specifications (see `STELLAR_CONNECTION.md`).
These are the points the specifications leave open; each is built the way
that fails safe, and each is worth confirming on Stellar UAT:

1. **PositionEffect (77).** Not in Stellar's spec; sent on every order
   (77=O / C) because a close must say it is a close. If Stellar refuses the
   tag, untick "Send 77 Open/Close" on the venue — the close still names its
   position in the ExchangeText party (452=1002) and is capped by the book.
2. **ClOrdID.** The spec says "a string up to 32 digits"; every example is
   numeric. Ours are alphanumeric (`FT-…`, `FTM-…`, at most 20 characters).
3. **Logon credentials.** The spec's Logon has 98 / 108 only. A password in
   `.env` is sent as 553 / 554; none sends a bare Logon.
4. **Sequence reset schedule.** The Order Router keeps its numbers so Stellar
   resends what was missed. When does Stellar reset its side (daily,
   weekly)? Then use Reset sequence numbers, or the venue's reset-on-logon.
5. **Unsubscribe.** The spec lists SubscriptionRequestType 263=1 only; the
   depth toggle and a Market Data gap send 263=2 then 263=1.
6. **Security List Request.** Sent with Symbol (55), CFI (461) and the
   exchange (207, as in the spec's examples). An inter-commodity strategy is
   searched for under each leg product.
7. **Trade CumQty / OrdStatus.** The examples' trades say 14=0 (and a
   strategy fill 39=0 with 151=0): the desk sums LastQty itself. Confirm a
   partial fill reports LeavesQty (151) reliably.
8. **CME SenderSubID (50) and SenderLocationID (142).** The 2025 spec says
   Stellar takes both from ExchangeClearingAccount1 (452=1000), written
   `SenderSubID:SenderLocationID`, else 50 is the user name: set the clearing
   account on the venue that way.
9. **Mass Order Status with nothing working.** The spec shows the answer for
   orders that exist (911 / 912=Y on the last). What does Stellar send when
   there are none? Until it is known, a sweep that sent an AF and heard
   nothing FAILS, naming the orders it asked about — never read as done.
10. **Security List Request combinations.** The spec marks Symbol (55) and
   CFI (461) required; the conformance script asks for a request by 461
   alone, 55 alone, 207 alone and 207 + 55. The search sends what is given;
   Stellar's answer to each is shown as it comes.

## Open questions for Orient (from the TT desk)

These change what the code sends, so they are worth asking before writing it.

1. **How is a spread contract identified?** `Symbol(55)` alone, `SecurityID(48)`
   + `SecurityIDSource(22)` + `SecurityExchange(207)`, or a multi-leg
   `SecurityDefinition` with `NoLegs(555)`? The current `ContractConfig`
   carries `symbol`, `security_id` and `security_exchange` so any of the three
   fits, but the contracts table and the security-definition request differ.
2. **Is market data a separate session?** `VenueConfig` has `md_host` /
   `md_port`, blank meaning "the same session". Knowing now saves a refactor.
3. **FIX version and data dictionary.** 4.2, 4.4, or FIXT.1.1 + FIX50SP2, and
   the custom-tag XML.
4. **Sequence-reset policy** on logon, and whether they expect
   `ResetSeqNumFlag(141)=Y` on every connect.
5. **Does anything report margin per contract?** The profit target is a
   percentage of initial margin (the desk's choice), so where margin cannot be
   read the window correctly shows no target. If Orient will not report it over
   FIX, the desk either supplies a per-contract figure or switches that
   contract's basis to notional — **not** silently, which is why
   `costs.missing_for_target` names the missing figure in words.
6. **Trading-hours source.** Session windows are configured per contract today.
   If the venue publishes them in the security definition, read them instead.
7. **UAT endpoints and hours**, and whether UAT comp ids differ from PROD.
8. **Which offset flag does each contract want?** Tag 77 `PositionEffect`, or
   the broker's `CombOffsetFlag`, and whether the close-today / close-yesterday
   split applies. See "Closing" below — this one is not cosmetic.

## The message set

| Purpose | Message |
|---|---|
| Session | `Logon(A)`, `Logout(5)`, `Heartbeat(0)`, `TestRequest(1)`, `ResendRequest(2)`, `SequenceReset(4)` |
| Contract specs | `SecurityDefinitionRequest(c)` → `SecurityDefinition(d)` |
| Market data | `MarketDataRequest(V)` → `MarketDataSnapshotFullRefresh(W)`, `MarketDataIncrementalRefresh(X)`, `MarketDataRequestReject(Y)` |
| Orders | `NewOrderSingle(D)`, `OrderCancelRequest(F)`, `OrderCancelReplaceRequest(G)` |
| Acknowledgement | `ExecutionReport(8)`, `OrderCancelReject(9)`, `BusinessMessageReject(j)` |

## Mapping onto what already exists

`FakeGateway` implements the same protocol and is the reference for behaviour:

- `top_of_book(key)` → `None` for **unknown**, never an empty book.
- `orders()` / `positions()` → `None` for **could not read**, which is not
  "none" and not "flat". `FakeGateway.readable = False` is the switch the
  tests use to produce that state.
- `send()` returns a `ClOrdID` prefixed `FT-`. Anything at the venue without
  that prefix belongs to somebody else — a hand order in Stellar — and is never
  cancelled, amended or counted as ours.
- **Amend is cancel-replace**, keeping the order id and its lineage. Never
  cancel-then-new: it gives up the queue and opens a window in which the order
  does not exist.
- **Every event must carry a snapshot of the order, not a live reference.**
  A real `ExecutionReport` is a distinct message; aliasing it broke the fill
  path once already (see `CLAUDE.md`).
- A reject carries **the venue's own text**, verbatim, into
  `GatewayEvent.text` — tag 58 and tag 103 where they are sent.

## Closing — the offset flag is not optional

**A close is never a bare opposite order.** On a venue that keeps long and
short apart — every Chinese exchange Orient routes to (SHFE, DCE, CZCE, INE,
GFEX) takes an offset flag on every order — an opposite order that does not
say it is closing is an order to OPEN the other way. The desk ends up long and
short at once, both live, both posting margin, and a screen that nets would
show it as flat.

This is the FIX equivalent of closing an MT5 ticket, and the system already
carries everything the wiring needs:

| Ours | What it becomes on the wire |
|---|---|
| `OrderRequest.position_effect` | `PositionEffect(77)`: `O` open, `C` close. On the Chinese exchanges, the broker's `CombOffsetFlag` — Open / Close / CloseToday / CloseYesterday |
| `OrderRequest.reduce_only` | The venue's reduce-only flag where it has one. **A cap, not an instruction** — sent as well as the effect, never instead of it |
| `OrderRequest.position_id` | Our own position, for a venue that wants a position reference |
| `OrderRequest.close_tickets` | The venue execution ids of the fills that built the position — its tickets |
| `qty` | Already capped at what is open on that side; a close can only reduce |

`close_offset_mode` is per contract: `CLOSE` (the plain flag, the default),
`CLOSE_TODAY`, `CLOSE_YESTERDAY`, or `AUTO`, which picks from the trading day
the position was opened on. **SHFE and INE price a close-today differently
from a close-yesterday**, so getting this wrong is a real cost rather than a
formality — ask Orient which flag each contract wants.

**Unknown degrades to `CLOSE`, never to `OPEN`.** A close whose flag could not
be determined is still a close; the other way round turns an exit into a
second position, which is the failure this whole mechanism exists to prevent.

`FakeGateway` models this properly rather than netting: it keeps the two sides
apart, obeys the flag it is given, and refuses an oversized close with
`"Order would not reduce position size"`. Two tests hold the line — one sends
an opposite order flagged OPEN and asserts the desk ends up with **both sides
live**, and its control sends the same order flagged CLOSE and asserts nothing
is left.

## Until it is wired

`start.py --simulated` (the default) runs the whole system against
`FakeGateway`, and the taskbar says **SIMULATED** in the place a **PROD** badge
would go. Every test in the suite runs against it, so the wiring phase changes
one module and re-runs the same tests.
