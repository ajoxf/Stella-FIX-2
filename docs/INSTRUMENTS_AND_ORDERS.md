# Instruments and manual UAT orders

Open **http://localhost:8000/instruments**. Both FIX sessions must be connected.

1. Search an exchange and product: for example **CME**, **ES**, **Futures**.
   Optionally restrict expiry using **YYYYMM**. Search results are definitions
   received from Stellar (Security List), not a hardcoded symbol list.
2. Check the contract, expiry and Stellar Security ID. Choose **Add + subscribe**.
   The watchlist survives restarts and subscribes again after reconnection.
3. Read bid, ask, sizes, last trade, midpoint and bid/ask spread. A missing
   quote is not zero. Quotes older than 15 seconds are marked stale.
   Incoming Stellar updates use a server-sent event channel with 5 ms burst
   coalescing. The screen reports quote age and application delivery latency
   in milliseconds. This latency begins when the application receives FIX;
   it does not measure exchange-to-Stellar network latency.
4. Choose **Buy** or **Sell**, then enter account, total quantity, order type,
   time in force, and required limit/stop fields.
5. Choose **Review order**. Inspect the parameters and optionally the exact
   application FIX fields. **Confirm · Send UAT order** sends the ticket.
   A preview expires after 60 seconds. Repeated confirmation of the same
   token does not send another order.
6. Follow order acknowledgements and fills in **Orders** and **Executions**.
   Cancel and replace remain requests until Stellar responds; both go by Stellar's OrderID (37). Replacement quantity
   is the new **total** order quantity, not the unfilled remainder.

## Explorer, ladders and closing fills

**Explore instruments** provides Exchange, Type, Product and Instrument columns.
The lists contain definitions received from Stellar in this session plus saved symbols;
they are not an exhaustive exchange directory. Enter an exchange/product code and
click **Search contracts** to discover more, then select an exact contract and
click **Select + subscribe**.

Use **Ladder** in the watchlist to open up to four outright or exchange-listed
spread ladders. Click a price, set quantity and time in force, then **Review BUY**
or **Review SELL**. A separate confirmation still sends the order. Centre and
Lock centre control the price window. **Request full depth** changes the Stellar
subscription (264=0); only reported quantities are displayed. Availability depends on
the venue and market-data entitlement. Depth positions (290) are applied against the book as it stood before the
message.

**Close position** appears beside eligible filled orders and in their ladder.
It reviews an opposite-side MARKET/DAY order with Open/Close=C for the unclosed
fills tracked by this ticket. Pending or uncertain close requests reserve their
quantity, preventing a duplicate close from this application. Working orders
must be cancelled and acknowledged before their partial fills can be closed.
This is a local fill ledger, not a full-account position reconciliation: trades
elsewhere must be checked in Stellar before confirming. Opening a review does not send.

## Price units

**Every price on the screen is the price a trader knows**, and Stellar sends
prices that way already — a JGB as `150.25`, a Eurodollar as `99.180`. There
is no display factor: a quote (270), a trade (31) and a replace's price (44)
come in as sent, and a ticket's price (44) and stop (99) go out as typed. The
one place a price crosses the FIX boundary (`ManualTerminal.to_display` /
`to_fix`) is kept, at a factor of 1, for the manual ticket and the Algo alike.

The tick is Stellar's MinPriceIncrement (969, "point increment") and its money
value MinPriceIncrementAmount (1146, "value of 1 point increment"): JGB
`969=0.01`, `1146=10000.00`. So `money = points x tick_value / tick_size x qty`.

Prices are asked for by Stellar's Security ID (48), e.g.
`F.STS_OSE 10-year JGB.DEC17` — spaces and all; it is unique, where a product
name such as ES is not. An ORDER names the instrument the way Stellar's
Security List does: symbol, CFI code, maturity, exchange — and a listed
strategy by its legs. A contract whose Security List entry has not arrived yet
cannot be ordered, and the review says so.

For listed spreads, search **Listed spreads / strategies (MLEG)** (CFI
`MXXXXX`). Select the returned strategy, inspect its legs, then add it to the
watchlist. It is submitted as ONE strategy order — New Order Multi-leg
(35=AB) with its legs (555) and subtype (762) — never as legs. Negative and
zero spread limit prices are accepted by the ticket; prices must align to the
returned tick size. Stellar reports a strategy's trade AND each leg's (442=2):
only the strategy's is booked.

## Ticket fields

| Field | FIX tag / behavior |
|---|---|
| Account | 448 with 452=24 (Stellar Account Name) |
| Stellar User Name | 448 with 452=11, from the venue |
| Exchange clearing account 1 | 448 with 452=1000, from the venue when set (CME, KRX) |
| Instrument | 55, 461 (CFI), 167, 200 (maturity), 202 (strike), 207 (exchange) |
| Listed strategy | 35=AB, 555 legs (600 / 608 / 610 / 623 / 624), 762 subtype |
| Buy / sell | 54=1 / 2 |
| Total quantity | 38 |
| Market / limit / stop / stop-limit | 40=1 / 2 / 3 / 4 |
| Limit price | 44; required for priced order types |
| Stop trigger | 99; required for Stop and Stop Limit |
| Day / GTC / IOC / FOK | 59=0 / 1 / 3 / 4 |
| Open / close | 77=O / C — not in Stellar's spec, sent unless switched off on the venue |
| Exchange text | 448 with 452=1002 (a close's reference: `Close FTM-…`) |
| MiFID | 29, 1724=5, 452=3 / 12 / 122 short codes — from the venue when set, qualified 24 (person) |
| Handling | 21=1 |

Exchange and account permissions still determine accepted combinations.
Stellar's rejections are displayed with Stellar's message (58) and its reason
code (103) in words.

## Scope

Manual tickets use IDs beginning `FTM-` and a separate persistent ledger.
The algorithm does not own or close these orders. After a lost connection or
restart, unresolved orders are marked **UNKNOWN** and Stellar is asked for
their status (Order Status Request, 35=H) once the Order Router is back; they
are never silently resent.

Full-account position recovery (Stellar's Order Router provides no
positions), synthetic multi-order spreads and bracket/OCO orders are not
implemented. An Open/Close flag is not a client-side reduce-only guarantee.

Protocol references: the Stellar FIX Order Router Specification (03/04/2018)
and the Stellar FIX Market Data Feed Specification (29/09/2017).
