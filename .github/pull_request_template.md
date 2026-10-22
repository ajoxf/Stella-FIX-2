## What this changes

<!-- One or two sentences. Why, not what — the diff says what. -->

## Work package

<!-- Which one from docs/WORK_FIX_GATEWAY.md, or "not one of them" and why. -->

## What I ran it against

- [ ] The simulator (`pytest tests/ -q`)
- [ ] Orient UAT (`FIXTRADER_CONTRACT_VENUE=1 pytest tests/test_gateway_contract.py -q`)

<!-- Paste the summary line from each run. -->

```
```

## The rules this touches

<!-- Tick the ones that apply and say how the change keeps them. Delete the rest. -->

- [ ] `None` means unknown — `orders()` / `positions()` never return `[]` on a failed read
- [ ] Events carry a **snapshot** of the order, never a live reference
- [ ] A close carries an explicit `PositionEffect`, its position and its tickets, capped at what is open
- [ ] `reduce_only` is sent as a cap **as well as** the flag, never instead of it
- [ ] A refusal carries the venue's own words, verbatim
- [ ] Only our own `FT-` orders are cancelled or amended
- [ ] No tag was guessed — open questions went to `docs/FIX_NOTES.md`

## Anything above `gateway.py` changed?

<!-- "No" is the expected answer. If yes, say what and why: it may be a
     finding about the protocol rather than something to work around. -->

## Notes for the reviewer

<!-- Anything that would be hard to see from the diff. -->
