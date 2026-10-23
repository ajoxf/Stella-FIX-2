# What the Order Router missed, recovered from Stellar

A FIX client that was not listening — the line dropped, the PC restarted, the
program was closed — never receives the Execution Reports Stellar sent
meanwhile. A fill among them is a position at Stellar that this screen does
not know about. Stellar has no separate recovery service (TT's FIX Recovery
has no Stellar equivalent); two things stand in for it.

## 1. The Order Router keeps its sequence numbers

The Order Router's numbers are kept across restarts (`SequenceStore`, in the
terminal's database): the outgoing number is written BEFORE each message goes,
so it is never used twice; the incoming one at most once a second and when
the session ends. At the next logon Stellar's first message shows how much was
missed, a **ResendRequest (2)** asks for it, and what Stellar resends
(`43=Y`) is applied **in Stellar's order** — anything that came ahead of the
gap is held until the gap is filled. New orders wait for that (`ready()`);
a CLOSE never waits. If the resend never completes, the session goes ahead
after `READY_FALLBACK_SEC`, said in the FIX log.

Market Data does NOT keep its numbers: Stellar's spec says resend requests on
Market Data are not supported, so it resets on every logon (141=Y) and a gap
there re-requests every book.

**Reset sequence numbers** (Connection page, sessions down, pressed twice)
starts the Order Router from 1 at the next logon — ONLY when Stellar says it
has reset its side. The venue's "Reset Order Router sequence on every logon"
does the same at every logon, for a session Stellar resets daily; it gives up
the resend.

## 2. The order status sweep

After each Order Router logon — once the resend is in, and once the previous
run's working Algo orders are adopted (so a fill is applied as what it was)
and the startup sweep has asked to cancel them — every order of ours still
recorded as working is asked about with an **Order Status Request (H)**:
ClOrdID (11), Stellar's OrderID (37), an OrdStatusReqID (790) and the
instrument. Stellar answers each with an Execution Report, ExecType I,
carrying the 790:

- a CANCELLED or REJECTED order is closed on the book;
- a CumQty above what this book has booked is SAID — "the trade report was
  not received; this program has NOT changed the position" — never booked
  without the trade's price;
- an order already FILLED or CANCELLED here is never moved back.

Every report goes through the same code as a live one: a fill already booked
is never booked twice (by ExecID), a strategy leg is never booked, a bust or
correction is said. While the sweep runs, Algo entries wait; exits never do.
An order of ours whose OrderID was never heard (its acknowledgement lost)
cannot be asked about by H, or cancelled — Stellar cancels by OrderID. While
there is one, the sweep also sends ONE **Mass Order Status Request (AF)**
(585=7, all orders; spec 2.19, added in 2025). Stellar answers with one report
per working order, carrying the 584 and ending with 912=Y; the report naming
our ClOrdID gives its OrderID, and from then on it can be cancelled. Stellar's
example answers with ExecType 5: a report carrying 584 is a STATUS, never a
replace. No answer in `RECOVERY_TIMEOUT_SEC` fails the sweep, naming the
orders — never assumed gone.

## On the screen

Trading Monitor → **Reconciler**: the last sweep (done / running / FAILED,
with the orders Stellar did not answer for), when the Order Router was last
heard, and **Ask Stellar now**.

## What it cannot do

- Positions: Stellar's Order Router provides none. A position opened by other
  means on the account is invisible here; Stellar's or the clearer's statement
  is the remedy.
- Reports beyond what Stellar keeps for a resend, if its side was reset.
