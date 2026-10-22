"""What a TT Execution Report (8) IS, before anything is booked from it.

From TT FIX's Execution Report documentation:

- A spread fill comes as the spread (MultiLegReportingType 442 = 3, or 1 /
  absent for an outright) AND, for an exchange-listed spread, one report per
  LEG (442 = 2) at the leg's own price and instrument. Booked as fills of
  the spread they double or treble the position at prices that are not the
  spread's. Only the spread is booked.
- ExecTransType (20) = 1 is a trade CANCEL (bust), 2 a CORRECTION of the
  fill named in ExecRefID (19), 3 a STATUS (the answer to an Order Status
  Request) — none of them is a new fill. In FIX 4.4 the same are ExecType
  (150) H, G and I.
- ExecID (17) is unique only "typically ... a single trading day or the life
  of a multi-day order"; TT's UniqueExecID (16612) is unique for the life of
  the order. A fill is recognised as already seen by that, or by 17 with
  TT's TradeDate (75) when TT sends one.
- OrdRejReason (103) says why TT or the exchange refused, as a number.

Pure functions: no FIX library, no order path.
"""
from typing import Dict, Optional

FILL, LEG, BUST, CORRECTION, STATUS = 'FILL', 'LEG', 'BUST', 'CORRECTION', 'STATUS'


def kind(f: Dict[str, str]) -> Optional[str]:
    """FILL (book it), LEG (a spread leg: never book it), BUST / CORRECTION
    (a change to an earlier fill: say it, do not book it), STATUS (a status
    answer: not a fill), or None (no fill in it at all)."""
    trans, et = f.get('20', ''), f.get('150', '')
    if trans == '3' or et in ('I', 'D'):
        return STATUS
    if trans == '1' or et == 'H':
        return BUST
    if trans == '2' or et == 'G':
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


def exec_key(f: Dict[str, str]) -> str:
    """Unique for the life of the order: TT's UniqueExecID (16612), or the
    ExecID (17) with its trade date."""
    if f.get('16612'):
        return 'U:' + f['16612']
    # Only TT's own TradeDate (75) — never a date that depends on whether an
    # optional tag came: a resend without it must still be the same fill.
    return f"{f.get('17', '')}@{f['75']}" if f.get('75') else f.get('17', '')


#: OrdRejReason (103), in words — the common ones; the number otherwise.
ORD_REJ_REASONS = {
    '0': 'broker option', '1': 'unknown symbol', '2': 'exchange closed',
    '3': 'order exceeds limit', '4': 'too late to enter', '5': 'unknown order',
    '6': 'duplicate order', '11': 'unsupported order characteristic',
    '13': 'incorrect quantity', '15': 'unknown account',
    '16': 'price exceeds current price band', '18': 'invalid price increment',
    '20': 'routing error', '1003': 'market closed',
    '1007': 'FIX field missing or incorrect', '1010': 'required field missing',
    '1011': 'FIX field incorrect', '1014': 'user not authorized',
    '2047': 'unknown contract', '2115': 'order quantity outside allowable range',
    '2137': 'order price outside limits', '2179': 'order price outside bands',
    '7000': 'order rejected', '7009': 'contract past expiration',
    '7011': 'max contract working quantity exceeded',
}


def reject_reason(f: Dict[str, str], text: str = '') -> str:
    """TT's words (58) with the reason code (103) said in words beside them."""
    code = f.get('103', '')
    if code == '99':
        return text or 'other'      # FIX's "Other": TT's text says it all
    words = ORD_REJ_REASONS.get(code) or (f'reason code {code}' if code else '')
    if words and words.lower() not in (text or '').lower():
        return f"{text} ({words})" if text else words
    return text


def correction_text(f: Dict[str, str], what: str) -> str:
    ref = f.get('19') or f.get('17') or '?'
    word = 'CANCELLED (busted)' if what == BUST else 'CORRECTED'
    return (f"TT reports fill {ref} {word}"
            + (f": {f['58']}" if f.get('58') else '')
            + " — this program has NOT changed the position; check it in TT")
