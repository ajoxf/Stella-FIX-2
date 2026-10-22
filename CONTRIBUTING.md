# Contributing

This repository has one contributor working on **FIX connectivity and order
execution**, and the codebase is arranged so that work can happen in one place
without touching anything else.

## The seam

`fixtrader/gateway.py` is the **only** module allowed to import a FIX library.
Everything above it — the engine, the executor, the statistics, the screen —
talks to the `Gateway` protocol and does not know FIX exists. That is
deliberate: the venue can be replaced without the rest of the system noticing.

```
  screen  →  webapp  →  commands  →  engine  →  executor  →  ┌──────────┐
                                        ↑                    │ Gateway  │
                                    statistics               │ protocol │
                                                             └────┬─────┘
                                              ┌───────────────────┴────────┐
                                        FakeGateway                  FixGateway
                                    (a real book, a real         ← the work
                                     fill model, a real reject)
```

## The definition of done

**`tests/test_gateway_contract.py` is the handover.** It runs against
`FakeGateway` — which the whole system is built and tested on — and it is the
same suite a `FixGateway` has to pass:

```bash
pytest tests/test_gateway_contract.py -q                     # the simulator

FIXTRADER_CONTRACT_VENUE=1 \
FIXTRADER_CONTRACT_CONFIG=config.json \
FIXTRADER_CONTRACT_KEY=fef_v6x6 \
  pytest tests/test_gateway_contract.py -q                   # a UAT session
```

A `FixGateway` that passes that suite against Orient's UAT drops in with no
change above `gateway.py`. If passing it *requires* a change further up, that
is a finding worth raising rather than working around — say so on the PR.

The venue run **sends real orders**. The fixture refuses any venue whose
environment is not `UAT`, and the suite flattens what it opened.

## Getting set up

**Python 3.9 or newer, Flask 2.0 or newer.** `start.py` checks both and names
the interpreter it found; a new terminal does not keep a conda activation, and
Anaconda's `base` is commonly 3.7.

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest tests/ -q                                # everything must pass first
python start.py                                 # the terminal, on the simulator
```

`start.py` writes `config.json` and `.env` on a first run and comes up against
the simulator, so there is a working screen before any venue exists. The
taskbar says **SIMULATED** where a **PROD** badge would go.

## Credentials

**You will not be given the desk's credentials, and you must never ask for
them in a PR, an issue or a commit.**

- Secrets live in `.env`, which is gitignored. `config.json` holds the *name*
  of each `.env` key and never a value.
- A venue reports its password as **set** or **not set**. Nothing returns it,
  not even masked — a masked value is one that gets echoed back into a form
  and saved over the real one.
- Work against the simulator, and against **your own UAT credentials** from
  Orient. PROD is out of scope for this work entirely.
- If you find a secret anywhere in the tree, in a log line or in an API
  response, that is a bug: open an issue and do not paste the value into it.

## What to change, and what not to

**Yours:**
`fixtrader/gateway.py` · `fixtrader/fake_gateway.py` ·
`tests/test_gateway_contract.py` · `tests/test_fake_gateway.py` ·
`docs/FIX_NOTES.md` · anything under `dict/` (data dictionaries)

**Ask first:** `fixtrader/executor.py` and `fixtrader/engine.py`. Order
lifecycle changes are in scope, but they are where a bug reaches money, and
they have tests that encode failures already paid for.

**Not yours without a conversation:** `stats.py`, `signals.py`, `costs.py`,
`sizing.py`, `webapp.py`, `static/`, `templates/`. If FIX work seems to need a
change there, say what and why on the PR.

## The rules that are not negotiable

`CLAUDE.md` carries the full list; these are the ones this work touches every
day.

- **`pytest tests/ -q` passes before every commit.** No exceptions, and a
  PROD venue is never run without it.
- **`None` means "unknown", and it is not "flat" and not zero.** `orders()`
  and `positions()` return `None` when the account could not be read. A
  gateway that returns `[]` on a failed read makes this system report a clean
  account while the money sits at the venue.
- **A gateway event carries a snapshot of the order, never the live object.**
  A real `ExecutionReport` is a distinct message. Aliasing it made a queued
  ACK report the order's current state; the reader marked it done, and the
  fill that followed was applied as an open — doubling the position instead
  of closing it.
- **A close is never a bare opposite order.** It carries an explicit
  `PositionEffect`, the position it closes and that position's tickets, and it
  is capped at what is open on that side. `reduce_only` is a *cap*, not an
  instruction. An unknown flag degrades to `CLOSE`, never to `OPEN`.
- **A refusal carries the venue's own words** — tag 58 verbatim, never "check
  the log".
- **Cancel only our own orders**, scoped to the `FT-` `ClOrdID` prefix.
  Anything else at the venue is somebody's hand order in TT.
- **Every test that asserts something is withheld needs a control** that turns
  the guard off and asserts the opposite.
- **Do not guess a tag.** A stub that says "not wired" is honest; a stub that
  invents a tag is a bug with a long fuse. Open questions live in
  `docs/FIX_NOTES.md` — add to it rather than guessing.

## Branches, commits and PRs

- Branch from the repository's **default branch** (`git clone` checks it out
  for you): `fix/<what-it-does>`, e.g. `fix/logon-and-heartbeat`.
- **One work package per PR** — see `docs/WORK_FIX_GATEWAY.md`. Six small PRs
  land; one big one does not get reviewed properly.
- Commit messages say *why*, not *what*. The diff already says what.
- CI runs the suite on every push. A red PR is not ready for review.
- Every PR describes **what you ran it against** — the simulator, UAT, or
  both — and pastes the conformance suite's summary line.

## Asking for a decision

Open questions about Orient's conventions go in `docs/FIX_NOTES.md` and get
raised on the PR. Seven are already listed there; two matter before much code
is written:

1. How is a spread contract identified — `Symbol(55)` alone, `SecurityID(48)`
   + `SecurityExchange(207)`, or a multi-leg definition?
2. Is market data a separate session?

Getting those answered from Orient is more valuable than any amount of code
written around the uncertainty.
