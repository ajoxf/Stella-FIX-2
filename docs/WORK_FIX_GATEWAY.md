# Work packages — FIX gateway and order execution

Six packages, in order. **One PR each.** Each one names what it delivers, what
proves it, and what it must not break. Read `CONTRIBUTING.md` first, and
`docs/FIX_NOTES.md` for the message set and the open questions.

The measure throughout is `tests/test_gateway_contract.py`: it runs green
against `FakeGateway` today, and the job is to make it run green against
Orient's UAT.

---

## 0. Before any code — answer two questions

Not a PR. Ask Orient, and write the answers into `docs/FIX_NOTES.md`:

1. **How is a spread contract identified?** `Symbol(55)` alone,
   `SecurityID(48)` + `SecurityIDSource(22)` + `SecurityExchange(207)`, or a
   multi-leg `SecurityDefinition` with `NoLegs(555)`? `ContractConfig` already
   carries all three fields, so any answer fits — but the contracts table and
   the security-definition request differ.
2. **Is market data a separate session?** `VenueConfig` has `md_host` /
   `md_port`, blank meaning "the same session".

Also worth having before package 4: **which offset flag does each contract
want** — tag 77 `PositionEffect`, or the broker's `CombOffsetFlag`, and whether
the close-today / close-yesterday split applies. SHFE and INE price them
differently.

---

## 1. Session: logon, heartbeat, and a state a person can read

**Deliver.** `FixGateway.start()` / `stop()` / `state()` / `state_text()`
against UAT. Configuration built from `VenueConfig` — comp ids, sub ids,
heartbeat, TLS, the data dictionary, the store and log paths. The password
read through `venue.password` and from nowhere else.

**Proves it.** The first two conformance tests pass against UAT
(`test_a_started_session_reports_logged_on_and_says_so_in_words`,
`test_a_stopped_session_is_down`), and the Exchanges page's **Connect** shows
the session's own words.

**Must not break.** `FixGateway` still imports and constructs with no venue
reachable, reports `DOWN` with a readable reason, and never takes the engine
down. `state_text()` is never blank — the banner shows it to a person.

**Watch for.** A sequence gap on the first reconnect. Decide with Orient
whether `ResetSeqNumFlag(141)=Y` goes on every logon, and write it down.

---

## 2. Security definitions: what a contract actually is

**Deliver.** `security_definition(contract)` returning tick size, tick value,
multiplier, currency and size bounds from the venue.

**Proves it.** `test_a_security_definition_is_absent_or_complete_never_zeroed`
against UAT, and the Exchanges page's **Diagnose** listing each configured
contract with the venue's own numbers beside the configured ones.

**Must not break.** **Absent is fine; zero is not.** A tick size of 0 divides
by zero in every money figure on the screen, so a field the venue did not send
comes back `None` and the screen renders an em dash. A disagreement between
the venue and the config is reported and offered as a one-click correction —
**never applied silently**.

---

## 3. Market data: the top of book, twice a second

**Deliver.** `subscribe(contract)` and `top_of_book(key)`:
`MarketDataRequest(V)`, then the snapshot (`W`) and incremental (`X`)
refreshes, plus `MarketDataRequestReject(Y)`.

**Proves it.** `test_an_unknown_contract_has_no_book_rather_than_an_empty_one`
and `test_the_book_that_does_exist_is_two_sided_and_priced`; then run
`start.py` against UAT and watch a window warm up and arm.

**Must not break.**

- **`None` is "we do not know"; an empty `BookTop` is "there is no market".**
  They are different and the algo treats them differently.
- A crossed or one-sided book is bad data, not an arbitrage — the statistics
  layer already rejects it, so pass it through rather than fixing it up.
- The staleness guard measures the quote **changing**, not messages arriving.
  A venue that republishes an unchanged book every second is not a live
  market. Do not touch `last_change` to make the guard quiet.

---

## 4. Orders: new, cancel, replace — and the close flag

**The one that costs money if it is wrong.**

**Deliver.** `send()` / `cancel()` / `amend()`, and `ExecutionReport(8)`,
`OrderCancelReject(9)` and `BusinessMessageReject(j)` folded into
`GatewayEvent`s. `OrderRequest.position_effect` onto tag 77 or the broker's
`CombOffsetFlag`; `position_id` and `close_tickets` onto whatever reference
the venue wants.

**Proves it.** The whole `# orders` and `# closing` half of the conformance
suite against UAT — in particular
`test_an_opposite_order_flagged_OPEN_does_not_close_anything` and its control
`test_a_close_flag_actually_closes`.

**Must not break.**

- **A close is never a bare opposite order.** Without the flag it opens the
  other side: long and short at once, both posting margin, and a netting
  screen calls it flat.
- **`reduce_only` is a cap, not an instruction** — sent as well as the effect,
  never instead of it.
- **Amend is cancel-replace**, keeping the id and its lineage. Cancel-then-new
  gives up the queue *and* opens a window where the order does not exist.
- **Events carry a snapshot**, never a live reference to your order book.
- **A reject carries the venue's text verbatim.**
- Every order we send is prefixed `FT-`; nothing else at the venue is ours.

---

## 5. Reading back: orders, positions and margin

**Deliver.** `orders()`, `positions()`, `margin_for()` — order and position
status requests, or the drop copy, whichever Orient offers.

**Proves it.** `test_an_unreadable_account_is_None_and_never_an_empty_list`
(fake only — a real venue cannot be told to fail, so test the failure path by
pointing at a dead session), and the Positions window showing the venue's
figures beside the book's.

**Must not break.**

- **`None` on a failed read.** Returning `[]` makes the reconciler treat live
  positions as orphans and the screen report a clean account. This is the
  single most expensive mistake available in this file.
- `VenuePosition.long_qty` / `short_qty` where the venue keeps the two sides
  apart. Netting them hides a close that went out as an open.
- `margin_for` returns `None` where the venue will not say. The profit target
  is a percentage of margin, so no margin means **no target** — and the window
  says which figure is missing. Do not substitute notional to make a number
  appear.

---

## 6. The awkward days

**Deliver.** Reconnect with backoff; resend and sequence-reset handling; a
session that drops mid-order; the venue's trading-status transitions.

**Proves it.** Kill the connection during a working order and show that the
book recovers without inventing or losing a position. Add the cases to the
conformance suite as you find them — **that file is the deliverable that
outlives the work**.

**Must not break.** Recovery leaves the book **incomplete** until it has read
the venue, and while it is incomplete the reconciler closes **nothing**. A
position is only an orphan if we are sure it is not ours.

---

## Definition of done for the whole piece

1. `pytest tests/ -q` green.
2. `FIXTRADER_CONTRACT_VENUE=1 pytest tests/test_gateway_contract.py -q` green
   against Orient UAT.
3. `python start.py` against a UAT venue: contracts warm, arm, enter, and
   close on their targets, with the screen showing **UAT** and not
   **SIMULATED**.
4. Nothing above `fixtrader/gateway.py` changed — or, where it did, the PR
   says what and why.
5. `docs/FIX_NOTES.md` has the answers, not the questions.
