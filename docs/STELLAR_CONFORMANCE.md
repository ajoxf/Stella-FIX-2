# Stellar FIX conformance test — how to run each line from the screen

Stellar requires its **FIX Conformance Self Test** on UAT before it allows
Production. The script is a workbook of tabs. You record each result as
Pass / Fail / Not Supported, and Stellar Support analyses the run.
Stellar's workbook is not kept in this repository. Keep your copy, pre-filled
with what this program supports and how each test is run, beside the
onboarding worksheet, and fill in Pass / Fail as you go.

Everything below is done from the screen. Nothing is edited by hand. The
FIX log (Logs page, or `logs/fix`) is the evidence for every line, with each
message in Stellar's tags, so keep it for Stellar Support.

Before you start:

- **Exchanges**: the Stellar UAT venue from Stellar's onboarding details:
  - hosts, ports and SenderCompIDs for both sessions;
  - the Stellar User Name and Account;
  - Heartbeat (sec);
  - for CME, Exchange clearing account 1 written `SenderSubID:SenderLocationID`.
- **Settings → Market orders as a limit = 0** for the Market Order tests.
  Stellar's script asks for true market orders (40=1). The desk's default
  sends "at market" as an IOC limit through the touch instead (see CLAUDE.md).
  Put it back afterwards.
- **Instruments & orders**: put the test contract on the watchlist. Manual
  tickets are the order path for the Order Management tests.

## Session Management

Mode: the Order Router is **persistent**. It keeps its sequence numbers
across disconnects and restarts, so Stellar resends what was missed. Market
Data resets on every logon (Stellar does not resend it). Tell Stellar this
when they ask which mode to set.

| Test | On the screen | What to look for in the FIX log |
|---|---|---|
| Normal Logon | Connection → **Connect FIX** | 35=A out, 35=A in |
| Heartbeat interval (108) | Exchanges → Heartbeat (sec), default 30 | 108 on our Logon |
| Logon with reset | Exchanges → tick "Reset Order Router sequence on every logon", Connect | 141=Y, 34=1 |
| Logon without reset | untick it (the default), Connect | no 141; 34 continues from the last session |
| Client initiates Logout | Connection → **Disconnect** | 35=5 out, Stellar's 35=5 back. The session shows DISCONNECTED, no error (`LOGOUT_WAIT_SEC`) |
| Heartbeating | leave it connected and idle | a 35=0 every interval; a TestRequest (35=1) is answered |
| Resend Request | rest two or more LIMIT orders away from the market, **Disconnect** (do NOT close the program: shutdown cancels our orders), ask Stellar to execute them, **Connect** | our 35=2 for the gap; Stellar's resent 8s (43=Y) applied once, in order |
| Gap Fill | **Disconnect**, Connection → **Skip ahead +10** (press twice), **Connect** | our Logon 10 numbers ahead; Stellar's 35=2; our 35=4 with 123=Y |

## Security Definitions

Instruments & orders → Find instruments. A field left blank is not sent, and
"Any type" sends no CFI code (461). Each search is one Security List Request
(35=x, 559=0).

| Test | Exchange (207) | Symbol (55) | Type (461) |
|---|---|---|---|
| By CFI code | blank | blank | Futures (FXXXXX) |
| By specific symbol | blank | e.g. ES | Any type |
| By entire market | e.g. XCME | blank | Any type |
| By combination | XCME | ES | Futures |
| By market and symbol | XCME | ES | Any type |

Stellar's specification marks 55 and 461 as required. Whatever Stellar
answers to the other combinations (a list, or 560 ≠ 0 in its words) is shown
under the search.

## Market Data

| Test | On the screen |
|---|---|
| Snapshot and updates | adding a contract to the watchlist subscribes (263=1); W then X |
| Full snapshot refresh | 35=W, applied as the whole book |
| Incremental refresh | 35=X, applied by MDEntryPositionNo (290) |
| Full depth | the ladder's Depth **FULL** (264=0); TOP is 264=1 |

## Order Management (manual tickets)

| Test | Ticket |
|---|---|
| Tag 50 | Exchanges → SenderSubID: sent as 50 in every header. For CME, Stellar also sets 50 / 142 from Clearing account 1 |
| Tag 116 | not supported (ICE_IPE is not traded here) |
| Buy / sell market | MARKET (with Market orders as a limit = 0) |
| Buy / sell limit | LIMIT at or through the touch |
| Cancel | LIMIT away from the market, then Cancel |
| Reject | a LIMIT with a wrong Account typed on the ticket: Stellar's 58 and 103 are shown in its words |
| Buy / sell stop | STOP with a stop trigger price (99) |
| Buy / sell stop limit | STOP_LIMIT: trigger (99) and limit (44) |
| Amend unfilled | LIMIT away, Replace to a fillable price (35=G by OrderID) |
| Amend partially filled | LIMIT for 2+, ask Stellar to part-fill, Replace, execute |
| Amend an amended order | Replace twice: away, away again, then fillable |
| IOC / FOK / GTC | Time in force on the ticket (59=3 / 4 / 1) |
| GTD (date / day) | not supported: Stellar's spec lists 59 = 0, 1, 3, 4 only |
| Held, Iceberg | not supported: not in Stellar's spec |

## Drop Copy

This program has no drop-copy session. The Trading Monitor's Fills tab
shows every execution on the Order Router session, ours or not, but that is
the order session, not a drop copy. Answer **Not Supported**.

## Recovery and Failover

- **Missed fills**: the Order Router keeps its sequence numbers, so after a
  reconnect Stellar resends what was missed (ResendRequest, applied in order,
  a fill never booked twice by its ExecID). Then every order of ours still
  working is asked about with an Order Status Request (H), and any whose
  OrderID was never heard with a Mass Order Status Request (AF). A fill
  Stellar reports that this program never received is said, never booked
  without its price. See [FIX_RECOVERY.md](FIX_RECOVERY.md).
- **Backup gateway**: one Stellar endpoint per session today, with no
  automatic failover. After a disconnect the trader reconnects from the
  Connection page (Connect / Reconnect) to the same gateway. To use a
  backup address from Stellar, change the venue's host on the Exchanges
  page. The sequence numbers are kept per host, so ask Stellar whether a
  backup shares the primary's numbers.
