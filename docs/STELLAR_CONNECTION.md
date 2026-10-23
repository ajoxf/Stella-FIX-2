# Stellar connection

`FixGateway` (`fixtrader/gateway.py`) speaks FIX 4.4 to Stellar over two
sessions, both with TargetCompID `STELLAR`:

| Session | Spec | Messages |
|---|---|---|
| **Order Router** | Stellar FIX Order Router Specification (19/03/2025) | D / AB new order (single / multi-leg), G / AC cancel-replace, F cancel, H order status, AF mass order status → 8 execution report, 9 cancel reject |
| **Market Data** | Stellar FIX Market Data Feed Specification (17/05/2024) | x security list request → y security list, V market data request → W snapshot / X incremental, f security status |

No QuickFIX: the session layer (`NativeFixSession`) is plain Python, the
same one the TT desk used, moved to FIX 4.4.

## Setting it up

Install once, from the program folder in **PowerShell** (not Command
Prompt — there a `.ps1` path is opened in Notepad instead of being run):

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No activation is needed: `run_fix.ps1` uses `.venv\Scripts\python.exe` by
itself. Then start it with `run_fix.bat` (double-click, or from either
shell). On the first run it creates `config.stellar-uat.json` from the
example — never over an existing one — and `.env` is created empty; neither
is opened or edited by hand. Everything else is done in the browser.

Until a venue has a host the engine WAITS — the Connection page says
"Waiting for a Stellar venue" — and connects by itself the moment one is
saved; it never falls back to the simulator.

Open http://127.0.0.1:8000/exchanges and fill in the venue `STELLAR-UAT`
(or create it with **New venue**):

| Field | What Stellar issued | Example |
|---|---|---|
| Order Router host / port | your Order Router endpoint | — |
| Market Data host / port | your Market Data endpoint | — |
| SenderCompID (Order Router) | the Order Router CompID | `STELLAR_UAT` |
| SenderCompID (Market Data) | the Market Data CompID | `STELLAR_UAT_MD` |
| TargetCompID | always | `STELLAR` |
| Stellar User Name | 452=11 on every order (and 553 on the Logon if a password is set) | `Phil` |
| Stellar Account | 452=24 on every order | — |
| Exchange clearing account 1 | 452=1000: for CME, `SenderSubID:SenderLocationID` — Stellar sets tags 50 and 142 from it (else 50 is the user name); KRX's beneficiary account | — |
| TLS | as Stellar provisioned the line — usually plain TCP | off |

Passwords are typed on the same page — **Password** for the Order Router,
**Market Data password** for Market Data — and the page writes them to `.env`
(`STELLAR_UAT_PASSWORD`, `STELLAR_UAT_MD_PASSWORD`); they never reach the
config, and the page only ever says "set" or "not set". A blank field leaves
a stored password alone. Stellar's Logon lists no credentials (98 / 108
only): a session authorised by its address leaves them blank and sends a bare
Logon; one Stellar issued a password for sends it as Username (553) /
Password (554). Saved session settings and passwords take effect when the
program is restarted.

Equivalent launch command:

```powershell
python start.py --fix --no-browser --config config.stellar-uat.json --status runtime\status.stellar-uat.json --commands runtime\commands.stellar-uat.jsonl --results runtime\results.stellar-uat.json --port 8000
```

`/connection` shows each session's logon, heartbeat, message counts, sequence
numbers and recent activity. Connect, Disconnect and Reconnect act on the
engine's sessions; reconnection waits ten seconds. **Reset sequence numbers**
(only with the sessions down, and pressed twice) starts the Order Router from
1 at the next logon — for when Stellar says it has reset its side.

## How each Stellar rule is met

- **Sequence numbers.** The Order Router KEEPS its numbers across restarts
  (`SequenceStore`, in the terminal's database): Stellar honours a
  ResendRequest there, so what the program missed while it was down — a fill
  included — is sent again at the next logon and applied once. Market Data
  resets on every logon (141=Y): Stellar does not resend there, so a gap
  re-requests every book instead.
- **Instruments.** Prices are asked for by Stellar's Security ID (48 —
  `F.STS_OSE 10-year JGB.DEC17`, spaces and all). An ORDER names the
  instrument as Stellar's Security List does: Symbol (55), CFI code (461),
  SecurityType (167), MaturityMonthYear (200), SecurityExchange (207) — and
  for a listed strategy, New Order Multi-leg (35=AB) with its legs (555) and
  subtype (762, e.g. `TSPR`). A contract on the desk is looked up in
  Stellar's Security List by its symbol and exchange; until that answer
  arrives it cannot be ordered, and the screen says so.
- **Prices** are trader prices — no display factor. The tick is
  MinPriceIncrement (969) and its money MinPriceIncrementAmount (1146).
- **Parties.** Every order carries 452=11 (user) and 452=24 (account), the
  optional 452=1000, and the MiFID short codes and tags (29, 1724) when set on
  the venue — qualified 22 (algorithm) on the Algo's orders, 24 (person) on a
  hand's. There is no tag 1.
- **Closes.** 77=O / 77=C is sent on every order (not in Stellar's spec — it
  can be switched off on the venue if Stellar refuses it), and a close names
  its position and tickets in the ExchangeText party (452=1002).
- **Executions.** A trade (150=F) carries no ClOrdID: it is matched by
  Stellar's OrderID (37). Its AvgPx is always 0 and its CumQty is not relied
  on: the filled quantity and average are this program's own sums of
  LastQty / LastPx. A strategy's leg reports (442=2) are never booked.
- **Cancel** needs Stellar's OrderID (37): a cancel asked for before the
  acknowledgement goes the moment it arrives.
- **After each logon** every order of ours still recorded as working is asked
  about with an Order Status Request (H), and one whose OrderID was never
  heard by a Mass Order Status Request (AF) — see [FIX_RECOVERY.md](FIX_RECOVERY.md).
- **Busts and corrections** (150=H / G) are said, naming the trade's 527 and,
  when Stellar sends it, the exchange's TradeID (1003); the tape keeps 1003.
- **Not used**: spreadMachine custom spreads (35=AB with 22=STS, 207=SPREAD_MACHINE,
  the 957 group) — this desk trades the spreads the exchange lists.
- **Conformance test.** Stellar certifies a FIX client on UAT before it
  allows Production — see [STELLAR_CONFORMANCE.md](STELLAR_CONFORMANCE.md).
- **Positions.** Stellar's Order Router provides none: `positions()` is None
  (unknown, never flat), and arming LIVE asks you to confirm that this book's
  own fills are the record.

The default `start.py` mode remains simulated. Stop the launcher with Ctrl+C.
Changing session settings or credentials requires restarting the launcher.
