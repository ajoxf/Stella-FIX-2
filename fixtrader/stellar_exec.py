"""What a Stellar Execution Report (8) IS, before anything is booked from it.

From the Stellar FIX Order Router specification (sections 2.16, 3 and 5):

- ExecType (150): 0 new, F trade, 4 cancelled, 5 replaced, 8 rejected,
  9 suspended (held), D restated (unheld), I order status (the answer to an
  Order Status Request, ExecID 17 = "0"). H / G are FIX 4.4's trade cancel
  and trade correct: a trade correction or cancellation has an ExecID of its
  own and the SecondaryExecID (527) of the trade it changes.
- A trade (150=F) carries NO ClOrdID (11): it is matched to its order by
  Stellar's OrderID (37). AvgPx (6) is always "0", and the CumQty (14) and
  OrdStatus (39) on a trade are not reliable (the spec's own strategy fill
  says 14=1, 39=0, 151=0) — so the filled quantity and the average price are
  summed here from LastQty (32) / LastPx (31), never read from 6 or 14.
- A strategy fill comes as the strategy (MultiLegReportingType 442 = 3) AND
  one report per LEG (442 = 2) at the leg's own price, with the SAME OrderID.
  Booked as fills of the spread they would double or treble the position at
  prices that are not the spread's. Only the strategy is booked.
- ExecID (17) is unique for each execution report sent ("not guaranteed to
  be sequential"): a fill already seen is known by it.
- A Mass Order Status Request (AF, spec 2.19 — added in the 2025 revision)
  is answered with one report per order carrying the request's
  MassStatusReqID (584), TotNumReports (911) and LastRptRequested (912=Y on
  the last). Stellar's own example answers with 150=5: read as a REPLACE it
  would rewrite the order from a status answer. Any report carrying 584 is a
  STATUS, unless it is a trade, a bust or a correction.
- TradeID (1003, spec 3.4 — 2025): the raw exchange trade id. A trade
  correction or cancellation carries the TradeID of the original trade, so
  it is named beside the 527 when a bust or correction is said.
- OrdRejReason (103) on a submit rejection: 1 unknown symbol, 3 order exceeds
  limit, 13 incorrect quantity, 99 other — Stellar's Text (58) says the rest.

Pure functions: no FIX library, no order path.
"""
from typing import Dict, Optional

FILL, LEG, BUST, CORRECTION, STATUS = 'FILL', 'LEG', 'BUST', 'CORRECTION', 'STATUS'


def kind(f: Dict[str, str]) -> Optional[str]:
    """FILL (book it), LEG (a strategy leg: never book it), BUST / CORRECTION
    (a change to an earlier fill: say it, do not book it), STATUS (a status
    answer, a hold or an unhold: not a fill), or None (no fill in it)."""
    et = f.get('150', '')
    if et in ('I', 'D', '9') or (f.get('584') and et not in ('F', 'H', 'G')):
        return STATUS
    if et == 'H':
        return BUST
    if et == 'G':
        return CORRECTION
    if et not in ('1', '2', 'F'):
        return None
    try:
        qty = float(f.get('32') or 0)
    except ValueError:
        qty = 0.0
    if qty <= 0:
        return None
    if f.get('442') == '2':
        return LEG
    return FILL


def is_status_answer(f: Dict[str, str]) -> bool:
    """An answer to an Order Status (H, 790) or Mass Order Status (AF, 584)
    request: Stellar's view of the order, not an event on it."""
    return f.get('150') == 'I' or bool(f.get('584') and kind(f) == STATUS)


def exec_key(f: Dict[str, str]) -> str:
    """Stellar's ExecID (17): unique for each execution report it sends."""
    return f.get('17', '')


#: OrdRejReason (103), in words: the codes Stellar's spec lists.
ORD_REJ_REASONS = {
    '1': 'unknown symbol', '3': 'order exceeds limit', '13': 'incorrect quantity',
}


def reject_reason(f: Dict[str, str], text: str = '') -> str:
    """Stellar's words (58) with the reason code (103) said in words beside them."""
    code = f.get('103', '')
    if code == '99':
        return text or 'other'      # FIX's "Other": Stellar's text says it all
    words = ORD_REJ_REASONS.get(code) or (f'reason code {code}' if code else '')
    if words and words.lower() not in (text or '').lower():
        return f"{text} ({words})" if text else words
    return text


def correction_text(f: Dict[str, str], what: str) -> str:
    ref = f.get('527') or f.get('19') or f.get('17') or '?'
    if f.get('1003') and f['1003'] != ref:
        ref += f" (exchange trade {f['1003']})"
    word = 'CANCELLED (busted)' if what == BUST else 'CORRECTED'
    return (f"Stellar reports trade {ref} {word}"
            + (f": {f['58']}" if f.get('58') else '')
            + " — this program has NOT changed the position; check it in Stellar")


def parties(raw_pairs) -> Dict[str, str]:
    """The Parties group (453) as {PartyRole (452): PartyID (448)} — the
    first ID given for each role. 448 opens each entry; its role follows."""
    out: Dict[str, str] = {}
    pending = None
    for tag, value in raw_pairs:
        if tag == '448':
            pending = value
        elif tag == '452' and pending is not None:
            out.setdefault(value, pending)
            pending = None
    return out
