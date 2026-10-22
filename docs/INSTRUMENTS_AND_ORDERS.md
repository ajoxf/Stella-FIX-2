# Instruments and manual UAT orders

Open **http://localhost:8000/instruments**. Both FIX sessions must be connected.

1. Search an exchange and product: for example **CME**, **ES**, **Futures**.
   Optionally restrict expiry using **YYYYMM**. Search results are definitions
   received from TT, not a hardcoded symbol list.
2. Check the contract code, expiry and TT Security ID. Choose **Add + subscribe**.
   The watchlist survives restarts and subscribes again after reconnection.
3. Read bid, ask, sizes, last trade, midpoint and bid/ask spread. A missing
   quote is not zero. Quotes older than 15 seconds are marked stale.
   Incoming TT updates use a server-sent event channel with 5 ms burst
   coalescing. The screen reports quote age and application delivery latency
   in milliseconds. This latency begins when the application receives FIX;
   it does not measure exchange-to-TT network latency.
4. Choose **Buy** or **Sell**, then enter account, total quantity, order type,
   time in force, and required limit/stop/expiry fields.
5. Choose **Review order**. Inspect the parameters and optionally the exact
   application FIX fields. **Confirm · Send UAT order** sends the ticket.
   A preview expires after 60 seconds. Repeated confirmation of the same
   token does not send another order.
6. Follow order acknowledgements and fills in **Orders** and **Executions**.
   Cancel and replace remain requests until TT responds. Replacement quantity
   is the new **total** order quantity, not the unfilled remainder.

## Explorer, ladders and closing fills

**Explore instruments** provides Exchange, Type, Product and Instrument columns.
The lists contain definitions received from TT in this session plus saved symbols;
they are not an exhaustive exchange directory. Enter an exchange/product code and
click **Search contracts** to discover more, then select an exact contract and
click **Select + subscribe**.

Use **Ladder** in the watchlist to open up to four outright or exchange-listed
spread ladders. Click a price, set quantity and time in force, then **Review BUY**
or **Review SELL**. A separate confirmation still sends the order. Centre and
Lock centre control the price window. **Request full depth** changes the TT
subscription; only reported quantities are displayed. Availability depends on
the venue and market-data entitlement. Depth positions are applied using TT's
before-message indexing rules.

**Close position** appears beside eligible filled orders and in their ladder.
It reviews an opposite-side MARKET/DAY order with Open/Close=C for the unclosed
fills tracked by this ticket. Pending or uncertain close requests reserve their
quantity, preventing a duplicate close from this application. Working orders
must be cancelled and acknowledged before their partial fills can be closed.
This is a local fill ledger, not a full-account position reconciliation: trades
elsewhere must be checked in TT before confirming. Opening a review does not send.

## Price units

**Every price on the screen is the price a trader knows** — Crude ~90, Gold
~4170 — never TT's FIX price (Crude `9057`, Gold `41998`). TT sends prices in
its FIX units; the instrument's **DisplayFactor (tag 9787)** turns one into the
other: `screen price = FIX price x 9787` (CL `9057 x 0.01 = 90.57`). The
conversion is made once, at the FIX boundary (`ManualTerminal.to_display` /
`to_fix`), in both directions: quotes (270), fills (31, 6) and a replace's
price (44) coming in; ticket prices (44) and stops (99) going out — for the
manual ticket and the Algo's orders alike.

Where TT sends no 9787 the factor is the ratio of the exchange's tick (16552,
in screen prices) to TT's tick (969, in FIX units) when that is a power of ten;
a definition that gives neither is in screen prices already (factor 1). The
exchange tick (16552) is in screen prices, which is why `money = points x
tick_value / tick_size x qty` was wrong by the factor while prices were raw.

A contract whose factor TT has not sent is not traded by the Algo: nothing is
recorded for it and entries wait, saying why. Recordings and orders made by an
earlier version (in FIX units) are rescaled once when the factor is first seen,
and the factor is kept beside them (`price_units`).

Use the exact TT Security ID (48, source 22=96). A product name such as ES can
represent many different expiries. It is not a unique tradable instrument.
All returned top-level FIX fields are visible under instrument parameters;
spread-leg identity, direction and ratio are shown separately.

For listed spreads, search **Listed spreads / strategies (MLEG)**. Select the
returned contract, for example a calendar spread, inspect its legs, then add
it to the watchlist. The spread is submitted as one exchange instrument using
its exact TT ID. Negative and zero spread limit prices are accepted by the
ticket; prices must align to the returned tick size.

## Ticket fields

| Field | FIX tag / behavior |
|---|---|
| Account | 1 |
| Instrument | 55, 48, 22=96 |
| Buy / sell | 54=1 / 2 |
| Total quantity | 38 |
| Market / limit / stop / stop-limit | 40=1 / 2 / 3 / 4 |
| Market-on-close / limit-on-close / post-only | 40=5 / B / p |
| Limit price | 44; required for priced order types |
| Stop trigger | 99; required for Stop and Stop Limit |
| Day / GTC / at-open / IOC / FOK / GTD / at-close | 59 |
| GTD expiry date | 432; required for GTD |
| Minimum quantity | 110 |
| Displayed quantity | 1138 |
| Open / close / FIFO | 77=O / C / F; account default if blank |
| Order capacity / customer capacity | 528 / 582 |
| Order note | 58 |
| Cancel on disconnect | 18=o 2; unavailable with GTC/GTD |
| Manual order indicator | 1028=Y |

CME FOK is encoded as 59=3 with 110 equal to total quantity; CME IOC omits
110. Exchange and account permissions still determine accepted combinations.
TT rejections are displayed with the venue's message.

## Scope

Manual tickets use IDs beginning `FTM-` and a separate persistent ledger.
The algorithm does not own or close these orders. After a lost connection or
restart, unresolved orders are marked **UNKNOWN**; they are not silently
resent. Verify them in TT before placing a replacement order.

This is a UAT integration. Full-account position recovery, automated strategy
execution, synthetic multi-order spreads, bracket/OCO orders and TT synthetic
algo types are not implemented. Instruments with variable tick tables are
blocked at review until their price-dependent validation is implemented.
An Open/Close flag is not a client-side reduce-only guarantee.

Protocol references:

- [TT New Order Single](https://library.tradingtechnologies.com/tt-fix/tt-fix-order-routing/supported-application-messages/new-order-single-d-message/)
- [TT Security Definition](https://library.tradingtechnologies.com/tt-fix/tt-fix-market-data/supported-application-messages-tt-fix-market-data/security-definition-message/)
- [TT Market Data Request](https://library.tradingtechnologies.com/tt-fix/tt-fix-market-data/supported-application-messages-tt-fix-market-data/market-data-request-v-message/)
